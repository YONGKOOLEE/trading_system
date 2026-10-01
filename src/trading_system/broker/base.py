"""브로커 어댑터 공통 인터페이스 (R-16, 03 §5.1·D-043).

주문 변경 함수는 결과를 4분류(SubmitOutcome)로 돌려주고, 자동 재시도하지 않는다.
이 함수들은 execution 패키지에서만 호출한다 (V-94).
"""

from __future__ import annotations

from typing import Protocol

from trading_system.domain.models import OrderEvent, OrderRequest, SubmitResult


class BrokerAdapter(Protocol):
    def place_order(self, request: OrderRequest) -> SubmitResult: ...

    def cancel_order(self, request: OrderRequest) -> SubmitResult: ...

    def poll_events(self) -> list[OrderEvent]: ...
