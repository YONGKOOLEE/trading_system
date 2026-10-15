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
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_system.domain.market import Quote
from trading_system.domain.sinks import HaltControl
from trading_system.domain.strategy import Bar
from trading_system.domain.trading_rules import SymbolRules as DomainRules

from .alert_throttle import ConditionAlerts
from .client import BinanceQueryError, BinanceSpotClient
from .facts import BinanceMarketFacts
from .market import KlineDataError, parse_avg_price


class _FeedProblem(Exception):
    """조회는 됐으나 쓸 수 없는 응답 — 조회 실패와 같은 경보 묶음으로."""


class SpotSink(Protocol):
    def update_quote(self, quote: Quote) -> None: ...

    def set_reference_prices(self, symbol: str, prices: Sequence[Decimal]) -> None: ...

    def set_daily_bars(self, symbol: str, bars: Sequence[Bar]) -> None: ...

    def load_rules(self, rules: Mapping[str, DomainRules]) -> list[str]: ...

    def clear_rules(self) -> None: ...


@dataclass(frozen=True)
class FeedPlan:
    ticker: timedelta = timedelta(seconds=5)
    avg_price: timedelta = timedelta(seconds=60)
    server_time: timedelta = timedelta(minutes=10)
    kline_limit: int = 200
    loop_s: float = 1.0


class BinanceFeed:
    def __init__(self, client: BinanceSpotClient, sink: SpotSink, symbols: Sequence[str], *,
                 now: Callable[[], datetime], alert: Callable[[str, str], None], halts: HaltControl | None = None,
                 account: str | None = None, plan: FeedPlan = FeedPlan()) -> None:
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
