"""설정 버전과 변경 규칙 (01 §2.3.1-4·5·7, 02 ENT-08·INV-14, R-19).

- 더 엄격해지는 변경은 즉시, 완화·혼합·일일 기준 시각 변경은 다음 일일 기준 시각부터 적용한다.
- 방향은 **현재 적용 중인 값**과 비교한다. 대기 중인 완화를 거쳐 즉시 적용을 얻는 우회를 막는다.
- 나중에 제출된 버전이 대기 중인 이전 버전을 덮는다. 엄격한 변경 뒤에 대기 중이던 완화가 되살아나지 않는다.
- 원격 채널(메신저)의 변경은 거부하고 기록한다 (로컬 설정 파일만 허용).
- 변경마다 설정 해시, 변경 전후 값, 적용 시각을 감사 기록한다.

ENT-08의 변경 방향 열거(최초/강화/완화/혼합)에는 "위험 값 변경 없음(보고 시각만 바뀜 등)"이 없다.
이 경우를 NEUTRAL로 두었다. 02-data-model 개정이 필요하면 CP로 제안한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_system.domain.clock import Clock, next_kst_time_after, to_iso
from trading_system.domain.limits import AccountLimits
from trading_system.storage.repository import Repository

KIND = "ACCOUNT_LIMITS"


class ChangeDirection(StrEnum):
    INITIAL = "INITIAL"
    TIGHTENED = "TIGHTENED"
    LOOSENED = "LOOSENED"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"


class ChangeSource(StrEnum):
    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class SettingsOutcome(StrEnum):
    APPLIED = "APPLIED"
    SCHEDULED = "SCHEDULED"
    UNCHANGED = "UNCHANGED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ChangeResult:
    outcome: SettingsOutcome
    version_hash: str
    direction: ChangeDirection | None
    effective_at: datetime | None
    reason: str | None = None


_INFINITY = Decimal("Infinity")


def _sign(old: object, new: object, *, larger_is_looser: bool) -> int:
    """+1 완화, -1 강화, 0 같음."""
    if old == new:
        return 0
    return 1 if ((new > old) == larger_is_looser) else -1  # type: ignore[operator]


def classify(old: AccountLimits, new: AccountLimits) -> ChangeDirection:
    """위험과 관련된 값의 변경 방향. 기준 시각·보고 시각은 비교하지 않는다."""
    signs = [
        _sign(old.l1_max_loss_ratio, new.l1_max_loss_ratio, larger_is_looser=True),
        _sign(old.l2_max_symbol_ratio, new.l2_max_symbol_ratio, larger_is_looser=True),
        _sign(old.l3_max_total_ratio, new.l3_max_total_ratio, larger_is_looser=True),
        _sign(old.l4_daily_loss_ratio, new.l4_daily_loss_ratio, larger_is_looser=True),
        _sign(old.l5_daily_new_orders, new.l5_daily_new_orders, larger_is_looser=True),
        _sign(old.l6_max_positions, new.l6_max_positions, larger_is_looser=True),
        _sign(old.l7_max_order_amount, new.l7_max_order_amount, larger_is_looser=True),
        _sign(_INFINITY if old.l10_leverage_total_ratio is None else old.l10_leverage_total_ratio,
              _INFINITY if new.l10_leverage_total_ratio is None else new.l10_leverage_total_ratio,
              larger_is_looser=True),
        _sign(old.warn_usage_ratio, new.warn_usage_ratio, larger_is_looser=True),  # 경고가 늦어지면 완화
        _sign(old.order_window_start, new.order_window_start, larger_is_looser=False),  # 더 일찍 열면 완화
        _sign(old.order_window_end, new.order_window_end, larger_is_looser=True),       # 더 늦게 닫으면 완화
    ]
    loosened, tightened = signs.count(1), signs.count(-1)
    if loosened and tightened:
        return ChangeDirection.MIXED
    if loosened:
        return ChangeDirection.LOOSENED
    if tightened:
        return ChangeDirection.TIGHTENED
    return ChangeDirection.NEUTRAL


class SettingsManager:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    @staticmethod
    def hash_of(limits: AccountLimits) -> str:
        return limits.version_hash()

    def effective_limits(self, account: str, at: datetime | None = None) -> AccountLimits | None:
        row = self._repo.effective_settings_row(KIND, account, at or self._clock.now())
        return None if row is None else AccountLimits.from_canonical(json.loads(row["values_json"]))

    def submit_account_limits(self, account: str, proposed: AccountLimits, source: ChangeSource) -> ChangeResult:
        new_hash = self.hash_of(proposed)
        with self._repo.tx():
            now = self._clock.now()
            base_row = {
                "version_hash": new_hash, "kind": KIND, "target": account,
                "values_json": json.dumps(proposed.to_canonical(), sort_keys=True, ensure_ascii=False),
                "submitted_at": to_iso(now), "source": source.value,
            }
            if source is ChangeSource.REMOTE:
                reason = "원격 채널로는 한도·시간 설정을 바꿀 수 없다 (로컬 설정 파일만 허용)"
                self._repo.insert_settings_version({
                    **base_row, "previous_hash": None, "direction": "NEUTRAL", "effective_at": to_iso(now),
                    "deferred": 0, "ref_time_used": None, "result": "REJECTED", "reason": reason})
                self._repo.audit(now, "SETTINGS_REJECTED", "remote", f"{account}:{new_hash}",
                                 {"account": account, "reason": reason})
                return ChangeResult(SettingsOutcome.REJECTED, new_hash, None, None, reason)

            latest = self._repo.latest_passed_settings_row(KIND, account)
            if latest is not None and latest["version_hash"] == new_hash:
                return ChangeResult(SettingsOutcome.UNCHANGED, new_hash, None, None)

            effective = self.effective_limits(account, now)
            if effective is None:
                direction, deferred, ref_used = ChangeDirection.INITIAL, False, None
                effective_at = now
                changes: dict[str, list] = {}
                previous_hash = None
            else:
                direction = classify(effective, proposed)
                ref_changed = effective.daily_reference_time != proposed.daily_reference_time
                deferred = direction in (ChangeDirection.LOOSENED, ChangeDirection.MIXED) or ref_changed
                ref_used = effective.daily_reference_time.strftime("%H:%M")
                effective_at = next_kst_time_after(now, effective.daily_reference_time) if deferred else now
                old_c, new_c = effective.to_canonical(), proposed.to_canonical()
                changes = {k: [old_c[k], new_c[k]] for k in new_c if old_c[k] != new_c[k]}
                previous_hash = self.hash_of(effective)

            self._repo.insert_settings_version({
                **base_row, "previous_hash": previous_hash, "direction": direction.value,
                "effective_at": to_iso(effective_at), "deferred": int(deferred), "ref_time_used": ref_used,
                "result": "PASSED", "reason": None})
            self._repo.audit(now, "SETTINGS_CHANGE", "local", f"{account}:{new_hash}", {
                "account": account, "direction": direction.value, "deferred": deferred,
                "effective_at": to_iso(effective_at), "changes": changes})
            outcome = SettingsOutcome.SCHEDULED if deferred else SettingsOutcome.APPLIED
            return ChangeResult(outcome, new_hash, direction, effective_at)
