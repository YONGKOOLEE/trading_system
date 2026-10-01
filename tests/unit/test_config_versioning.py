"""설정 변경 규칙 (04 V-87, 01 §2.3.1-4·5·7, 02 ENT-08·INV-14).

강화는 즉시, 완화·혼합·일일 기준 시각 변경은 다음 일일 기준 시각부터 적용한다.
원격 변경은 거부하고 기록한다. 아래 수치는 테스트용 가상 값이다.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from trading_system.config.versioning import ChangeDirection, ChangeSource, SettingsManager, SettingsOutcome, classify
from trading_system.domain.clock import KST, FixedClock, next_kst_time_after
from trading_system.domain.enums import Mode
from trading_system.domain.limits import AccountLimits
from trading_system.storage.repository import Repository

ACCOUNT = "KR-BT-01"

BASE = AccountLimits(
    l1_max_loss_ratio=Decimal("0.01"), l2_max_symbol_ratio=Decimal("0.20"), l3_max_total_ratio=Decimal("0.50"),
    l4_daily_loss_ratio=Decimal("0.02"), l5_daily_new_orders=10, l6_max_positions=5,
    l7_max_order_amount=Decimal("1000000"), l10_leverage_total_ratio=None, warn_usage_ratio=Decimal("0.80"),
    order_window_start=time(9, 5), order_window_end=time(15, 15),
    daily_reference_time=time(8, 0), report_time=time(16, 0),
)


def with_(**kw: object) -> AccountLimits:
    return dataclasses.replace(BASE, **kw)


def kst(h: int, m: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=KST)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
        self.clock = FixedClock(kst(10, 0))
        self.mgr = SettingsManager(self.repo, self.clock)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


# --- 방향 판정 ---------------------------------------------------------------

@pytest.mark.parametrize(("change", "expected"), [
    ({}, ChangeDirection.NEUTRAL),
    ({"l2_max_symbol_ratio": Decimal("0.10")}, ChangeDirection.TIGHTENED),
    ({"l2_max_symbol_ratio": Decimal("0.30")}, ChangeDirection.LOOSENED),
    ({"l1_max_loss_ratio": Decimal("0.02")}, ChangeDirection.LOOSENED),
    ({"l3_max_total_ratio": Decimal("0.40")}, ChangeDirection.TIGHTENED),
    ({"l4_daily_loss_ratio": Decimal("0.03")}, ChangeDirection.LOOSENED),
    ({"l5_daily_new_orders": 5}, ChangeDirection.TIGHTENED),
    ({"l6_max_positions": 6}, ChangeDirection.LOOSENED),
    ({"l7_max_order_amount": Decimal("500000")}, ChangeDirection.TIGHTENED),
    ({"warn_usage_ratio": Decimal("0.90")}, ChangeDirection.LOOSENED),   # 경고가 늦어짐
    ({"warn_usage_ratio": Decimal("0.70")}, ChangeDirection.TIGHTENED),
    ({"l10_leverage_total_ratio": Decimal("0.20")}, ChangeDirection.TIGHTENED),  # 한도 없음 → 한도 있음
    ({"order_window_start": time(9, 0)}, ChangeDirection.LOOSENED),   # 창이 넓어짐
    ({"order_window_end": time(15, 0)}, ChangeDirection.TIGHTENED),
    ({"order_window_start": time(9, 30), "order_window_end": time(15, 30)}, ChangeDirection.MIXED),  # 이동
    ({"l2_max_symbol_ratio": Decimal("0.10"), "l6_max_positions": 8}, ChangeDirection.MIXED),
    ({"report_time": time(17, 0)}, ChangeDirection.NEUTRAL),
    ({"daily_reference_time": time(7, 30)}, ChangeDirection.NEUTRAL),
])
def test_classify(change: dict, expected: ChangeDirection) -> None:
    assert classify(BASE, with_(**change)) is expected


def test_classify_l10_from_ratio_to_none_is_loosened() -> None:
    assert classify(with_(l10_leverage_total_ratio=Decimal("0.20")), BASE) is ChangeDirection.LOOSENED


# --- 다음 일일 기준 시각 계산 ------------------------------------------------

@pytest.mark.parametrize(("submitted", "expected"), [
    (kst(10, 0), kst(8, 0, day=2)),
    (kst(7, 0), kst(8, 0, day=1)),
    (kst(8, 0), kst(8, 0, day=2)),     # 정확히 기준 시각이면 다음 날
    (kst(23, 59), kst(8, 0, day=2)),
])
def test_next_reference_time(submitted: datetime, expected: datetime) -> None:
    assert next_kst_time_after(submitted, time(8, 0)) == expected.astimezone(UTC)


# --- 적용 시점 ---------------------------------------------------------------

def test_first_submission_is_initial_and_immediate(env: Env) -> None:
    r = env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED and r.direction is ChangeDirection.INITIAL
    assert env.mgr.effective_limits(ACCOUNT) == BASE


def test_tightening_applies_immediately(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    tighter = with_(l2_max_symbol_ratio=Decimal("0.10"))
    r = env.mgr.submit_account_limits(ACCOUNT, tighter, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED and r.direction is ChangeDirection.TIGHTENED
    assert r.effective_at == env.clock.now()
    assert env.mgr.effective_limits(ACCOUNT) == tighter


def test_loosening_is_deferred_to_next_reference_time(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    looser = with_(l3_max_total_ratio=Decimal("0.80"))
    r = env.mgr.submit_account_limits(ACCOUNT, looser, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.SCHEDULED and r.direction is ChangeDirection.LOOSENED
    assert r.effective_at == kst(8, 0, day=2).astimezone(UTC)
    assert env.mgr.effective_limits(ACCOUNT) == BASE  # 오늘은 그대로
    env.clock.advance(timedelta(hours=21, minutes=59))  # 07:59 다음 날
    assert env.mgr.effective_limits(ACCOUNT) == BASE
    env.clock.advance(timedelta(minutes=1))  # 08:00
    assert env.mgr.effective_limits(ACCOUNT) == looser


def test_mixed_is_treated_as_loosening(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    mixed = with_(l2_max_symbol_ratio=Decimal("0.10"), l6_max_positions=8)
    r = env.mgr.submit_account_limits(ACCOUNT, mixed, ChangeSource.LOCAL)
    assert r.direction is ChangeDirection.MIXED and r.outcome is SettingsOutcome.SCHEDULED
    assert env.mgr.effective_limits(ACCOUNT) == BASE


def test_reference_time_change_is_deferred_even_if_not_looser(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    r = env.mgr.submit_account_limits(ACCOUNT, with_(daily_reference_time=time(7, 30)), ChangeSource.LOCAL)
    assert r.direction is ChangeDirection.NEUTRAL and r.outcome is SettingsOutcome.SCHEDULED
    # 변경 전 기준 시각(08:00) 기준으로 다음 시각에 적용
    assert r.effective_at == kst(8, 0, day=2).astimezone(UTC)


def test_report_time_change_applies_immediately(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    r = env.mgr.submit_account_limits(ACCOUNT, with_(report_time=time(17, 0)), ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED and r.direction is ChangeDirection.NEUTRAL
    assert env.mgr.effective_limits(ACCOUNT).report_time == time(17, 0)


def test_same_values_resubmitted_is_unchanged(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    r = env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.UNCHANGED
    assert len(env.repo.settings_rows("ACCOUNT_LIMITS", ACCOUNT)) == 1


def test_resubmitting_pending_value_does_not_shorten_wait(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    looser = with_(l3_max_total_ratio=Decimal("0.80"))
    first = env.mgr.submit_account_limits(ACCOUNT, looser, ChangeSource.LOCAL)
    env.clock.advance(timedelta(minutes=5))
    again = env.mgr.submit_account_limits(ACCOUNT, looser, ChangeSource.LOCAL)
    assert again.outcome is SettingsOutcome.UNCHANGED
    assert env.mgr.effective_limits(ACCOUNT) == BASE
    assert first.effective_at > env.clock.now()


def test_looser_than_effective_cannot_skip_wait_by_going_through_pending(env: Env) -> None:
    """대기 중인 완화(B) 뒤에 B보다 엄격하지만 현재값(A)보다는 느슨한 C를 내도 즉시 적용되면 안 된다."""
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    env.mgr.submit_account_limits(ACCOUNT, with_(l3_max_total_ratio=Decimal("0.90")), ChangeSource.LOCAL)  # B
    c = with_(l3_max_total_ratio=Decimal("0.60"))
    r = env.mgr.submit_account_limits(ACCOUNT, c, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.SCHEDULED
    assert env.mgr.effective_limits(ACCOUNT) == BASE


def test_tightening_supersedes_pending_loosening(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    env.mgr.submit_account_limits(ACCOUNT, with_(l3_max_total_ratio=Decimal("0.90")), ChangeSource.LOCAL)  # 대기
    tighter = with_(l3_max_total_ratio=Decimal("0.30"))
    r = env.mgr.submit_account_limits(ACCOUNT, tighter, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED
    assert env.mgr.effective_limits(ACCOUNT) == tighter
    env.clock.advance(timedelta(days=3))  # 대기 중이던 완화가 되살아나면 안 된다
    assert env.mgr.effective_limits(ACCOUNT) == tighter


def test_resubmitting_effective_value_cancels_pending_loosening(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    env.mgr.submit_account_limits(ACCOUNT, with_(l3_max_total_ratio=Decimal("0.90")), ChangeSource.LOCAL)
    r = env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    assert r.outcome is SettingsOutcome.APPLIED and r.direction is ChangeDirection.NEUTRAL
    env.clock.advance(timedelta(days=3))
    assert env.mgr.effective_limits(ACCOUNT) == BASE


# --- 원격 변경 불가 (01 §2.3.1-5) -----------------------------------------------

def test_remote_change_rejected_and_recorded(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    r = env.mgr.submit_account_limits(ACCOUNT, with_(l2_max_symbol_ratio=Decimal("0.05")), ChangeSource.REMOTE)
    assert r.outcome is SettingsOutcome.REJECTED
    assert env.mgr.effective_limits(ACCOUNT) == BASE
    rows = env.repo.settings_rows("ACCOUNT_LIMITS", ACCOUNT)
    assert rows[-1]["result"] == "REJECTED" and rows[-1]["source"] == "REMOTE" and "원격" in rows[-1]["reason"]
    env.clock.advance(timedelta(days=3))
    assert env.mgr.effective_limits(ACCOUNT) == BASE


def test_remote_initial_setting_rejected_too(env: Env) -> None:
    r = env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.REMOTE)
    assert r.outcome is SettingsOutcome.REJECTED
    assert env.mgr.effective_limits(ACCOUNT) is None


# --- 기록 (01 §2.3.1-7) --------------------------------------------------------

def test_audit_records_before_after_and_hash(env: Env) -> None:
    env.mgr.submit_account_limits(ACCOUNT, BASE, ChangeSource.LOCAL)
    r = env.mgr.submit_account_limits(ACCOUNT, with_(l2_max_symbol_ratio=Decimal("0.10")), ChangeSource.LOCAL)
    event = [e for e in env.repo.audit_events() if e["kind"] == "SETTINGS_CHANGE"][-1]
    assert r.version_hash in event["ref"]
    assert "l2_max_symbol_ratio" in event["summary"] and "0.2" in event["summary"] and "0.1" in event["summary"]
    rows = env.repo.settings_rows("ACCOUNT_LIMITS", ACCOUNT)
    assert rows[-1]["previous_hash"] == rows[0]["version_hash"]


def test_hash_ignores_decimal_formatting(env: Env) -> None:
    a = env.mgr.submit_account_limits(ACCOUNT, with_(l2_max_symbol_ratio=Decimal("0.20")), ChangeSource.LOCAL)
    env2_hash = SettingsManager.hash_of(with_(l2_max_symbol_ratio=Decimal("0.2")))
    assert a.version_hash == env2_hash


def test_effective_limits_none_before_any_setting(env: Env) -> None:
    assert env.mgr.effective_limits(ACCOUNT) is None
    assert env.mgr.effective_limits("OTHER") is None


def test_accounts_are_independent(env: Env) -> None:
    env.mgr.submit_account_limits("KR-BT-01", BASE, ChangeSource.LOCAL)
    other = with_(l2_max_symbol_ratio=Decimal("0.05"))
    r = env.mgr.submit_account_limits("KR-BT-02", other, ChangeSource.LOCAL)
    assert r.direction is ChangeDirection.INITIAL
    assert env.mgr.effective_limits("KR-BT-01") == BASE
    assert env.mgr.effective_limits("KR-BT-02") == other
