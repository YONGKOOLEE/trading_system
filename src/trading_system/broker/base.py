"""브로커 어댑터 공통 인터페이스 (R-16, 03 §5.1·D-043).

주문 변경 함수는 결과를 4분류(SubmitOutcome)로 돌려주고, 자동 재시도하지 않는다.
이 함수들은 execution 패키지에서만 호출한다 (V-94).
조회 함수는 실패하면 BrokerQueryError를 올린다. 재시도는 호출자 정책이다 (03 §5.3).
"""

from __future__ import annotations

from typing import Protocol

from trading_system.domain.models import BrokerOrderRecord, FillRecord, OrderEvent, OrderRequest, SubmitResult


class BrokerQueryError(RuntimeError):
    """조회 실패 (연결·호출 제한·응답 해석 불가). 결과를 알 수 없다는 뜻이지 "없음"이 아니다."""


class BrokerAdapter(Protocol):
    def place_order(self, request: OrderRequest) -> SubmitResult: ...

    def cancel_order(self, request: OrderRequest) -> SubmitResult: ...

    def modify_order(self, request: OrderRequest) -> SubmitResult: ...

    def poll_events(self) -> list[OrderEvent]: ...

    def query_orders(self, account: str) -> list[BrokerOrderRecord]: ...

    def query_fills(self, account: str) -> list[FillRecord]: ...
