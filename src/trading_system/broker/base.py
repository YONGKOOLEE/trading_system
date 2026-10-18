"""브로커 어댑터 공통 인터페이스 (R-16, 03 §5.1·D-043).

주문 변경 함수는 결과를 4분류(SubmitOutcome)로 돌려주고, 자동 재시도하지 않는다.
이 함수들은 execution 패키지에서만 호출한다 (V-94).
조회 함수는 실패하면 BrokerQueryError를 올린다. 재시도는 호출자 정책이다 (03 §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from trading_system.domain.market import CashView
from trading_system.domain.models import (
    BrokerOrderRecord, BrokerPosition, FillRecord, OrderEvent, OrderRequest, SubmitResult,
)


class BrokerQueryError(RuntimeError):
    """조회 실패 (연결·호출 제한·응답 해석 불가). 결과를 알 수 없다는 뜻이지 "없음"이 아니다."""


class BrokerDataError(ValueError):
    """응답은 받았으나 형식·품질 위반(필드·경계·순서·OHLC 등) — 그 응답은 쓰지 않는다. 브로커 공통 (T-066 이력 채우기)."""


class LookupKind(StrEnum):
    """단건 주문 조회 결과 (M2, CP-024 D-111)."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"   # -2013: 접수 직후 반영 지연일 수 있다(B-U3 미확인) — "미접수" 확정이 아님
    ARCHIVED = "ARCHIVED"     # -2026: 보관됨. 전송 90일 이내 주문에 오면 이상 → K3


@dataclass(frozen=True)
class OrderLookup:
    kind: LookupKind
    record: BrokerOrderRecord | None = None


class SpotOrderQueries(Protocol):
    """M2 결과 불명 해소·대조용 조회 계약 (CP-024 D-111·D-103, 03 §5.1 확장). M1 어댑터는 구현하지 않는다.

    모두 실패하면 BrokerQueryError. 시각 창은 24시간 이내(allOrders·myTrades 조회 창, BN §7)."""

    def lookup_order(self, account: str, instrument: str, *, order_no: str | None,
                     client_order_id: str | None) -> OrderLookup: ...

    def orders_between(self, account: str, instrument: str, start: datetime,
                       end: datetime) -> list[BrokerOrderRecord]: ...

    def trades_between(self, account: str, instrument: str, start: datetime, end: datetime) -> list[FillRecord]: ...

    def query_balances(self, account: str) -> dict[str, tuple[Decimal, Decimal]]:
        """자산 → (free, locked) (D-103: 대조 값 = free + locked)."""
        ...


class BrokerAdapter(Protocol):
    def place_order(self, request: OrderRequest) -> SubmitResult: ...

    def cancel_order(self, request: OrderRequest) -> SubmitResult: ...

    def modify_order(self, request: OrderRequest) -> SubmitResult: ...

    def poll_events(self) -> list[OrderEvent]: ...

    def query_orders(self, account: str) -> list[BrokerOrderRecord]: ...

    def query_fills(self, account: str) -> list[FillRecord]: ...

    def query_positions(self, account: str) -> list[BrokerPosition]: ...

    def query_cash(self, account: str) -> CashView: ...
