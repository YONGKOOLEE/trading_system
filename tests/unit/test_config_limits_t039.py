"""T-039 정책 프로필 (CP-034 D-196, 04 V-270·V-271 중 설정·변경 규칙 부분, CP-032 D-185 틀).

- 한도 파일 최상위 `profile = "<이름>"`(없으면 legacy = 기존 동작), `approved_by = "A-xxx"`(승인된 프로필 표시).
- L7 형식: 금액 `l7_max_order_amount` 또는 기준 금액 대비 비율 `l7_max_order_ratio` — 둘 다면 시작 거부.
- 프로필 전환 = 한도 변경(01 §2.3.1): 엄격/완화는 기존 값 비교 방식, 원격 불가.
값은 시험용 가상 값이다(paper1 등 프로필 값은 Q-6 미확정).
"""

from __future__ import annotations

import copy
import dataclasses
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.gate_fakes import scenario_limits
from tests.support.limits_fixtures import VALID
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import parse_account_limits
from trading_system.config.versioning import ChangeDirection, classify
from trading_system.domain.limits import (
    LEGACY_PROFILE, UNAPPROVED_LABEL, AccountLimits, profile_approval_label, profile_label,
)

ROOT = Path(__file__).resolve().parents[2]


def _raw(**limits: object) -> dict:
    raw = copy.deepcopy(VALID)
    raw["limits"].update(limits)  # type: ignore[union-attr]
    return raw


def _top(**top: object) -> dict:
    raw = copy.deepcopy(VALID)
    raw.update(top)
    return raw


def _no_amount(raw: dict) -> dict:
    del raw["limits"]["l7_max_order_amount"]
    return raw


def _problems(raw: dict) -> str:
    with pytest.raises(ConfigError) as exc:
        parse_account_limits(raw)
    return "\n".join(exc.value.problems)


# --- 프로필 이름·승인 표시 -------------------------------------------------------------------------

def test_profile_absent_is_legacy_and_canonical_unchanged() -> None:
    lim = parse_account_limits(VALID).limits
    assert (lim.profile, lim.approved_by) == (None, None)
    assert profile_label(lim) == LEGACY_PROFILE and profile_approval_label(lim) == UNAPPROVED_LABEL
    assert not {"profile", "approved_by", "l7_max_order_ratio"} & set(lim.to_canonical())


def test_profile_and_approval_parsed_and_recorded_in_canonical() -> None:
    lim = parse_account_limits(_top(profile="paper0", approved_by="A-034")).limits
    assert (lim.profile, lim.approved_by) == ("paper0", "A-034")
    c = lim.to_canonical()
    assert (c["profile"], c["approved_by"]) == ("paper0", "A-034")
    assert AccountLimits.from_canonical(c) == lim
    assert profile_label(lim) == "paper0" and "A-034" in profile_approval_label(lim)
    assert lim.version_hash() != parse_account_limits(VALID).limits.version_hash()


def test_profile_without_approval_is_unapproved() -> None:
    lim = parse_account_limits(_top(profile="research")).limits
    assert profile_label(lim) == "research" and profile_approval_label(lim) == UNAPPROVED_LABEL


@pytest.mark.parametrize("name", ["", " ", "Paper0", "paper 0", "가나", "x" * 33, 1, True])
def test_profile_name_format_rejected(name: object) -> None:
    assert "profile" in _problems(_top(profile=name))


@pytest.mark.parametrize("aid", ["", "A34", "B-034", "A-", "a-034", 34])
def test_approved_by_format_rejected(aid: object) -> None:
    assert "approved_by" in _problems(_top(profile="paper0", approved_by=aid))


def test_approved_by_requires_profile() -> None:
    assert "approved_by" in _problems(_top(approved_by="A-034"))


# --- L7 금액·비율 --------------------------------------------------------------------------------

def test_l7_ratio_instead_of_amount() -> None:
    lim = parse_account_limits(_no_amount(_raw(l7_max_order_ratio="0.10"))).limits
    assert lim.l7_max_order_amount is None and lim.l7_max_order_ratio == Decimal("0.10")
    assert lim.l7_cap(Decimal("10000000")) == Decimal("1000000")
    c = lim.to_canonical()
    assert "l7_max_order_amount" not in c and c["l7_max_order_ratio"] == "0.1"
    assert AccountLimits.from_canonical(c) == lim


def test_l7_amount_cap_unchanged() -> None:
    lim = parse_account_limits(VALID).limits
    assert lim.l7_max_order_ratio is None and lim.l7_cap(Decimal("123")) == Decimal("1000000")


def test_l7_amount_and_ratio_both_rejected() -> None:
    text = _problems(_raw(l7_max_order_ratio="0.10"))
    assert "l7_max_order_amount" in text and "l7_max_order_ratio" in text and "하나만" in text


def test_l7_neither_rejected() -> None:
    assert "l7_max_order_amount" in _problems(_no_amount(copy.deepcopy(VALID)))


@pytest.mark.parametrize("value", ["0", "-0.1", 0.1, 1, "x"])
def test_l7_ratio_invalid_rejected(value: object) -> None:
    assert "l7_max_order_ratio" in _problems(_no_amount(_raw(l7_max_order_ratio=value)))


def test_account_limits_requires_exactly_one_l7_form() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(scenario_limits(), l7_max_order_ratio=Decimal("0.1"))
    with pytest.raises(ValueError):
        dataclasses.replace(scenario_limits(), l7_max_order_amount=None)


# --- 변경 방향 (01 §2.3.1 기존 값 비교 재사용) --------------------------------------------------------

def _ratio_limits(ratio: str) -> AccountLimits:
    return dataclasses.replace(scenario_limits(), l7_max_order_amount=None, l7_max_order_ratio=Decimal(ratio))


@pytest.mark.parametrize(("old", "new", "expected"), [
    (_ratio_limits("0.10"), _ratio_limits("0.05"), ChangeDirection.TIGHTENED),
    (_ratio_limits("0.05"), _ratio_limits("0.10"), ChangeDirection.LOOSENED),
    (scenario_limits(), _ratio_limits("0.01"), ChangeDirection.LOOSENED),    # 형식 전환은 비교 불가 → 완화로 봄
    (_ratio_limits("0.10"), scenario_limits(), ChangeDirection.LOOSENED),
    (scenario_limits(), dataclasses.replace(scenario_limits(), profile="paper1"), ChangeDirection.NEUTRAL),
])
def test_l7_and_profile_change_direction(old: AccountLimits, new: AccountLimits, expected: ChangeDirection) -> None:
    assert classify(old, new) is expected


def test_real_paper_file_with_paper0_profile_equals_current_values() -> None:
    """paper0 프로필 = 지금 limits.paper.toml 값(A-034) 그대로 (D-196). 파일은 바꾸지 않는다 — 읽어서 비교만."""
    raw = tomllib.loads((ROOT / "config" / "limits.paper.toml").read_text(encoding="utf-8"))
    current = parse_account_limits(raw).limits
    assert (current.profile, current.approved_by, current.l7_max_order_ratio) == (None, None, None)
    as_profile = parse_account_limits({**raw, "profile": "paper0", "approved_by": "A-034"}).limits
    assert dataclasses.replace(as_profile, profile=None, approved_by=None) == current
    assert classify(current, as_profile) is ChangeDirection.NEUTRAL
