"""주문 실행기와 통보 반영 (R-03, R-06, 02 ST-01~05, 03 §5.2·D-043·D-051).

- ApprovedIntent만 받는다 (V-94). 의도당 전송은 최대 1회, 자동 재시도 없음 (02 D-028).
- 위험 확대 전송 직전에 KILL 파일을 확인한다. 파일이 있으면 전송하지 않고 미전송 종결 (D-051).
- 전송 시도를 먼저 기록(커밋)한 뒤 전송한다 (INV-03).
- 결과 불명은 결과 불명으로 남긴다. 해소 조회는 T-004.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trading_system.broker.base import BrokerAdapter
from trading_system.domain.clock import Clock
from trading_system.domain.enums import IntentState, OrderType, RequestType, Side, SubmitOutcome
from trading_system.domain.models import ApprovedIntent, OrderEvent, OrderRequest, SubmitResult
from trading_system.storage.repository import IntentRow, Repository, StorageFailure


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
        with self._repo.tx():
            now = self._clock.now()
            attempt_id = self._repo.insert_attempt(intent.intent_id, now)
            self._repo.set_intent_state(intent.intent_id, IntentState.SENT)
            self._repo.audit(now, "ATTEMPT", "executor", attempt_id, {"intent_id": intent.intent_id, "kind": order.kind})

        try:
            if order.kind == "CANCEL":
                result = self._broker.cancel_order(order)
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

    def _order_request(self, intent: IntentRow) -> OrderRequest:
        if intent.request_type == RequestType.CANCEL.value:
            target = self._repo.broker_order(intent.target_order_key or "")
            assert target is not None
            return OrderRequest(kind="CANCEL", account=intent.account, instrument=intent.instrument,
                                side=intent.side, quantity=Decimal("0"),
                                order_type=OrderType(intent.order_type), price=Decimal("0"),
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
        elif outcome is SubmitOutcome.UNKNOWN:
            self._set_state(intent.intent_id, IntentState.UNKNOWN, result.reason)
        elif result.order_no is None:  # 접수인데 주문번호 없음 → 보수적으로 결과 불명 (03 §5.2)
            self._set_state(intent.intent_id, IntentState.UNKNOWN, "접수 응답에 주문번호 없음")
        else:
            target_key = intent.target_order_key if order.kind == "CANCEL" else None
            key = self._repo.insert_broker_order(
                account=intent.account, order_no=result.order_no, at=now, intent_id=intent.intent_id,
                instrument=intent.instrument, side=intent.side, quantity=intent.quantity,
                origin="INTERNAL_CANCEL" if order.kind == "CANCEL" else "INTERNAL_NEW", target_order_key=target_key,
            )
            self._repo.link_intent_order(intent.intent_id, key)
            self._repo.audit(now, "BROKER_ORDER", "executor", key, {"intent_id": intent.intent_id, "kind": order.kind})
            self._set_state(intent.intent_id, IntentState.ACCEPTED)

    # --- 통보 반영 (OrderTracker, T-001 최소) ------------------------------------
    def process_events(self) -> None:
        """통보를 하나씩 반영한다. 저장에 실패하면 남은 통보를 보관하고 예외를 올린다 (다음 호출에서 이어서 처리)."""
        self._pending.extend(self._broker.poll_events())
        while self._pending:
            with self._repo.tx():
                self._apply_event(self._pending[0])
            self._pending.pop(0)

    def _anomaly(self, ev: OrderEvent, reason: str) -> None:
        """반영할 수 없는 통보. 원장을 바꾸지 않고 기록만 한다. 대조 불일치 정지(K4)는 T-008."""
        self._repo.audit(self._clock.now(), "EVENT_ANOMALY", "tracker", ev.order_no, {
            "kind": ev.kind, "fill_id": ev.fill_id, "quantity": str(ev.quantity), "price": str(ev.price),
            "reason": reason})

    def _apply_event(self, ev: OrderEvent) -> None:
        now = self._clock.now()
        order = self._repo.broker_order_by_no(ev.order_no)
        if order is None or order.intent_id is None:
            self._repo.audit(now, "UNMATCHED_EVENT", "tracker", ev.order_no, {"kind": ev.kind})  # 대조 대상 (T-008)
            return
        intent = self._repo.intent(order.intent_id)
        if ev.kind == "FILL":
            self._apply_fill(order, intent, ev, now)
        elif ev.kind == "CANCELLED":
            self._repo.update_broker_order(order.order_key, order.filled_quantity, "CANCELLED")
            if not intent.state.is_terminal:
                self._close(intent, IntentState.CANCELLED, "취소 확인")
            for c in self._repo.active_cancel_for(order.order_key):
                self._set_state(c.intent_id, IntentState.DONE, "취소 확인")
        elif ev.kind == "REJECTED":
            self._repo.update_broker_order(order.order_key, order.filled_quantity, "REJECTED")
            if not intent.state.is_terminal:
                self._close(intent, IntentState.REJECTED, "접수 후 거부")

    def _apply_fill(self, order, intent: IntentRow, ev: OrderEvent, now) -> None:
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
            return

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
        if rsv is not None:
            self._repo.consume_reservation(rsv.reservation_id, rsv.consumed + ev.quantity * intent.price)

        own = self._repo.active_ownership(intent.account, intent.instrument)
        if intent.side == Side.BUY.value and own is not None and own.state == "RESERVED":
            self._repo.set_ownership_state(own.ownership_id, "CONFIRMED")

        if intent_filled >= intent.quantity:
            self._close(self._repo.intent(intent.intent_id), IntentState.FILLED, None)
        else:
            self._set_state(intent.intent_id, IntentState.PARTIAL)

    # --- 공통 ---------------------------------------------------------------
    def _set_state(self, intent_id: str, state: IntentState, reason: str | None = None) -> None:
        self._repo.set_intent_state(intent_id, state, reason)
        self._repo.audit(self._clock.now(), "INTENT_STATE", "executor", intent_id, {"state": state.value, "reason": reason})

    def _close(self, intent: IntentRow, state: IntentState, reason: str | None) -> None:
        """의도를 종결하고 예약·소유권을 정리한다 (02 ST-04·ST-05)."""
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
                intent.account, intent.instrument, Side.SELL.value):
            self._repo.set_ownership_state(own.ownership_id, "RELEASED", "전량 청산")
