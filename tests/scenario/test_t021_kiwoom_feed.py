"""T-021 키움 시장 데이터·거래 상태 공급자 (네트워크 없이, 합성 응답) — 05-plan T-021, 01 §2.8, 03 D-095.

모르면 "없음"(위험 확대 거부), 품질 실패 → K10, 시세 시각 = 받은 시각.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.scenario.test_t014_kiwoom import ALIAS, adapter, realtime, wait_until
from tests.support.kiwoom_sim import Sim, _md_init, push_real
from trading_system.broker.kiwoom.realtime import WsState
from trading_system.broker.ratelimit import Priority
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.market import InstrumentInfo, InstrumentKind, TradingStatus
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.kiwoom_feed import KiwoomMarketFeed, MarketPoller, master_statuses
from trading_system.marketdata.store import MarketDataStore

A, B = "005930", "000660"
CAL = KrxCalendar({2026: parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t",
                                         "holidays": [date(2026, 10, 9)]}, 2026)})


class Alerts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.items.append((level, message))


def kst(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST).astimezone(UTC)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(kst(2026, 10, 2, 10, 0))   # 금요일 장중


@pytest.fixture
def sim(clock: FixedClock) -> Iterator[Sim]:
    s = Sim(now=lambda: clock.now().astimezone(KST).replace(tzinfo=None))
    _md_init(s)
    yield s
    s.close()


def feed(sim: Sim, clock: FixedClock, alerts: Alerts | None = None, held: set[str] | None = None):  # type: ignore[no-untyped-def]
    a = adapter(sim, clock)
    store = MarketDataStore(CAL, None, alerts=alerts)
    infos = {c: InstrumentInfo(c, InstrumentKind.STOCK, Decimal("1"), Decimal("1")) for c in (A, B)}
    f = KiwoomMarketFeed(a, store, clock, infos, held=lambda c: c in (held or set()), alerts=alerts)
    return a, store, f


def day_rows(last: date, n: int) -> list[dict]:
    rows, d = [], last
    while len(rows) < n:
        if CAL.is_trading_day(d) or (CAL.is_trading_day(d) is None and d.weekday() < 5):
            p = 70000 + len(rows)
            rows.append({"dt": d.strftime("%Y%m%d"), "open_pric": str(p), "high_pric": str(p + 500),
                         "low_pric": str(p - 500), "cur_prc": f"+{p}", "trde_qty": "1000", "pred_pre": "+0"})
        d -= timedelta(days=1)
    return rows   # 최신 먼저 (명세 예시와 같은 순서)


# --- 일봉 -------------------------------------------------------------------------------------------

def test_daily_excludes_partial_today_until_close(sim: Sim, clock: FixedClock) -> None:
    _, store, f = feed(sim, clock)
    sim.daily[A] = day_rows(date(2026, 10, 2), 60)
    p = f.refresh_daily(A)
    assert p is not None and p.report.ok and max(b.at for b in p.bars) == date(2026, 10, 1)   # 장중 당일 봉 제외
    clock._now = kst(2026, 10, 2, 15, 40)  # noqa: SLF001
    p2 = f.refresh_daily(A)
    assert p2 is not None and max(b.at for b in p2.bars) == date(2026, 10, 2)
    snap = store.snapshot("ev", "M1", ALIAS, kst(2026, 10, 2, 16, 0), Decimal("1"), [A], lambda i: None,
                          lambda i: False)
    assert snap.view(A).data_ok


def test_daily_paging_stops_at_lookback_and_bad_row_rejected(sim: Sim, clock: FixedClock) -> None:
    alerts = Alerts()
    _, store, f = feed(sim, clock, alerts)
    f.lookback_days = 30
    sim.daily[A] = day_rows(date(2026, 10, 1), 300)
    sim.page_size = 10
    f.refresh_daily(A)
    assert sum(1 for api, _, _ in sim.log if api == "ka10081") <= 4      # 30일치에서 멈춤 (300행 전부 받지 않음)
    sim.daily[B] = day_rows(date(2026, 10, 1), 5)
    sim.daily[B][2]["cur_prc"] = "7,0000"
    for _ in range(3):
        assert f.refresh_daily(B) is None
    assert store.snapshot("e", "M1", ALIAS, clock.now(), Decimal("1"), [B], lambda i: None,
                          lambda i: False).view(B).data_ok is False
    assert [m for lv, m in alerts.items if "ka10081" in m]                 # 3회 연속 실패 알림 1회


# --- 시세·상하한가 ------------------------------------------------------------------------------------

def test_quote_received_time_limits_and_plausibility(sim: Sim, clock: FixedClock) -> None:
    a, store, f = feed(sim, clock)
    sim.info[A] = {"cur_prc": "+71000", "upl_pric": "+92300", "lst_pric": "-49700", "base_pric": "71000"}
    q = f.refresh_quote(A)
    assert q is not None and (q.price, q.upper_limit, q.lower_limit, q.at) == (
        Decimal("71000"), Decimal("92300"), Decimal("49700"), clock.now())
    assert ("ka10001", int(Priority.MARKET)) in a.limiter.grants                 # 시세 조회는 최하 순위
    sim.info[A] = {"cur_prc": "0.00", "upl_pric": "20241016", "lst_pric": "-47.41"}  # 어긋난 응답(명세 예시 같은)
    clock.advance(timedelta(seconds=30))
    assert f.refresh_quote(A) is None and store.quote(A).at == clock.now() - timedelta(seconds=30)
    sim.info[A] = {"cur_prc": "+100000", "upl_pric": "+92300", "lst_pric": "-49700"}   # 형식은 맞지만 범위 밖
    assert f.refresh_quote(A) is None and store.quote(A).price == Decimal("71000")
    f._master[A] = frozenset()  # noqa: SLF001 — 상태 합성 시험용
    sim.info[A] = {"cur_prc": "+92300", "upl_pric": "+92300", "lst_pric": "-49700"}
    f.refresh_quote(A)
    assert store.trading_status(A) == frozenset({TradingStatus.UPPER_LIMIT})


# --- 종목 상태 -----------------------------------------------------------------------------------------

def test_master_statuses_and_unknown(sim: Sim, clock: FixedClock) -> None:
    alerts = Alerts()
    _, store, f = feed(sim, clock, alerts, held={A})
    sim.master["0"] = [{"code": A, "state": "관리종목", "orderWarning": "0"}]
    assert f.refresh_master()
    assert store.trading_status(A) == frozenset({TradingStatus.CAUTION})
    assert store.trading_status(B) is None                                     # 목록에 없음 → 모름
    assert any(B in m for lv, m in alerts.items)
    assert master_statuses({"orderWarning": "2"}) == {TradingStatus.DELISTING}
    assert master_statuses({"orderWarning": "4", "state": "거래정지"}) == {TradingStatus.CAUTION, TradingStatus.HALTED}
    assert master_statuses({"orderWarning": "0", "state": "증거금100%"}) == frozenset()
    sim.master["0"] = [{"code": A, "state": "거래정지", "orderWarning": "0"}]
    f.refresh_master()
    assert any(lv == "EMERGENCY" and A in m for lv, m in alerts.items)       # 보유 종목 거래정지 → 긴급


# --- VI ------------------------------------------------------------------------------------------------

def test_vi_from_query_and_realtime_with_expiry(sim: Sim, clock: FixedClock) -> None:
    _, store, f = feed(sim, clock)
    f._master.update({A: frozenset(), B: frozenset()})  # noqa: SLF001
    sim.vi_rows = [{"stk_cd": A, "virelis_time": "100130", "trde_cntr_proc_time": "095930"},
                   {"stk_cd": B, "virelis_time": "093000", "trde_cntr_proc_time": "092800"}]   # B는 이미 해제
    assert f.refresh_vi()
    assert store.trading_status(A) == {TradingStatus.VI} and store.trading_status(B) == frozenset()
    clock.advance(timedelta(minutes=2))
    f.tick()
    assert store.trading_status(A) == frozenset()
    h = f.handlers()
    h["1h"](B, {"9001": B, "1223": "100200", "1224": ""})                     # 해제 시각 없음 → 발동 + 2분
    assert store.trading_status(B) == {TradingStatus.VI}
    clock.advance(timedelta(minutes=2, seconds=1))
    f.tick()
    assert store.trading_status(B) == frozenset()


def test_realtime_0g_limits_and_0s_calendar_conflict(sim: Sim, clock: FixedClock) -> None:
    alerts = Alerts()
    _, store, f = feed(sim, clock, alerts)
    sim.info[A] = {"cur_prc": "71000", "upl_pric": "92300", "lst_pric": "49700"}
    q = f.refresh_quote(A)
    h = f.handlers()
    clock.advance(timedelta(seconds=20))      # 한도 갱신은 시세 시각을 새로 만들지 않는다 (G11 신선도 보존)
    h["0g"](A, {"305": "+78300", "306": "-42300", "307": "60300"})
    assert (store.quote(A).upper_limit, store.quote(A).lower_limit, store.quote(A).at) == (
        Decimal("78300"), Decimal("42300"), q.at)
    clock._now = kst(2026, 10, 9, 9, 0)  # noqa: SLF001                                          # 달력상 휴장일
    h["0s"]("", {"215": "3", "20": "090000"})
    assert any(lv == "EMERGENCY" for lv, _ in alerts.items) and CAL.session(date(2026, 10, 9)) is None


def test_poller_round_robin_and_realtime_registration(sim: Sim, clock: FixedClock) -> None:
    a, store, f = feed(sim, clock)
    sim.info[A] = {"cur_prc": "71000", "upl_pric": "92300", "lst_pric": "49700"}
    sim.info[B] = {"cur_prc": "120000", "upl_pric": "156000", "lst_pric": "84000"}
    sim.master["0"] = [{"code": A, "state": "", "orderWarning": "0"}, {"code": B, "state": "", "orderWarning": "0"}]
    p = MarketPoller(f, master_every=10)
    p.poll_once()
    p.poll_once()
    assert store.quote(A) is not None and store.quote(B) is not None
    assert sum(1 for api, _, _ in sim.log if api == "ka10099") == 4           # 첫 회차에만 (시장 4개)
    states: list = []
    rt = realtime(a, sim, clock, states)
    rt._regs = f.registrations() + rt._regs  # noqa: SLF001
    rt._handlers = {**rt._handlers, **f.handlers()}  # noqa: SLF001
    rt.start()
    try:
        assert wait_until(lambda: WsState.ONLINE in states)
        regs = [m["data"][0] for m in sim.ws_in if isinstance(m, dict) and m.get("trnm") == "REG"]
        assert {"item": sorted([A, B]), "type": ["1h"]} in regs
        push_real(sim, "1h", A, {"9001": A, "1223": "100000", "1224": "100200"})
        assert wait_until(lambda: TradingStatus.VI in (store.trading_status(A) or frozenset()))
    finally:
        rt.stop()
