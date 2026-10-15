"""CP-035 보유 중 전략 교체 거부 (A-104 "(가) 보유 중 교체 금지", D-200, V-278·V-279, DP-49).

- V-278: 교체 중지(STOPPED, 사유 REPLACE)는 그 전략 소유 보유·예약(소유권 RESERVED·CONFIRMED)이 있으면 거부.
  무보유면 허용. 거부는 상태·전이 기록·K9를 바꾸지 않는다. 사용자 판단 중지(USER)는 보유 중에도 허용(변경 없음).
- V-279: 배포 설정에서 빠진 전략이 보유·예약을 소유하면 시작 거부(DP-49). 사용자 판단 중지(USER)·폐기는 예외
  (그 보유는 운영 요청만 처리 — 01 K9, 배포 여부와 무관). 다른 계좌 보유는 보지 않는다.

가상 계좌·가상 승인 A-9xx만 쓴다. 네트워크 없음.
"""

from __future__ import annotations

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t007_strategy import register, run, sig, to_paper, wire
from tests.support.sample_strategies import cross_spec
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import GateResult, RequestType
from trading_system.domain.strategy import StopReason, StrategyState


def _state(env: Env, version: int = 1) -> dict:
    row = env.repo.strategy_state("t-cross", version, "M1")
    assert row is not None
    return row


def _k9(env: Env) -> list[dict]:
    return [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K9"]


# --- V-278 교체 중지 전이 ------------------------------------------------------------------------

def test_v278_replace_stop_refused_while_strategy_holds(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    assert env.repo.position(ACCOUNT, "TST-A").quantity > 0
    before = env.repo.strategy_transitions("t-cross", 1)
    with pytest.raises(ValueError) as exc:
        reg.transition("t-cross", 1, StrategyState.STOPPED, "A-931", stop_reason=StopReason.REPLACE)
    msg = str(exc.value)
    assert "보유 중 교체 금지" in msg and "A-104" in msg and "1건" in msg and "TST-A" in msg
    assert _state(env)["state"] == "PAPER"                               # 상태 불변
    assert env.repo.strategy_transitions("t-cross", 1) == before        # 전이 기록 없음
    assert _k9(env) == []                                                # K9 발동 없음
    assert env.repo.active_ownership(ACCOUNT, "TST-A").owner == "t-cross:1"


def test_v278_replace_stop_refused_while_entry_is_only_reserved(env: Env) -> None:
    """미체결 매수(소유권 RESERVED, 예약)만 있어도 거부 — '보유·예약'."""
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(spec, RequestType.ENTRY, "TST-B", ev="r")).result is GateResult.APPROVED
    [own] = env.repo.active_ownerships_of("t-cross:1")
    assert own["state"] == "RESERVED" and env.repo.position(ACCOUNT, "TST-B").quantity == 0
    with pytest.raises(ValueError, match="보유 중 교체 금지"):
        reg.transition("t-cross", 1, StrategyState.STOPPED, "A-932", stop_reason=StopReason.REPLACE)
    assert _state(env)["state"] == "PAPER" and _k9(env) == []


def test_v278_replace_stop_allowed_when_flat(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    assert run(env, sig(spec, RequestType.EXIT, "TST-A", ev="x")).result is GateResult.APPROVED
    assert env.repo.active_ownerships_of("t-cross:1") == []
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-933", stop_reason=StopReason.REPLACE)
    row = _state(env)
    assert row["state"] == "STOPPED" and row["stop_reason"] == "REPLACE"
    [k9] = _k9(env)
    assert k9["variant"] == "REPLACE" and k9["scope_owner"] == "t-cross:1"


def test_v278_user_stop_still_allowed_while_holding(env: Env) -> None:
    """사용자 판단 중지는 이 결정 범위 밖 — 보유 중에도 허용(보유는 운영 요청으로 정리, 01 K9)."""
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-934", stop_reason=StopReason.USER)
    assert _state(env)["state"] == "STOPPED" and _state(env)["stop_reason"] == "USER"


# --- V-279 시작 시 배포 설정 검사 (DP-49) ------------------------------------------------------------

def _holding_v1(env: Env):  # type: ignore[no-untyped-def]
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    return reg


def test_v279_undeployed_owner_with_holding_is_a_start_problem(env: Env) -> None:
    reg = _holding_v1(env)
    problems = reg.undeployed_holding_problems(ACCOUNT, {"t-cross:2"})
    assert len(problems) == 1
    assert "t-cross:1" in problems[0] and "PAPER" in problems[0] and "보유 중 교체 금지" in problems[0]
    assert reg.undeployed_holding_problems(ACCOUNT, {"t-cross:1"}) == []          # 배포 중이면 문제 없음
    assert reg.undeployed_holding_problems("KR-OTHER-01", set()) == []            # 다른 계좌 보유는 보지 않는다


def test_v279_user_stopped_owner_may_stay_undeployed(env: Env) -> None:
    reg = _holding_v1(env)
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-935", stop_reason=StopReason.USER)
    assert reg.undeployed_holding_problems(ACCOUNT, set()) == []                  # 운영 요청만 — 배포와 무관


def test_v279_flat_replaced_owner_may_stay_undeployed(env: Env) -> None:
    """오늘의 PAPER 배포와 같은 형태: v1 교체 중지(무보유)·배포 설정에서 빠짐 → 문제 없음."""
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-936", stop_reason=StopReason.REPLACE)
    v2 = register(reg, env, cross_spec(version=2))
    to_paper(reg, v2)
    assert reg.undeployed_holding_problems(ACCOUNT, {"t-cross:2"}) == []
