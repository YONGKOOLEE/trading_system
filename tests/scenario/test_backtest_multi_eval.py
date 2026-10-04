"""백테스트 보강 (A-045 작업: 다중 판정 시각·상하한가 호가 내림·V-91 상태 교란·체결 시각).

모든 가격·달력은 가상이다 (형식·동작 시험용, 실제 시장 데이터 아님).
가정(제안, 미승인): 하루 첫 판정의 주문은 그날 시가(DP-7), 이후 판정의 주문은 그날 종가로 체결하고,
체결 시각은 체결을 일으킨 판정 시각으로 기록한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t009_backtest import CAL, END, INFOS, SETTINGS, START, trading_days
from tests.support.gate_fakes import scenario_limits
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED
from trading_system.app.backtest import (
    BacktestConfig, BacktestError, perturbation_check, price_limits, render_report, run_backtest,
)
from trading_system.config.strategy_params import validate_params
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.market import InstrumentInfo, InstrumentKind
from trading_system.domain.strategy import EvaluationSnapshot
from trading_system.marketdata.quality import RawDaily
from trading_system.storage.repository import Repository
from trading_system.strategy.base import make_signal
from trading_system.strategy.paper0_verify import SPEC as P0_SPEC
from trading_system.strategy.paper0_verify import Paper0Verify

INST = "005930"  # paper0-verify 유니버스 (가상 가격)


def rows() -> list[RawDaily]:
    """시가·종가가 서로 다른 가상 일봉 (고가·저가는 둘을 감쌈)."""
    out, prev = [], Decimal("10000")
    for i, d in enumerate(trading_days()):
        c = Decimal(10000 + (i % 7) * 50)
        o = prev + 20
        out.append(RawDaily(d, o, max(o, c) + 30, min(o, c) - 30, c, Decimal("100000")))
        prev = c
    return out


DATA = {INST: rows()}
P0_INFOS = {INST: InstrumentInfo(INST, InstrumentKind.STOCK, Decimal("1"), Decimal("1"))}
TWO = (time(9, 30), time(14, 30))


def cfg_p0(tmp_path: Path, run_id: str, **over: object) -> BacktestConfig:
    strat = Paper0Verify()
    cfg = BacktestConfig(
        run_id=run_id, strategy=strat, params=validate_params(strat.spec, {"qty": 1, "hold_days": 0}),
        caps=CAPS_ALL_SUPPORTED, account_limits=scenario_limits(l7_max_order_amount=Decimal("10000000")),
        gate_settings=SETTINGS, calendar=CAL, ticks=None, slippage_tick=Decimal("10"), data=DATA, infos=P0_INFOS,
        start=START, end=END, initial_cash=Decimal("10000000"), out_dir=tmp_path / "var" / "backtest",
        data_version="virtual-v1", code_commit="test", eval_time=TWO[0], eval_times=TWO)
    return replace(cfg, **over)  # type: ignore[arg-type]


# --- 1. 다중 판정 시각 ---------------------------------------------------------------------------

def test_two_evals_round_trip_same_day_open_then_close(tmp_path: Path) -> None:
    r = run_backtest(cfg_p0(tmp_path, "two"))
    bars = {b.day: b for b in DATA[INST]}
    by_day: dict[date, list[tuple]] = {}
    for f in r.fills:
        by_day.setdefault(f[0], []).append(f)
    assert len(by_day) == len(r.days)
    for day, fs in by_day.items():
        assert [f[3] for f in fs] == ["BUY", "SELL"], day
        assert Decimal(fs[0][5]) == bars[day].open + 10    # 첫 판정: 그날 시가 + 1틱 (DP-7)
        assert Decimal(fs[1][5]) == bars[day].close - 10   # 이후 판정: 그날 종가 − 1틱 (제안 가정)
    assert len(r.trades) == len(r.days) and all(t.holding_days == 0 for t in r.trades)


def test_single_eval_keeps_next_day_exit(tmp_path: Path) -> None:
    r = run_backtest(cfg_p0(tmp_path, "one", eval_times=(time(9, 30),)))
    assert all(t.holding_days == 1 for t in r.trades)  # 기존 동작: D일 진입, D+1일 청산


def test_later_eval_cannot_see_todays_bar_and_sees_open_as_price(tmp_path: Path) -> None:
    seen: list[tuple[datetime, date | None, Decimal | None]] = []

    class Spy(Paper0Verify):
        def evaluate(self, snapshot: EvaluationSnapshot, params):  # type: ignore[no-untyped-def]
            v = snapshot.view(INST)
            seen.append((snapshot.as_of, v.daily_bars[-1].at if v and v.daily_bars else None,
                         v.last_price if v else None))
            return super().evaluate(snapshot, params)

    run_backtest(cfg_p0(tmp_path, "spy", strategy=Spy()))
    bars = {b.day: b for b in DATA[INST]}
    assert len(seen) == 2 * len([d for d in trading_days() if START <= d <= END])
    for as_of, last_day, last_price in seen:
        today = as_of.astimezone(KST).date()
        assert last_day is not None and last_day < today          # DP-5: 14:30에도 당일 봉 없음
        assert last_price == bars[today].open                      # 판정 시세 = 당일 시가 (종가 아님)


def test_eval_times_must_be_ascending_unique_and_in_session(tmp_path: Path) -> None:
    for bad in ((time(14, 30), time(9, 30)), (time(9, 30), time(9, 30)), (time(8, 0),), (time(15, 30),)):
        with pytest.raises(BacktestError, match="판정 시각"):
            run_backtest(cfg_p0(tmp_path, f"bad{bad[0]:%H%M}{len(bad)}", eval_times=bad))


# --- 4. 체결 시각 = 판정 시각 --------------------------------------------------------------------

def test_fill_time_not_before_decision_time(tmp_path: Path) -> None:
    r = run_backtest(cfg_p0(tmp_path, "ft"))
    repo = Repository(r.db_path, Mode.BACKTEST)
    q = ("SELECT i.created_at, f.at FROM fill f JOIN broker_order b ON f.order_key = b.order_key "
         "JOIN intent i ON b.intent_id = i.intent_id")
    pairs = repo.connection.execute(q).fetchall()
    repo.close()
    assert pairs
    times = set()
    for created, at in pairs:
        c, a = datetime.fromisoformat(created), datetime.fromisoformat(at)
        assert a >= c, (created, at)                 # 감사 순서: 주문 생성 → 체결
        times.add(a.astimezone(KST).time())
    assert times == {time(9, 30), time(14, 30)}       # 체결을 일으킨 판정 시각으로 기록


# --- 2. 상·하한가 호가 단위 -------------------------------------------------------------------------

def test_price_limits_rounded_to_tick() -> None:
    up, low = price_limits(Decimal("10010"), Decimal("0.30"), lambda p: Decimal("10"))
    assert (up, low) == (Decimal("13010"), Decimal("7010"))       # 13013 → 내림, 7007 → 올림
    up, low = price_limits(Decimal("71000"), Decimal("0.30"), lambda p: Decimal("100"))
    assert (up, low) == (Decimal("92300"), Decimal("49700"))       # 92300 정확, 49700 정확
    up, low = price_limits(Decimal("71050"), Decimal("0.30"), lambda p: Decimal("100"))
    assert (up, low) == (Decimal("92300"), Decimal("49800"))       # 92365 → 92300, 49735 → 49800


def test_buy_at_rounded_upper_limit_open_does_not_fill(tmp_path: Path) -> None:
    """기준가 10010, 다음 날 시가 13010 = 내림한 상한가 → 매수 미체결 (내림 전 13013과 비교하면 체결됐음)."""
    days = [d for d in trading_days()]
    data = []
    for i, d in enumerate(days):
        if d < START:
            data.append(RawDaily(d, Decimal("10010"), Decimal("10010"), Decimal("10010"), Decimal("10010"),
                                 Decimal("100000")))
        else:
            data.append(RawDaily(d, Decimal("13010"), Decimal("13010"), Decimal("13010"), Decimal("13010"),
                                 Decimal("100000")))
            break
    r = run_backtest(cfg_p0(tmp_path, "lim", data={INST: data}, end=data[-1].day, eval_times=(time(9, 30),)))
    assert r.fills == []


def test_report_mentions_tick_fallback_and_eval_times(tmp_path: Path) -> None:
    cfg = cfg_p0(tmp_path, "rep")
    r = run_backtest(cfg)
    text = render_report(cfg, r, settings_hash="h", reproducible=True, lookahead_bad=[])
    assert "09:30,14:30" in text
    assert "호가 표 없음" in text and "10원" in text
    assert "종가" in text


# --- 3. V-91 상태 교란 ---------------------------------------------------------------------------

def test_v91_stateful_check_passes_for_honest_pipeline(tmp_path: Path) -> None:
    days = [d for d in trading_days() if START <= d <= END]
    assert perturbation_check(cfg_p0(tmp_path, "la"), days[3:40:9]) == []


class ExitPeeks:
    """보유 중일 때만 일봉을 보는 가상 전략: 진입은 시세만, 청산은 마지막 봉 종가 조건."""

    def __init__(self) -> None:
        self.spec = replace(P0_SPEC, strategy_id="exit-peeks")

    def evaluate(self, snapshot: EvaluationSnapshot, params):  # type: ignore[no-untyped-def]
        out = []
        for v in snapshot.instruments:
            if not v.data_ok or v.has_pending_order:
                continue
            ref = v.last_price if v.last_price is not None else Decimal("10000")
            pos = v.position
            if pos is not None and pos.quantity > 0:
                if v.daily_bars:  # 청산 기준가 = 마지막 확정 봉 종가 (누출되면 당일 종가가 보임)
                    out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                           pos.quantity, OrderType.MARKET, v.daily_bars[-1].close,
                                           timedelta(minutes=10)))
            else:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       Decimal("1"), OrderType.MARKET, ref, timedelta(minutes=10)))
        return out


def test_v91_stateful_check_detects_leak_in_exit_path(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    import trading_system.marketdata.store as store_mod
    real = store_mod.daily_bar_available
    monkeypatch.setattr(store_mod, "daily_bar_available",
                        lambda day, as_of, cal: real(day, as_of, cal) or day == as_of.astimezone(KST).date())
    strat = ExitPeeks()
    cfg = cfg_p0(tmp_path, "leak", strategy=strat, params=validate_params(strat.spec, {"qty": 1, "hold_days": 0}))
    days = [d for d in trading_days() if START <= d <= END][3:40:9]
    # 상태 교란만으로(보유 상태의 청산 경로) 누출을 잡는다. 당일 봉은 시가·고가·저가·거래량을 두고 종가만 바꾼다
    assert perturbation_check(cfg, days, flat=False)


def test_fills_are_utc_aware(tmp_path: Path) -> None:
    r = run_backtest(cfg_p0(tmp_path, "tz", eval_times=(time(9, 30),)))
    repo = Repository(r.db_path, Mode.BACKTEST)
    at = repo.connection.execute("SELECT at FROM fill LIMIT 1").fetchone()[0]
    repo.close()
    assert datetime.fromisoformat(at).astimezone(UTC).astimezone(KST).time() == time(9, 30)
