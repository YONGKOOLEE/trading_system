"""바이낸스 **실서버 공개 시세** 전용 모듈 — 신호 출처(CP-039 D-243~D-247, 승인 A-115 Q-1 (가)·Q-2 (가)). 네트워크 라이브러리 없음.

D-098 좁은 개정(D-244): 실서버 도메인 상수는 **이 파일 하나에만**, 허용 호스트 집합(PUBLIC_HOSTS)만 둔다
(tests/unit/test_tm2_08_market_scope 예외 1파일 × 1호스트 집합, 사유 기록).

구조적 차단 (CLAUDE.md §7 — 이 모듈로는 실전 주문·계정 접속이 될 수 없다)
- 자격정보를 받는 인자·서명 함수·비밀 설정 로더가 없다(V-315). 바이낸스 실행 패키지(broker/binance/)를 import하지 않고, 그
  패키지도 이 모듈을 import하지 않는다(V-315 AST 시험).
- 전송은 PublicDataGuard를 거친 주입 전송으로만 한다: GET·https·허용 호스트·기본 포트·사용자 정보 없음·허용 경로·경로별
  허용 쿼리 키·빈 헤더가 아니면 **보내지 않는다**(V-314). 서명 쿼리(signature·timestamp·recvWindow)는 허용 키 목록에 없다.
- 실행 출처 RunGuard(테스트넷 REST만)는 바꾸지 않는다 — 두 가드는 서로 다른 전송 객체에 붙는다(D-245).

데이터 범위 (A-115 Q-2 "처음 범위 = klines·현재가(REST)만") — CP 문안 D-246의 exchangeInfo·time은 Q-2 답에 없어 넣지 않았다
[임시 판단, 보수]. 호스트 근거: BN §2.1 "실전 대응 URL REST `https://api.binance.com`"(S1·S9), 대체 호스트(api1~api4·api-gcp)는
쓰지 않는다. 공개 시세 전용 호스트(data-api)는 BN에 기록이 없어 [미확인] — 쓰지 않는다.
- `GET /api/v3/klines` weight 2 (BN §8 S6·S7 확인): symbol·interval·startTime·endTime·limit
- `GET /api/v3/ticker/price` (symbol 지정): weight 2는 **[미확인 — 추정]**(테스트넷 공급 feed와 같은 가정)

호출 예산·제한 응답 (D-247)
- 실서버 REQUEST_WEIGHT는 IP 기준(BN §4 S2) — 실행 출처 예산(WeightBudget)과 **별개**인 PublicBudget, 상한 = 분당 60
  [제안: min(1% × REQUEST_WEIGHT, 60) — exchangeInfo를 부르지 않으므로 60, 문서 예시 6000/분의 1%와 같다].
  시작 시 일정 추정(schedule_weight_per_minute)이 상한을 넘으면 시작 거부(V-327). 예산이 모자라면 보내지 않는다(BUDGET).
- 429·418: 재시도 없이 **이 출처 호출 전부 중단**, 긴급 알림 1건. 429는 Retry-After(없으면 60초 [제안]) 뒤 재개, 418은
  Retry-After가 있으면 그 뒤, 없으면 사람이 resume_after_ban()을 부를 때까지(BN §4: 반복 시 차단 2분~3일).
- 5xx·전송 실패: 최대 3회 재시도(대기 1·2·4초) — A-110 수집 도구와 같다. 그 밖의 4xx는 재시도 없음.
- 공개 시세 조회는 주문이 아니므로 결과 불명이 없다.
기록: 경로·HTTP 상태만(쿼리 문자열·헤더·응답 원문 없음).
"""

from __future__ import annotations

import json
import math
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market
from trading_system.domain.minute_series import DEFAULT_CONFIRM_MARGIN, ORDER_WINDOW, PeriodGrid
from trading_system.domain.signal_source import KIND_PUBLIC

PUBLIC_REST = "https://api.binance.com"
PUBLIC_HOSTS = frozenset({"api.binance.com"})
KLINES_PATH = "/api/v3/klines"
TICKER_PATH = "/api/v3/ticker/price"
PATH_QUERY_KEYS: Mapping[str, frozenset[str]] = {
    KLINES_PATH: frozenset({"symbol", "interval", "startTime", "endTime", "limit"}),
    TICKER_PATH: frozenset({"symbol"}),
}
PATH_WEIGHT = {KLINES_PATH: 2, TICKER_PATH: 2}   # klines 2 확인(BN §8), ticker/price 2 [미확인 — 추정]
BUDGET_CAP_PER_MINUTE = 60
RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
DEFAULT_RETRY_AFTER_S = 60
KLINE_PAGE_MAX = 1000             # klines limit 상한 (BN §8)
MINUTE_KLINE_EXTRA = 3            # limit = lookback + 3 (진행 중 봉·늦은 단계 여유) — 실행 출처 분봉 조회와 같은 값
INTERVAL_MS = {"1m": 60_000, "1d": 86_400_000}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

Alert = Callable[[str, str], None]


# --- 데이터 출처 시작 표 (D-245) -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DataSourceRule:
    """데이터 출처 한 행: 접속 호스트·메서드·경로별 허용 쿼리 키. 자격정보는 언제나 없음(헤더를 보내지 않는다)."""

    hosts: frozenset[str]
    methods: frozenset[str]
    paths: Mapping[str, frozenset[str]]


# (모드, 시장, 출처 종류) → 행. testnet 종류는 행이 없다(데이터 출처 = 실행 출처, 추가 호스트 없음). LIVE 행 없음.
DATA_SOURCE_TABLE: dict[tuple[Mode, Market, str], DataSourceRule] = {
    (Mode.PAPER, Market.M2, KIND_PUBLIC): DataSourceRule(PUBLIC_HOSTS, frozenset({"GET"}), PATH_QUERY_KEYS),
}


class DataSourceRefused(RuntimeError):
    """데이터 출처 시작 표에 행이 없음 → 시작 거부(연결 0)."""


def data_source_rule(mode: Mode, market: Market, kind: str) -> DataSourceRule:
    rule = DATA_SOURCE_TABLE.get((mode, market, kind))
    if rule is None:
        raise DataSourceRefused(f"데이터 출처 시작 표에 ({mode.value}, {market.value}, {kind}) 행이 없다 — 시작하지 않는다 "
                                "(CP-039 D-245)")
    return rule


# --- 전송 경계 (PublicDataGuard, V-314) --------------------------------------------------------------------------------

class PublicReply(Protocol):
    status: int
    headers: Mapping[str, str]
    text: str


class PublicTransport(Protocol):
    def send(self, method: str, url: str, headers: Mapping[str, str]) -> PublicReply: ...


class PublicDataRefused(RuntimeError):
    """허용되지 않은 요청 — 보내지 않았다."""


class PublicTransportError(RuntimeError):
    """하위 전송 실패(연결 전·전송 중 모두). scripts가 실제 전송의 오류 형식으로 넣는다."""


@dataclass
class PublicDataGuard:
    inner: PublicTransport
    rule: DataSourceRule
    sent: int = 0
    calls: Counter = field(default_factory=Counter)
    blocked: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"PublicDataGuard(hosts={sorted(self.rule.hosts)}, sent={self.sent}, blocked={len(self.blocked)})"

    def check(self, method: str, url: str, headers: Mapping[str, str]) -> str:
        """통과하면 경로를 돌려준다. 거부 사유에는 경로·호스트만(쿼리 값·헤더 값 없음)."""
        p = urlsplit(url)
        m = method.upper()
        where = f"{m} {p.hostname or '?'}{p.path}"
        if m not in self.rule.methods:
            self._refuse(where, "허용되지 않은 메서드")
        if p.scheme != "https" or p.hostname not in self.rule.hosts or p.port is not None or p.username or p.password:
            self._refuse(where, "허용되지 않은 접속 대상(실서버 공개 시세 호스트만)")
        keys = self.rule.paths.get(p.path)
        if keys is None:
            self._refuse(where, "허용되지 않은 경로(klines·ticker/price만)")
        if p.fragment:
            self._refuse(where, "프래그먼트 불가")
        for k, _v in parse_qsl(p.query, keep_blank_values=True):
            if k not in keys:   # type: ignore[operator]
                self._refuse(where, f"허용되지 않은 쿼리 키 {k}")
        if headers:
            self._refuse(where, "헤더를 보내지 않는다(키·서명 헤더 불가)")
        return p.path

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> PublicReply:
        path = self.check(method, url, headers)
        self.sent += 1
        self.calls[path] += 1
        return self.inner.send(method, url, {})

    def _refuse(self, where: str, why: str) -> None:
        self.blocked.append(f"{where}: {why}")
        del self.blocked[:-200]
        raise PublicDataRefused(f"{where}: {why} — 보내지 않는다 (CP-039 D-245)")


# --- 별도 호출 예산 (D-247, V-327) ------------------------------------------------------------------------------------

class PublicBudget:
    """실서버 공개 시세 전용 weight 예산 — 최근 60초 창 합계가 상한을 넘지 않게. 실행 출처 예산과 공유하지 않는다."""

    def __init__(self, now_ms: Callable[[], int], cap_per_minute: int = BUDGET_CAP_PER_MINUTE) -> None:
        if cap_per_minute <= 0:
            raise ValueError("예산 상한은 0보다 커야 한다")
        self._now = now_ms
        self.cap = cap_per_minute
        self._used: deque[tuple[int, int]] = deque()

    def __repr__(self) -> str:
        return f"PublicBudget(cap={self.cap}/분, used={self.used()})"

    def used(self) -> int:
        now = self._now()
        while self._used and self._used[0][0] <= now - 60_000:
            self._used.popleft()
        return sum(w for _t, w in self._used)

    def take(self, weight: int) -> bool:
        if self.used() + weight > self.cap:
            return False
        self._used.append((self._now(), weight))
        return True


@dataclass(frozen=True)
class PublicMinutePlan:
    """신호 출처 1m klines 계획 — 판정 격자 N분·창 길이·확정 여유 (CP-037 D-220과 같은 격자, 한 쪽으로 끝냄)."""

    period_minutes: int
    lookback_bars: int
    margin: timedelta = DEFAULT_CONFIRM_MARGIN

    def __post_init__(self) -> None:
        n = self.lookback_bars
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= KLINE_PAGE_MAX - MINUTE_KLINE_EXTRA:
            raise ValueError(f"lookback_bars는 1~{KLINE_PAGE_MAX - MINUTE_KLINE_EXTRA} 정수 (klines 한 쪽)")
        self.grid()

    def grid(self) -> PeriodGrid:
        return PeriodGrid(self.period_minutes, self.margin, ORDER_WINDOW[0], ORDER_WINDOW[1])

    @property
    def limit(self) -> int:
        return self.lookback_bars + MINUTE_KLINE_EXTRA


@dataclass(frozen=True)
class SignalPlan:
    """신호 출처 공급 일정 [제안] (D-247 (2)): 참조 시세 10초마다, 1d klines 시작·UTC 일 경계(실패 시 1분 뒤 다시),
    1m klines 판정 격자마다(minute이 있을 때만)."""

    reference_every: timedelta = timedelta(seconds=10)
    daily_retry_every: timedelta = timedelta(seconds=60)
    kline_limit: int = 200
    loop_s: float = 1.0
    minute: PublicMinutePlan | None = None


def schedule_weight_per_minute(plan: SignalPlan, symbols: int) -> int:
    """가장 바쁜 1분의 weight 추정: 심볼마다 참조 시세(60초 ÷ 주기 회 × 2) + 1m klines(격자 시각 1회 × 2, 있으면) +
    1d klines(일 경계·재시도 1회 × 2). 쪽 나눔 없음(한 쪽)."""
    per_symbol = math.ceil(60 / plan.reference_every.total_seconds()) * PATH_WEIGHT[TICKER_PATH]
    if plan.minute is not None:
        per_symbol += PATH_WEIGHT[KLINES_PATH]
    per_symbol += PATH_WEIGHT[KLINES_PATH]
    return symbols * per_symbol


def public_budget_problem(plan: SignalPlan, symbols: int, cap: int = BUDGET_CAP_PER_MINUTE) -> str | None:
    need = schedule_weight_per_minute(plan, symbols)
    if need > cap:
        return (f"실서버 공개 시세 호출 예산 초과: 일정 추정 분당 weight {need} > 상한 {cap} (심볼 {symbols}) — "
                "시작하지 않는다 (CP-039 D-247)")
    return None


# --- 조회 클라이언트 ---------------------------------------------------------------------------------------------

class PublicQueryError(RuntimeError):
    """조회 실패. kind: BLOCKED(가드 거부) | BUDGET(예산 보류) | RATE_LIMIT(429 중단) | BANNED(418 중단) |
    RETRY_EXHAUSTED(5xx·전송 실패 재시도 소진) | HTTP(그 밖의 4xx) | PARSE(응답 해석 불가)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class BinancePublicClient:
    """공개 GET 두 가지(klines·ticker/price)만. 자격정보 없음 — 헤더를 보내지 않는다."""

    def __init__(self, guard: PublicDataGuard, budget: PublicBudget, *, now_ms: Callable[[], int],
                 sleep: Callable[[float], None], on_alert: Alert) -> None:
        self._guard = guard
        self._budget = budget
        self._now = now_ms
        self._sleep = sleep
        self._alert = on_alert
        self.stopped_until_ms: int | None = None     # 429·418(Retry-After) 중단 끝 시각
        self.banned = False                          # 418 + Retry-After 없음 → 사람 재개까지
        self.rate_limited = 0

    def __repr__(self) -> str:
        return f"BinancePublicClient(banned={self.banned}, stopped_until_ms={self.stopped_until_ms})"

    def resume_after_ban(self) -> None:
        """사람 확인 뒤 재개 (418 + Retry-After 없음)."""
        self.banned = False
        self.stopped_until_ms = None

    def klines(self, symbol: str, interval: str, *, limit: int, start_ms: int | None = None,
               end_ms: int | None = None) -> object:
        q: dict[str, object] = {"symbol": symbol, "interval": interval}
        if start_ms is not None:
            q["startTime"] = start_ms
        if end_ms is not None:
            q["endTime"] = end_ms
        q["limit"] = limit
        return self._get(KLINES_PATH, q)

    def ticker_price(self, symbol: str) -> Decimal:
        body = self._get(TICKER_PATH, {"symbol": symbol})
        if not isinstance(body, dict) or body.get("symbol") != symbol or not isinstance(body.get("price"), str):
            raise PublicQueryError(f"{TICKER_PATH}: 응답 형식 아님", "PARSE")
        try:
            price = Decimal(body["price"])
        except InvalidOperation:
            raise PublicQueryError(f"{TICKER_PATH}: 가격 형식 아님", "PARSE") from None
        if not price.is_finite() or price <= 0:
            raise PublicQueryError(f"{TICKER_PATH}: 가격이 0 이하", "PARSE")
        return price

    def _get(self, path: str, query: Mapping[str, object]) -> object:
        if self.banned:
            raise PublicQueryError(f"{path}: 418 차단 뒤 사람 재개 전 — 보내지 않는다 (D-247)", "BANNED")
        if self.stopped_until_ms is not None:
            if self._now() < self.stopped_until_ms:
                raise PublicQueryError(f"{path}: 호출 제한 중단 중 — 보내지 않는다 (D-247)", "RATE_LIMIT")
            self.stopped_until_ms = None
        if not self._budget.take(PATH_WEIGHT[path]):
            raise PublicQueryError(f"{path}: 실서버 호출 예산 부족 — 보류 (D-247)", "BUDGET")
        url = f"{PUBLIC_REST}{path}?{urlencode(list(query.items()))}"
        err = ""
        for attempt in range(RETRIES + 1):
            try:
                r = self._guard.send("GET", url, {})
            except PublicDataRefused as exc:
                raise PublicQueryError(str(exc), "BLOCKED") from None
            except PublicTransportError as exc:
                err = f"전송 실패 ({type(exc).__name__})"
            else:
                status = int(r.status)
                if status == 200:
                    try:
                        return json.loads(r.text)
                    except ValueError:
                        raise PublicQueryError(f"{path}: 응답이 JSON이 아님", "PARSE") from None
                if status in (418, 429):
                    self._stop(path, status, r.headers)
                    raise PublicQueryError(f"{path}: HTTP {status} 호출 제한 — 재시도 없이 중단 (D-247)",
                                           "BANNED" if status == 418 and self.banned else "RATE_LIMIT")
                if status < 500:
                    raise PublicQueryError(f"{path}: HTTP {status} — 재시도 없음", "HTTP")
                err = f"HTTP {status}"
            if attempt >= RETRIES:
                break
            self._sleep(BACKOFF_S[attempt])
        raise PublicQueryError(f"{path}: 재시도 {RETRIES}회 소진 ({err})", "RETRY_EXHAUSTED")

    def _stop(self, path: str, status: int, headers: Mapping[str, str]) -> None:
        ra = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        secs = int(ra) if ra is not None and str(ra).isdigit() else None
        self.rate_limited += 1
        if status == 418 and secs is None:
            self.banned = True
            what = "사람 확인 뒤 재개"
        else:
            wait = secs if secs is not None else DEFAULT_RETRY_AFTER_S
            self.stopped_until_ms = self._now() + wait * 1000
            what = f"{wait}초 뒤 재개"
        self._alert("EMERGENCY", f"실서버 공개 시세 HTTP {status} ({path}) — 신호 출처 호출 전부 중단, {what}. "
                                 "신규 진입·증액은 참조 시세가 늙으면 게이트가 거부, 청산 경로 영향 없음 (CP-039 D-247)")


# --- klines 해석 (BN §8 응답 형식) ----------------------------------------------------------------------------------

class PublicDataError(ValueError):
    """klines 응답 품질 위반(필드·경계·closeTime·순서·중복·OHLC·음수)."""


@dataclass(frozen=True)
class PublicKline:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time_ms: int
    quote_volume: Decimal
    trades: int
    taker_buy_base: Decimal
    taker_buy_quote: Decimal


def _num(v: object, what: str) -> Decimal:
    if not isinstance(v, str):
        raise PublicDataError(f"{what}: 문자열 소수가 아님")
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise PublicDataError(f"{what}: 소수 형식 아님") from None
    if not d.is_finite() or d < 0:
        raise PublicDataError(f"{what}: 음수·무한")
    return d


def parse_public_klines(rows: object, *, interval: str) -> list[PublicKline]:
    """[openTime, open, high, low, close, volume, closeTime, quoteVolume, tradeCount, takerBuyBase, takerBuyQuote, ignore]."""
    step = INTERVAL_MS.get(interval)
    if step is None:
        raise PublicDataError(f"지원하지 않는 간격 {interval}")
    if not isinstance(rows, list):
        raise PublicDataError("klines 응답이 목록이 아님")
    out: list[PublicKline] = []
    prev: int | None = None
    for i, r in enumerate(rows):
        if not isinstance(r, list) or len(r) < 11:
            raise PublicDataError(f"{i}번째 행: 필드 수 부족")
        ot, ct, n = r[0], r[6], r[8]
        if any(isinstance(x, bool) or not isinstance(x, int) for x in (ot, ct, n)):
            raise PublicDataError(f"{i}번째 행: 시각·거래 수가 정수가 아님")
        if ot % step != 0 or ct != ot + step - 1 or n < 0:
            raise PublicDataError(f"{i}번째 행: 봉 경계·closeTime 위반")
        if prev is not None and ot <= prev:
            raise PublicDataError(f"{i}번째 행: 순서·중복 위반")
        prev = ot
        o, h, lo, c = (_num(r[k], f"{i}번째 행 {name}") for k, name in ((1, "open"), (2, "high"), (3, "low"),
                                                                         (4, "close")))
        if h < max(o, c, lo) or lo > min(o, c):
            raise PublicDataError(f"{i}번째 행: OHLC 관계 위반")
        out.append(PublicKline(_EPOCH + timedelta(milliseconds=ot), o, h, lo, c, _num(r[5], f"{i}번째 행 volume"), ct,
                               _num(r[7], f"{i}번째 행 quoteVolume"), n, _num(r[9], f"{i}번째 행 takerBuyBase"),
                               _num(r[10], f"{i}번째 행 takerBuyQuote")))
    return out

