"""신호 출처 공급 스레드 `signal-feed`·참조 시세 저장 (CP-039 D-248·D-254, 승인 A-115, T-061). 네트워크 라이브러리 없음.

`[signal_source] kind = "binance-public"`일 때만 만든다(app 조립 지점). 실서버 공개 시세(marketdata/binance_public — 키 없는 GET
두 가지)를 읽어 판정 입력의 **봉(1d 확정 봉·1m 닫힌 봉)과 참조 시세(reference_quote)**만 공급한다. 주문 가격·수량·대조·dust·
한도 평가는 실행 출처(테스트넷) 값 그대로다(D-249·D-253) — 이 스레드는 실행 경로에 아무것도 넣지 않는다.

- 1d klines(limit 200): 시작 직후·UTC 날짜가 바뀐 뒤. 확정 봉(봉 시작 + 1일 ≤ 지금)만 일봉 받는 쪽(SpotFactsStore.set_daily_bars)에.
  실패하면 값을 바꾸지 않고(어제 봉이 없으면 판정 입력이 진입 무효 — domain/daily_input), 1분 뒤 다시.
- 참조 시세 ticker/price: 10초마다 → SignalFacts(수신 시각 = 시세 시각). 실패하면 갱신하지 않는다 → 60초 뒤 게이트 G11-S가
  위험 확대를 거부(D-250 (a)), 위험 축소 영향 없음.
- 1m klines: 판정 격자 시각마다 한 번(분봉 받는 쪽 SpotMinuteBook — 닫힌 봉 거르기·저장은 받는 쪽, source = binance-public).
  조회 실패·품질 위반 = 그 심볼 분봉 입력 무효(minute_quality_failed — 정지 없음, 감사·묶음 경고).
장애 처리 (D-254): **K10·K8·K5를 걸지 않는다**(테스트넷 실행 경로는 정상), 테스트넷 봉으로 대체하지 않는다. 같은 조회의 실패는
첫 번째·3회 연속에만 경고, 회복 1건(묶음). 429·418은 클라이언트가 이 출처 호출 전부 중단·긴급 알림(D-247). 그 밖의 예외는
스레드를 끝낸다 — 감시 스레드가 긴급 알림만(정지 없음, app 조립: watched_alert_only).
테스트넷 시세 공급(binance-feed)의 1d 봉은 이 모드에서 BarsDroppedSink로 버린다(출처를 섞지 않음, D-248).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_system.domain.market import Quote
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT
from trading_system.domain.strategy import Bar

from trading_system.marketdata.binance_public import (
    BinancePublicClient, PublicDataError, PublicMinutePlan, PublicQueryError, SignalPlan, parse_public_klines,
)

FAIL_STREAK_WARN = 3
Notify = Callable[[str, str], None]     # (수준 EMERGENCY|WARNING|INFO, 문구) — 정지 없음


class SignalFacts:
    """신호 출처 참조 시세(심볼별 최신 1개). 공급 스레드가 쓰고 판정·실행 스레드(게이트·판정 입력·체결 기록)가 읽는다."""

    def __init__(self, universe: Sequence[str]) -> None:
        self.universe = tuple(universe)
        self._q: dict[str, Quote] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"SignalFacts(universe={list(self.universe)})"

    def update_reference(self, symbol: str, price: Decimal, at: datetime) -> None:
        if symbol not in self.universe or price <= 0:
            return
        with self._lock:
            self._q[symbol] = Quote(symbol, price, at)

    def reference_quote(self, instrument: str) -> Quote | None:
        with self._lock:
            return self._q.get(instrument)

    def fill_reference(self, instrument: str) -> tuple[Decimal, datetime] | None:
        """체결 기록 시점의 최신 참조 시세와 그 시각 (D-251 '체결 시각 P_s'). 없으면 None."""
        q = self.reference_quote(instrument)
        return None if q is None else (q.price, q.at)


class DailySink(Protocol):
    def set_daily_bars(self, symbol: str, bars: Sequence[Bar]) -> None: ...


class MinuteSink(Protocol):
    def put_minute_bars(self, symbol: str, bars: Sequence[MinuteBar], fetched_at: datetime) -> None: ...

    def minute_quality_failed(self, symbol: str, reason: str, at: datetime) -> None: ...


class BarsDroppedSink:
    """실행 출처(테스트넷) 시세 공급에 넘기는 받는 쪽: 1d 봉만 버리고 나머지(시세·참조가·규칙)는 그대로 넘긴다 (D-248)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.dropped = 0

    def __repr__(self) -> str:
        return f"BarsDroppedSink(dropped={self.dropped})"

    def set_daily_bars(self, symbol: str, bars: Sequence[Bar]) -> None:
        self.dropped += 1

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class SignalFeed:
    def __init__(self, client: BinancePublicClient, symbols: Sequence[str], *, now: Callable[[], datetime],
                 notify: Notify, daily_sink: DailySink, reference: SignalFacts, plan: SignalPlan = SignalPlan(),
                 minute_sink: MinuteSink | None = None) -> None:
        if plan.minute is not None and minute_sink is None:
            raise ValueError("1m 조회 계획에는 받는 쪽(minute_sink)이 필요하다")
        self._client = client
        self._symbols = tuple(symbols)
        self._now = now
        self._notify = notify
        self._daily = daily_sink
        self.reference = reference
        self._plan = plan
        self._minute: PublicMinutePlan | None = plan.minute
        self._grid = plan.minute.grid() if plan.minute is not None else None
        self._minute_sink = minute_sink
        self._minute_at: datetime | None = None
        self._day: date | None = None
        self._daily_due: dict[str, datetime] = {}
        self._next_ref: datetime | None = None
        self._streak: dict[str, int] = {}
        self.minute_skipped = 0
        self.steps = 0

    def __repr__(self) -> str:
        return f"SignalFeed(symbols={list(self._symbols)}, steps={self.steps})"

    @property
    def client(self) -> BinancePublicClient:
        return self._client

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.step()
            stop.wait(self._plan.loop_s)

    def step(self) -> None:
        now = self._now()
        self.steps += 1
        if self._day != now.date():
            self._day = now.date()
            self._daily_due = {s: now for s in self._symbols}
        for s, due in list(self._daily_due.items()):
            if now >= due:
                self._load_daily(s, now)
        if self._grid is not None:
            self._minute_step(now)
        if self._next_ref is None or now >= self._next_ref:
            self._next_ref = now + self._plan.reference_every
            for s in self._symbols:
                self._call(f"참조 시세 {s}", lambda s=s: self.reference.update_reference(
                    s, self._client.ticker_price(s), self._now()))

    # --- 단계 ---
    def _load_daily(self, s: str, now: datetime) -> None:
        def one() -> None:
            rows = self._client.klines(s, "1d", limit=self._plan.kline_limit)
            ks = parse_public_klines(rows, interval="1d")
            bars = [Bar(k.open_time.date(), k.open, k.high, k.low, k.close, k.volume)
                    for k in ks if k.open_time + timedelta(days=1) <= now]
            self._daily.set_daily_bars(s, bars)
        if self._call(f"1d klines {s}", one):
            self._daily_due.pop(s, None)
        else:
            self._daily_due[s] = now + self._plan.daily_retry_every

    def _minute_step(self, now: datetime) -> None:
        grid, plan, sink = self._grid, self._minute, self._minute_sink
        assert grid is not None and plan is not None and sink is not None
        at = grid.latest_at_or_before(now)
        if at is None or at == self._minute_at:
            return
        self._minute_at = at
        if now - at > grid.grace:
            self.minute_skipped += 1
            return
        for s in self._symbols:
            def one(s: str = s) -> None:
                rows = self._client.klines(s, "1m", limit=plan.limit)
                ks = parse_public_klines(rows, interval="1m")   # 닫힌 봉 거르기는 받는 쪽(SpotMinuteBook)
                sink.put_minute_bars(s, [MinuteBar(s, 1, k.open_time, k.open, k.high, k.low, k.close, k.volume,
                                                   NO_ADJUSTMENT, quote_volume=k.quote_volume, trades=k.trades,
                                                   taker_buy_base=k.taker_buy_base, taker_buy_quote=k.taker_buy_quote)
                                         for k in ks], now)
            if not self._call(f"1m klines {s}", one):
                sink.minute_quality_failed(s, "신호 출처 1m 조회 실패·품질 위반 (CP-039 D-254)", now)

    # --- 실패 묶음 (D-254 (1)) ---
    def _call(self, what: str, fn: Callable[[], None]) -> bool:
        try:
            fn()
        except (PublicQueryError, PublicDataError) as exc:
            n = self._streak.get(what, 0) + 1
            self._streak[what] = n
            if n == 1:
                self._notify("WARNING", f"신호 출처 {what} 실패: {exc} — 그 판정의 신호 입력 무효(정지 없음, 테스트넷 봉으로 "
                                        "대체하지 않음, CP-039 D-254)")
            elif n == FAIL_STREAK_WARN:
                self._notify("WARNING", f"신호 출처 {what} {FAIL_STREAK_WARN}회 연속 실패 — 신규 진입·증액은 계속 막힘, "
                                        "청산은 사람 운영 요청(/exit) 가능 (CP-039 D-254)")
            return False
        if self._streak.pop(what, 0):
            self._notify("INFO", f"신호 출처 {what} 회복")
        return True
