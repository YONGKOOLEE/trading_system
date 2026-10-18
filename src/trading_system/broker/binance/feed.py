"""바이낸스 시세·규칙·서버 시각 공급 스레드 `binance-feed` (CP-036 D-206·D-215, T-048). 네트워크 라이브러리 import 없음.

공급 대상(sink)은 공통 계약 모양(marketdata/spot_facts.SpotFactsStore와 같은 메서드)이다 — 이 패키지는 marketdata를
import하지 않는다(03 §7). 응답 해석은 BinanceMarketFacts(같은 패키지)를 거쳐 domain 값(Quote·Bar·SymbolRules)만 넘긴다.

일정 (주기 값은 [제안] — 시세 신선도 60초(A-064)의 1/12 등. 엔드포인트 weight: klines 2·avgPrice 2·exchangeInfo 20·time 1은
BN §3·§6·§8 확인값, ticker/price(심볼 지정) 2는 **미확인(추정)** — 예산 계산에는 2로 잡는다)
- 최근가 ticker/price: 5초마다 유니버스 심볼별. 수신 시각 = 시세 시각(D-116). 실패하면 값을 갱신하지 않는다 → 시세가
  늙어 60초 뒤 게이트 G11이 위험 확대를 거부, 지연 매도 규칙(D-120)이 적용된다.
- avgPrice: 60초마다(시장가 명목가 참조가, dust 판정 D-102). referencePrice는 응답 형식 미확인이라 부르지 않는다 [DP-59].
- 1d klines(limit 200): 시작 직후·UTC 날짜가 바뀐 뒤 첫 단계. 확정 봉만(봉 시작 + 1일 ≤ 지금) 넘긴다(진행 중 봉 미사용).
  품질 실패 → K10(종목 범위, 큐 경유 halts).
- exchangeInfo: UTC 날짜가 바뀐 뒤·-1013 수신 뒤(request_rules_reload) 다시 적재. 실패(조회·해석·rateLimits 없음)면
  clear_rules() — 위험 확대 거부, 위험 축소는 마지막 정상 단위로 허용(D-106). TRADING 아닌 심볼은 규칙에서 뺀다(DP-56).
- 서버 시각: 10분마다(D-115). 0.5초 이상 오차는 클라이언트가 CLOCK 경고.
- 호출 우선순위: 시세·평균가·klines = MARKET(최하위, 예산 70% 상한) — 예산이 부족하면 시세가 먼저 보류된다(D-113).
  보류·조회 실패는 그 단계만 건너뛴다(조회 재시도는 클라이언트 규칙: 최대 3회).
오류: 조회 실패(BinanceQueryError)·응답 해석 불가는 단계별로 삼키고 경고만 — 같은 조회(키 "ticker BTCUSDT" 등)의 반복은
alert_throttle.ConditionAlerts로 묶는다(시작 1건·30분마다 요약·회복 1건, DP-67). 그 밖의 예외는 스레드를 끝낸다 — 감시 스레드가 죽은
binance-feed를 보고 긴급 알림 + K5(D-215).

1m klines (CP-037 D-220, T-051, 승인 A-113) — **기본 꺼짐**(minute=None). 테스트넷 1분봉 주기 조회는 별도 승인 범위라
(CP-037 §4·T-056) app이 설정 `[minute] enabled = true` + 승인 A-ID를 확인했을 때만 계획을 넣는다 [DP-71].
- 일정: 판정 격자(domain/minute_series.PeriodGrid — UTC 00:00 + k×N분 + 확정 여유, 주문 창 안) 시각마다 유니버스 심볼별
  `GET /api/v3/klines?interval=1m&limit=lookback+3`(한 쪽, weight 2, 시세 우선순위 = 같은 WeightBudget 최하위). 같은 격자
  시각은 한 번만, 유예(min(60초, N분의 절반))를 넘겨 늦은 단계면 조회하지 않고 minute_skipped만 센다.
- 해석: market.parse_klines(필드·경계·closeTime·순서·중복·OHLC·음수 검사) → domain MinuteBar(adjusted = NO_ADJUSTMENT)
  → minute_sink.put_minute_bars. 해석 실패 = 품질 실패 → minute_sink.minute_quality_failed(받는 쪽이 분봉 입력 무효 처리 —
  정지 없음, DP-72 개정).
  조회 실패·보류는 다른 조회와 같은 경보 묶음(DP-67). 닫힌 봉 거르기·저장은 받는 쪽(marketdata/spot_minute)이 한다.
- 시작 예산 검사: minute_budget_problem — 심볼 수 × 쪽 × weight 2 × (창 길이 안 격자 수)가 내부 예산(rateLimits 50%)의
  10%를 넘으면 시작 거부(D-220 [제안], run_start.M2RunConnector) [DP-74]. 이력 채우기(CP-040 D-261)를 켜면 같은 식에
  채우기 몫(분당 상한 × 창 분 × weight 2, [제안] 분당 20회)을 더해 검사한다.
- 확장 열 (CP-040 D-260, T-066): 응답 7~10번째 값(quoteVolume·tradeCount·takerBuyBase·takerBuyQuote, BN S6)을 MinuteBar
  선택 필드로 넘긴다 — 저장은 받는 쪽이 추가 전용 `minute_bar_ext`에.
- 이력 채우기 조회 (D-261): minute_page_fetcher(client) — startTime·endTime 나눔 조회 한 쪽(같은 클라이언트·WeightBudget·
  시세 우선순위, RunGuard 경유). 일정·저장·실패 처리는 app/m2_backfill. **실행 경로 연결은 별도 승인 뒤**(실제 백필 호출).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from trading_system.broker.base import BrokerDataError, BrokerQueryError
from trading_system.domain.market import Quote
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import (
    DEFAULT_CONFIRM_MARGIN, NO_ADJUSTMENT, ORDER_WINDOW, PeriodGrid,
)
from trading_system.domain.sinks import HaltControl
from trading_system.domain.strategy import Bar
from trading_system.domain.trading_rules import SymbolRules as DomainRules

from .alert_throttle import ConditionAlerts
from .client import WEIGHT, BinanceQueryError, BinanceSpotClient
from .facts import BinanceMarketFacts
from .market import Kline, KlineDataError, parse_avg_price, parse_klines
from .weights import BUDGET_FRACTION_PERCENT, RateLimits


class _FeedProblem(Exception):
    """조회는 됐으나 쓸 수 없는 응답 — 조회 실패와 같은 경보 묶음으로."""


class SpotSink(Protocol):
    def update_quote(self, quote: Quote) -> None: ...

    def set_reference_prices(self, symbol: str, prices: Sequence[Decimal]) -> None: ...

    def set_daily_bars(self, symbol: str, bars: Sequence[Bar]) -> None: ...

    def load_rules(self, rules: Mapping[str, DomainRules]) -> list[str]: ...

    def clear_rules(self) -> None: ...


class MinuteSink(Protocol):
    """1m 봉 받는 쪽 (marketdata/spot_minute.SpotMinuteBook과 같은 메서드)."""

    def put_minute_bars(self, symbol: str, bars: Sequence[MinuteBar], fetched_at: datetime) -> None: ...

    def minute_quality_failed(self, symbol: str, reason: str, at: datetime) -> None: ...


MINUTE_KLINE_EXTRA = 3            # limit = lookback + 3 (진행 중 봉·늦은 단계 여유) [제안]
KLINE_PAGE_MAX = 1000             # klines limit 상한 (BN §8)
MAX_MINUTE_LOOKBACK = KLINE_PAGE_MAX - MINUTE_KLINE_EXTRA   # 한 쪽으로 끝낸다 (쪽 나눔 없음) [DP-71]
MINUTE_BUDGET_PERCENT = 10        # 분봉 조회 몫 상한 = 내부 예산의 10% (D-220 [제안])


@dataclass(frozen=True)
class MinutePlan:
    """1m klines 조회 계획 (D-220). period_minutes N = 판정 격자, lookback_bars = 판정 창 길이."""

    period_minutes: int
    lookback_bars: int
    margin: timedelta = DEFAULT_CONFIRM_MARGIN
    window_start: time = ORDER_WINDOW[0]
    window_end: time = ORDER_WINDOW[1]

    def __post_init__(self) -> None:
        n = self.lookback_bars
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_MINUTE_LOOKBACK:
            raise ValueError(f"lookback_bars는 1~{MAX_MINUTE_LOOKBACK} 정수 (klines 한 쪽)")
        self.grid()   # period_minutes·margin 검사

    def grid(self) -> PeriodGrid:
        return PeriodGrid(self.period_minutes, self.margin, self.window_start, self.window_end)

    @property
    def limit(self) -> int:
        return self.lookback_bars + MINUTE_KLINE_EXTRA

    @property
    def pages(self) -> int:
        return math.ceil(self.limit / KLINE_PAGE_MAX)


def minute_budget_problem(limits: RateLimits, symbols: int, plan: MinutePlan, *,
                          backfill_per_minute: int = 0) -> str | None:
    """분봉 조회가 REQUEST_WEIGHT 창마다 내부 예산(한도 × 50%)의 10%를 넘으면 거부 사유, 아니면 None (D-220).
    klines 한 쪽 weight는 client.WEIGHT["klines"] 하나만 쓴다(조회 때 차감하는 값과 같은 상수).
    backfill_per_minute > 0 = 이력 채우기 분당 상한(CP-040 D-261) — 창마다 그 몫(분당 상한 × 창 분, 최소 1회)을 더한다."""
    weight = WEIGHT["klines"]
    for w in limits.weight:
        per_window = max(1, math.ceil(w.interval_ms / (plan.period_minutes * 60_000)))
        need = symbols * plan.pages * weight * per_window
        fill = backfill_per_minute * max(1, math.ceil(w.interval_ms / 60_000)) * weight if backfill_per_minute > 0 else 0
        cap = w.limit * BUDGET_FRACTION_PERCENT // 100
        if (need + fill) * 100 > cap * MINUTE_BUDGET_PERCENT:
            extra = (f" + 백필 분당 {backfill_per_minute}회 × weight {weight} = {fill} (D-261)" if fill else "")
            return (f"분봉 조회 호출 예산 초과: 심볼 {symbols} × {plan.pages}쪽 × weight {weight} × 창 {w.interval_ms // 1000}초 "
                    f"안 {per_window}회 = {need}{extra} > 내부 예산 {cap}의 {MINUTE_BUDGET_PERCENT}% — 시작하지 않는다 (D-220)")
    return None


def parsed_minute_pages(raw: Callable[[str, int, int, int], object]
                        ) -> Callable[[str, int, int, int], list[MinuteBar]]:
    """이력 채우기 한 쪽 (D-261): 원본 klines 1m 응답 → parse_klines(필드·경계·closeTime·순서·OHLC) → MinuteBar.
    바이낸스 전용 타입·오류는 이 패키지 안에서 공통 오류로 바꾼다 — 조회 실패·보류 BinanceQueryError → BrokerQueryError,
    해석 실패 KlineDataError → BrokerDataError (app/m2_backfill은 바이낸스 패키지를 import하지 않는다, 계약 시험)."""
    def fetch(symbol: str, start_ms: int, end_ms: int, limit: int) -> list[MinuteBar]:
        try:
            rows = raw(symbol, start_ms, end_ms, limit)
        except BinanceQueryError as exc:
            raise BrokerQueryError(str(exc)) from exc
        try:
            ks = parse_klines(rows, symbol=symbol, interval="1m")
        except KlineDataError as exc:
            raise BrokerDataError(str(exc)) from exc
        return [minute_bar_of(k) for k in ks]
    return fetch


def minute_page_fetcher(client: BinanceSpotClient) -> Callable[[str, int, int, int], list[MinuteBar]]:
    """이력 채우기 한 쪽 조회 (D-261): (심볼, 시작 ms, 끝 ms, limit) → 해석된 MinuteBar. 실패는 BrokerQueryError·BrokerDataError."""
    return parsed_minute_pages(
        lambda symbol, start_ms, end_ms, limit: client.klines(symbol, "1m", limit=limit, start_ms=start_ms, end_ms=end_ms))


@dataclass(frozen=True)
class FeedPlan:
    ticker: timedelta = timedelta(seconds=5)
    avg_price: timedelta = timedelta(seconds=60)
    server_time: timedelta = timedelta(minutes=10)
    kline_limit: int = 200
    loop_s: float = 1.0


def minute_bar_of(k: Kline) -> MinuteBar:
    """klines 1m 한 행 → MinuteBar(adjusted = NO_ADJUSTMENT) + 확장 열(7~10번째 값, D-260)."""
    return MinuteBar(k.symbol, 1, k.open_time, k.open, k.high, k.low, k.close, k.volume, NO_ADJUSTMENT,
                     quote_volume=k.quote_volume, trades=k.trades, taker_buy_base=k.taker_buy_base,
                     taker_buy_quote=k.taker_buy_quote)


class BinanceFeed:
    def __init__(self, client: BinanceSpotClient, sink: SpotSink, symbols: Sequence[str], *,
                 now: Callable[[], datetime], alert: Callable[[str, str], None], halts: HaltControl | None = None,
                 account: str | None = None, plan: FeedPlan = FeedPlan(), minute: MinutePlan | None = None,
                 minute_sink: MinuteSink | None = None) -> None:
        if minute is not None and minute_sink is None:
            raise ValueError("1m 조회 계획에는 받는 쪽(minute_sink)이 필요하다")
        self._minute = minute
        self._minute_grid = minute.grid() if minute is not None else None
        self._minute_sink = minute_sink
        self._minute_at: datetime | None = None
        self.minute_skipped = 0
        self._client = client
        self._sink = sink
        self._symbols = tuple(symbols)
        self._now = now
        self._alert = alert
        self._conds = ConditionAlerts(alert, lambda: now().timestamp())
        self._plan = plan
        self._facts = BinanceMarketFacts(now=now, universe=list(symbols), halts=halts, account=account)
        self._day: date | None = None
        self._next: dict[str, datetime] = {}
        self._reload = threading.Event()
        self.steps = 0

    def __repr__(self) -> str:
        return f"BinanceFeed(symbols={list(self._symbols)}, steps={self.steps})"

    def request_rules_reload(self, symbol: str | None = None) -> None:
        """-1013 필터 거부 뒤(어댑터 on_filter_reject) — 다음 단계에서 exchangeInfo 재적재 (D-106)."""
        self._reload.set()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.step()
            stop.wait(self._plan.loop_s)

    def step(self) -> None:
        now = self._now()
        self.steps += 1
        today = now.date()
        first = self._day is None
        if self._day != today:
            if not first:
                self._load_rules()
            self._load_klines()
            self._day = today
        if self._reload.is_set():
            self._reload.clear()
            self._load_rules()
        if self._minute_grid is not None:
            self._minute_step(now)
        if self._due("ticker", now, self._plan.ticker):
            for s in self._symbols:
                self._call(f"ticker {s}", lambda s=s: self._ticker(s))
        if self._due("avg", now, self._plan.avg_price):
            for s in self._symbols:
                self._call(f"avgPrice {s}", lambda s=s: self._avg(s))
        if self._due("time", now, self._plan.server_time):
            self._call("서버 시각", self._client.server_time_offset_ms)

    def _due(self, key: str, now: datetime, every: timedelta) -> bool:
        at = self._next.get(key)
        if at is not None and now < at:
            return False
        self._next[key] = now + every
        return True

    def _call(self, what: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except BinanceQueryError as exc:
            self._conds.fail(what, "FEED", f"{what} 실패·보류: {exc}")
            return
        except _FeedProblem as exc:
            self._conds.fail(what, "FEED", str(exc))
            return
        self._conds.ok(what, what)

    def _ticker(self, s: str) -> None:
        body = self._client.ticker_price(s)
        if self._facts.update_ticker(s, body):
            q = self._facts.quote(s)
            if q is not None:
                self._sink.update_quote(q)
        else:
            raise _FeedProblem(f"ticker {s}: 응답 해석 불가 — 시세 갱신 안 함")

    def _avg(self, s: str) -> None:
        body = self._client.avg_price(s)
        try:
            avg = parse_avg_price(body, symbol=s)
        except KlineDataError:
            self._sink.set_reference_prices(s, [])   # 모름 — 참조가 없음으로 (형식 오류는 K10 아님, facts [제안])
            return
        self._sink.set_reference_prices(s, [avg.price])

    def _minute_step(self, now: datetime) -> None:
        """판정 격자 시각마다 한 번 1m klines (D-220). 늦은 단계(유예 초과)는 조회하지 않는다."""
        grid = self._minute_grid
        assert grid is not None
        at = grid.latest_at_or_before(now)
        if at is None or at == self._minute_at:
            return
        self._minute_at = at
        if now - at > grid.grace:
            self.minute_skipped += 1
            return
        for s in self._symbols:
            self._call(f"klines 1m {s}", lambda s=s: self._minute_klines(s, now))

    def _minute_klines(self, s: str, now: datetime) -> None:
        plan, sink = self._minute, self._minute_sink
        assert plan is not None and sink is not None
        rows = self._client.klines(s, "1m", limit=plan.limit)
        try:
            ks = parse_klines(rows, symbol=s, interval="1m")
        except KlineDataError as exc:
            sink.minute_quality_failed(s, str(exc), now)
            return
        sink.put_minute_bars(s, [minute_bar_of(k) for k in ks], now)

    def _load_klines(self) -> None:
        for s in self._symbols:
            def one(s: str = s) -> None:
                rows = self._client.klines(s, "1d", limit=self._plan.kline_limit)
                if self._facts.update_klines(s, rows, "1d"):
                    self._sink.set_daily_bars(s, [Bar(k.open_time.date(), k.open, k.high, k.low, k.close, k.volume)
                                                  for k in self._facts.daily_bars(s)])
            self._call(f"klines {s}", one)

    def _load_rules(self) -> None:
        bodies = []
        try:
            for s in self._symbols:
                bodies.append(self._client.exchange_info(s))
        except BinanceQueryError as exc:
            self._fail(f"exchangeInfo 조회 실패: {exc}")
            return
        merged = {"timezone": "UTC", "exchangeFilters": [], "symbols": []}
        for b in bodies:
            if not isinstance(b, dict) or "rateLimits" not in b or not isinstance(b.get("symbols"), list):
                self._fail("exchangeInfo 응답 형식 아님·rateLimits 없음")
                return
            merged.setdefault("rateLimits", b["rateLimits"])
            merged["symbols"].extend(b["symbols"])  # type: ignore[union-attr]
        if not self._facts.load_exchange_info(merged):
            self._fail(self._facts.load_error or "exchangeInfo 해석 실패")
            return
        rules = {s: r for s in self._symbols if (r := self._facts.rules(s)) is not None
                 and not self._facts.trading_status(s)}
        missing = self._sink.load_rules(rules)
        if missing:
            self._alert("RULES", f"거래 규칙 없음·TRADING 아님: {', '.join(missing)} — 그 심볼 위험 확대 거부 (D-106·D-107)")

    def _fail(self, why: str) -> None:
        self._sink.clear_rules()
        self._alert("RULES", f"{why} — 직전 규칙 버림, 위험 확대 거부 (D-106)")
