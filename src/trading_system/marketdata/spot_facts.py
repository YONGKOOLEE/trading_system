"""M2(크립토 현물) 시세·규칙·1d 봉 메모리 공급자 (CP-036 D-201 (4)·D-207·D-215, T-045).

게이트 입력 gate.ports.MarketFacts·SymbolRulesSource와 M2 판정 입력(daily_bars)을 한 객체로 준다. 값은 공급자가 넣는다:
BACKTEST·시험은 가상 공급(규칙·시세·봉), PAPER는 바이낸스 공급 스레드(T-048, broker/binance/facts.BinanceMarketFacts와
같은 계약). 네트워크 없음. 시각은 주입 시계.

- 잠금(D-215): 공급 스레드(쓰기)와 판정·실행 스레드(읽기)가 함께 쓰므로 모든 읽기·쓰기를 잠금 안에서 한다. 판정은
  daily_bars가 돌려준 복사본(튜플)을 읽는다.
- 규칙(D-106): load_rules가 받은 심볼만 "적재됨". 유니버스 밖 심볼은 넣지 않는다. clear_rules = 직전 적재 실패(위험 확대 거부,
  위험 축소 허용) — 종목 정보(수량 단위)·호가 단위는 마지막 정상 값을 유지한다(BinanceMarketFacts와 같은 규칙).
- 심볼 상태: 규칙이 적재된 심볼만 제한 없음(빈 집합), 모르면 None.
- 세션(D-108): 매일 UTC 00:00~24:00 한 구간(session_247).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trading_system.domain.market import InstrumentInfo, InstrumentKind, Quote, SessionInfo, TradingStatus
from trading_system.domain.market_time import session_247
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.domain.strategy import Bar
from trading_system.domain.trading_rules import SymbolRules


@dataclass
class SpotFactsStore:
    now: Callable[[], datetime]
    universe: Sequence[str]
    halts: HaltControl | None = None      # 조립 지점이 연결 (공급 품질 실패 → K10, 공급자 몫)
    alerts: AlertSink | None = None
    account: str | None = None
    _rules: dict[str, SymbolRules] = field(default_factory=dict)
    _last_known: dict[str, SymbolRules] = field(default_factory=dict)
    _quotes: dict[str, Quote] = field(default_factory=dict)
    _extra_prices: dict[str, tuple[Decimal, ...]] = field(default_factory=dict)
    _bars: dict[str, tuple[Bar, ...]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- 공급자 입력 ------------------------------------------------------------------------
    def load_rules(self, rules: Mapping[str, SymbolRules]) -> list[str]:
        """규칙 적재(교체). 유니버스 밖 심볼은 버린다. 돌려주는 값 = 규칙이 없는 유니버스 심볼(빈 목록이면 전부 적재)."""
        with self._lock:
            self._rules = {s: r for s, r in rules.items() if s in self.universe}
            self._last_known.update(self._rules)
            return [s for s in self.universe if s not in self._rules]

    def clear_rules(self) -> None:
        """직전 적재 실패 (D-106): 위험 확대 거부. 종목 정보·호가 단위는 마지막 정상 값 유지."""
        with self._lock:
            self._rules = {}

    def update_quote(self, quote: Quote) -> None:
        if quote.instrument not in self.universe:
            return
        with self._lock:
            self._quotes[quote.instrument] = quote

    def set_reference_prices(self, symbol: str, prices: Sequence[Decimal]) -> None:
        """최근가 외 시장가 명목가 참조가(avgPrice·referencePrice, D-102)."""
        with self._lock:
            self._extra_prices[symbol] = tuple(p for p in prices if p > 0)

    def set_daily_bars(self, symbol: str, bars: Sequence[Bar]) -> None:
        if symbol not in self.universe:
            return
        with self._lock:
            self._bars[symbol] = tuple(sorted(bars, key=lambda b: b.at))

    # --- M2 판정 입력 -----------------------------------------------------------------------
    def daily_bars(self, symbol: str) -> tuple[Bar, ...]:
        """저장된 1d 봉 전체(봉 시작 UTC 날짜). 확정 봉 거르기는 판정 입력 구성기(D-207)가 판정 시각으로 한다."""
        with self._lock:
            return self._bars.get(symbol, ())

    # --- gate.ports.MarketFacts --------------------------------------------------------------
    def quote(self, instrument: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(instrument)

    def instrument(self, instrument: str) -> InstrumentInfo | None:
        with self._lock:
            r = self._last_known.get(instrument)
        # 크립토 현물 상품 유형은 열거에 없다 — m2_fakes·BinanceMarketFacts와 같은 임시 처리(배수 1, 단위 = LOT_SIZE step)
        return None if r is None else InstrumentInfo(instrument, InstrumentKind.STOCK, Decimal("1"), r.lot_step)

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None:
        with self._lock:
            return frozenset() if instrument in self._rules else None

    def session(self, trade_date: date) -> SessionInfo | None:
        return session_247(trade_date)

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        with self._lock:
            r = self._last_known.get(instrument)
        if r is None:
            return None
        return True if r.tick_size == 0 else price % r.tick_size == 0

    def tick_of(self, instrument: str, price: Decimal) -> Decimal | None:
        with self._lock:
            r = self._last_known.get(instrument)
        return None if r is None or r.tick_size == 0 else r.tick_size

    # --- gate.ports.SymbolRulesSource ---------------------------------------------------------
    def rules(self, instrument: str) -> SymbolRules | None:
        with self._lock:
            return self._rules.get(instrument)

    def market_prices(self, instrument: str) -> tuple[Decimal, ...]:
        with self._lock:
            q = self._quotes.get(instrument)
            return (() if q is None else (q.price,)) + self._extra_prices.get(instrument, ())
