"""T-007 전략 프레임워크 (05-plan T-007 + CP-018 추가, 04 §3.6 V-80~V-86, V-113, V-121, INV-13).

모든 값·전략은 가상이다 (tests/support/sample_strategies.py).
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, PRICES, T0, Env
from tests.support.gate_fakes import TEST_POLICY, make_facts, scenario_limits
from tests.support.invariants import check_invariants
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, CrossStrategy, cross_spec
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.config.capabilities import load_capabilities, parse_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_params import load_params_file, validate_params
from trading_system.config.strategy_registry import StrategyRegistry, validate_spec
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestType
from trading_system.domain.models import Account, GateRequest, Signal
from trading_system.domain.sizing import size_fixed_amount, size_fixed_quantity, size_ratio, size_risk
from trading_system.domain.strategy import (
    Bar, EvaluationSnapshot, InstrumentView, PositionView, RuleSpec, StopReason, StrategyLimits, StrategyState,
    evaluation_id_of,
)
from trading_system.evaluation.runner import run_evaluation
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs
from trading_system.halt.manager import Channel, HaltManager
from trading_system.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[2]


# --- 준비 ---------------------------------------------------------------------------------

def wire(env: Env) -> StrategyRegistry:
    """env의 게이트 전략 입력을 실제 레지스트리로 바꾼다."""
    reg = StrategyRegistry(env.repo, env.clock, env.halts)
    f = env.facts
    env.gate = Gate(env.repo, env.clock, GateInputs(f.limits, f.market, f.account, reg, f.clock_health), TEST_POLICY)
    env.halts._gate = env.gate  # 정지 정책 취소도 같은 게이트로
    return reg


def register(reg: StrategyRegistry, env: Env, spec=None, mode: Mode = Mode.PAPER):  # type: ignore[no-untyped-def]
    spec = spec or cross_spec()
    r = reg.register(spec, dict(PARAMS), CAPS_ALL_SUPPORTED, env.facts.limits.by_account[ACCOUNT], mode)
    assert r.ok, r.problems
    return spec


def to_paper(reg: StrategyRegistry, spec) -> None:  # type: ignore[no-untyped-def]
    reg.transition(spec.strategy_id, spec.version, StrategyState.BACKTEST_PASSED, "A-901",
                   evidence="docs/strategies/x/backtest-v1.md")
    reg.transition(spec.strategy_id, spec.version, StrategyState.PAPER, "A-902")


def sig(spec, kind: RequestType, inst: str = "TST-A", qty: str = "10", ev: str = "e1",  # type: ignore[no-untyped-def]
        stop: str | None = None, at: datetime = T0) -> GateRequest:
    return GateRequest.from_signal(Signal(spec.strategy_id, spec.version, ev, ACCOUNT, inst, "R", kind, Decimal(qty),
                                          OrderType.MARKET, Decimal("10000"), at - timedelta(minutes=1),
                                          at + timedelta(minutes=20),
                                          stop_price=None if stop is None else Decimal(stop)))


def run(env: Env, req: GateRequest):  # type: ignore[no-untyped-def]
    d = env.gate.decide(req)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


# --- V-85 등록 검증 ---------------------------------------------------------------------------

@pytest.mark.parametrize(("over", "needle"), [
    ({"name": ""}, "name"),
    ({"strategy_id": "Bad_ID"}, "strategy_id"),
    ({"version": 0}, "version"),
    ({"capabilities": ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01", "CAP-DAT-05")}, "CAP-DAT-05"),
    ({"rules": (RuleSpec("E1", "ENTRY", (), "CAP-TRG-01", "c", "a", "i", 1),
                RuleSpec("E2", "ENTRY", (), "CAP-TRG-01", "c", "a", "i", 1))}, "우선순위 중복"),
    ({"rules": (RuleSpec("E1", "ENTRY", ("CAP-DAT-02",), "CAP-TRG-01", "c", "a", "i", 1),)}, "선언하지 않은 능력"),
    ({"limits": StrategyLimits(l2_max_symbol_ratio=Decimal("0.5"))}, "계좌 한도보다 큼"),
    ({"capabilities": ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01", "CAP-ORD-03")}, "미체결 정책"),
    ({"sizing": "S-9"}, "sizing"),
    ({"validity_max_minutes": 0}, "절대 상한"),
])
def test_v85_registration_rejects(over: dict, needle: str) -> None:
    caps = dict(CAPS_ALL_SUPPORTED)
    caps["CAP-DAT-05"] = {"M1": "미지원", "M2": "미지원", "M3": "미지원"}
    problems = validate_spec(cross_spec(**over), caps, scenario_limits(), Mode.PAPER)
    assert any(needle in p for p in problems), problems


def test_v85_capability_status_by_target_mode() -> None:
    caps = {k: {"M1": "검증 중", "M2": "미지원", "M3": "미지원"} for k in CAPS_ALL_SUPPORTED}
    assert validate_spec(cross_spec(), caps, scenario_limits(), Mode.BACKTEST) == []
    assert validate_spec(cross_spec(), caps, scenario_limits(), Mode.PAPER)  # PAPER는 "지원"만


def test_v85_rejected_registration_is_recorded_and_creates_no_state(env: Env) -> None:
    reg = wire(env)
    r = reg.register(cross_spec(name=""), dict(PARAMS), CAPS_ALL_SUPPORTED, scenario_limits())
    assert not r.ok
    assert env.repo.strategy_state("t-cross", 1, "M1") is None
    assert reg.signal_rejection("t-cross:1", RequestType.ENTRY) == "등록되지 않은 전략 (R-17)"


# --- V-86 파라미터 --------------------------------------------------------------------------

@pytest.mark.parametrize(("params", "needle"), [
    ({"entry_level": "9000", "exit_level": "8000"}, "qty: 필수"),
    ({**PARAMS, "qty": 0}, "최소값"),
    ({**PARAMS, "entry_level": 9000.5}, "부동소수"),
    ({**PARAMS, "qty": True}, "정수"),
    ({**PARAMS, "extra": 1}, "명세에 없는"),
    ({**PARAMS, "exit_level": "abc"}, "소수 형식"),
])
def test_v86_parameter_validation(params: dict, needle: str) -> None:
    with pytest.raises(ConfigError) as e:
        validate_params(cross_spec(), params)
    assert any(needle in p for p in e.value.problems), e.value.problems


def test_v86_params_file(tmp_path: Path) -> None:
    p = tmp_path / "t-cross-v1.toml"
    p.write_text('strategy = "t-cross"\nversion = 1\n[params]\nentry_level = "9000"\nexit_level = "8000"\nqty = 10\n',
                 encoding="utf-8")
    v = load_params_file(cross_spec(), p)
    assert v == {"entry_level": Decimal("9000"), "exit_level": Decimal("8000"), "qty": 10}
    p.write_text('strategy = "other"\nversion = 1\n[params]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_params_file(cross_spec(), p)


def test_capabilities_document_parses_and_rejects_bad_status() -> None:
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    assert "CAP-TRG-01" in caps and set(caps["CAP-TRG-01"]) == {"M1", "M2", "M3"}
    with pytest.raises(ConfigError):
        parse_capabilities("| CAP-ID | 능력 | M1 | M2 | M3 |\n|---|---|---|---|---|\n| CAP-X | x | 아마도 | 미지원 | 미지원 |")


# --- INV-13 상태 전이 -------------------------------------------------------------------------

def test_inv13_transitions_need_approval_and_follow_table(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env, mode=Mode.PAPER)
    with pytest.raises(ValueError, match="A-ID"):
        reg.transition("t-cross", 1, StrategyState.BACKTEST_PASSED, "", evidence="r.md")
    with pytest.raises(ValueError, match="허용되지 않는"):
        reg.transition("t-cross", 1, StrategyState.PAPER, "A-901")
    with pytest.raises(ValueError, match="백테스트 실행 기록"):
        reg.transition("t-cross", 1, StrategyState.BACKTEST_PASSED, "A-901")
    to_paper(reg, spec)
    with pytest.raises(ValueError, match="사유"):
        reg.transition("t-cross", 1, StrategyState.STOPPED, "A-903")
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-903", stop_reason=StopReason.USER)
    with pytest.raises(ValueError, match="중지 전 상태"):
        reg.transition("t-cross", 1, StrategyState.LIVE, "A-904")
    reg.transition("t-cross", 1, StrategyState.PAPER, "A-904")
    assert [t["approval_id"] for t in env.repo.strategy_transitions("t-cross", 1)] == ["A-901", "A-902", "A-903", "A-904"]


def test_paper_requires_registration_for_paper_target(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env, mode=Mode.BACKTEST)
    reg.transition(spec.strategy_id, 1, StrategyState.BACKTEST_PASSED, "A-901", evidence="bt.md")
    with pytest.raises(ValueError, match="PAPER·LIVE 대상 등록"):
        reg.transition(spec.strategy_id, 1, StrategyState.PAPER, "A-902")


def test_inv13_checker_detects_transition_without_approval(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "x.db", Mode.BACKTEST)
    repo.insert_strategy_transition("s", 1, "M1", None, "DRAFT", "", None, None, T0)
    assert any(v.startswith("INV-13") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


# --- V-83 상태별 처리, K9 연동 ---------------------------------------------------------------

def test_v83_stopped_and_retired_reject_entries(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-903", stop_reason=StopReason.USER)
    d = env.gate.decide(sig(spec, RequestType.ENTRY, "TST-B", ev="b"))
    assert d.result is GateResult.REJECTED and d.failed_check in (Check.G2, Check.G6)
    assert "K9" in env.repo.active_halts(ACCOUNT)
    ex = env.gate.decide(sig(spec, RequestType.EXIT, "TST-A", ev="c"))
    assert ex.result is GateResult.REJECTED  # 사용자 판단 중지: 운영 요청만
    # 운영 명령으로 K9를 직접 재개할 수 없음 (전략 상태 전환으로만, 01 K9 v0.5)
    assert not env.halts.resume(HaltType.K9, None, Channel.LOCAL, "u", owner="t-cross:1").ok
    reg.transition("t-cross", 1, StrategyState.PAPER, "A-904")
    assert "K9" not in env.repo.active_halts(ACCOUNT)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-B", ev="d")).result is GateResult.APPROVED
    # 묶음 B 검토 B-10(2026-10-03): 소유 보유가 남은 전략은 폐기하지 않는다 → 청산 후 폐기로 시험 변경
    with pytest.raises(ValueError, match="소유"):
        reg.transition("t-cross", 1, StrategyState.RETIRED, "A-905")
    for inst, ev in (("TST-A", "xa"), ("TST-B", "xb")):
        assert run(env, sig(spec, RequestType.EXIT, inst, ev=ev)).result is GateResult.APPROVED
    reg.transition("t-cross", 1, StrategyState.RETIRED, "A-905")
    assert env.gate.decide(sig(spec, RequestType.ENTRY, "TST-C", ev="e")).result is GateResult.REJECTED


# --- V-113 PAPER 모드에서 검증 전 전략 거부 ----------------------------------------------------

def _paper_env(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = Repository(tmp_path / "paper.db", Mode.PAPER)
    repo.register_account(Account(alias=ACCOUNT, mode=Mode.PAPER, broker_env="mock"))
    clock = FixedClock(T0)
    facts = make_facts(ACCOUNT, clock.now)
    for inst, price in PRICES.items():
        facts.market.add(inst, str(price), upper=str(price))
    ex = Executor(repo, FakeBroker(), clock, tmp_path / "KILL")
    gate0 = Gate(repo, clock, facts.inputs(), TEST_POLICY)
    halts = HaltManager(repo, gate0, ex, clock)
    reg = StrategyRegistry(repo, clock, halts)
    gate = Gate(repo, clock, GateInputs(facts.limits, facts.market, facts.account, reg, facts.clock_health),
                TEST_POLICY)
    return repo, reg, gate, facts


def test_v113_paper_mode_rejects_draft_and_backtest_passed(tmp_path: Path) -> None:
    repo, reg, gate, facts = _paper_env(tmp_path)
    spec = cross_spec()
    assert reg.register(spec, dict(PARAMS), CAPS_ALL_SUPPORTED, facts.limits.by_account[ACCOUNT]).ok
    d1 = gate.decide(sig(spec, RequestType.ENTRY, ev="a"))
    assert d1.failed_check is Check.G6 and "V-113" in d1.reason
    reg.transition("t-cross", 1, StrategyState.BACKTEST_PASSED, "A-901", evidence="bt.md")
    assert gate.decide(sig(spec, RequestType.ENTRY, ev="b")).failed_check is Check.G6
    reg.transition("t-cross", 1, StrategyState.PAPER, "A-902")
    assert gate.decide(sig(spec, RequestType.ENTRY, ev="c")).result is GateResult.APPROVED
    assert check_invariants(repo.connection, "PAPER") == []
    repo.close()


# --- V-84 버전 교체 ------------------------------------------------------------------------

def test_v84_version_replacement(env: Env) -> None:
    reg = wire(env)
    v1 = register(reg, env)
    to_paper(reg, v1)
    assert run(env, sig(v1, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    v2 = register(reg, env, cross_spec(version=2))
    reg.transition("t-cross", 2, StrategyState.BACKTEST_PASSED, "A-911", evidence="bt2.md")
    reg.transition("t-cross", 2, StrategyState.PAPER, "A-912")
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-913", stop_reason=StopReason.REPLACE)
    blocked = env.gate.decide(sig(v2, RequestType.ENTRY, "TST-A", ev="b"))
    assert blocked.result is GateResult.REJECTED and blocked.failed_check is Check.G6  # v1 소유 종목
    assert env.gate.decide(sig(v1, RequestType.ENTRY, "TST-C", ev="c")).result is GateResult.REJECTED
    assert run(env, sig(v1, RequestType.EXIT, "TST-A", ev="d")).result is GateResult.APPROVED  # 교체: 청산 허용
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0
    assert run(env, sig(v2, RequestType.ENTRY, "TST-A", ev="e")).result is GateResult.APPROVED  # 소유권 풀림


# --- V-80~V-82 소유권 ------------------------------------------------------------------------

def test_v80_two_strategies_race_for_same_symbol(env: Env) -> None:
    reg = wire(env)
    a = register(reg, env, cross_spec("s-a"))
    b = register(reg, env, cross_spec("s-b"))
    to_paper(reg, a)
    to_paper(reg, b)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    results = []
    threads = [threading.Thread(target=lambda s=s: results.append(env.gate.decide(sig(s, RequestType.ENTRY, ev="x"))))
               for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.result.value for r in results) == ["APPROVED", "REJECTED"]
    loser = next(r for r in results if r.result is GateResult.REJECTED)
    # 같은 종목·방향의 처리 중 의도(INV-15, G4)가 먼저 걸리고, 소유권(G6) 실패도 진단 기록에 남는다 (DP-2)
    assert loser.failed_check in (Check.G4, Check.G6) and (Check.G6, False) in loser.checks
    for r in results:
        if r.approved:
            env.executor.submit(r.approved)


def test_v81_ownership_release_cases(env: Env) -> None:
    reg = wire(env)
    s = register(reg, env)
    to_paper(reg, s)
    env.broker.behavior = FakeBehavior.REJECT
    run(env, sig(s, RequestType.ENTRY, "TST-A", ev="a"))
    assert env.repo.active_ownership(ACCOUNT, "TST-A") is None  # 체결 0주 종결 → 해제
    env.broker.behavior = FakeBehavior.FILL
    run(env, sig(s, RequestType.ENTRY, "TST-B", ev="b"))
    run(env, sig(s, RequestType.EXIT, "TST-B", ev="c"))
    assert env.repo.active_ownership(ACCOUNT, "TST-B") is None  # 전량 청산 → 해제
    env.broker.behavior = FakeBehavior.UNKNOWN
    run(env, sig(s, RequestType.ENTRY, "TST-C", ev="d"))
    assert env.repo.active_ownership(ACCOUNT, "TST-C") is not None  # 결과 불명 → 유지


def test_v82_non_owner_strategy_cannot_sell(env: Env) -> None:
    reg = wire(env)
    a = register(reg, env, cross_spec("s-a"))
    b = register(reg, env, cross_spec("s-b"))
    to_paper(reg, a)
    to_paper(reg, b)
    run(env, sig(a, RequestType.ENTRY, "TST-A", ev="a"))
    d = env.gate.decide(sig(b, RequestType.REDUCE, "TST-A", "5", ev="b"))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G6


# --- V-121 손절가와 L1, 전략별 한도 -----------------------------------------------------------

def test_v121_stop_price_risk_for_l1(env: Env) -> None:
    reg = wire(env)
    s = register(reg, env)
    to_paper(reg, s)
    env.facts.limits.by_account[ACCOUNT] = scenario_limits(l1_max_loss_ratio=Decimal("0.01"))  # 100,000원
    # 손절가 없음: 주문 금액 전체 200 × 10,000 = 2,000,000 > 100,000 → 거부
    assert env.gate.decide(sig(s, RequestType.ENTRY, qty="200", ev="a")).failed_check is Check.G7
    # 손절가 9,500: (10,000 − 9,500) × 200 = 100,000 → 경계 통과
    assert env.gate.decide(sig(s, RequestType.ENTRY, qty="200", ev="b", stop="9500")).result is GateResult.APPROVED
    # 손절가 9,499: 100,200 > 100,000 → 거부
    assert env.gate.decide(sig(s, RequestType.ENTRY, "TST-B", qty="200", ev="c", stop="9499")).failed_check is Check.G7
    # 손절가가 진입가 이상이면 형식 오류
    assert env.gate.decide(sig(s, RequestType.ENTRY, "TST-C", qty="1", ev="d", stop="10000")).failed_check is Check.G3


def test_strategy_limits_apply_min_with_account(env: Env) -> None:
    reg = wire(env)
    s = register(reg, env, cross_spec(limits=StrategyLimits(l2_max_symbol_ratio=Decimal("0.05"),
                                                            l3_max_total_ratio=Decimal("0.08"))))
    to_paper(reg, s)
    assert env.gate.decide(sig(s, RequestType.ENTRY, qty="51", ev="a")).failed_check is Check.G7  # 전략 L2 500,000
    assert run(env, sig(s, RequestType.ENTRY, qty="50", ev="b")).result is GateResult.APPROVED
    d = env.gate.decide(sig(s, RequestType.ENTRY, "TST-B", qty="31", ev="c"))  # 전략 L3 800,000
    assert d.failed_check is Check.G7 and "전략 L3" in d.reason
    assert env.gate.decide(sig(s, RequestType.ENTRY, "TST-B", qty="30", ev="d")).result is GateResult.APPROVED


# --- 판정 실행기 -------------------------------------------------------------------------------

def _snapshot(closes: dict[str, str], held: dict[str, str] | None = None, next_at: datetime | None = None,
              bad: set[str] | None = None) -> EvaluationSnapshot:
    held = held or {}
    views = tuple(InstrumentView(
        instrument=i, daily_bars=(Bar(date(2026, 9, 30), Decimal(c), Decimal(c), Decimal(c), Decimal(c), Decimal("1")),),
        position=PositionView(Decimal(held[i]), Decimal("9000"), 3, True) if i in held else None,
        data_ok=i not in (bad or set())) for i, c in closes.items())
    return EvaluationSnapshot(evaluation_id_of("t-cross", 1, "M1", "2026-10-01T09:05"), "M1", ACCOUNT, T0,
                              Decimal("10000000"), views, next_at)


def test_runner_produces_deterministic_signals_and_filters() -> None:
    strat = CrossStrategy()
    params = validate_params(strat.spec, PARAMS)
    snap = _snapshot({"TST-A": "9500", "TST-B": "7000", "TST-C": "9500"}, held={"TST-B": "10"}, bad={"TST-C"})
    r1 = run_evaluation(strat, snap, params)
    r2 = run_evaluation(strat, snap, params)
    assert [s.signal_id for s in r1.signals] == [s.signal_id for s in r2.signals]
    assert [(s.kind, s.instrument) for s in r1.signals] == [(RequestType.EXIT, "TST-B"), (RequestType.ENTRY, "TST-A")]
    assert all(s.instrument != "TST-C" for s in r1.signals)


def test_runner_enforces_one_signal_per_symbol_with_risk_reduction_first() -> None:
    class Noisy(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            from trading_system.strategy.base import make_signal
            return [make_signal(self.spec, snapshot, "TST-A", "ENTRY-1", RequestType.ENTRY, Decimal("1"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5)),
                    make_signal(self.spec, snapshot, "TST-A", "EXIT-1", RequestType.EXIT, Decimal("10"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5))]
    snap = _snapshot({"TST-A": "1"}, held={"TST-A": "10"})
    r = run_evaluation(Noisy(), snap, {})
    assert [s.kind for s in r.signals] == [RequestType.EXIT]
    assert any("종목당 1개" in d.reason for d in r.dropped)


@pytest.mark.parametrize(("valid_for", "next_at", "ok"), [
    (timedelta(minutes=30), None, True),
    (timedelta(minutes=31), None, False),                   # 절대 상한 30분 초과
    (timedelta(minutes=20), T0 + timedelta(minutes=10), False),  # 다음 예정 판정 이후
])
def test_runner_validity_caps(valid_for: timedelta, next_at: datetime | None, ok: bool) -> None:
    class Fixed(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            from trading_system.strategy.base import make_signal
            return [make_signal(self.spec, snapshot, "TST-A", "ENTRY-1", RequestType.ENTRY, Decimal("1"),
                                OrderType.MARKET, Decimal("1"), valid_for)]
    r = run_evaluation(Fixed(), _snapshot({"TST-A": "1"}, next_at=next_at), {})
    assert bool(r.signals) is ok


def test_runner_strategy_exception_yields_no_signals() -> None:
    class Broken(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            raise ZeroDivisionError("x")
    r = run_evaluation(Broken(), _snapshot({"TST-A": "1"}), {})
    assert r.signals == [] and "ZeroDivisionError" in (r.error or "")


def test_runner_rejects_foreign_or_malformed_output() -> None:
    class Bad(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            from trading_system.strategy.base import make_signal
            other = cross_spec("other")
            return [make_signal(other, snapshot, "TST-A", "ENTRY-1", RequestType.ENTRY, Decimal("1"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5)),
                    make_signal(self.spec, snapshot, "ZZZ", "ENTRY-1", RequestType.ENTRY, Decimal("1"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5)),
                    make_signal(self.spec, snapshot, "TST-A", "NOPE", RequestType.ENTRY, Decimal("1"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5)),
                    make_signal(self.spec, snapshot, "TST-A", "EXIT-1", RequestType.EXIT, Decimal("5"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5)),
                    "not a signal"]
    r = run_evaluation(Bad(), _snapshot({"TST-A": "1"}, held={"TST-A": "10"}), {})
    assert r.signals == [] and len(r.dropped) == 5


def test_evaluation_and_signal_ids_are_deterministic() -> None:
    a = evaluation_id_of("t-cross", 1, "M1", "2026-10-01T09:05")
    assert a == evaluation_id_of("t-cross", 1, "M1", "2026-10-01T09:05")
    assert a != evaluation_id_of("t-cross", 2, "M1", "2026-10-01T09:05")


# --- 크기 산정 (00 §2.8) -------------------------------------------------------------------------

def test_sizing_methods_floor_to_lot() -> None:
    assert size_fixed_amount(Decimal("1000000"), Decimal("30000")) == Decimal("33")
    assert size_ratio(Decimal("10000000"), Decimal("0.05"), Decimal("30000")) == Decimal("16")
    assert size_risk(Decimal("100000"), Decimal("10000"), Decimal("9700")) == Decimal("333")
    assert size_risk(Decimal("100000"), Decimal("10000"), Decimal("10000")) == 0
    assert size_fixed_quantity(Decimal("15"), Decimal("10")) == Decimal("10")
    assert size_fixed_amount(Decimal("100"), Decimal("30000")) == 0
    with pytest.raises(TypeError):
        size_fixed_amount(1000.0, Decimal("1"))  # type: ignore[arg-type]


def test_strategy_package_is_pure() -> None:
    from tests.architecture.import_rules import check_tree
    assert [v for v in check_tree(ROOT / "src") if "strategy" in v.module] == []
    assert IntentState  # 표시용 import 사용
    assert UTC and replace  # 표시용


def test_registry_alone_rejects_risk_increase_of_stopped_strategy(env: Env) -> None:
    """K9와 별개로 레지스트리도 중지 전략의 위험 확대를 거부한다 (이중 방어)."""
    reg = StrategyRegistry(env.repo, env.clock, None)  # 정지 관리자 없음
    spec = register(reg, env)
    to_paper(reg, spec)
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-903", stop_reason=StopReason.USER)
    assert reg.signal_rejection("t-cross:1", RequestType.ENTRY)
    assert reg.signal_rejection("t-cross:1", RequestType.INCREASE)
    assert reg.signal_rejection("t-cross:1", RequestType.EXIT) is None  # 위험 축소는 K9 정지표가 판정


def _emitting(instrument: str):  # type: ignore[no-untyped-def]
    class Emit(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            from trading_system.strategy.base import make_signal
            return [make_signal(self.spec, snapshot, instrument, "ENTRY-1", RequestType.ENTRY, Decimal("1"),
                                OrderType.MARKET, Decimal("1"), timedelta(minutes=5))]
    return Emit


def test_runner_drops_instrument_outside_declared_universe() -> None:
    strat = _emitting("TST-Z")(cross_spec(universe=("TST-A",)))
    r = run_evaluation(strat, _snapshot({"TST-A": "1", "TST-Z": "1"}), {})
    assert r.signals == [] and "유니버스" in r.dropped[0].reason


def test_runner_drops_signal_on_failed_data_quality() -> None:
    strat = _emitting("TST-A")()
    r = run_evaluation(strat, _snapshot({"TST-A": "1"}, bad={"TST-A"}), {})
    assert r.signals == [] and "품질" in r.dropped[0].reason


@pytest.mark.parametrize("exc", [KeyError, ValueError, RuntimeError, ZeroDivisionError])
def test_runner_any_strategy_exception_yields_no_signals(exc: type[Exception]) -> None:
    class Broken(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            raise exc("x")
    r = run_evaluation(Broken(), _snapshot({"TST-A": "1"}), {})
    assert r.signals == [] and exc.__name__ in (r.error or "")
