"""시장 데이터 저장소(메모리 스냅샷)와 판정 스냅샷 구성 (03 §5.1·D-073·D-088, 00 §2.2, 04 V-90).

- 게이트 입력(MarketFacts)을 메모리 값으로만 제공한다(네트워크 호출 금지, 03 §5.1 v0.5). 값의 갱신은
  공급자(fake, 키움 T-021)가 `update_*`로 한다.
- 시세 시각 = 시스템이 가격을 받은 시각 (03 D-095).
- 판정 스냅샷의 봉은 판정 기준 시각에 **확정된 것만** 넣는다: 일봉은 그날 확정 시각(애프터마켓 종료 20:00) 이후,
  분봉은 봉 끝 시각 이후 (룩어헤드 방지, D-073).
- 거래 상태 갱신(VI·거래정지 등, 키움 1h·0g)과 품질 실패는 정지(K10)와 알림으로 이어진다(연결된 경우).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_system.domain.clock import KST
from trading_system.domain.enums import HaltType
from trading_system.domain.history import RECENT_ENTRY_DAYS, history_view
from trading_system.domain.market import InstrumentInfo, Quote, SessionInfo, TradingStatus
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView

from .calendar import KrxCalendar
from .quality import Prepared, RawDaily, preprocess_daily
from .ticks import TickTable


DAILY_BAR_FINAL = time(20, 0)   # DP-5(A-031): 애프터마켓(NXT ~20:00) 종료 뒤. ka10081 일봉 범위(KRX/통합) 미확인


def daily_bar_final_at(day: date, calendar: KrxCalendar | None) -> datetime:
    """그날 일봉을 확정으로 보는 시각: 정규장 마감과 DAILY_BAR_FINAL 중 늦은 쪽."""
    close = None
    if calendar is not None:
        s = calendar.session(day)
        if s is not None and s.is_open:
            close = s.regular_close
    final = datetime.combine(day, DAILY_BAR_FINAL, tzinfo=KST)
    return final if close is None else max(close, final)


def daily_bar_available(day: date, as_of: datetime, calendar: KrxCalendar | None) -> bool:
    """일봉은 그날 확정 시각(daily_bar_final_at) 이후에만 확정된 것으로 본다."""
    return daily_bar_final_at(day, calendar) <= as_of


def latest_complete_day(as_of: datetime, calendar: KrxCalendar | None) -> date:
    """as_of 시점에 일봉이 확정됐어야 하는 가장 최근 거래일. 달력에 없는 연도는 평일을 거래일로 본다(보수적:
    휴장일을 거래일로 보면 '일봉 없음'으로 판정이 막힐 뿐 오래된 데이터를 통과시키지 않는다)."""
    d = as_of.astimezone(KST).date()
    for _ in range(31):
        known = calendar.is_trading_day(d) if calendar is not None else None
        trading = known if known is not None else d.weekday() < 5
        if trading and daily_bar_available(d, as_of, calendar):
            return d
        d -= timedelta(days=1)
    return d


@dataclass
class _Instrument:
    info: InstrumentInfo
    quote: Quote | None = None
    statuses: frozenset[TradingStatus] | None = None
    raw: tuple[RawDaily, ...] | None = None
    data_version: str = ""


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

    def update_daily(self, instrument: str, raw: Sequence[RawDaily], data_version: str = "") -> Prepared:
        """원시 일봉을 넣는다. 지금까지의 데이터로 품질을 판정해, 실패면 그 종목을 K10으로 막고 정상화되면 해제를 보고한다.

        판정 스냅샷은 저장된 원시 봉 중 판정 기준 시각에 확정된 것만으로 **다시** 전처리·품질 판정한다.
        미래 봉의 품질이 과거 판정에 영향을 주면 룩어헤드이기 때문이다 (V-91, D-073).
        """
        inst = self._get(instrument)
        inst.raw, inst.data_version = tuple(raw), data_version
        prepared = preprocess_daily(raw, self._trading_days, data_version=data_version)
        if self.halts is None:
            return prepared
        if not prepared.report.ok:
            self.halts.activate(HaltType.K10, "marketdata", f"데이터 품질 실패 {instrument}: "
                                f"{'; '.join(prepared.report.issues[:3])}", self.account, instrument=instrument)
        else:
            self.halts.report_condition(HaltType.K10, self.account, "data_ok", True, prepared.data_version,
                                        instrument=instrument)
        return prepared

    def _trading_days(self, a: date, b: date) -> list[date]:
        out, d = [], a
        while d <= b:
            if self.calendar.is_trading_day(d):
                out.append(d)
            d = d + timedelta(days=1)
        return out

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
                 pending: Callable[[str], bool], next_evaluation_at: datetime | None = None,
                 history: Callable[[str], dict] | None = None,
                 recent_days: int = RECENT_ENTRY_DAYS) -> EvaluationSnapshot:
        """history: 종목 → 저장소 `strategy_history` 원자료. 주면 보유 거래일 수·직전 청산·최근 진입을 채운다."""
        views = []
        cal = self.calendar.is_trading_day if self.calendar is not None else None
        for i in instruments:
            inst = self._data.get(i)
            pos = positions(i)
            extra: dict = {}
            if history is not None:
                h = history_view(history(i), as_of, cal, recent_days)
                if pos is not None and h.holding_days is not None:
                    pos = replace(pos, holding_days=h.holding_days)
                extra = {"last_exit_at": h.last_exit_at, "last_exit_reason": h.last_exit_reason,
                         "entries_recent": h.entries_recent}
            if inst is None or inst.raw is None:
                views.append(InstrumentView(instrument=i, data_ok=False, position=pos,
                                            has_pending_order=pending(i), **extra))
                continue
            known = [r for r in inst.raw if daily_bar_available(r.day, as_of, self.calendar)]
            prepared = preprocess_daily(known, self._trading_days, data_version=inst.data_version)
            bars = prepared.bars
            q = inst.quote
            views.append(InstrumentView(
                instrument=i, daily_bars=bars,
                last_price=q.price if q is not None and q.at <= as_of else None,
                statuses=inst.statuses or frozenset(), position=pos, has_pending_order=pending(i), **extra,
                # 공급이 끊겨 마지막 일봉이 오래됐으면 품질 실패 (지난 확정 거래일 일봉이 있어야 함)
                data_ok=prepared.report.ok and bool(bars) and bars[-1].at >= latest_complete_day(as_of, self.calendar)))
        return EvaluationSnapshot(evaluation_id, market, account, as_of, base_amount, tuple(views),
                                  next_evaluation_at)
