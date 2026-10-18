"""run 시작 시 바이낸스 쪽 준비 — 서버 시각 → exchangeInfo(규칙·rateLimits) → 예산 → 어댑터 (CP-036 D-209·D-113, T-047).

app은 바이낸스 패키지를 import하지 않는다(D-119, tests/contract/test_binance_boundary) — 이 함수는 scripts가 app에 주입한다.
연결 조건(모드·설정·접속 스위치·주문 스위치·비밀 키 존재)은 호출 전에 app이 검사한다.

순서 (어긋나면 SpotStartRefused — 호출자는 시작 거부, 종료 코드 2. 서명 조회·주문 없음)
1. 접속 대상 = 테스트넷 REST(client.TESTNET_REST)만. 아니면 전송 객체를 만들기 전에 거부(D-098).
2. 전송 = RunGuard(테스트넷 호스트만, 주문 스위치 꺼짐이면 주문 계열 전송 전 차단, D-204).
3. 부트스트랩 예산(preflight.BOOTSTRAP_RATE_LIMITS [제안] 1200/분의 50%)으로 서버 시각 → 0.5초 이상 오차는 시계 경고(D-115).
4. 유니버스 심볼마다 exchangeInfo(symbol=). 응답들을 한 본문으로 합쳐 BinanceMarketFacts로 해석한다.
   - 조회 실패·해석 실패·rateLimits 없음(REQUEST_WEIGHT 없음 포함) → 거부(V-283 b, D-113 "못 읽으면 시작 거부").
   - 규칙이 없는(거래소에 없음·필터 해석 불가) 심볼, TRADING이 아닌 심볼 → 거부 [DP-56 보수: run 공급 저장소는 심볼 상태를
     따로 들고 있지 않으므로 시작에서 막는다, D-107].
5. 예산 = rateLimits의 50%(WeightBudget), 같은 RunGuard·자격정보로 실제 클라이언트·어댑터를 만든다.
돌려주는 rules는 공통 규칙 값 객체(domain SymbolRules) — app이 시세·규칙 저장소(SpotFactsStore)에 적재한다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_system.domain.sinks import HaltControl

from trading_system.domain.trading_rules import SymbolRules as DomainRules

from .adapter import Alert, BinanceSpotAdapter
from .client import (
    TESTNET_REST, AfterSendError, BinanceQueryError, BinanceSpotClient, PreSendError, Transport,
)
from .facts import BinanceMarketFacts
from .feed import BinanceFeed, FeedPlan, MinutePlan, MinuteSink, SpotSink, minute_budget_problem
from .preflight import BOOTSTRAP_RATE_LIMITS, TESTNET_HOST
from .run_guard import RunGuard
from .signing import Credentials, SecretLike
from .user_stream import TESTNET_WS_API, WS_API_HOST, UserStream, WsConnect
from .weights import RateLimits, WeightBudget, parse_rate_limits

MakeTransport = Callable[[type[Exception], type[Exception]], Transport]


class SpotStartRefused(RuntimeError):
    """run 시작 거부(바이낸스 쪽 준비 실패). 문구에 키·서명·응답 원문 없음."""


@dataclass
class SpotSession:
    adapter: BinanceSpotAdapter
    client: BinanceSpotClient
    budget: WeightBudget
    guard: RunGuard
    credentials: Credentials = field(repr=False)
    rules: dict[str, DomainRules]
    rate_limits: RateLimits
    server_offset_ms: int
    exchange_body: dict = field(repr=False)
    quote_quantum: Decimal = Decimal("0.00000001")   # 호가 자산 금액 자릿수 = quoteAssetPrecision 최솟값 (D-118)


def _merge(bodies: list[object]) -> dict:
    """심볼별 exchangeInfo 응답 → 한 본문(rateLimits는 첫 응답, 모든 응답에 있어야 함)."""
    symbols: list[object] = []
    limits: object = None
    for b in bodies:
        if not isinstance(b, dict) or "rateLimits" not in b:
            raise SpotStartRefused("exchangeInfo 응답에 rateLimits 없음 — 호출 한도를 모르면 시작하지 않는다 (D-113, V-283 b)")
        if limits is None:
            limits = b["rateLimits"]
        raw = b.get("symbols")
        if not isinstance(raw, list):
            raise SpotStartRefused("exchangeInfo 응답에 symbols 목록 없음 (D-209)")
        symbols.extend(raw)
    return {"timezone": "UTC", "rateLimits": limits, "exchangeFilters": [], "symbols": symbols}


def start_spot_session(base_url: str, api_key: SecretLike, secret: SecretLike, make_transport: MakeTransport, *,
                       symbols: Sequence[str], account: str, now_ms: Callable[[], int],
                       sleep: Callable[[float], None], orders_enabled: bool,
                       units: Callable[[str], DomainRules | None] | None, on_alert: Alert,
                       on_filter_reject: Callable[[str], None] | None = None) -> SpotSession:
    if base_url.rstrip("/") != TESTNET_REST:
        raise SpotStartRefused("접속 대상은 테스트넷 REST만 (실전·Demo·다른 포트·평문 거부, D-098)")
    if not symbols:
        raise SpotStartRefused("유니버스 심볼 없음 (D-105)")
    creds = Credentials(api_key, secret)
    guard = RunGuard(make_transport(PreSendError, AfterSendError), orders_enabled=orders_enabled)
    boot = BinanceSpotClient(TESTNET_REST, creds, guard, WeightBudget(parse_rate_limits(BOOTSTRAP_RATE_LIMITS), now_ms),
                             now_ms=now_ms, sleep=sleep, on_alert=on_alert)
    try:
        offset = boot.server_time_offset_ms()
    except BinanceQueryError as exc:
        raise SpotStartRefused(f"바이낸스 서버 시각 조회 실패: {exc}") from None
    bodies = []
    for sym in symbols:
        try:
            bodies.append(boot.exchange_info(sym))
        except BinanceQueryError as exc:
            raise SpotStartRefused(f"exchangeInfo {sym} 조회 실패: {exc} (D-209)") from None
    facts = BinanceMarketFacts(now=lambda: datetime.fromtimestamp(now_ms() / 1000, UTC), universe=list(symbols))
    if not facts.load_exchange_info(_merge(bodies)):
        why = facts.load_error or "해석 실패"
        hint = " — rateLimits(호출 한도) 확인 (D-113, V-283 b)" if "rateLimits" in why else ""
        raise SpotStartRefused(f"{why}{hint}")
    limits = facts.rate_limits()
    if limits is None:
        raise SpotStartRefused("rateLimits 없음 — 시작하지 않는다 (D-113, V-283 b)")
    rules: dict[str, DomainRules] = {}
    problems = []
    for sym in symbols:
        r = facts.rules(sym)
        if r is None:
            problems.append(f"{sym}: 거래 규칙 없음 ({facts.rejected.get(sym) or 'exchangeInfo에 심볼 없음'})")
            continue
        if facts.trading_status(sym):
            problems.append(f"{sym}: 상태가 TRADING 아님 (D-107)")
            continue
        rules[sym] = r
    if problems:
        raise SpotStartRefused("; ".join(problems) + " (D-209)")
    budget = WeightBudget(limits, now_ms=now_ms)
    client = BinanceSpotClient(TESTNET_REST, creds, guard, budget, now_ms=now_ms, sleep=sleep, on_alert=on_alert)
    adapter = BinanceSpotAdapter(client, symbols, account=account, now_ms=now_ms,
                                 units=units if units is not None else rules.get, orders_enabled=orders_enabled,
                                 on_alert=on_alert, on_filter_reject=on_filter_reject, budget=budget)
    precision = min(r.quote_asset_precision for s in symbols if (r := facts._exchange.rules(s)) is not None)  # noqa: SLF001
    return SpotSession(adapter, client, budget, guard, creds, rules, limits, offset, _merge(bodies),
                       Decimal(1).scaleb(-precision))


# --- run 연결 묶음 (T-048): 어댑터 + 사용자 스트림 + 시세 공급 ------------------------------------------------------

@dataclass
class M2Run:
    """app이 받는 run 연결 묶음. 바이낸스 타입은 속성 안에만 있고 app은 공통 계약(BrokerAdapter·스레드 본체)만 쓴다."""

    session: SpotSession
    feed: BinanceFeed
    stream: UserStream

    @property
    def adapter(self) -> BinanceSpotAdapter:
        return self.session.adapter

    @property
    def rules(self) -> dict[str, DomainRules]:
        return self.session.rules

    @property
    def server_offset_ms(self) -> int:
        return self.session.server_offset_ms

    @property
    def orders_enabled(self) -> bool:
        return self.session.guard.orders_enabled

    @property
    def quote_quantum(self) -> Decimal:
        return self.session.quote_quantum

    def threads(self, stop: threading.Event) -> list[threading.Thread]:
        """시작은 호출자(시작 대조 뒤). 이름은 감시 스레드 표시에 쓴다(D-215)."""
        return [threading.Thread(target=self.feed.run, args=(stop,), name="binance-feed", daemon=True),
                threading.Thread(target=self.stream.run, args=(stop,), name="binance-user-stream", daemon=True)]

    def resume_after_ban(self) -> None:
        self.session.adapter.resume_after_ban()


class M2RunConnector:
    """scripts/run-m2.py가 실제 전송(REST·WS)을 넣어 만들고 app.main에 주입한다(app은 이 패키지를 import하지 않는다).

    hosts: 시작 일치 표 검사(check_start_scope)에 쓰는 접속 도메인 — REST·WS API 테스트넷 호스트."""

    hosts = frozenset({TESTNET_HOST, WS_API_HOST})

    def __init__(self, make_transport: MakeTransport, ws_connect: WsConnect, *, feed_plan: FeedPlan = FeedPlan(),
                 ws_url: str = TESTNET_WS_API) -> None:
        self._make_transport = make_transport
        self._ws_connect = ws_connect
        self._plan = feed_plan
        self._ws_url = ws_url

    def __repr__(self) -> str:
        return f"M2RunConnector(hosts={sorted(self.hosts)})"

    def __call__(self, *, api_key: SecretLike, secret: SecretLike, symbols: Sequence[str], account: str,
                 now: Callable[[], datetime], sleep: Callable[[float], None], orders_enabled: bool, sink: SpotSink,
                 on_alert: Alert, on_stream_state: Callable[[str, str], None], halts: HaltControl,
                 minute_plan: tuple[int, int, int] | None = None, minute_sink: MinuteSink | None = None) -> M2Run | str:
        """준비 실패면 거부 사유(문자열) — 호출자는 시작 거부(종료 코드 2).

        minute_plan = (판정 주기 N분, 판정 창 봉 수, 확정 여유 초) — 1m klines 조회(CP-037 D-220). 기본 None = 조회 없음.
        app이 설정 `[minute] enabled = true`와 승인 A-ID를 확인했을 때만 넘긴다. 호출 예산 시작 검사를 넘지 못하면 거부."""
        minute: MinutePlan | None = None
        if minute_plan is not None:
            if minute_sink is None:
                return "1m klines 조회 계획에 받는 쪽이 없다 (D-220)"
            try:
                minute = MinutePlan(minute_plan[0], minute_plan[1], timedelta(seconds=minute_plan[2]))
            except (ValueError, IndexError, TypeError) as exc:
                return f"1m klines 조회 계획 오류: {exc} (D-220)"

        def now_ms() -> int:
            return int(now().timestamp() * 1000)
        feed_box: list[BinanceFeed] = []
        try:
            session = start_spot_session(
                TESTNET_REST, api_key, secret, self._make_transport, symbols=symbols, account=account, now_ms=now_ms,
                sleep=sleep, orders_enabled=orders_enabled, units=getattr(sink, "units", None), on_alert=on_alert,
                on_filter_reject=lambda s: feed_box[0].request_rules_reload(s) if feed_box else None)
        except SpotStartRefused as exc:
            return str(exc)
        if minute is not None:
            problem = minute_budget_problem(session.rate_limits, len(symbols), minute)
            if problem is not None:
                return problem
        feed = BinanceFeed(session.client, sink, symbols, now=now, alert=on_alert, halts=halts, account=account,
                           plan=self._plan, minute=minute, minute_sink=minute_sink if minute is not None else None)
        feed_box.append(feed)
        stream = UserStream(self._ws_connect, session.credentials, session.adapter, now_ms=now_ms,
                            on_state=lambda st, m: on_stream_state(st.value, m), url=self._ws_url)
        return M2Run(session, feed, stream)



def open_balance_queries(make_transport: MakeTransport, *, api_key: SecretLike, secret: SecretLike,
                         symbols: Sequence[str], account: str, now_ms: Callable[[], int],
                         sleep: Callable[[float], None]) -> BinanceSpotAdapter | str:
    """기준선 확정·리셋 확인 도구(CP-036 D-210)의 **조회만** 연결: 주문 전송 스위치 꺼짐 전송 계층(RunGuard) + 같은 시작 준비
    (서버 시각·exchangeInfo·rateLimits). 돌려주는 어댑터로는 주문이 나가지 않는다. 준비 실패면 거부 사유."""
    try:
        session = start_spot_session(TESTNET_REST, api_key, secret, make_transport, symbols=symbols, account=account,
                                     now_ms=now_ms, sleep=sleep, orders_enabled=False, units=None,
                                     on_alert=lambda kind, msg: None)
    except SpotStartRefused as exc:
        return str(exc)
    return session.adapter
