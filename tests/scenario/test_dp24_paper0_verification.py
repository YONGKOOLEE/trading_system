"""DP-24 해법 (a) — PAPER-0 검증 전략에만 "검증 중" 능력 허용 (사용자 승인 A-037, 04 §4.1, DP-1 예외).

- 사용자 승인 A-ID가 있어야 하고, 범위를 좁게 강제한다: M1 한정, 고정 수량(S-4), 종목 목록 명시(U-A),
  시장가만(지정가·미체결 정책·레버리지 능력 금지), 전략 1회 주문 금액 한도(L7) 필수.
- 승인이 없는 전략, BACKTEST·LIVE 대상, "미지원" 능력은 기존 규칙(DP-1) 그대로 거부.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t007_strategy import wire
from tests.support.gate_fakes import scenario_limits
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, cross_spec
from trading_system.config.strategy_registry import PAPER0_CAPABILITIES, validate_spec
from trading_system.domain.enums import Mode
from trading_system.domain.strategy import StrategyLimits

VERIFYING = {k: {"M1": "검증 중", "M2": "미지원", "M3": "미지원"} for k in CAPS_ALL_SUPPORTED}
APPROVAL = "A-037"


def paper0_spec(**over: object):  # type: ignore[no-untyped-def]
    base = {"strategy_id": "paper0-verify", "universe": ("005930",),
            "limits": StrategyLimits(l7_max_order_amount=Decimal("100000"))}
    base.update(over)
    return cross_spec(**base)


def test_without_approval_paper_target_still_needs_supported() -> None:
    problems = validate_spec(paper0_spec(), VERIFYING, scenario_limits(), Mode.PAPER)
    assert any("검증 중" in p for p in problems)


def test_with_approval_conforming_spec_passes() -> None:
    assert validate_spec(paper0_spec(), VERIFYING, scenario_limits(), Mode.PAPER,
                         verification_approval=APPROVAL) == []


@pytest.mark.parametrize(("over", "needle"), [
    ({"markets": ("M1", "M2")}, "M1"),
    ({"sizing": "S-2"}, "S-4"),
    ({"universe_method": "U-C", "universe": ()}, "U-A"),
    ({"capabilities": ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-01")}, "CAP-ORD-02"),
    ({"limits": StrategyLimits()}, "l7_max_order_amount"),
])
def test_with_approval_scope_is_enforced(over: dict, needle: str) -> None:
    problems = validate_spec(paper0_spec(**over), VERIFYING, scenario_limits(), Mode.PAPER,
                             verification_approval=APPROVAL)
    assert any(needle in p for p in problems), problems


@pytest.mark.parametrize("approval", ["", "A-", "X-037", "a-037"])
def test_bad_approval_id_rejected(approval: str) -> None:
    problems = validate_spec(paper0_spec(), VERIFYING, scenario_limits(), Mode.PAPER,
                             verification_approval=approval)
    assert problems


@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.LIVE])
def test_approval_only_for_paper_target(mode: Mode) -> None:
    problems = validate_spec(paper0_spec(), VERIFYING, scenario_limits(), mode, verification_approval=APPROVAL)
    assert any("PAPER" in p for p in problems)


def test_unsupported_capability_still_rejected_with_approval() -> None:
    caps = dict(VERIFYING)
    caps["CAP-DAT-01"] = {"M1": "미지원", "M2": "미지원", "M3": "미지원"}
    problems = validate_spec(paper0_spec(), caps, scenario_limits(), Mode.PAPER, verification_approval=APPROVAL)
    assert any("CAP-DAT-01" in p for p in problems)


def test_allowed_capability_set_is_market_order_only() -> None:
    assert PAPER0_CAPABILITIES == frozenset({"CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"})


def test_registration_records_verification_approval(env: Env) -> None:
    reg = wire(env)
    r = reg.register(paper0_spec(), dict(PARAMS), VERIFYING, env.facts.limits.by_account[ACCOUNT], Mode.PAPER,
                     verification_approval=APPROVAL)
    assert r.ok, r.problems
    row = env.repo.latest_registration("paper0-verify", 1)
    assert row["target_mode"] == "PAPER"
    assert json.loads(row["spec_json"])["verification_approval"] == APPROVAL
