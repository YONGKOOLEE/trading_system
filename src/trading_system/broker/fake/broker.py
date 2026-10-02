"""fake 브로커 (03 D-062). 결정적으로 동작하며, 동작 방식을 테스트에서 지정한다.

T-001 범위: 접수 후 전량 체결 / 접수만 / 거부 / 전송 전 실패 / 결과 불명.
장애 주입 확장은 T-006.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trading_system.domain.enums import SubmitOutcome
from trading_system.domain.models import OrderEvent, OrderRequest, SubmitResult


class FakeBehavior(StrEnum):
    FILL = "FILL"
    ACCEPT_ONLY = "ACCEPT_ONLY"
    REJECT = "REJECT"
    PRE_SEND_FAIL = "PRE_SEND_FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass
class _OpenOrder:
    request: OrderRequest
    remaining: Decimal


class FakeBroker:
    def __init__(self, behavior: FakeBehavior = FakeBehavior.FILL) -> None:
        self.behavior = behavior
        self.sent: list[OrderRequest] = []  # 실제로 "전송된" 요청만 기록
        self._seq = 0
        self._fill_seq = 0
        self._open: dict[str, _OpenOrder] = {}
        self._events: list[OrderEvent] = []

    def _next_no(self) -> str:
        self._seq += 1
        return f"{self._seq:07d}"

    def place_order(self, request: OrderRequest) -> SubmitResult:
        if self.behavior is FakeBehavior.PRE_SEND_FAIL:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason="fake: 연결 없음")
        self.sent.append(request)
        if self.behavior is FakeBehavior.REJECT:
            return SubmitResult(SubmitOutcome.REJECTED, reason="fake: 거부")
        if self.behavior is FakeBehavior.UNKNOWN:
            return SubmitResult(SubmitOutcome.UNKNOWN, reason="fake: 응답 없음")
        order_no = self._next_no()
        self._open[order_no] = _OpenOrder(request, request.quantity)
        if self.behavior is FakeBehavior.FILL:
            self._fill_all(order_no)
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=order_no)

    def cancel_order(self, request: OrderRequest) -> SubmitResult:
        if self.behavior is FakeBehavior.PRE_SEND_FAIL:
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason="fake: 연결 없음")
        self.sent.append(request)
        target = request.target_order_no
        if target is None or target not in self._open:
            return SubmitResult(SubmitOutcome.REJECTED, reason="fake: 취소 대상 없음")
        open_order = self._open.pop(target)
        self._events.append(OrderEvent(kind="CANCELLED", order_no=target, quantity=open_order.remaining))
        return SubmitResult(SubmitOutcome.ACCEPTED, order_no=self._next_no())

    def _fill_all(self, order_no: str) -> None:
        o = self._open.pop(order_no)
        self._fill_seq += 1
        self._events.append(OrderEvent(
            kind="FILL", order_no=order_no, fill_id=f"F{self._fill_seq:07d}",
            quantity=o.remaining, price=o.request.price,
        ))

    def inject(self, event: OrderEvent) -> None:
        """테스트용: 임의의 통보를 넣는다 (비정상·중복·순서 바뀐 통보 재현)."""
        self._events.append(event)

    def poll_events(self) -> list[OrderEvent]:
        events, self._events = self._events, []
        return events
