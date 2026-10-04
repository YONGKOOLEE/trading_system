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
from trading_system.domain.sinks import AlertSink
from trading_system.storage.alerts import AuditAlertSink
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
    HaltType.K9: ("strategy_resumed",),                 # 전략 상태 전환(승인 A-ID)으로만 (01 K9 v0.5, INV-13)
    HaltType.K10: ("data_ok",),
    HaltType.K11: ("notify_ok",),
}


# K5 연결 경로 (RV-02). K5 행 조건에 경로별 상태를 "link:<경로>"로 남긴다.
# - broker_connected = 기록된 모든 경로가 마지막 보고에서 정상(ok). 끊긴 경로를 다른 경로의 성공으로 풀지 않는다.
# - reconciled는 마지막 경로 복구 보고 이후의 대조여야 한다(재접속 전 대조는 끊긴 동안의 통보 누락을 못 본다).
# - 조회 경로(rest): 대조기가 브로커 조회 3종 성공 시 보고한다(조회 성공 = 연결 증거).
# - 실시간 경로(realtime): 실시간 수신 쪽이 재접속(ONLINE·RECONNECTED) 시 보고한다(app/main.py).
CANCEL_PENDING = "cancel_pending"   # record_only로 기록된 정지의 매수 미체결 취소 대기 (재검토 F1)
K5_LINK_REST = "link:rest"
K5_LINK_REALTIME = "link:realtime"
K5_LINK_THREAD = "link:thread"   # 입출력 스레드 종료 — 프로세스 재시작(engine.start)만 정상 보고 (재검토 F2)
_K5_LINK_OF_ACTOR = {"realtime": K5_LINK_REALTIME, "thread": K5_LINK_THREAD}   # 발동 주체 → 끊긴 경로 (그 밖은 조회 경로)


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
                 reference_time_of: Callable[[str], dtime | None] | None = None,
                 alerts: AlertSink | None = None) -> None:
        self._repo = repo
        self._gate = gate
        self._executor = executor
        self._clock = clock
        self._reference_time_of = reference_time_of or (lambda account: None)
        self._alerts: AlertSink = alerts or AuditAlertSink(repo, clock, actor="halt")
        executor.attach_halts(self)  # 실행기·해소기의 정지 발동도 이 관리자를 거친다 (03 D-093)

    # --- 발동 ---------------------------------------------------------------------
    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, *,
                 instrument: str | None = None, owner: str | None = None, variant: str | None = None) -> str:
        """정지를 발동한다. 같은 유형·범위의 해제되지 않은 정지가 있으면 그 ID를 돌려준다."""
        created = pending_cancel = False
        with self._repo.tx():
            now = self._clock.now()
            existing = self._find(halt_type, account, instrument, owner)
            if existing is not None:
                halt_id = existing["halt_id"]
                pending_cancel = _take_cancel_pending(self._repo, existing)
                if existing["state"] == HaltState.AWAITING_RESUME.value:  # 다시 발동되면 조건을 처음부터
                    self._repo.update_halt(halt_id, state=HaltState.ACTIVE.value, conditions={})
                if variant is not None and existing.get("variant") != variant:
                    # 사유가 바뀌면(예: 교체 중지 → 폐기 = 사용자 판단) 정지표 행도 바뀐다 (01 K9)
                    self._repo.update_halt(halt_id, variant=variant)
                    self._repo.audit(now, "HALT_VARIANT", actor, halt_id, {
                        "type": halt_type.value, "from": existing.get("variant"), "to": variant, "reason": reason})
                awaiting = existing["state"] == HaltState.AWAITING_RESUME.value
                conds = {} if awaiting else json.loads(existing["conditions"] or "{}")
            else:
                halt_id = self._repo.insert_halt(halt_type.value, account, actor, reason, now,
                                                 instrument=instrument, owner=owner, variant=variant)
                created = True
                conds = {}
                self._repo.audit(now, "HALT_ACTIVATED", actor, halt_id, {
                    "type": halt_type.value, "account": account, "instrument": instrument, "owner": owner,
                    "variant": variant, "reason": reason})
                level = "EMERGENCY" if halt_type in EMERGENCY else "WARNING"
                self._alert(level, f"정지 {halt_type.value} 발동: {reason}", halt_id)
            if halt_type is HaltType.K5:   # 끊긴 연결 경로를 기록한다 (RV-02)
                conds[_K5_LINK_OF_ACTOR.get(actor, K5_LINK_REST)] = {"ok": False, "at": now.isoformat(),
                                                                      "evidence": reason}
                self._repo.update_halt(halt_id, conditions=conds)
        # 매수 미체결 취소는 정지 행이 새로 생길 때 한 번만 (RV-07). 정지 유지 중 재발동(매 tick L4 점검 등)이
        # 취소를 새 멱등키로 다시 보내면 주문 변경 자동 재시도가 된다 (CLAUDE.md §8-7). 다른 정지에 막힌 취소의
        # 재시도는 정지 해제 시점(사용자 재개·자동 해제)의 _retry_halt_cancels로만 한다 (CP-018 R-10)
        # 감시 스레드가 행만 먼저 기록한 정지(record_only)는 취소 대기 표시를 남긴다 → 여기서 한 번 취소 (재검토 F1)
        if (created or pending_cancel) and halt_type in CANCELS_OPEN_BUYS:
            self._cancel_open_buys(halt_id, account, owner if halt_type is HaltType.K9 else None)
        return halt_id

    def record_only(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None) -> str | None:
        """감시 스레드용 발동 (RV-22, 03 D-038): 정지 행·감사만 기록하고 주문은 내지 않는다.

        매수 미체결 취소가 필요한 유형은 조건에 취소 대기 표시를 남기고, 판정·실행 스레드의 activate가 그 표시를
        보고 한 번 취소한다(재검토 F1 — 표시 없이 행만 있으면 activate가 기존 행으로 보고 취소를 건너뛴다).
        같은 범위의 미해제 정지가 이미 있으면 아무것도 하지 않고 None.
        """
        with self._repo.tx():
            if self._find(halt_type, account, None, None) is not None:
                return None
            now = self._clock.now()
            halt_id = self._repo.insert_halt(halt_type.value, account, actor, reason, now)
            if halt_type in CANCELS_OPEN_BUYS:
                self._repo.update_halt(halt_id, conditions={CANCEL_PENDING: {"ok": False, "at": now.isoformat()}})
            self._repo.audit(now, "HALT_ACTIVATED", actor, halt_id, {
                "type": halt_type.value, "account": account, "instrument": None, "owner": None,
                "variant": None, "reason": reason, "path": "supervisor"})
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
                         evidence: str = "", *, instrument: str | None = None, owner: str | None = None) -> list[str]:
        """재개 조건 충족 여부를 기록한다 (대조·시계·연결·저장 점검 등이 호출). 자동 해제 유형은 여기서 해제된다.

        K10은 종목 범위가 정확히 같은 정지에만 적용한다 (instrument=None은 계좌 전체 K10, CP-018 R-7).
        """
        released: list[str] = []
        with self._repo.tx():
            now = self._clock.now()
            rows = self._rows(halt_type, account)
            if halt_type is HaltType.K10:
                rows = [r for r in rows if r["scope_instrument"] == instrument]
            if halt_type is HaltType.K9:
                rows = [r for r in rows if r["scope_owner"] == owner]
            for row in rows:
                conds = json.loads(row["conditions"])
                conds[name] = {"ok": ok, "at": now.isoformat(), "evidence": evidence}
                if halt_type is HaltType.K5 and name == "broker_connected":
                    # 경로를 가리지 않는 연결 보고 = 모든 경로에 대한 보고 (RV-02 이전 계약 유지).
                    # 운영 코드는 경로별(K5_LINK_REST·K5_LINK_REALTIME)로 보고한다
                    for k in [k for k in conds if k.startswith("link:")]:
                        conds[k] = {"ok": ok, "at": now.isoformat(), "evidence": evidence}
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
        if released:
            # 자동 해제(K5·K6·K10·K11)도 해제다: 그 정지에 막혔던 K1·K2·K9 매수 미체결 취소를 다시 시도한다
            # (CP-018 R-10, 3차 검토 #1 — 재개 경로만 재시도하면 K5 자동 해제 뒤 K1인데 매수 미체결이 남는다).
            # 스레드: 다른 스레드의 조건 보고는 QueuedHalts로 판정·실행 스레드에서 이 함수를 부르므로 전송도 그 스레드다
            # (RV-22, D-038). 이 관리자를 다른 스레드에서 직접 부르는 배선을 만들지 않는다(activate와 같은 제약).
            # 반복 전송 없음: 해제는 행마다 한 번(ACTIVE → RELEASED)이고, 이미 활성인 취소는 INV-17이 거부한다
            self._retry_after_release(f"자동 해제 {halt_type.value}")
        return released

    def resume(self, halt_type: HaltType, account: str | None, channel: str, actor: str, *,
               owner: str | None = None, superseded: Callable[[], str | None] | None = None) -> ResumeResult:
        """사용자 재개 명령. 원격 허용·재개 조건을 검사하고 감사 기록한다. K9는 전략(owner)별.

        superseded: 저장 단위 **안에서** 부르는 확인 함수. 거부 사유 문자열을 돌려주면 재개를 거부한다
        (예: 이 RESUME보다 뒤에 들어온 대기 KILL — 3차 검토 #2, ops/commands.py).
        """
        with self._repo.tx():
            now = self._clock.now()
            rows = self._rows(halt_type, account)
            if halt_type is HaltType.K9:
                rows = [r for r in rows if r["scope_owner"] == owner]
            veto = superseded() if superseded is not None else None
            result = ResumeResult(False, veto) if veto else self._check_resume(halt_type, rows, channel)
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
            self._retry_after_release(f"재개 {halt_type.value}")
        return result

    def _retry_after_release(self, why: str) -> None:
        if self._repo.in_transaction:
            # 바깥 저장 단위 안에서는 보내지 않는다(INV-03 — 실행기도 거부). 다음 해제·재개 때 다시 시도된다
            self._alert("WARNING", f"{why}: 저장 단위 안이라 막힌 매수 미체결 취소 재시도 보류", None)
            return
        self._retry_halt_cancels()

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
        if halt_type is HaltType.K5:
            return self._k5_missing(conds)
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

    @staticmethod
    def _k5_missing(conds: dict) -> list[str]:
        """K5: 모든 기록된 연결 경로 정상 + 그 뒤의 대조 일치 (RV-02, 01 K5 "연결 복구 + 대조")."""
        links = {k: v for k, v in conds.items() if k.startswith("link:")}
        missing: list[str] = []
        if links:
            missing += [f"broker_connected({k})" for k, v in sorted(links.items()) if not v.get("ok")]
        elif not conds.get("broker_connected", {}).get("ok"):
            missing.append("broker_connected")   # 경로 기록이 없는 이전 행: 명시 보고만 인정
        rec = conds.get("reconciled", {})
        if not rec.get("ok"):
            missing.append("reconciled")
        elif links and from_iso(rec["at"]) < max(from_iso(v["at"]) for v in links.values()):
            missing.append("reconciled(연결 복구 이후 대조 필요)")
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
        self._alerts.alert(level, message, ref)


def _take_cancel_pending(repo: Repository, row: dict) -> bool:
    """취소 대기 표시가 있으면 지우고 True (activate의 tx 안에서 부른다 — 취소는 한 번만)."""
    conds = json.loads(row["conditions"] or "{}")
    if CANCEL_PENDING not in conds:
        return False
    del conds[CANCEL_PENDING]
    repo.update_halt(row["halt_id"], conditions=conds)
    row["conditions"] = json.dumps(conds)
    return True
