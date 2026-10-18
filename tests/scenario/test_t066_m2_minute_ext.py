"""CP-040 T-066 (A-115 Q-1·A-123 ④) — 분봉 확장 열(D-260, V-329)·집계 봉 스냅샷(D-259, V-328)·이력 확보(D-261, V-330)·
판정 주기 허용 범위(D-258, V-337).

네트워크 없음: 합성 klines 응답(BN S6 12값 — docs/broker-specs/binance-spot.md:181)·가짜 시계·임시 SQLite. 실제 백필 호출은
별도 승인 뒤(T-056류 관찰) — 여기서는 주입한 fetch·합성 거래소만.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t047_m2_adapter import ACCOUNT, KEY, SECRET, SYM, _ms
from tests.scenario.test_t051_m2_minute import Halts, MinuteRig
from tests.scenario.test_t052_m2_periodic import CAPS, PeriodicProbe, _si, periodic_spec
from tests.support.binance_exchange_sim import ExchangeSim
from tests.support.binance_ws_fake import FakeWsApi
from tests.support.m2_engine_world import SYM as WSYM
from tests.support.m2_engine_world import M2World
from trading_system.app.m2_backfill import BackfillState, MinuteBackfill
from trading_system.app.m2_minutes import (
    MINUTE_INPUT_AUDIT, MINUTE_SOURCE, MINUTE_STORED_AUDIT, M2MinuteInput, MinuteRequest, minute_request_of,
)
from trading_system.app.wiring import StrategyInput, period_grid_of
from trading_system.broker.binance.client import BinanceQueryError, QueryKind
from trading_system.broker.binance.feed import MinutePlan, minute_budget_problem, minute_page_fetcher
from trading_system.broker.binance.market import parse_klines
from trading_system.broker.binance.run_start import M2RunConnector
from trading_system.broker.binance.weights import parse_rate_limits
from trading_system.config.errors import ConfigError
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT, PeriodGrid
from trading_system.domain.strategy import ParamSpec
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.storage.repository import SCHEMA_VERSION, Repository

D = Decimal
M = timedelta(minutes=1)
MINUTE_BAR_COLUMNS = ["instrument", "bar_minutes", "bar_start", "adjusted", "mode", "open", "high", "low", "close",
                      "volume", "source", "recorded_at"]


def utc(hh: int, mm: int, ss: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=UTC)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def row(t: datetime, *, close: str = "100", qv: str = "150.75", trades: int = 7, tbb: str = "0.6",
        tbq: str = "60.3") -> list:
    """BN S6 응답 한 행: [openTime, open, high, low, close, volume, closeTime, quoteVolume, tradeCount, takerBuyBase,
    takerBuyQuote, ignore] (binance-spot.md:181)."""
    o = ms(t)
    return [o, "100", "101", "99", close, "1.5", o + 59_999, qv, trades, tbb, tbq, "0"]


def ext_bar(t: datetime, *, tbb: str = "0.6", adjusted: str = NO_ADJUSTMENT, sym: str = SYM) -> MinuteBar:
    return MinuteBar(sym, 1, t, D(100), D(101), D(99), D(100), D("1.5"), adjusted,
                     quote_volume=D("150.75"), trades=7, taker_buy_base=D(tbb), taker_buy_quote=D("60.3"))


@pytest.fixture
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    r = MinuteRig(tmp_path)
    yield r
    r.close()


# --- V-329 확장 열 해석·저장 -----------------------------------------------------------------------------------

def test_v329_parse_klines_reads_values_7_to_10() -> None:
    [k] = parse_klines([row(utc(0, 0))], symbol=SYM, interval="1m")
    assert (k.quote_volume, k.trades, k.taker_buy_base, k.taker_buy_quote) == (D("150.75"), 7, D("0.6"), D("60.3"))
    short = row(utc(0, 1))[:9]                                     # 9값(taker 없음) — 기존 허용 형식
    [k9] = parse_klines([short], symbol=SYM, interval="1m")
    assert k9.taker_buy_base is None and k9.taker_buy_quote is None


def test_v329_feed_stores_ext_columns_in_minute_bar_ext(rig: MinuteRig) -> None:
    rig.at(utc(0, 30, 2))
    assert rig.input.flush(rig.repo, rig.alerts, utc(0, 30, 2)) == (12, 0, 0)
    stored = rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT, with_ext=True)
    assert len(stored) == 12
    assert all((b.quote_volume, b.trades, b.taker_buy_base, b.taker_buy_quote) == (D("90000"), 7, D("0.7"), D("42000"))
               for b in stored)                                    # 합성 거래소 7~10번째 값
    plain = rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT)
    assert all(b.quote_volume is None for b in plain)             # 기존 읽기는 OHLCV만(그대로)
    src = rig.repo.connection.execute("SELECT DISTINCT source FROM minute_bar_ext").fetchall()
    assert [r[0] for r in src] == [MINUTE_SOURCE]


def test_v329_rereceived_ext_with_other_values_keeps_existing_and_counts(rig: MinuteRig) -> None:
    rig.book.put_minute_bars(SYM, [ext_bar(utc(0, m)) for m in range(10)], utc(0, 10, 3))
    rig.input.flush(rig.repo, rig.alerts, utc(0, 10, 3))
    rig.book.put_minute_bars(SYM, [ext_bar(utc(0, m), tbb="0.9") for m in range(10)], utc(0, 10, 4))
    assert rig.input.flush(rig.repo, rig.alerts, utc(0, 10, 4)) == (0, 10, 0)   # OHLCV 같음
    assert rig.input.last_ext == (0, 0, 10)                        # 확장 열 다름 10건 — 기존 유지
    assert {b.taker_buy_base for b in rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT, with_ext=True)} == {D("0.6")}
    [a] = rig.audits(MINUTE_STORED_AUDIT)
    assert '"ext_differing": 10' in a["summary"]


def test_v329_kiwoom_bar_has_no_ext_and_schema_unchanged(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "m1.db", Mode.PAPER)
    try:
        kiwoom = MinuteBar("005930", 1, utc(0, 0), D(70000), D(70100), D(69900), D(70000), D(10))   # adjusted "1"
        assert kiwoom.quote_volume is None and kiwoom.taker_buy_base is None and not kiwoom.has_ext
        with repo.tx():
            assert repo.insert_minute_bars([kiwoom], "kiwoom", utc(0, 2)) == (1, 0, 0)
            assert repo.insert_minute_bar_ext([kiwoom], "kiwoom", utc(0, 2)) == (0, 0, 0)
        [b] = repo.minute_bars("005930", 1, "1", with_ext=True)
        assert b == kiwoom
        cols = [r[1] for r in repo.connection.execute("PRAGMA table_info(minute_bar)").fetchall()]
        assert cols == MINUTE_BAR_COLUMNS                          # minute_bar 열 불변
        assert SCHEMA_VERSION == "2"
        meta = repo.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert meta == "2"
        assert repo.connection.execute("SELECT COUNT(*) FROM minute_bar_ext").fetchone()[0] == 0   # M1 DB에서는 빈 표
    finally:
        repo.close()


def test_v329_ext_table_created_on_open_of_existing_db_and_append_only(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    repo = Repository(path, Mode.PAPER)
    repo.connection.execute("DROP TABLE minute_bar_ext")           # T-066 이전 DB 흉내
    repo.close()
    repo = Repository(path, Mode.PAPER)                            # 스키마 번호 그대로 열림 + 표 생성
    try:
        with repo.tx():
            assert repo.insert_minute_bar_ext([ext_bar(utc(0, 0))], MINUTE_SOURCE, utc(0, 2)) == (1, 0, 0)
        with pytest.raises(sqlite3.DatabaseError):
            repo.connection.execute("UPDATE minute_bar_ext SET trades = 1")
        with pytest.raises(sqlite3.DatabaseError):
            repo.connection.execute("DELETE FROM minute_bar_ext")
    finally:
        repo.close()


def test_v329_flow_strategy_1m_window_ext_missing_invalid(rig: MinuteRig) -> None:
    bars = [ext_bar(utc(0, m)) for m in range(20, 30)]
    bars[4] = MinuteBar(SYM, 1, utc(0, 24), D(100), D(101), D(99), D(100), D("1.5"), NO_ADJUSTMENT)   # 확장 열 없음
    rig.book.put_minute_bars(SYM, bars, utc(0, 30, 3))
    plain = rig.input.inputs(rig.repo, rig.alerts, [SYM], utc(0, 30, 2), utc(0, 30, 3), "ev1")[SYM]
    assert plain.ok and plain.bars[4].taker_buy_base is None and plain.bars[0].taker_buy_base == D("0.6")
    flow = rig.input.inputs(rig.repo, rig.alerts, [SYM], utc(0, 30, 2), utc(0, 30, 3), "ev2",
                            request=MinuteRequest(flow=True))[SYM]
    assert not flow.ok and flow.bars == () and flow.record["status"] == "EXT_MISSING"


# --- V-328 스냅샷: 집계 봉·결측 목록·지문 ---------------------------------------------------------------------

def test_v328_snapshot_agg_bars_missing_list_and_fingerprint(rig: MinuteRig) -> None:
    rig.input = M2MinuteInput(rig.book, lookback_bars=10, period_minutes=5)
    bars = [ext_bar(utc(0, 0) + M * i) for i in range(60) if utc(0, 0) + M * i != utc(0, 7)]
    rig.book.put_minute_bars(SYM, bars, utc(1, 0, 3))
    req = MinuteRequest(agg_minutes=15, agg_lookback=4, max_missing=1)
    res = rig.input.inputs(rig.repo, rig.alerts, [SYM], utc(1, 0, 2), utc(1, 0, 3), "ev", request=req)[SYM]
    assert res.ok and [b.at for b in res.agg_bars] == [utc(0, 15), utc(0, 30), utc(0, 45)]
    assert res.agg_missing == (utc(0, 0),)
    assert res.agg_bars[0].volume == D("22.5") and res.agg_bars[0].taker_buy_base == D("9.0")
    agg = res.record["agg"]
    assert agg["status"] == "OK" and agg["minutes"] == 15 and agg["missing"] == [utc(0, 0).isoformat()]
    assert len(agg["fingerprint"]) == 64 and agg["rule"]
    again = rig.input.inputs(rig.repo, rig.alerts, [SYM], utc(1, 0, 2), utc(1, 0, 4), "ev2", request=req)[SYM]
    assert again.record["agg"]["fingerprint"] == agg["fingerprint"]      # 같은 입력 같은 지문
    strict = rig.input.inputs(rig.repo, rig.alerts, [SYM], utc(1, 0, 2), utc(1, 0, 4), "ev3",
                              request=replace(req, max_missing=0))[SYM]
    assert not strict.ok and strict.agg_bars == () and strict.record["agg"]["status"] == "TOO_MANY_MISSING"
    assert strict.bars == ()                                       # 입력 무효면 1분 창도 넘기지 않음
    assert rig.audits(MINUTE_INPUT_AUDIT)


def _agg_probe() -> PeriodicProbe:
    p = PeriodicProbe(minute=True)
    p.spec = replace(p.spec, params=p.spec.params + (
        ParamSpec("agg_bar_minutes", "int", "분", "집계 봉 길이 (D-259)", D("15"), D("60")),
        ParamSpec("agg_lookback_bars", "int", "봉", "집계 봉 창", D("1"), D("2000")),
        ParamSpec("max_missing_bars", "int", "봉", "창 안 결측 허용 (D-259 (5))", D("0"), D("100"))))
    return p


def _bars(first: datetime, n: int) -> list[MinuteBar]:
    return [ext_bar(first + M * i, sym=WSYM) for i in range(n)]   # 엔진 세계 심볼


def test_v328_engine_snapshot_carries_agg_bars_and_blocks_entry_when_last_missing(tmp_path: Path) -> None:
    book = SpotMinuteBook((WSYM,))
    p = _agg_probe()
    params = {"qty": "0.0100", "period_minutes": 15, "agg_bar_minutes": 15, "agg_lookback_bars": 2,
              "max_missing_bars": 0}
    w = M2World(tmp_path, strategies=[StrategyInput(p, params, ())], caps=CAPS,
                minutes=M2MinuteInput(book, lookback_bars=3, period_minutes=15))
    w.set_baseline()
    try:
        w.start_and_resume()
        book.put_minute_bars(WSYM, _bars(utc(0, 0), 31), utc(0, 30, 4))     # 00:00~00:30 (00:30 진행 중)
        w.tick_at(0, 30, 5)
        [s] = p.snaps
        v = s.view(WSYM)
        assert v is not None and v.minute_ok and [b.at for b in v.agg_bars] == [utc(0, 0), utc(0, 15)]
        assert v.agg_missing == () and len(w.sent()) == 1
        for sec in (10, 20, 30):
            w.tick_at(0, 31, sec)
        # 00:45:02 판정: [00:30, 00:45) 봉의 00:40 분 결측 → 직전 집계 봉 결측 → 분봉 입력 무효(진입·증액만 막음).
        # 1분 창(3봉 00:42~00:44)은 정상 — 무효 원인은 집계 봉만
        book.put_minute_bars(WSYM, [b for b in _bars(utc(0, 30), 16) if b.start != utc(0, 40)], utc(0, 45, 4))
        w.tick_at(0, 45, 5)
        v2 = p.snaps[-1].view(WSYM)
        assert v2 is not None and not v2.minute_ok and v2.agg_bars == () and v2.position is not None
        assert [r.side for r in w.sent()] == ["BUY", "SELL"]       # 청산은 그대로 (DP-78, §8-3)
        mi = w.audits(MINUTE_INPUT_AUDIT)[-1]
        assert "AGG_LAST_MISSING" in mi["summary"]
    finally:
        w.close()


def test_minute_request_of_params_and_registration_rules() -> None:
    assert minute_request_of({"qty": D("1")}) == MinuteRequest()
    req = minute_request_of({"agg_bar_minutes": 60, "agg_lookback_bars": 1440, "max_missing_bars": 3,
                             "flow_columns": True})
    assert req == MinuteRequest(agg_minutes=60, agg_lookback=1440, max_missing=3, flow=True)
    for bad in ({"agg_bar_minutes": 30, "agg_lookback_bars": 4},
                {"agg_bar_minutes": 60},
                {"agg_bar_minutes": 60, "agg_lookback_bars": 2, "max_missing_bars": 2},
                {"agg_lookback_bars": 2}):
        with pytest.raises(ValueError):
            minute_request_of(bad)


# --- V-330 이력 확보 -------------------------------------------------------------------------------------------

class PageFetch:
    """합성 나눔 조회: [start, end] 1분 봉 행(BN S6 12값). fail_on = 몇 번째 호출에서 실패할지(1부터)."""

    def __init__(self, *, fail_on: int | None = None, bad_on: int | None = None, outside_on: int | None = None) -> None:
        self.calls: list[tuple[str, int, int, int]] = []
        self.fail_on, self.bad_on, self.outside_on = fail_on, bad_on, outside_on

    def __call__(self, symbol: str, start_ms: int, end_ms: int, limit: int) -> object:
        self.calls.append((symbol, start_ms, end_ms, limit))
        n = len(self.calls)
        if n == self.fail_on:
            raise BinanceQueryError("합성 조회 실패", QueryKind.FAILED)
        rows = [row(datetime.fromtimestamp(t / 1000, UTC)) for t in range(start_ms, end_ms + 1, 60_000)][:limit]
        if n == self.bad_on:
            rows[0][2] = "1"                                       # 고가 < 시가 — 품질 실패
        if n == self.outside_on:
            rows.append(row(datetime.fromtimestamp((end_ms + 60_001) / 1000, UTC)))
        return rows


NOW = utc(10, 0, 2)


def _backfill(fetch: PageFetch, window: int = 2500, per_minute: int = 2) -> MinuteBackfill:
    return MinuteBackfill(fetch, [SYM], window_minutes=window, per_minute=per_minute)


def test_v330_backfill_pages_only_missing_then_signal_valid(rig: MinuteRig) -> None:
    last = utc(9, 59)
    with rig.repo.tx():                                            # 최근 500분은 이미 저장
        rig.repo.insert_minute_bars([ext_bar(last - M * i) for i in range(500)], MINUTE_SOURCE, NOW)
    fetch = PageFetch()
    bf = _backfill(fetch)
    assert bf.plan(rig.repo, NOW) == {SYM: 2}                      # 부족분 2000분 → 2쪽
    rig.input.history = bf
    pending = rig.input.inputs(rig.repo, rig.alerts, [SYM], NOW, NOW, "ev0")[SYM]
    assert not pending.ok and pending.record["status"] == "HISTORY_PENDING"   # 채우기 전 신호 무효
    assert bf.step(NOW, rig.alerts) == 2 and bf.step(NOW, rig.alerts) == 0    # 분당 상한
    assert bf.status(SYM)[0] is BackfillState.DONE
    first = last - M * 2499
    assert [c[1] for c in fetch.calls] == [ms(first), ms(first + M * 1000)]
    assert fetch.calls[-1][2] == ms(last - M * 500) + 59_999      # 저장된 구간은 조회하지 않음
    assert bf.flush(rig.repo, NOW) == 2000
    got = rig.repo.minute_bars(SYM, 1, NO_ADJUSTMENT, start=first, end=last + M, with_ext=True)
    assert len(got) == 2500 and got[0].taker_buy_base == D("0.6")
    srcs = {r[0] for r in rig.repo.connection.execute("SELECT DISTINCT source FROM minute_bar")}
    assert srcs == {MINUTE_SOURCE}
    ok = rig.input.inputs(rig.repo, rig.alerts, [SYM], NOW, NOW, "ev1")[SYM]
    assert ok.ok


def test_v330_rate_cap_spreads_pages_over_minutes(rig: MinuteRig) -> None:
    fetch = PageFetch()
    bf = _backfill(fetch, window=2500, per_minute=2)
    assert bf.plan(rig.repo, NOW) == {SYM: 3}
    assert bf.step(NOW, rig.alerts) == 2 and bf.status(SYM)[0] is BackfillState.PENDING
    assert bf.step(NOW + timedelta(seconds=30), rig.alerts) == 0
    assert bf.step(NOW + M, rig.alerts) == 1 and bf.status(SYM)[0] is BackfillState.DONE


@pytest.mark.parametrize("kind", ["fail", "bad", "outside"])
def test_v330_backfill_failure_invalidates_signal_alerts_and_no_other_source(rig: MinuteRig, kind: str) -> None:
    fetch = PageFetch(**{f"{kind}_on": 2})
    bf = _backfill(fetch, window=2500, per_minute=5)
    bf.plan(rig.repo, NOW)
    bf.step(NOW, rig.alerts)
    state, reason = bf.status(SYM)
    assert state is BackfillState.FAILED and reason
    assert len(fetch.calls) == 2                                   # 실패 뒤 남은 쪽 조회·재시도 없음
    infos = [m for lv, m, _ in rig.alerts.items if lv == "INFO" and "D-261" in m]
    assert len(infos) == 1
    assert bf.flush(rig.repo, NOW) == 1000                         # 실패 전 정상 쪽만 (같은 출처)
    rig.input.history = bf
    res = rig.input.inputs(rig.repo, rig.alerts, [SYM], NOW, NOW, "ev")[SYM]
    assert not res.ok and res.record["status"] == "HISTORY_FAILED"
    srcs = {r[0] for r in rig.repo.connection.execute("SELECT DISTINCT source FROM minute_bar")}
    assert srcs == {MINUTE_SOURCE}                                 # 다른 출처로 메움 0
    assert bf.step(NOW + M, rig.alerts) == 0


def test_v330_page_fetcher_uses_start_end_through_run_guard(rig: MinuteRig) -> None:
    rig.clock._now = NOW  # noqa: SLF001
    bf = MinuteBackfill(minute_page_fetcher(rig.client), [SYM], window_minutes=30, per_minute=20)
    assert bf.plan(rig.repo, NOW) == {SYM: 1}
    assert bf.step(NOW, rig.alerts) == 1 and bf.status(SYM)[0] is BackfillState.DONE
    [call] = rig.minute_calls()
    assert call["startTime"] == str(ms(utc(9, 30))) and call["endTime"] == str(ms(utc(9, 59)) + 59_999)
    assert call["limit"] == "30" and rig.guard.order_sent == 0
    assert bf.flush(rig.repo, NOW) == 30


def test_v330_budget_with_backfill_refuses_start() -> None:
    limits = parse_rate_limits([{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                 "limit": 1000}])                  # 내부 예산 500, 분봉 몫 10% = 50
    plan = MinutePlan(60, 60)
    assert minute_budget_problem(limits, 1, plan) is None
    assert minute_budget_problem(limits, 1, plan, backfill_per_minute=20) is None    # 2 + 20×2 = 42 ≤ 50
    problem = minute_budget_problem(limits, 1, plan, backfill_per_minute=25)        # 2 + 50 = 52 > 50
    assert problem is not None and "백필" in problem and "D-261" in problem


def test_v330_connector_refuses_start_when_backfill_budget_exceeded() -> None:
    clock = FixedClock(NOW)
    sim = ExchangeSim(lambda: _ms(clock))
    from tests.contract.test_binance_rules import _symbol
    sim.exchange_body = {"timezone": "UTC", "serverTime": _ms(clock), "exchangeFilters": [],
                         "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                         "limit": 400}], "symbols": [_symbol(SYM)]}
    conn = M2RunConnector(sim.factory(), FakeWsApi(sim).connect)
    kw = dict(api_key=KEY, secret=SECRET, symbols=(SYM,), account=ACCOUNT, now=clock.now, sleep=lambda s: None,
              orders_enabled=False, sink=SpotFactsStore(now=clock.now, universe=(SYM,)),
              on_alert=lambda k, m: None, on_stream_state=lambda s, m: None, halts=Halts(),
              minute_plan=(60, 60, 2), minute_sink=SpotMinuteBook((SYM,)))
    out = conn(**kw, backfill_per_minute=20)                       # 내부 200의 10% = 20 < 2 + 40
    assert isinstance(out, str) and "백필" in out
    assert sim.order_posts() == [] and ("GET", "/api/v3/klines") not in sim.paths()


# --- V-337 판정 주기 허용 범위 ---------------------------------------------------------------------------------

def test_v337_m2_registration_accepts_only_15_and_60() -> None:
    spec = periodic_spec()
    for n in (15, 60):
        assert period_grid_of(_si(spec, {"qty": D("0.01"), "period_minutes": n}), "M2") == PeriodGrid(n)
    for n in (5, 30, 1440):
        with pytest.raises(ConfigError) as exc:
            period_grid_of(_si(spec, {"qty": D("0.01"), "period_minutes": n}), "M2")
        assert any("D-258" in x and "15" in x and "60" in x for x in exc.value.problems), exc.value.problems


def test_v337_engine_build_refuses_period_5(tmp_path: Path) -> None:
    p = PeriodicProbe()
    with pytest.raises(ConfigError) as exc:
        M2World(tmp_path, strategies=[StrategyInput(p, {"qty": "0.0100", "period_minutes": 5}, ())], caps=CAPS)
    assert any("D-258" in x for x in exc.value.problems)


def test_v337_agg_strategy_registration_checks(tmp_path: Path) -> None:
    p = _agg_probe()
    bad = {"qty": "0.0100", "period_minutes": 15, "agg_bar_minutes": 15, "agg_lookback_bars": 2,
           "max_missing_bars": 2}
    with pytest.raises(ConfigError) as exc:
        M2World(tmp_path, strategies=[StrategyInput(p, bad, ())], caps=CAPS,
                minutes=M2MinuteInput(SpotMinuteBook((SYM,)), lookback_bars=15, period_minutes=15))
    assert any("max_missing_bars" in x for x in exc.value.problems)

