"""T-006 fake 브로커 장애 주입·무작위 사건열 (05-plan T-006, 04 V-95·D-070, 03 D-062).

- 지정 시나리오: DSL로 장애(타임아웃·부분 체결·거부·단절·중복·순서 뒤바뀜)를 재현하고 기대 결과를 본다.
- 무작위 사건열: 시드 200개 × 사건 50개 (사용자 결정 2026-10-02 "결정은 권장대로"). 매 실행 끝에 불변 조건과
  안전 속성(재전송 없음, 정지·KILL 중 위험 확대 없음)을 검사한다. 위반하면 시드와 명령열을 출력해 재현한다.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, build_env
from tests.support.invariants import check_invariants
from tests.support.scenario_dsl import Runner, random_script
from trading_system.domain.enums import IntentState, Mode

SEEDS = 200
EVENTS = 50


def _intents(env: Env) -> list:
    return env.repo.intents()


# --- 지정 시나리오 ----------------------------------------------------------------------------

def test_duplicate_and_shuffled_partial_fills_counted_once(env: Env) -> None:
    r = Runner(env)
    r.run("""
        behavior ACCEPT_ONLY
        entry TST-A 100
        fill 0 30
        fill 0 30
        fill 0
        dup
        shuffle 7
        process
    """)
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")
    [i] = _intents(env)
    assert i.state is IntentState.FILLED and i.filled_quantity == Decimal("100")


def test_cancel_confirmation_before_fill_keeps_order_closed(env: Env) -> None:
    """순서 뒤바뀐 통보: 취소 확인이 먼저 오고 체결이 나중에 와도 종결된 의도·주문을 다시 열지 않는다 (INV-11)."""
    r = Runner(env)
    r.run("""
        behavior ACCEPT_ONLY
        entry TST-A 100
        fill 0 40
        cancel 0
        shuffle 3
        process
    """)
    [entry, cancel] = _intents(env)
    assert entry.state is IntentState.CANCELLED
    order = env.repo.broker_order(entry.broker_order_key)
    assert order.state == "CANCELLED"
    assert env.repo.open_orders(ACCOUNT) == []
    assert env.repo.position(ACCOUNT, "TST-A").quantity in (Decimal("0"), Decimal("40"))


def test_connection_drop_after_send_is_unknown_then_found(env: Env) -> None:
    r = Runner(env)
    r.run("""
        behavior ACCEPT_ONLY
        raise-after-send on
        entry TST-A 100
        raise-after-send off
        resolve
    """)
    [i] = _intents(env)
    assert i.state is IntentState.ACCEPTED
    assert len(env.broker.sent) == 1


def test_disconnect_is_pre_send_failure_and_query_during_disconnect_goes_k3(env: Env) -> None:
    r = Runner(env)
    r.run("""
        disconnect on
        entry TST-A 100
        disconnect off
        behavior UNKNOWN
        entry TST-B 100
        disconnect on
        resolve
    """)
    a, b = _intents(env)
    assert a.state is IntentState.NOT_SENT
    assert b.state is IntentState.UNKNOWN and "K3" in env.repo.active_halts(ACCOUNT)


def test_reject_and_restart_scenario(env: Env) -> None:
    r = Runner(env)
    r.run("""
        behavior REJECT
        entry TST-A 10
        behavior FILL
        restart
        entry TST-B 10
        report K8 recovery_ok 1
        resume K8 LOCAL
        entry TST-C 10
        process
    """)
    states = [i.state for i in _intents(env)]
    assert states == [IntentState.REJECTED, IntentState.FILLED]  # K8 중 진입은 게이트에서 거부(의도 없음)


# --- 무작위 사건열 (V-95) ----------------------------------------------------------------------

def _run_seed(seed: int, base: Path) -> list[str]:
    tmp = base / f"s{seed}"
    tmp.mkdir()
    env = build_env(tmp)
    runner = Runner(env)
    script = random_script(seed, EVENTS)
    problems: list[str] = []
    try:
        runner.run(script)
        runner.step("process")
    except Exception as exc:  # 예상하지 못한 예외도 위반으로 기록
        problems.append(f"예외 {type(exc).__name__}: {exc}")
    env.repo.fail_next_transaction = False
    problems += runner.violations
    problems += check_invariants(env.repo.connection, Mode.BACKTEST.value)
    attempts = env.repo.connection.execute("SELECT COUNT(*) FROM attempt WHERE state <> 'NOT_SENT'").fetchone()[0]
    if len(env.broker.sent) > attempts:
        problems.append(f"재전송 의심: 브로커 수신 {len(env.broker.sent)} > 전송 시도 {attempts}")
    env.repo.close()
    if problems:
        return [f"seed={seed} 위반={problems}\n  명령열:\n    " + "\n    ".join(script)]
    return []


@pytest.mark.slow
def test_v95_random_event_sequences_keep_invariants(tmp_path: Path) -> None:
    failures: list[str] = []
    for seed in range(SEEDS):
        failures += _run_seed(seed, tmp_path)
    assert not failures, f"{len(failures)}/{SEEDS} 시드 위반\n" + "\n".join(failures[:5])


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_random_runs_are_reproducible(tmp_path: Path, seed: int) -> None:
    assert random_script(seed, EVENTS) == random_script(seed, EVENTS)
