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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from trading_system.domain.trading_rules import SymbolRules as DomainRules

from .adapter import Alert, BinanceSpotAdapter
from .client import (
    TESTNET_REST, AfterSendError, BinanceQueryError, BinanceSpotClient, PreSendError, Transport,
)
from .facts import BinanceMarketFacts
from .preflight import BOOTSTRAP_RATE_LIMITS
from .run_guard import RunGuard
from .signing import Credentials, SecretLike
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
    return SpotSession(adapter, client, budget, guard, creds, rules, limits, offset, _merge(bodies))


__all__ = ["SpotSession", "SpotStartRefused", "start_spot_session"]
