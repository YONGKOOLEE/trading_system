"""정지 관리 K1~K11 (R-07, 01 §2.6·§2.9·§2.11, 02 ENT-23·ST-06, 03 §6.5).

- 정지는 청산이 아니다. 보유 포지션을 자동으로 팔지 않는다 (CLAUDE.md §8-8).
- 요청별 허용은 게이트 G2가 domain/halts.py 표로 판정한다. 여기서는 발동·조건 기록·해제·재개만 한다.
- 발동 시 매수 미체결 취소(K1·K2·K9)는 취소 요청을 만들어 **게이트를 거쳐** 보낸다.
- 상태: 활성 → (재개 조건 충족) → 자동 해제 유형은 해제 / 사용자 재개 유형은 재개 대기 → 사용자 재개 → 해제.
- 원격 재개는 K1·K2만 (01 §2.9). 재개 조건 검사는 재개 시점에 다시 한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time as dtime
from decimal import Decimal

from trading_system.domain.clock import Clock, from_iso, next_kst_time_after
from trading_system.domain.enums import GateResult, HaltType, IntentState, RequestSource, Side
from trading_system.domain.halts import AUTO_RELEASE, CANCELS_OPEN_BUYS, EMERGENCY, REMOTE_RESUMABLE, HaltState
from trading_system.domain.market import DailyRisk
from trading_system.domain.models import GateRequest
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.gate.gate import Gate
from trading_system.storage.repository import Repository, StorageFailure, new_id

# 재개 조건 (01 §2.6 "재개 조건" 열). 외부에서 report_condition으로 알려 주는 항목.
REQUIRED_CONDITIONS: dict[HaltType, tuple[str, ...]] = {
    HaltType.K1: (),
    HaltType.K2: (),                                    # 다음 일일 기준 시각 경과 (재개 시 계산)
    HaltType.K3: ("reconciled",),                       # + 결과 불명 의도 없음 (재개 시 확인)
    HaltType.K4: ("reconciled",),
    HaltType.K5: ("broker_connected", "reconciled"),
    HaltType.K6: ("clock_ok",),
    HaltType.K7: ("storage_ok",),
    HaltType.K8: ("recovery_ok",),                      # 01 §2.11 조건 전부 (T-008이 판정해 알림)
    HaltType.K9: (),                                    # 사용자 상태 전환 (T-007)
    HaltType.K10: ("data_ok",),
    HaltType.K11: ("notify_ok",),
}


class Channel:
    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


@dataclass(frozen=True)
class ResumeResult:
    ok: bool
    reason: str
    released: tuple[str, ...] = ()


class HaltManager:
    def __init__(self, repo: Repository, gate: Gate, executor: Executor, clock: Clock,
                 reference_time_of: Callable[[str], dtime | None] | None = None) -> None:
        self._repo = repo
        self._gate = gate
        self._executor = executor
        self._clock = clock
        self._reference_time_of = reference_time_of or (lambda account: None)

    # --- 발동 ---------------------------------------------------------------------
    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, *,
                 instrument: str | None = None, owner: str | None = None, variant: str | None = None) -> str:
        """정지를 발동한다. 같은 유형·범위의 해제되지 않은 정지가 있으면 그 ID를 돌려준다."""
        with self._repo.tx():
            now = self._clock.now()
            existing = self._find(halt_type, account, instrument, owner)
            if existing is not None:
                halt_id = existing["halt_id"]
                if existing["state"] == HaltState.AWAITING_RESUME.value:  # 다시 발동되면 조건을 처음부터
                    self._repo.update_halt(halt_id, state=HaltState.ACTIVE.value, conditions={})
            else:
                halt_id = self._repo.insert_halt(halt_type.value, account, actor, reason, now,
                                                 instrument=instrument, owner=owner, variant=variant)
                self._repo.audit(now, "HALT_ACTIVATED", actor, halt_id, {
                    "type": halt_type.value, "account": account, "instrument": instrument, "owner": owner,
                    "variant": variant, "reason": reason})
                level = "EMERGENCY" if halt_type in EMERGENCY else "WARNING"
                self._alert(level, f"정지 {halt_type.value} 발동: {reason}", halt_id)
        if halt_type in CANCELS_OPEN_BUYS:
            self._cancel_open_buys(halt_id, account, owner if halt_type is HaltType.K9 else None)
        return halt_id

    def activate_best_effort(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None) -> bool:
        """저장소가 불안정할 때(K7) 쓰는 발동. 기록 실패는 삼키고 False를 돌려준다 (그래도 실행기는 막혀 있음)."""
        try:
            self.activate(halt_type, actor, reason, account)
            return True
        except StorageFailure:
            return False

    def on_start(self) -> str:
        """프로세스 시작: 항상 K8 (01 §2.11). 재시작 직후에는 주문하지 않는다 (CLAUDE.md §8-9)."""
        return self.activate(HaltType.K8, "system", "프로세스 시작: 재시작 복구 대기")

    def check_daily_loss(self, account: str, daily: DailyRisk | None, l4_ratio: Decimal) -> str | None:
        """일일 손실 한도(L4) 도달 → K2 자동 발동 (01 §2.6). 평가 불능은 게이트가 위험 확대를 거부한다."""
        if daily is None or not daily.evaluable or daily.loss_ratio < l4_ratio:
            return None
        return self.activate(HaltType.K2, "monitor", f"일일 손실 {daily.loss_ratio} ≥ L4 {l4_ratio}", account)

    def _cancel_open_buys(self, halt_id: str, account: str | None, owner: str | None) -> None:
        accounts = [account] if account else self._repo.accounts()
        for acct in accounts:
            for order in self._repo.open_orders(acct, Side.BUY.value):
                order_owner = self._repo.intent(order.intent_id).owner if order.intent_id else None
                if owner is not None and order_owner != owner:
                    continue
                req = GateRequest.cancel(
                    RequestSource.HALT_POLICY, acct, order.instrument, Side.BUY, order.order_key,
                    # 시도마다 새 멱등키: 다른 정지에 막혀 거부된 취소도 나중에 다시 시도할 수 있게 (CP-018 R-10).
                    # 같은 주문의 이중 취소는 INV-17(활성 취소·정정 1개)이 막는다
                    idem_key=f"halt:{halt_id}:{order.order_key}:{new_id('try')}", owner=order_owner,
                )
                decision = self._gate.decide(req)
                if decision.result is GateResult.APPROVED and decision.approved is not None:
                    try:
                        self._executor.submit(decision.approved)
                    except ExecutionBlocked:
                        return
                elif decision.failed_check is not None and "INV-17" not in (decision.reason or ""):
                    self._alert("EMERGENCY", f"정지 정책 취소 거부: {decision.reason}", order.order_key)

    # --- 조건·해제 -------------------------------------------------------------------
    def report_condition(self, halt_type: HaltType, account: str | None, name: str, ok: bool,
                         evidence: str = "", *, instrument: str | None = None) -> list[str]:
        """재개 조건 충족 여부를 기록한다 (대조·시계·연결·저장 점검 등이 호출). 자동 해제 유형은 여기서 해제된다.

        K10은 종목 범위가 정확히 같은 정지에만 적용한다 (instrument=None은 계좌 전체 K10, CP-018 R-7).
        """
        released: list[str] = []
        with self._repo.tx():
            now = self._clock.now()
            rows = self._rows(halt_type, account)
            if halt_type is HaltType.K10:
                rows = [r for r in rows if r["scope_instrument"] == instrument]
            for row in rows:
                conds = json.loads(row["conditions"])
                conds[name] = {"ok": ok, "at": now.isoformat(), "evidence": evidence}
                met = self._conditions_met(halt_type, row, conds)
                state = row["state"]
                if not met:
                    state = HaltState.ACTIVE.value
                elif halt_type in AUTO_RELEASE:
                    state = HaltState.RELEASED.value
                else:
                    state = HaltState.AWAITING_RESUME.value
                self._repo.update_halt(row["halt_id"], state=state, conditions=conds)
                self._repo.audit(now, "HALT_CONDITION", "system", row["halt_id"],
                                 {"type": halt_type.value, "condition": name, "ok": ok, "state": state})
                if state == HaltState.RELEASED.value:
                    self._repo.update_halt(row["halt_id"], released_at=now, released_by="auto")
                    self._alert("INFO", f"정지 {halt_type.value} 자동 해제", row["halt_id"])
                    released.append(row["halt_id"])
        return released

    def resume(self, halt_type: HaltType, account: str | None, channel: str, actor: str) -> ResumeResult:
        """사용자 재개 명령. 원격 허용·재개 조건을 검사하고 감사 기록한다."""
        with self._repo.tx():
            now = self._clock.now()
            rows = self._rows(halt_type, account)
            result = self._check_resume(halt_type, rows, channel)
            if result.ok:
                for row in rows:
                    self._repo.update_halt(row["halt_id"], state=HaltState.RELEASED.value,
                                           released_at=now, released_by=f"{channel}:{actor}")
                result = ResumeResult(True, result.reason, tuple(r["halt_id"] for r in rows))
            self._repo.audit(now, "HALT_RESUME", actor, halt_type.value, {
                "account": account, "channel": channel, "ok": result.ok, "reason": result.reason})
            self._alert("INFO" if result.ok else "WARNING",
                        f"정지 {halt_type.value} 재개 {'완료' if result.ok else '거부'}: {result.reason}", None)
        if result.ok:
            self._retry_halt_cancels()
        return result

    def _retry_halt_cancels(self) -> None:
        """한 정지가 풀리면 남은 정지의 매수 미체결 취소를 다시 시도한다 (다른 정지에 막혔던 취소, CP-018 R-10)."""
        for row in self._repo.halt_rows(None):
            k = HaltType(row["halt_type"])
            if k in CANCELS_OPEN_BUYS:
                self._cancel_open_buys(row["halt_id"], row["account"], row["scope_owner"] if k is HaltType.K9 else None)

    def _check_resume(self, halt_type: HaltType, rows: list[dict], channel: str) -> ResumeResult:
        if channel != Channel.LOCAL and halt_type not in REMOTE_RESUMABLE:
            return ResumeResult(False, f"{halt_type.value}는 로컬에서만 재개할 수 있음 (01 §2.9)")
        if not rows:
            return ResumeResult(False, "해제되지 않은 해당 정지 없음")
        for row in rows:
            if not self._conditions_met(halt_type, row, json.loads(row["conditions"])):
                return ResumeResult(False, f"재개 조건 미충족: {self._missing(halt_type, row)}")
        return ResumeResult(True, "재개 조건 충족")

    def _conditions_met(self, halt_type: HaltType, row: dict, conds: dict) -> bool:
        return not self._missing(halt_type, row, conds)

    def _missing(self, halt_type: HaltType, row: dict, conds: dict | None = None) -> list[str]:
        conds = json.loads(row["conditions"]) if conds is None else conds
        missing = [n for n in REQUIRED_CONDITIONS[halt_type] if not conds.get(n, {}).get("ok")]
        if halt_type is HaltType.K2:
            ref = self._reference_time_of(row["account"]) if row["account"] else None
            if ref is None:
                missing.append("일일 기준 시각 설정 없음")
            elif self._clock.now() < next_kst_time_after(from_iso(row["created_at"]), ref):
                missing.append("다음 일일 기준 시각 전")
        if halt_type is HaltType.K3:
            accounts = [row["account"]] if row["account"] else self._repo.accounts()
            if any(i.account in accounts for i in self._repo.intents_in_state(IntentState.UNKNOWN)):
                missing.append("결과 불명 의도 남아 있음")
        return missing

    # --- K3 사람 확정 (02 ST-01 "사람 확정") ---------------------------------------
    def confirm_unknown(self, intent_id: str, accepted: bool, actor: str, order_no: str | None = None) -> str:
        """사람이 결과 불명 의도의 결과를 확정한다. 로컬 운영 명령으로만 호출한다."""
        with self._repo.tx():
            intent = self._repo.intent(intent_id)
            if intent.state is not IntentState.UNKNOWN:
                raise ValueError(f"결과 불명 의도가 아님: {intent.state}")
            now = self._clock.now()
            if accepted:
                if not order_no:
                    raise ValueError("접수로 확정하려면 브로커 주문번호가 필요")
                kind = {"CANCEL": "CANCEL", "MODIFY": "MODIFY"}.get(intent.request_type, "NEW")
                self._executor.adopt_order(intent, kind, order_no, now)
            else:
                self._executor.close(intent, IntentState.NOT_ACCEPTED, f"사람 확정: 미접수 ({actor})")
            self._repo.audit(now, "UNKNOWN_CONFIRMED", actor, intent_id, {"accepted": accepted, "order_no": order_no})
        return intent_id

    # --- 조회 ----------------------------------------------------------------------
    def status(self) -> list[dict]:
        return self._repo.halt_rows(None)

    def _rows(self, halt_type: HaltType, account: str | None) -> list[dict]:
        return [r for r in self._repo.halt_rows(None)
                if r["halt_type"] == halt_type.value and (account is None or r["account"] in (None, account))]

    def _find(self, halt_type: HaltType, account: str | None, instrument: str | None,
              owner: str | None) -> dict | None:
        for r in self._repo.halt_rows(None):
            if (r["halt_type"], r["account"], r["scope_instrument"], r["scope_owner"]) == (
                    halt_type.value, account, instrument, owner):
                return r
        return None

    def _alert(self, level: str, message: str, ref: str | None) -> None:
        self._repo.audit(self._clock.now(), "ALERT", "halt", ref, {"level": level, "message": message})
