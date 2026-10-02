"""주문 실행기와 통보 반영 (R-03, R-06, 02 ST-01~05, 03 §5.2·D-043·D-051).

- ApprovedIntent만 받는다 (V-94). 의도당 전송은 최대 1회, 자동 재시도 없음 (02 D-028).
- 위험 확대 전송 직전에 KILL 파일을 확인한다. 파일이 있으면 전송하지 않고 미전송 종결 (D-051).
- 전송 시도를 먼저 기록(커밋)한 뒤 전송한다 (INV-03).
- 결과 불명은 결과 불명으로 남긴다. 해소 조회는 resolver.UnknownResolver (D-047).
- 정정·취소는 새 주문번호를 받는다. 새 브로커 주문은 원주문을 참조한다 (02 ST-03, V-48).
- 재시작 직후: 전송 대기 의도는 미전송 종결(V-59), 응답 대기 시도는 결과 불명으로 바꾼다(V-58).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system.broker.base import BrokerAdapter
from trading_system.domain.clock import Clock
from trading_system.domain.enums import IntentState, OrderType, RequestType, Side, SubmitOutcome
from trading_system.domain.models import ApprovedIntent, OrderEvent, OrderRequest, SubmitResult
from trading_system.storage.repository import BrokerOrderRow, IntentRow, Repository, StorageFailure

REJECT_ALERT_STREAK = 3  # 같은 사유 연속 거부 → 경고 알림 (01 §2.7)
_ORIGIN = {"NEW": "INTERNAL_NEW", "CANCEL": "INTERNAL_CANCEL", "MODIFY": "INTERNAL_MODIFY"}


class ExecutionBlocked(RuntimeError):
    """저장 실패 등으로 상태를 확신할 수 없어 전송을 막은 상태."""


class Executor:
    def __init__(self, repo: Repository, broker: BrokerAdapter, clock: Clock, kill_file: Path) -> None:
        self._repo = repo
        self._broker = broker
        self._clock = clock
        self._kill_file = kill_file
        self._blocked: str | None = None
        self._pending: list[OrderEvent] = []

    @property
    def blocked_reason(self) -> str | None:
        return self._blocked

    # --- 재시작 복구 -------------------------------------------------------------
    def recover_on_start(self) -> list[str]:
        """재시작 직후 1회. 전송 대기 → 미전송 종결, 응답 대기 → 결과 불명. 결과 불명 의도 ID 목록을 돌려준다.

        이 함수는 주문을 보내지 않는다. 결과 불명 해소(조회)와 재개 조건(K8)은 호출자가 처리한다 (01 §2.11).
        """
        with self._repo.tx():
            for intent in self._repo.intents_in_state(IntentState.PENDING_SEND):
                self._close(intent, IntentState.NOT_SENT, "재시작: 전송 대기 의도는 보내지 않고 종결 (V-59)")
            for attempt in self._repo.attempts_awaiting_response():
                self._repo.finish_attempt(attempt["attempt_id"], "NO_RESPONSE", SubmitOutcome.UNKNOWN.value,
                                          "재시작: 응답 대기 중 종료", self._clock.now())
                intent = self._repo.intent(attempt["intent_id"])
                if not intent.state.is_terminal and intent.state is not IntentState.UNKNOWN:
                    self._set_state(intent.intent_id, IntentState.UNKNOWN, "재시작: 응답 대기 중 종료 (V-58)")
        return [i.intent_id for i in self._repo.intents_in_state(IntentState.UNKNOWN)]

    # --- 전송 ---------------------------------------------------------------
    def submit(self, approved: ApprovedIntent) -> IntentState:
        if not isinstance(approved, ApprovedIntent):
            raise TypeError("Executor는 게이트가 승인한 ApprovedIntent만 받는다 (R-03)")
        if self._blocked is not None:
            raise ExecutionBlocked(self._blocked)
        req = approved.request
        intent = self._repo.intent(approved.intent_id)
        if intent.state is not IntentState.PENDING_SEND:
            raise ValueError(f"전송 대기 상태가 아닌 의도는 보내지 않는다 (재전송 금지): {intent.state}")

        if req.is_risk_increasing and self._kill_file.exists():
            with self._repo.tx():
                self._close(intent, IntentState.NOT_SENT, "KILL 파일 존재")
            return IntentState.NOT_SENT

        order = self._order_request(intent)
        if order is None:
            with self._repo.tx():
                self._close(intent, IntentState.NOT_SENT, "대상 주문이 없거나 종결됨")
            return IntentState.NOT_SENT
        with self._repo.tx():
            now = self._clock.now()
            attempt_id = self._repo.insert_attempt(intent.intent_id, now)
            self._set_state(intent.intent_id, IntentState.SENT)
            self._repo.audit(now, "ATTEMPT", "executor", attempt_id, {"intent_id": intent.intent_id, "kind": order.kind})

        try:
            if order.kind == "CANCEL":
                result = self._broker.cancel_order(order)
            elif order.kind == "MODIFY":
                result = self._broker.modify_order(order)
            else:
                result = self._broker.place_order(order)
        except Exception as exc:  # 어댑터 예외는 전송 여부를 알 수 없으므로 결과 불명 (03 §5.2)
            result = SubmitResult(SubmitOutcome.UNKNOWN, reason=f"어댑터 예외: {type(exc).__name__}")

        try:
            with self._repo.tx():
                self._apply_result(intent, attempt_id, order, result)
        except StorageFailure:
            # 브로커에는 보냈지만 결과를 기록하지 못했다. 내부 상태를 믿을 수 없으므로 이후 전송을 모두 막는다.
            # 해제는 재시작 후 대조(K8, T-008)와 저장 정지(K7, T-005) 절차로만 한다.
            self._blocked = "전송 후 결과 기록 실패 (재시작·대조 전까지 전송 금지)"
            raise
        return self._repo.intent(intent.intent_id).state

    def _order_request(self, intent: IntentRow) -> OrderRequest | None:
        rt = intent.request_type
        if rt in (RequestType.CANCEL.value, RequestType.MODIFY.value):
            target = self._repo.broker_order(intent.target_order_key or "")
            if target is None or target.state not in ("ACCEPTED", "PARTIAL"):
                return None
            kind = "CANCEL" if rt == RequestType.CANCEL.value else "MODIFY"
            qty = Decimal("0") if kind == "CANCEL" else intent.quantity
            price = Decimal("0") if kind == "CANCEL" else intent.price
            return OrderRequest(kind=kind, account=intent.account, instrument=intent.instrument, side=intent.side,
                                quantity=qty, order_type=OrderType(intent.order_type), price=price,
                                target_order_no=target.order_no)
        return OrderRequest(kind="NEW", account=intent.account, instrument=intent.instrument, side=intent.side,
                            quantity=intent.quantity, order_type=OrderType(intent.order_type), price=intent.price)

    def _apply_result(self, intent: IntentRow, attempt_id: str, order: OrderRequest, result: SubmitResult) -> None:
        now = self._clock.now()
        outcome = result.outcome
        attempt_state = {
            SubmitOutcome.PRE_SEND_FAILURE: "NOT_SENT",
            SubmitOutcome.REJECTED: "RESPONDED",
            SubmitOutcome.ACCEPTED: "RESPONDED",
            SubmitOutcome.UNKNOWN: "NO_RESPONSE",
        }[outcome]
        self._repo.finish_attempt(attempt_id, attempt_state, outcome.value, result.reason, now)

        if outcome is SubmitOutcome.PRE_SEND_FAILURE:
            self._close(intent, IntentState.NOT_SENT, result.reason)
        elif outcome is SubmitOutcome.REJECTED:
            self._close(intent, IntentState.REJECTED, result.reason)
            self._alert_on_reject_streak(result.reason)
        elif outcome is SubmitOutcome.UNKNOWN:
            self._set_state(intent.intent_id, IntentState.UNKNOWN, result.reason)
            self.alert("EMERGENCY", "결과 불명", intent.intent_id)
        elif result.order_no is None:  # 접수인데 주문번호 없음 → 보수적으로 결과 불명 (03 §5.2)
            self._set_state(intent.intent_id, IntentState.UNKNOWN, "접수 응답에 주문번호 없음")
            self.alert("EMERGENCY", "결과 불명 (주문번호 없음)", intent.intent_id)
        else:
            self.adopt_order(intent, order.kind, result.order_no, now)

    def adopt_order(self, intent: IntentRow, kind: str, order_no: str, at: datetime) -> str:
        """접수(또는 조회로 발견)된 브로커 주문을 의도와 연결하고 의도를 접수로 바꾼다."""
        target_key = intent.target_order_key if kind in ("CANCEL", "MODIFY") else None
        owner_intent = intent.intent_id
        if kind == "MODIFY":  # 정정 주문의 체결은 원주문 의도(경제적 주인)에 반영한다
            target = self._repo.broker_order(target_key or "")
            owner_intent = target.intent_id if target and target.intent_id else intent.intent_id
        key = self._repo.insert_broker_order(
            account=intent.account, order_no=order_no, at=at, intent_id=owner_intent,
            instrument=intent.instrument, side=intent.side, quantity=intent.quantity,
            origin=_ORIGIN[kind], target_order_key=target_key,
        )
        self._repo.link_intent_order(intent.intent_id, key)
        self._repo.audit(at, "BROKER_ORDER", "executor", key, {"intent_id": intent.intent_id, "kind": kind})
        self._set_state(intent.intent_id, IntentState.ACCEPTED)
        return key

    def _alert_on_reject_streak(self, reason: str | None) -> None:
        recent = self._repo.recent_attempt_results(REJECT_ALERT_STREAK)
        if len(recent) == REJECT_ALERT_STREAK and all(
                o == SubmitOutcome.REJECTED.value and r == reason for o, r in recent):
            self.alert("WARNING", f"같은 사유 연속 거부 {REJECT_ALERT_STREAK}회: {reason}", None)

    def alert(self, level: str, message: str, ref: str | None) -> None:
        """알림 기록. 전송(텔레그램)은 T-013 Notifier가 이 기록을 읽어 보낸다 (01 §2.10)."""
        self._repo.audit(self._clock.now(), "ALERT", "executor", ref, {"level": level, "message": message})

    # --- 통보 반영 (OrderTracker) ------------------------------------------------
    def process_events(self) -> None:
        """통보를 하나씩 반영한다. 저장에 실패하면 남은 통보를 보관하고 예외를 올린다 (다음 호출에서 이어서 처리)."""
        self._pending.extend(self._broker.poll_events())
        while self._pending:
            with self._repo.tx():
                self.apply_event(self._pending[0])
            self._pending.pop(0)

    def _anomaly(self, ev: OrderEvent, reason: str) -> None:
        """반영할 수 없는 통보. 원장을 바꾸지 않고 기록만 한다. 대조 불일치 정지(K4)는 T-008."""
        self._repo.audit(self._clock.now(), "EVENT_ANOMALY", "tracker", ev.order_no, {
            "kind": ev.kind, "fill_id": ev.fill_id, "quantity": str(ev.quantity), "price": str(ev.price),
            "reason": reason})

    def apply_event(self, ev: OrderEvent) -> None:
        """통보 1건 반영. 호출자의 트랜잭션 안에서 실행한다."""
        now = self._clock.now()
        order = self._repo.broker_order_by_no(ev.order_no)
        if order is None or order.intent_id is None:
            self._repo.audit(now, "UNMATCHED_EVENT", "tracker", ev.order_no, {"kind": ev.kind})  # 대조 대상 (T-008)
            return
        intent = self._repo.intent(order.intent_id)
        if ev.kind == "FILL":
            self._apply_fill(order, intent, ev, now)
        elif ev.kind in ("CANCELLED", "EXPIRED", "REJECTED"):
            if order.state not in ("ACCEPTED", "PARTIAL"):
                # 이미 종결된 주문의 중복 통보. 그 사이 결과 불명에서 해소된 취소·정정 의도가 있으면 정리한다
                self._close_changes_for(order, f"대상 주문 이미 {order.state}")
                return
            state = {"CANCELLED": IntentState.CANCELLED, "EXPIRED": IntentState.EXPIRED,
                     "REJECTED": IntentState.REJECTED}[ev.kind]
            self._repo.update_broker_order(order.order_key, order.filled_quantity, ev.kind)
            if not intent.state.is_terminal:
                self._close(intent, state, {"CANCELLED": "취소 확인", "EXPIRED": "장 마감 소멸",
                                            "REJECTED": "접수 후 거부"}[ev.kind])
            self._close_changes_for(order, f"대상 주문 {ev.kind}")
        elif ev.kind == "MODIFIED":
            self._apply_modified(order, intent, ev, now)
        else:
            self._anomaly(ev, f"알 수 없는 통보 종류 {ev.kind}")

    def _close_changes_for(self, order: BrokerOrderRow, reason: str) -> None:
        """대상 주문이 종결되면 그 주문에 걸린 활성 취소·정정 의도도 종결한다 (예약 해제 포함).

        전송 대기 → 미전송 종결, 접수 → 완료. 응답 대기·결과 불명은 응답·조회가 정하므로 건드리지 않는다.
        """
        for c in self._repo.active_cancel_for(order.order_key):
            if c.state is IntentState.PENDING_SEND:
                self._close(c, IntentState.NOT_SENT, reason)
            elif c.state in (IntentState.ACCEPTED, IntentState.PARTIAL):
                self._close(c, IntentState.DONE, reason)

    def _apply_modified(self, order: BrokerOrderRow, intent: IntentRow, ev: OrderEvent, now: datetime) -> None:
        """정정 확인: 원주문 종결(MODIFIED), 원 의도의 수량·가격을 정정 후로 바꾸고 정정 의도의 예약을 옮긴다."""
        if order.state not in ("ACCEPTED", "PARTIAL"):
            return
        new = self._repo.broker_order_by_no(ev.new_order_no or "")
        modify = next((c for c in self._repo.active_cancel_for(order.order_key)
                       if c.request_type == RequestType.MODIFY.value), None)
        if new is None or modify is None:  # 정정 응답이 결과 불명이면 조회로 발견한 뒤 다시 반영한다 (resolver)
            self._repo.audit(now, "UNMATCHED_EVENT", "tracker", ev.order_no, {"kind": ev.kind})
            return None
        self._repo.update_broker_order(order.order_key, order.filled_quantity, "MODIFIED")
        self._repo.set_intent_terms(intent.intent_id, intent.filled_quantity + ev.quantity, ev.price)
        mod_rsv = self._repo.reservation_for(modify.intent_id)
        own_rsv = self._repo.reservation_for(intent.intent_id)
        if mod_rsv is not None:
            if own_rsv is not None:
                self._repo.add_to_reservation(own_rsv.reservation_id, mod_rsv.amount, mod_rsv.cash_amount)
            self._repo.release_reservation(mod_rsv.reservation_id)
        self._repo.audit(now, "ORDER_MODIFIED", "tracker", order.order_key,
                         {"new_order": new.order_key, "quantity": str(ev.quantity), "price": str(ev.price)})
        self._close(modify, IntentState.DONE, "정정 확인")

    def _apply_fill(self, order: BrokerOrderRow, intent: IntentRow, ev: OrderEvent, now: datetime) -> None:
        if not ev.fill_id:
            return self._anomaly(ev, "체결 식별자 없음")
        if ev.quantity <= 0 or ev.price <= 0:
            return self._anomaly(ev, "체결 수량·가격이 0 이하")
        filled = order.filled_quantity + ev.quantity
        if filled > order.quantity:
            return self._anomaly(ev, f"주문 수량 초과 체결 ({filled} > {order.quantity})")
        if intent.side == Side.SELL.value and not intent.state.is_terminal:
            held = self._repo.position(intent.account, intent.instrument).quantity
            if ev.quantity > held:
                return self._anomaly(ev, f"내부 보유({held})보다 큰 매도 체결 — 외부 거래 의심")
        if not self._repo.insert_fill(order.order_key, ev.fill_id, ev.quantity, ev.price, ev.at or now):
            return None  # 중복 통보
        self._repo.update_broker_order(order.order_key, filled, "FILLED" if filled >= order.quantity else "PARTIAL")
        self._repo.audit(now, "FILL", "tracker", order.order_key, {"quantity": str(ev.quantity), "price": str(ev.price)})
        if intent.state.is_terminal:
            self._repo.audit(now, "LATE_FILL", "tracker", intent.intent_id, {})  # INV-11: 대조로 처리 (T-008)
            return None

        intent_filled = intent.filled_quantity + ev.quantity
        self._repo.set_intent_filled(intent.intent_id, intent_filled)
        pos = self._repo.position(intent.account, intent.instrument)
        if intent.side == Side.BUY.value:
            qty = pos.quantity + ev.quantity
            avg = (pos.quantity * pos.avg_price + ev.quantity * ev.price) / qty
        else:
            qty = pos.quantity - ev.quantity
            avg = pos.avg_price if qty > 0 else Decimal("0")
        self._repo.set_position(intent.account, intent.instrument, qty, avg)

        rsv = self._repo.reservation_for(intent.intent_id)
        if rsv is not None:  # 체결 비율만큼 예약 노출을 포지션 노출로 옮긴다 (배수 반영된 예약 금액 기준)
            self._repo.consume_reservation(rsv.reservation_id, rsv.amount * min(intent_filled / intent.quantity, 1))

        own = self._repo.active_ownership(intent.account, intent.instrument)
        if intent.side == Side.BUY.value and own is not None and own.state == "RESERVED":
            self._repo.set_ownership_state(own.ownership_id, "CONFIRMED")

        if intent_filled >= intent.quantity:
            self._close(self._repo.intent(intent.intent_id), IntentState.FILLED, None)
        elif intent.state is not IntentState.PARTIAL:
            self._set_state(intent.intent_id, IntentState.PARTIAL)
        return None

    # --- 공통 ---------------------------------------------------------------
    def _set_state(self, intent_id: str, state: IntentState, reason: str | None = None) -> None:
        self._repo.set_intent_state(intent_id, state, reason)
        self._repo.audit(self._clock.now(), "INTENT_STATE", "executor", intent_id, {"state": state.value, "reason": reason})

    def close(self, intent: IntentRow, state: IntentState, reason: str | None) -> None:
        """의도를 종결하고 예약·소유권을 정리한다 (02 ST-04·ST-05). 호출자의 트랜잭션 안에서 실행한다."""
        self._close(intent, state, reason)

    def _close(self, intent: IntentRow, state: IntentState, reason: str | None) -> None:
        self._set_state(intent.intent_id, state, reason)
        rsv = self._repo.reservation_for(intent.intent_id)
        if rsv is not None:
            self._repo.release_reservation(rsv.reservation_id)
        own = self._repo.active_ownership(intent.account, intent.instrument)
        if own is None:
            return
        pos = self._repo.position(intent.account, intent.instrument)
        filled = self._repo.intent(intent.intent_id).filled_quantity
        if own.state == "RESERVED" and own.intent_id == intent.intent_id and filled == 0:
            self._repo.set_ownership_state(own.ownership_id, "RELEASED", "체결 0주 종결")
        elif own.state == "CONFIRMED" and pos.quantity == 0 and not self._repo.active_new_intents(
                intent.account, intent.instrument, Side.SELL.value) and not self._repo.unknown_intents(
                intent.account, intent.instrument):
            self._repo.set_ownership_state(own.ownership_id, "RELEASED", "전량 청산")
