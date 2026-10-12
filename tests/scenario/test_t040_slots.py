"""T-040 전략별 슬롯 (CP-034 D-197, 04 V-272·V-273, A-092 Q-7 미배정 슬롯 빌려 쓰기 금지).

- 배포 설정 `[[strategies]] l6_slots`(정수 ≥ 1). 없으면 슬롯 검사 없음(기존 동작).
- 시작 검사: 활성 전략 l6_slots 합 > 계좌 L6 → 시작 거부. 일부 전략만 배정 → 시작 거부 [제안: 미배정 전략이 남는 슬롯을
  빌려 쓰는 것을 막기 위해].
- 게이트 G7: 신규 진입 시 그 전략의 (소유 보유 종목 + 진입 예약 종목, R-28) + 1 > l6_slots → 거부("전략 슬롯").
  계좌 L6 검사는 그대로. 위험 축소는 슬롯과 무관.
가상 값: 기준 금액 10,000,000원, TST-A~E 10,000원, 계좌 L6 5 (scenario_limits).
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, T0, Env
from tests.support.gate_fakes import TEST_POLICY
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.app_file import StrategyDeploy, check_strategy_slots, parse_app_settings
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import Check, GateResult, Mode, OrderType, RequestType
from trading_system.domain.models import GateDecision, GateRequest, Signal
from trading_system.gate.gate import Gate

ROOT = Path(__file__).resolve().parents[2]
SLOTS = {"s-a:1": 2, "s-b:1": 1}


def _req(strategy: str, inst: str, kind: RequestType = RequestType.ENTRY, qty: str = "10",
         ev: str | None = None) -> GateRequest:
    return GateRequest.from_signal(Signal(
        strategy_id=strategy, strategy_version=1, evaluation_id=ev or f"{strategy}-{inst}-{kind.value}",
        account=ACCOUNT, instrument=inst, rule_id="R1", kind=kind, quantity=Decimal(qty),
        order_type=OrderType.MARKET, price=Decimal("10000"), as_of=T0 - timedelta(minutes=1),
        valid_until=T0 + timedelta(hours=8)))


def _slot_gate(env: Env, slots: dict[str, int] | None = SLOTS) -> None:
    env.gate = Gate(env.repo, env.clock, dataclasses.replace(env.facts.inputs(), strategy_slots=slots), TEST_POLICY)


def _run(env: Env, req: GateRequest) -> GateDecision:
    d = env.gate.decide(req)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def _slot_rejected(d: GateDecision) -> bool:
    return d.result is GateResult.REJECTED and d.failed_check is Check.G7 and "전략 슬롯" in (d.reason or "")


# --- V-272 시작 검사 ------------------------------------------------------------------------------

def _deploy(slots: int | None, name: str = "a") -> StrategyDeploy:
    return StrategyDeploy(f"pkg.{name}:S", ROOT / "config" / f"{name}.toml", (), None, slots)


@pytest.mark.parametrize(("slots", "l6", "ok"), [((1, 2), 3, True), ((1, 2), 4, True), ((2, 2), 3, False),
                                                 ((3,), 3, True), ((4,), 3, False)])
def test_v272_slot_sum_vs_account_l6(slots: tuple[int, ...], l6: int, ok: bool) -> None:
    deps = tuple(_deploy(s, f"s{i}") for i, s in enumerate(slots))
    if ok:
        check_strategy_slots(deps, l6)
    else:
        with pytest.raises(ConfigError, match="l6_slots"):
            check_strategy_slots(deps, l6)


def test_v272_no_slots_means_no_check() -> None:
    check_strategy_slots((_deploy(None, "a"), _deploy(None, "b")), 1)


def test_v272_partial_assignment_rejected() -> None:
    with pytest.raises(ConfigError, match="l6_slots"):
        check_strategy_slots((_deploy(1, "a"), _deploy(None, "b")), 3)


def _app_raw(**strategy: object) -> dict:
    return {"app": {"account": "KR-BT-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                    "sntp_servers": ["time.example:123"]},
            "strategies": [{"module": "trading_system.strategy.paper0_verify:Paper0VerifyV2",
                            "params": "config/strategies/paper0-verify-v2.toml", "eval_times": ["09:30"], **strategy}]}


def test_v272_app_file_l6_slots_parsed() -> None:
    app = parse_app_settings(_app_raw(l6_slots=2), Mode.BACKTEST, ROOT)
    assert app.strategies[0].l6_slots == 2
    assert parse_app_settings(_app_raw(), Mode.BACKTEST, ROOT).strategies[0].l6_slots is None


@pytest.mark.parametrize("bad", [0, -1, "2", True, 1.0])
def test_v272_app_file_l6_slots_format(bad: object) -> None:
    with pytest.raises(ConfigError, match="l6_slots"):
        parse_app_settings(_app_raw(l6_slots=bad), Mode.BACKTEST, ROOT)


def test_v272_real_paper_app_has_no_slots_and_passes() -> None:
    """M1 PAPER 불변: 실제 app.paper.toml에는 l6_slots가 없다 → 슬롯 검사 없음."""
    from trading_system.config.app_file import load_app_settings
    from trading_system.config.limits_file import load_limits_file
    app = load_app_settings(ROOT / "config" / "app.paper.toml", Mode.PAPER, ROOT)
    assert [s.l6_slots for s in app.strategies] == [None] * len(app.strategies)
    check_strategy_slots(app.strategies, load_limits_file(ROOT / "config" / "limits.paper.toml").limits.l6_max_positions)


def test_v272_build_engine_checks_slot_sum_and_wires_gate(tmp_path: Path) -> None:
    from tests.scenario.test_t020_app import DAY, SETTINGS, ClockOk, kst, make_store
    from tests.support.gate_fakes import scenario_limits
    from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, CrossStrategy
    from trading_system.app.wiring import StrategyInput, build_engine
    from trading_system.broker.fake.broker import FakeBroker
    from trading_system.domain.clock import FixedClock
    from trading_system.storage.repository import Repository

    repo = Repository(tmp_path / "x.db", Mode.BACKTEST)
    clock = FixedClock(kst(DAY, 8, 0))
    common = dict(mode=Mode.BACKTEST, repo=repo, clock=clock, broker=FakeBroker(), store=make_store(clock),
                  account=ACCOUNT, gate_settings=SETTINGS, kill_file=tmp_path / "K", clock_health=ClockOk(),
                  caps=CAPS_ALL_SUPPORTED)
    strat = CrossStrategy()
    with pytest.raises(ConfigError, match="l6_slots"):
        build_engine(account_limits=scenario_limits(l6_max_positions=2),   # type: ignore[arg-type]
                     strategies=[StrategyInput(strat, dict(PARAMS), (), l6_slots=3)], **common)
    e = build_engine(account_limits=scenario_limits(l6_max_positions=3),    # type: ignore[arg-type]
                     strategies=[StrategyInput(strat, dict(PARAMS), (), l6_slots=3)], **common)
    assert e.gate._in.strategy_slots == {strat.spec.owner: 3}   # noqa: SLF001
    repo.close()


# --- V-273 전략 슬롯 게이트 ---------------------------------------------------------------------------

def test_v273_strategy_slots_full_rejects_only_that_strategy(env: Env) -> None:
    _slot_gate(env)
    assert _run(env, _req("s-a", "TST-A")).result is GateResult.APPROVED     # A 보유 1 (체결)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert _run(env, _req("s-a", "TST-B")).result is GateResult.APPROVED     # A 진입 예약 1 (미체결)
    assert _slot_rejected(env.gate.decide(_req("s-a", "TST-C")))             # A 2/2 → 거부
    assert _run(env, _req("s-b", "TST-C")).result is GateResult.APPROVED     # B는 자기 슬롯 안에서 승인
    # 계좌 L6(5)에는 2자리가 남았지만 미배정 슬롯은 아무도 쓰지 않는다 (A-092 Q-7)
    assert _slot_rejected(env.gate.decide(_req("s-b", "TST-D")))
    assert _slot_rejected(env.gate.decide(_req("s-a", "TST-E")))


def test_v273_exit_frees_slot_and_risk_reduction_not_blocked(env: Env) -> None:
    _slot_gate(env, {"s-a:1": 1})
    assert _run(env, _req("s-a", "TST-A")).result is GateResult.APPROVED
    assert _slot_rejected(env.gate.decide(_req("s-a", "TST-B")))
    x = _run(env, _req("s-a", "TST-A", RequestType.EXIT))                    # 슬롯이 차도 청산은 허용
    assert x.result is GateResult.APPROVED, x.reason
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0
    assert _run(env, _req("s-a", "TST-B", ev="again")).result is GateResult.APPROVED


def test_v273_increase_not_counted_as_new_slot(env: Env) -> None:
    _slot_gate(env, {"s-a:1": 1})
    assert _run(env, _req("s-a", "TST-A")).result is GateResult.APPROVED
    d = _run(env, _req("s-a", "TST-A", RequestType.INCREASE))
    assert d.result is GateResult.APPROVED, d.reason


def test_v273_strategy_without_slot_rejected_when_slots_configured(env: Env) -> None:
    _slot_gate(env)
    d = env.gate.decide(_req("s-z", "TST-A"))
    assert _slot_rejected(d) and "미배정" in (d.reason or "")


def test_v273_no_slot_config_keeps_account_l6_only(env: Env) -> None:
    _slot_gate(env, None)
    for i, inst in enumerate(("TST-A", "TST-B", "TST-C", "TST-D", "TST-E")):
        assert _run(env, _req("s-a", inst, qty="1")).result is GateResult.APPROVED, i


def test_v273_account_l6_still_applies(env: Env) -> None:
    from tests.support.gate_fakes import scenario_limits
    env.facts.limits.by_account[ACCOUNT] = scenario_limits(l6_max_positions=2)
    _slot_gate(env, {"s-a:1": 1, "s-b:1": 1})
    assert _run(env, _req("s-a", "TST-A")).result is GateResult.APPROVED
    assert _run(env, _req("s-b", "TST-B")).result is GateResult.APPROVED
    d = env.gate.decide(_req("s-b", "TST-C"))
    assert d.failed_check is Check.G7 and "L6" in (d.reason or "")         # 계좌 L6이 먼저


def test_v273_concurrent_signals_respect_strategy_slots(env: Env) -> None:
    """V-04 방식: 같은 전략의 동시 신호 3개(슬롯 2) → 판정 직렬화·예약으로 정확히 2개 승인."""
    _slot_gate(env)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    results: list[GateDecision] = []

    def go(inst: str) -> None:
        results.append(env.gate.decide(_req("s-a", inst)))

    threads = [threading.Thread(target=go, args=(i,)) for i in ("TST-A", "TST-B", "TST-C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    approved = [d for d in results if d.result is GateResult.APPROVED]
    assert len(approved) == 2 and sum(1 for d in results if _slot_rejected(d)) == 1
    for d in approved:
        env.executor.submit(d.approved)   # type: ignore[arg-type]
    env.executor.process_events()
