"""CP-034 한도 파일 확장 (T-038 D-195 `l1_entry_buffer_ratio`).

값은 시험용 가상 값이다. 승인된 리스크 수치가 아니다.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.gate_fakes import scenario_limits
from tests.support.invariants import check_invariants
from tests.support.limits_fixtures import VALID
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import parse_account_limits
from trading_system.config.versioning import ChangeDirection, ChangeSource, SettingsManager, SettingsOutcome, classify
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.limits import AccountLimits
from trading_system.storage.repository import Repository

BUF = Decimal("0.03")


def _raw(**limits: object) -> dict:
    raw = copy.deepcopy(VALID)
    raw["limits"].update(limits)  # type: ignore[union-attr]
    return raw


def _problems(raw: dict) -> str:
    with pytest.raises(ConfigError) as exc:
        parse_account_limits(raw)
    return "\n".join(exc.value.problems)


# --- T-038 l1_entry_buffer_ratio (D-195) ----------------------------------------------------------

def test_buffer_absent_is_none_and_canonical_unchanged() -> None:
    lim = parse_account_limits(VALID).limits
    assert lim.l1_entry_buffer_ratio is None
    assert "l1_entry_buffer_ratio" not in lim.to_canonical()     # 해시 불변 (기존 파일·PAPER)


@pytest.mark.parametrize("value", ["0", "0.03", "0.199"])
def test_buffer_valid_values(value: str) -> None:
    lim = parse_account_limits(_raw(l1_entry_buffer_ratio=value)).limits
    assert lim.l1_entry_buffer_ratio == Decimal(value)
    assert lim.to_canonical()["l1_entry_buffer_ratio"] == format(Decimal(value).normalize(), "f")
    assert AccountLimits.from_canonical(lim.to_canonical()) == lim


@pytest.mark.parametrize("value", ["0.2", "0.25", "-0.01", "abc", 0.03, 0, True, "1e-2"])
def test_buffer_invalid_values_rejected(value: object) -> None:
    assert "l1_entry_buffer_ratio" in _problems(_raw(l1_entry_buffer_ratio=value))


def test_buffer_changes_hash() -> None:
    a = parse_account_limits(VALID).limits
    b = parse_account_limits(_raw(l1_entry_buffer_ratio="0.03")).limits
    assert a.version_hash() != b.version_hash()


def test_from_canonical_without_buffer_key() -> None:
    """구현 전 기록(설정 버전 values_json)에는 키가 없다 → None으로 읽는다."""
    lim = parse_account_limits(VALID).limits
    data = lim.to_canonical()
    assert AccountLimits.from_canonical(data).l1_entry_buffer_ratio is None


def test_adding_buffer_is_loosening_deferred_and_audited(tmp_path: Path) -> None:
    """여유 키 추가 = L1 완화 → 다음 일일 기준 시각부터 (A-092, 01 §2.3.1). 변경 기록에 키 추가가 남는다."""
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    clock = FixedClock(datetime(2026, 10, 1, 10, 0, tzinfo=KST))
    mgr = SettingsManager(repo, clock)
    base = scenario_limits()
    mgr.submit_account_limits("KR-BT-01", base, ChangeSource.LOCAL)
    looser = dataclasses.replace(base, l1_entry_buffer_ratio=BUF)
    r = mgr.submit_account_limits("KR-BT-01", looser, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.SCHEDULED and r.direction is ChangeDirection.LOOSENED
    assert mgr.effective_limits("KR-BT-01") == base
    event = [e for e in repo.audit_events() if e["kind"] == "SETTINGS_CHANGE"][-1]
    assert "l1_entry_buffer_ratio" in event["summary"]
    clock.advance(timedelta(hours=22))   # 다음 날 08:00
    assert mgr.effective_limits("KR-BT-01") == looser
    back = mgr.submit_account_limits("KR-BT-01", base, ChangeSource.LOCAL)   # 여유 제거 = 엄격 → 즉시
    assert back.outcome is SettingsOutcome.APPLIED and mgr.effective_limits("KR-BT-01") == base
    assert check_invariants(repo.connection, Mode.BACKTEST.value) == []
    repo.close()


@pytest.mark.parametrize(("old", "new", "expected"), [
    (None, "0.03", ChangeDirection.LOOSENED),    # 상한가 기준 → 신호 기준가 + 여유: L1 완화 (A-092)
    ("0.03", None, ChangeDirection.TIGHTENED),
    ("0.03", "0.05", ChangeDirection.TIGHTENED), # 여유가 커지면 위험 추정이 커짐 = 엄격
    ("0.05", "0.03", ChangeDirection.LOOSENED),
    ("0.03", "0.03", ChangeDirection.NEUTRAL),
])
def test_buffer_change_direction(old: str | None, new: str | None, expected: ChangeDirection) -> None:
    base = scenario_limits()
    a = dataclasses.replace(base, l1_entry_buffer_ratio=None if old is None else Decimal(old))
    b = dataclasses.replace(base, l1_entry_buffer_ratio=None if new is None else Decimal(new))
    assert classify(a, b) is expected
