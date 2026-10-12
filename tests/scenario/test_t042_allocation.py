"""T-042 배분 규칙 (CP-034 D-199, A-092 Q-9 / V-277).

모든 지수·종목·가격은 가상이다. 지수 공급자는 합성 ka20006 응답으로만 시험한다(네트워크·키움 연결 없음).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.allocation_rules import CAPS_WITH_INDEX, BrokenRule, IndexAboveSma, regime_spec
from tests.support.engine_world import ACCOUNT, CAL, DAY, EngineWorld, kst
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, cross_spec
from tests.support.universe_strategies import UbIncrease, UbProbe, ub_spec
from trading_system.app.wiring import AllocationInput, add_allocations
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_registry import StrategyRegistry, validate_spec
from trading_system.domain.allocation import IndexView
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView, StrategyState
from trading_system.evaluation.runner import run_evaluation
from trading_system.marketdata.index_feed import IndexBarStore, IndexParseError, confirm_at, parse_ka20006
from trading_system.marketdata.quality import RawDaily
from trading_system.storage.repository import Repository
from trading_system.strategy.base import make_signal

REPO_ROOT = Path(__file__).resolve().parents[2]
A, B, C = "000010", "000020", "000030"


def index_rows(end: date, closes: list[int]) -> list[RawDaily]:
    """end까지(포함) 거래일에 종가 목록을 오래된 순으로 배치."""
    days, d = [], end
    while len(days) < len(closes):
        if CAL.is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return [RawDaily(day, Decimal(c), Decimal(c) + 5, Decimal(c) - 5, Decimal(c), Decimal("1000"))
            for day, c in zip(days, closes, strict=True)]


def prev_day(d: date) -> date:
    p = d - timedelta(days=1)
    while not CAL.is_trading_day(p):
        p -= timedelta(days=1)
    return p


UP = [2500, 2510, 2520, 2530, 2600]        # 마지막 종가 > 5일 평균 → 허용
DOWN = [2600, 2590, 2580, 2570, 2400]      # 마지막 종가 < 5일 평균 → 불허


class Source:
    """엔진 지수 공급자 (index_source). 판정 직전 지수 일봉을 바꿀 수 있다."""

    def __init__(self, closes: list[int] | None) -> None:
        self.store = IndexBarStore(CAL)
        if closes is not None:
            self.store.update("001", index_rows(prev_day(DAY), closes), None)

    def set(self, closes: list[int]) -> None:
        self.store = IndexBarStore(CAL)
        self.store.update("001", index_rows(prev_day(DAY), closes), None)

    def __call__(self, code: str, at: datetime) -> IndexView:
        return self.store.view(code, at)


def world(tmp_path: Path, rule=None, source: Source | None = None, strat=None,  # type: ignore[no-untyped-def]
          times: tuple[time, ...] = (time(9, 5), time(10, 5), time(11, 5))) -> EngineWorld:
    strat = strat or UbProbe(ub_spec(strategy_id="t-cross", universe_method="U-A", universe=(A, B, C),
                                     universe_list=None))
    w = EngineWorld(tmp_path, [(strat, {"qty": 1}, times)], [A, B, C], caps=CAPS_WITH_INDEX)
    add_allocations(w.engine, [AllocationInput(rule or IndexAboveSma(), {"sma_days": 5})], CAPS_WITH_INDEX,
                    w.engine.limits(), Mode.BACKTEST, index_source=source if source is not None else Source(UP))
    return w


def _alloc(w: EngineWorld) -> list[dict]:
    return [json.loads(e["summary"]) for e in w.audits("ALLOCATION_EVALUATION")]


def _rows(w: EngineWorld) -> list[tuple[str, str]]:
    return [(r["request_type"], r["instrument"]) for r in w.intents()]


# --- V-277 엔진: 스위치·청산 허용·판정 실패 --------------------------------------------------------

def test_v277_switch_off_blocks_entry_then_on_allows_then_off_allows_exit(tmp_path: Path) -> None:
    src = Source(DOWN)
    w = world(tmp_path, source=src)
    try:
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        assert _rows(w) == []                                         # 꺼짐: 신규 진입 거부
        [a] = _alloc(w)
        assert (a["rule"], a["target"], a["allow_entry"], a["failed"]) == ("t-regime:1", "t-cross:1", False, False)
        ev = json.loads(w.audits("EVALUATION")[-1]["summary"])
        assert ev["dropped"] == 3 and ev["allocation"]["allow_entry"] is False
        src.set(UP)
        w.at(10, 5)
        w.engine.tick()
        w.at(10, 6)
        w.engine.tick()
        assert sorted(_rows(w)) == [("ENTRY", A), ("ENTRY", B), ("ENTRY", C)]   # 켜짐: 진입
        src.set(DOWN)
        w.at(11, 5)
        w.engine.tick()
        assert sorted(_rows(w)[3:]) == [("EXIT", A), ("EXIT", B), ("EXIT", C)]  # 꺼짐에도 청산 허용
    finally:
        w.close()


def test_v277_switch_off_blocks_increase(tmp_path: Path) -> None:
    src = Source(UP)
    strat = UbIncrease(ub_spec(strategy_id="t-cross", universe_method="U-A", universe=(A,), universe_list=None))
    w = world(tmp_path, source=src, strat=strat)
    try:
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        w.at(9, 6)
        w.engine.tick()
        assert _rows(w) == [("ENTRY", A)]
        src.set(DOWN)
        w.at(10, 5)
        w.engine.tick()
        assert _rows(w) == [("ENTRY", A)]                              # 증액도 위험 확대 → 거부 [제안]
    finally:
        w.close()


@pytest.mark.parametrize("case", ["no_source", "missing", "stale", "gap", "rule_error"])
def test_v277_rule_failure_blocks_entry_and_alerts(tmp_path: Path, case: str) -> None:
    rule = BrokenRule() if case == "rule_error" else IndexAboveSma()
    src: Source | None = Source(UP)
    if case == "missing":
        src = Source(None)
    elif case == "stale":                                             # 전 거래일 일봉 없음 → 지연
        src = Source(None)
        src.store.update("001", index_rows(prev_day(prev_day(DAY)), UP), None)
    elif case == "gap":                                               # 중간 거래일 빠짐 → 결측
        rows = index_rows(prev_day(DAY), UP)
        src = Source(None)
        src.store.update("001", rows[:2] + rows[3:], None)
    w = world(tmp_path, rule=rule, source=src)
    if case == "no_source":
        w.engine.index_source = None
    try:
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        assert _rows(w) == []
        [a] = _alloc(w)
        assert a["failed"] is True and a["allow_entry"] is False and a["reason"]
        alerts = [json.loads(e["summary"]) for e in w.audits("ALERT") if "배분 규칙" in e["summary"]]
        assert alerts and alerts[0]["level"] in ("WARNING", "EMERGENCY") and "신규 진입 거부" in alerts[0]["message"]
    finally:
        w.close()


def test_v277_rule_without_target_has_no_effect(tmp_path: Path) -> None:
    w = world(tmp_path, rule=IndexAboveSma(regime_spec(targets=("other",))), source=Source(DOWN))
    try:
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        assert sorted(_rows(w)) == [("ENTRY", A), ("ENTRY", B), ("ENTRY", C)]
        assert _alloc(w) == []
        assert "allocation" not in json.loads(w.audits("EVALUATION")[-1]["summary"])
    finally:
        w.close()


def test_v277_rule_sees_only_days_before_evaluation_day(tmp_path: Path) -> None:
    rule = IndexAboveSma()
    src = Source(UP)
    src.store.update("001", [RawDaily(DAY, Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1))], None)
    w = world(tmp_path, rule=rule, source=src)
    try:
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        assert rule.views and all(b.at < DAY for b in rule.views[0].bars)   # 당일 값은 넣지 않는다 (D-189)
    finally:
        w.close()


def test_runner_entry_block_drops_entry_increase_only() -> None:
    spec = ub_spec(universe_method="U-A", universe=(A, B, C), universe_list=None)
    as_of = kst(DAY, 9, 5)
    bars = (Bar(DAY - timedelta(days=1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1)),)
    pos = PositionView(Decimal("2"), Decimal("1"), 0, True)
    snap = EvaluationSnapshot("ev-1", "M1", ACCOUNT, as_of, Decimal("1000000"),
                              (InstrumentView(A, bars, position=pos), InstrumentView(B, bars, position=pos),
                               InstrumentView(C, bars)))

    class S:
        def __init__(self) -> None:
            self.spec = spec

        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            mk = lambda i, r, k, q: make_signal(spec, snapshot, i, r, k, Decimal(q), OrderType.MARKET,  # noqa: E731
                                                Decimal(1), timedelta(minutes=5))
            return [mk(A, "INC-1", RequestType.INCREASE, "1"), mk(B, "EXIT-1", RequestType.EXIT, "2"),
                    mk(C, "ENTRY-1", RequestType.ENTRY, "1")]

    r = run_evaluation(S(), snap, {}, entry_block="배분 규칙: 신규 진입 불허")
    assert [(s.kind, s.instrument) for s in r.signals] == [(RequestType.EXIT, B)]
    assert sorted(d.instrument for d in r.dropped) == [A, C]
    assert all(d.reason == "배분 규칙: 신규 진입 불허" for d in r.dropped)


# --- 지수 공급자 (합성 ka20006 응답, 오프라인) --------------------------------------------------------

def test_parse_ka20006_synthetic_response() -> None:
    payload = {"inds_dt_pole_qry": [
        {"dt": "20260930", "open_pric": "+263512", "high_pric": "264000", "low_pric": "-262001", "cur_prc": "263900",
         "trde_qty": "123456", "trde_prica": "1"},
        {"dt": "20260929", "open_pric": "262000", "high_pric": "263000", "low_pric": "261000", "cur_prc": "263512",
         "trde_qty": "100"},
    ], "return_code": 0}
    bars = parse_ka20006(payload)
    assert [b.day for b in bars] == [date(2026, 9, 29), date(2026, 9, 30)]
    assert (bars[1].open, bars[1].low, bars[1].close) == (Decimal("2635.12"), Decimal("2620.01"), Decimal("2639.00"))


@pytest.mark.parametrize("payload", [
    {}, {"inds_dt_pole_qry": [{"dt": "2026-09-30", "cur_prc": "1"}]},
    {"inds_dt_pole_qry": [{"dt": "20260930", "open_pric": "1", "high_pric": "1", "low_pric": "1", "cur_prc": "1",
                           "trde_qty": "1"}] * 2},
    {"inds_dt_pole_qry": [{"dt": "20260930", "open_pric": "1.5", "high_pric": "1", "low_pric": "1", "cur_prc": "1",
                           "trde_qty": "1"}]},
    {"inds_dt_pole_qry": [{"dt": "20260930", "high_pric": "1", "low_pric": "1", "cur_prc": "1", "trde_qty": "1"}]},
])
def test_parse_ka20006_rejects_bad_format(payload: dict) -> None:
    with pytest.raises(IndexParseError):
        parse_ka20006(payload)


def test_d189_confirmation_by_collection_time() -> None:
    d = DAY                                        # 판정일 (목)
    p1 = prev_day(d)                               # 전 거래일
    rows = index_rows(p1, UP)
    assert confirm_at(p1, CAL) == datetime.combine(d, time(6, 0), tzinfo=KST)
    s = IndexBarStore(CAL)
    s.update("001", rows[:-1], datetime.combine(p1, time(6, 30), tzinfo=KST))   # 그 전 날들: 확정
    s.update("001", rows[-1:], datetime.combine(p1, time(18, 0), tzinfo=KST))  # 전 거래일 당일 저녁 수집: 미확정
    v = s.view("001", kst(d, 9, 5))
    assert not v.data_ok and "지연" in v.issue and v.bars[-1].at == rows[-2].day
    s.update("001", rows[-1:], datetime.combine(d, time(6, 10), tzinfo=KST))   # 판정일 06:10 재수집 → 확정
    v = s.view("001", kst(d, 9, 5))
    assert v.data_ok and v.bars[-1].at == p1 and len(v.bars) == len(UP)


# --- 등록·상태 전환 (전략과 같게) ----------------------------------------------------------------------

def test_allocation_spec_validation() -> None:
    assert validate_spec(regime_spec(), CAPS_WITH_INDEX, None, Mode.BACKTEST) == []
    p = validate_spec(regime_spec(targets=()), CAPS_WITH_INDEX, None, Mode.BACKTEST)
    assert any("allocation_targets" in x for x in p)
    p = validate_spec(regime_spec(targets=("t-regime",)), CAPS_WITH_INDEX, None, Mode.BACKTEST)
    assert any("자기 자신" in x for x in p)
    p = validate_spec(regime_spec(capabilities=("CAP-TRG-01",)), CAPS_WITH_INDEX, None, Mode.BACKTEST)
    assert any("CAP-DAT-05d" in x for x in p)
    p = validate_spec(regime_spec(kind="OTHER"), CAPS_WITH_INDEX, None, Mode.BACKTEST)
    assert any("kind" in x for x in p)
    bad_rule = replace(regime_spec(), rules=(replace(regime_spec().rules[0], kind="ENTRY"),))
    assert any("ALLOCATION" in x for x in validate_spec(bad_rule, CAPS_WITH_INDEX, None, Mode.BACKTEST))
    strat_with_alloc_rule = replace(cross_spec(), rules=cross_spec().rules + (regime_spec().rules[0],))
    assert any("ALLOCATION" in x for x in
               validate_spec(strat_with_alloc_rule, CAPS_WITH_INDEX, None, Mode.BACKTEST))
    assert any("allocation_targets" in x for x in
               validate_spec(cross_spec(allocation_targets=("x",)), CAPS_WITH_INDEX, None, Mode.BACKTEST))


def test_allocation_index_capability_undefined_in_current_table_is_rejected() -> None:
    # 기존 동작 유지: capabilities.md에 CAP-DAT-05d 행이 없으면 "정의되지 않은 능력"으로 거부 (표 반영은 별도)
    caps = load_capabilities(REPO_ROOT / "docs" / "capabilities.md")
    if "CAP-DAT-05d" in caps:
        pytest.skip("capabilities.md에 CAP-DAT-05d 행이 반영됨 — 이 시험은 반영 전 동작 고정용")
    for mode in (Mode.BACKTEST, Mode.PAPER):
        p = validate_spec(regime_spec(), caps, None, mode)
        assert "CAP-DAT-05d: 정의되지 않은 능력" in p


def test_allocation_registration_and_transitions_like_strategy(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    try:
        clock = FixedClock(kst(DAY, 8, 0))
        reg = StrategyRegistry(repo, clock, None)
        assert reg.register(regime_spec(), {"sma_days": 5}, CAPS_WITH_INDEX, None, Mode.BACKTEST).ok
        assert reg.state_of("t-regime:1") is StrategyState.DRAFT
        with pytest.raises(ValueError):
            reg.transition("t-regime", 1, StrategyState.BACKTEST_PASSED, "A-999")       # 증거 없음
        reg.transition("t-regime", 1, StrategyState.BACKTEST_PASSED, "A-999", evidence="var/x.md")
        assert reg.state_of("t-regime:1") is StrategyState.BACKTEST_PASSED
        with pytest.raises(ValueError):                                                   # PAPER 대상 등록 없음
            reg.transition("t-regime", 1, StrategyState.PAPER, "A-999")
    finally:
        repo.close()


def test_add_allocations_rejects_bad_registration(tmp_path: Path) -> None:
    w = EngineWorld(tmp_path, [(UbProbe(ub_spec(strategy_id="t-cross", universe_method="U-A", universe=(A,),
                                                universe_list=None)), {"qty": 1}, (time(9, 5),))], [A])
    try:
        with pytest.raises(ConfigError):
            add_allocations(w.engine, [AllocationInput(IndexAboveSma(), {"sma_days": 5})], CAPS_ALL_SUPPORTED,
                            w.engine.limits(), Mode.BACKTEST, index_source=Source(UP))
        with pytest.raises(ConfigError):                       # 전략을 배분 규칙으로 넣으면 거부
            add_allocations(w.engine, [AllocationInput(UbProbe(), {"qty": 1})], CAPS_WITH_INDEX,
                            w.engine.limits(), Mode.BACKTEST, index_source=Source(UP))
        assert w.engine.allocations == []
    finally:
        w.close()


# --- 백테스트·검증 실행기 (Q-9 대조) ----------------------------------------------------------------

def _bt(tmp_path: Path, index: list[RawDaily] | None, run_id: str, rule=None):  # type: ignore[no-untyped-def]
    from tests.scenario.test_t009_backtest import CAL as BCAL, SETTINGS, path
    from tests.support.gate_fakes import scenario_limits
    from trading_system.app.backtest import BacktestConfig
    from trading_system.config.strategy_params import validate_params
    from trading_system.domain.market import InstrumentInfo, InstrumentKind
    data = {"TST-A": path([9000, 9100, 9200, 9050]), "TST-B": path([10000, 10100, 9900])}
    strat = UbProbe(ub_spec(strategy_id="t-cross", universe_method="U-A", universe=tuple(data), universe_list=None))
    return BacktestConfig(
        run_id=run_id, strategy=strat, params=validate_params(strat.spec, {"qty": 1}), caps=CAPS_WITH_INDEX,
        account_limits=scenario_limits(l7_max_order_amount=Decimal("10000000")), gate_settings=SETTINGS,
        calendar=BCAL, ticks=None, slippage_tick=Decimal("10"), data=data,
        infos={i: InstrumentInfo(i, InstrumentKind.STOCK, Decimal("1"), Decimal("1")) for i in data},
        start=date(2026, 1, 5), end=date(2026, 3, 31), initial_cash=Decimal("10000000"),
        out_dir=tmp_path / "bt", data_version="v", code_commit="t", final_from=date(2027, 1, 1),
        allocation=rule or IndexAboveSma(), allocation_params={"sma_days": 5}, index_bars=index)


def _index_series(pattern: list[int]) -> list[RawDaily]:
    from tests.scenario.test_t009_backtest import trading_days
    rows = []
    for i, d in enumerate(trading_days()):
        c = Decimal(pattern[i % len(pattern)])
        rows.append(RawDaily(d, c, c + 5, c - 5, c, Decimal("1")))
    return rows


def test_backtest_allocation_blocks_entries_when_off_and_counts(tmp_path: Path) -> None:
    from trading_system.app.backtest import run_backtest
    falling = _index_series([3000 - 10 * i for i in range(200)])          # 계속 하락 → 늘 불허
    r = run_backtest(_bt(tmp_path, falling, "off"))
    assert r.trades == [] and r.allocation is not None
    assert r.allocation["blocked_evaluations"] > 0 and r.allocation["failed_evaluations"] == 0
    rising = _index_series([2000 + 10 * i for i in range(200)])           # 계속 상승 → 늘 허용
    r2 = run_backtest(_bt(tmp_path, rising, "on"))
    assert r2.trades and r2.allocation["allowed_evaluations"] > 0 and r2.allocation["failed_evaluations"] == 0
    assert r2.allocation["blocked_evaluations"] < 5                    # 워밍업(지수 일봉 < sma_days)만 불허
    summary = json.loads((tmp_path / "bt" / "on" / "summary.json").read_text(encoding="utf-8"))
    assert summary["allocation"]["rule"] == "t-regime:1"
    r3 = run_backtest(_bt(tmp_path, None, "noindex"))                    # 지수 없음 → 판정 실패 → 진입 0
    assert r3.trades == [] and r3.allocation["failed_evaluations"] > 0


def test_backtest_allocation_target_must_include_strategy(tmp_path: Path) -> None:
    from trading_system.app.backtest import BacktestError, run_backtest
    with pytest.raises(BacktestError, match="대상"):
        run_backtest(_bt(tmp_path, None, "x", rule=IndexAboveSma(regime_spec(targets=("other",)))))


def test_q9_verdict_rules() -> None:
    from trading_system.app.allocation_check import decide_allocation
    base = dict(loss_windows=1, worst_return=Decimal("0.00"), exp_base=Decimal("100"), exp_double=Decimal("50"),
                positive_windows=3, traded_windows=6, missed_profit=Decimal("-10"), failures=0)
    off = dict(loss_windows=3, worst_return=Decimal("-0.0138"), exp_base=Decimal("20"), exp_double=Decimal("10"),
               positive_windows=4, traded_windows=7, missed_profit=Decimal("0"), failures=0)
    v = decide_allocation(base, off)
    assert v["verdict"] == "배분 규칙 채택 후보" and v["①"] == v["②"] == v["③"] == "충족"
    assert v["④"].startswith("미충족") and "기록만" in v["④"]               # ④는 판정에 쓰지 않는다
    assert "LIVE 금지" in v["⑥"] and "C-2" in v["note"]
    assert decide_allocation({**base, "exp_double": Decimal("9")}, off)["verdict"] == "채택 후보 아님"   # ② 2배
    assert decide_allocation({**base, "loss_windows": 3}, off)["verdict"] == "채택 후보 아님"           # ① 같음
    assert decide_allocation({**base, "worst_return": Decimal("-0.0138")}, off)["verdict"] == "채택 후보 아님"
    assert decide_allocation({**base, "exp_base": None}, off)["verdict"] == "판단 불가"
    assert decide_allocation({**base, "failures": 2}, off)["verdict"] == "판단 불가"                    # 지수 결측


def test_q9_comparison_runs_with_and_without_filter(tmp_path: Path) -> None:
    from trading_system.app.allocation_check import compare_allocation, render_allocation
    from trading_system.app.validation import Window
    idx = _index_series([2000 + 10 * i for i in range(30)] + [2290 - 10 * i for i in range(30)])
    windows = (Window(1, date(2026, 1, 5), date(2026, 1, 9), date(2026, 1, 12), date(2026, 2, 13)),
               Window(2, date(2026, 1, 5), date(2026, 2, 13), date(2026, 2, 16), date(2026, 3, 31)))
    out = compare_allocation(_bt(tmp_path, idx, "tpl"), out_dir=tmp_path / "cmp", report_version=1,
                             windows=windows, final_from=date(2027, 1, 1), slippage_base=Decimal("0.001"))
    assert out["runs"] == 2 * 2 * 2                                    # 창 × 필터 유·무 × 슬리피지 기본·2배
    assert set(out["with_filter"]) >= {"loss_windows", "worst_return", "exp_base", "exp_double"}
    assert out["verdict"]["verdict"] in ("배분 규칙 채택 후보", "채택 후보 아님", "판단 불가")
    md = render_allocation(out)
    for key in ("①", "②", "③", "④", "⑤", "⑥", "LIVE 금지"):
        assert key in md
    assert (tmp_path / "cmp" / "allocation-v1.md").exists() and (tmp_path / "cmp" / "allocation.json").exists()


def test_validation_cli_allocation_argument_checks(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from trading_system.app.validation_tool import validate_main
    data = tmp_path / "data"
    data.mkdir()
    (data / "TST-A.csv").write_text("date,open,high,low,close,volume\n2026-01-05,1,1,1,1,1\n", encoding="utf-8")
    sp = tmp_path / "s.toml"
    sp.write_text('strategy = "t-ub"\nversion = 1\n[params]\nqty = 1\n', encoding="utf-8")
    rp = tmp_path / "r.toml"
    rp.write_text('strategy = "t-regime"\nversion = 1\n[params]\nsma_days = 5\n', encoding="utf-8")
    base = ["--strategy", "tests.support.universe_strategies:UbProbe", "--params", str(sp), "--data", str(data),
            "--report-version", "1", "--out-root", str(tmp_path / "out")]
    assert validate_main(base + ["--allocation", "tests.support.allocation_rules:IndexAboveSma",
                                 "--allocation-params", str(rp)], root=REPO_ROOT) == 2
    assert "--index" in capsys.readouterr().out
    assert validate_main(base + ["--allocation", "tests.support.universe_strategies:UbProbe",
                                 "--allocation-params", str(sp), "--index", str(data / "TST-A.csv")],
                         root=REPO_ROOT) == 2
    assert "배분 규칙 형식이 아님" in capsys.readouterr().out
