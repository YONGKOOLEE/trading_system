"""시장 데이터 저장소(메모리 스냅샷)와 판정 스냅샷 구성 (03 §5.1·D-073·D-088, 00 §2.2, 04 V-90).

- 게이트 입력(MarketFacts)을 메모리 값으로만 제공한다(네트워크 호출 금지, 03 §5.1 v0.5). 값의 갱신은
  공급자(fake, 키움 T-021)가 `update_*`로 한다.
- 시세 시각 = 시스템이 가격을 받은 시각 (03 D-095).
- 판정 스냅샷의 봉은 판정 기준 시각에 **확정된 것만** 넣는다: 일봉은 그날 정규장 마감 시각 이후,
  분봉은 봉 끝 시각 이후 (룩어헤드 방지, D-073).
- 거래 상태 갱신(VI·거래정지 등, 키움 1h·0g)과 품질 실패는 정지(K10)와 알림으로 이어진다(연결된 경우).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

from trading_system.domain.clock import KST
from trading_system.domain.enums import HaltType
from trading_system.domain.market import InstrumentInfo, Quote, SessionInfo, TradingStatus
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView

from .calendar import KrxCalendar
from .quality import Prepared
from .ticks import TickTable


def daily_bar_available(day: date, as_of: datetime, calendar: KrxCalendar | None) -> bool:
    """일봉은 그날 정규장 마감 이후에만 확정된 것으로 본다."""
    close = None
    if calendar is not None:
        s = calendar.session(day)
        if s is not None and s.is_open:
            close = s.regular_close
    if close is None:
        close = datetime(day.year, day.month, day.day, 15, 30, tzinfo=KST)
    return close <= as_of


@dataclass
class _Instrument:
    info: InstrumentInfo
    quote: Quote | None = None
    statuses: frozenset[TradingStatus] | None = None
    daily: Prepared | None = None


@dataclass
class MarketDataStore:
    calendar: KrxCalendar
    ticks: TickTable | None
    halts: HaltControl | None = None
    alerts: AlertSink | None = None
    account: str | None = None
    _data: dict[str, _Instrument] = field(default_factory=dict)

    # --- 갱신 (공급자) ------------------------------------------------------------------
    def register(self, info: InstrumentInfo) -> None:
        cur = self._data.get(info.instrument)
        self._data[info.instrument] = _Instrument(info) if cur is None else replace(cur, info=info)

    def update_quote(self, quote: Quote) -> None:
        self._get(quote.instrument).quote = quote

    def update_status(self, instrument: str, statuses: frozenset[TradingStatus], held: bool = False) -> None:
        inst = self._get(instrument)
        before = inst.statuses or frozenset()
        inst.statuses = statuses
        added = statuses - before
        if added and held and self.alerts is not None:
            self.alerts.alert("EMERGENCY" if added & {TradingStatus.HALTED, TradingStatus.DELISTING,
                                                       TradingStatus.ETN_REDEMPTION} else "WARNING",
                              f"보유 종목 거래 상태 변경: {instrument} {','.join(sorted(added))}", instrument)

    def update_daily(self, instrument: str, prepared: Prepared) -> None:
        """전처리된 일봉을 넣는다. 품질 실패면 그 종목을 K10으로 막고, 정상화되면 K10 조건을 보고한다."""
        self._get(instrument).daily = prepared
        if self.halts is None:
            return
        if not prepared.report.ok:
            self.halts.activate(HaltType.K10, "marketdata", f"데이터 품질 실패 {instrument}: "
                                f"{'; '.join(prepared.report.issues[:3])}", self.account, instrument=instrument)
        else:
            self.halts.report_condition(HaltType.K10, self.account, "data_ok", True, prepared.data_version,
                                        instrument=instrument)

    def _get(self, instrument: str) -> _Instrument:
        if instrument not in self._data:
            raise KeyError(f"등록되지 않은 종목 {instrument}")
        return self._data[instrument]

    # --- 게이트 입력 (MarketFacts) ---------------------------------------------------------
    def quote(self, instrument: str) -> Quote | None:
        inst = self._data.get(instrument)
        return inst.quote if inst else None

    def instrument(self, instrument: str) -> InstrumentInfo | None:
        inst = self._data.get(instrument)
        return inst.info if inst else None

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None:
        inst = self._data.get(instrument)
        return inst.statuses if inst else None

    def session(self, trade_date: date) -> SessionInfo | None:
        return self.calendar.session(trade_date)

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        inst = self._data.get(instrument)
        if inst is None or self.ticks is None:
            return None
        return self.ticks.valid(inst.info.kind, price)

    # --- 판정 스냅샷 (V-90) ----------------------------------------------------------------
    def snapshot(self, evaluation_id: str, market: str, account: str, as_of: datetime, base_amount: Decimal,
                 instruments: list[str], positions: Callable[[str], PositionView | None],
                 pending: Callable[[str], bool], next_evaluation_at: datetime | None = None) -> EvaluationSnapshot:
        views = []
        for i in instruments:
            inst = self._data.get(i)
            if inst is None or inst.daily is None:
                views.append(InstrumentView(instrument=i, data_ok=False, position=positions(i),
                                            has_pending_order=pending(i)))
                continue
            bars = tuple(b for b in inst.daily.bars
                         if isinstance(b.at, date) and daily_bar_available(b.at, as_of, self.calendar))
            q = inst.quote
            views.append(InstrumentView(
                instrument=i, daily_bars=bars,
                last_price=q.price if q is not None and q.at <= as_of else None,
                statuses=inst.statuses or frozenset(), position=positions(i), has_pending_order=pending(i),
                data_ok=inst.daily.report.ok))
        return EvaluationSnapshot(evaluation_id, market, account, as_of, base_amount, tuple(views),
                                  next_evaluation_at)
