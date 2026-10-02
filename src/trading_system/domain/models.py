"""도메인 값 객체 (02-data-model). 금액·가격·수량은 Decimal, 시각은 시간대 있는 값."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .clock import require_aware
from .enums import GateResult, Check, Mode, OrderType, RequestSource, RequestType, Side, SubmitOutcome


def _require_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name}은 Decimal이어야 한다 (float 금지)")
    if not value.is_finite():
        raise ValueError(f"{name}이 유한한 값이 아니다")
    return value


def signal_id_of(strategy_id: str, version: int, evaluation_id: str, instrument: str, rule_id: str) -> str:
    """결정적 신호 ID (00 §2.5, 02 ENT-11)."""
    key = "\x1f".join([strategy_id, str(version), evaluation_id, instrument, rule_id])
    return "sig-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Account:
    alias: str
    mode: Mode
    broker_env: str  # fake / 모의 / 실전


@dataclass(frozen=True)
class Signal:
    """전략 출력 (00 §2.5). 전략은 이 형식 밖의 출력을 할 수 없다."""

    strategy_id: str
    strategy_version: int
    evaluation_id: str
    account: str
    instrument: str
    rule_id: str
    kind: RequestType
    quantity: Decimal
    order_type: OrderType
    price: Decimal
    as_of: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        if not self.kind.is_new_order:
            raise ValueError("전략 신호는 진입·증액·감액·청산만 낼 수 있다 (00 D-005)")
        _require_decimal("quantity", self.quantity)
        _require_decimal("price", self.price)
        if self.quantity <= 0:
            raise ValueError("수량은 0보다 커야 한다")
        require_aware(self.as_of)
        require_aware(self.valid_until)

    @property
    def signal_id(self) -> str:
        return signal_id_of(self.strategy_id, self.strategy_version, self.evaluation_id, self.instrument, self.rule_id)

    @property
    def owner(self) -> str:
        return f"{self.strategy_id}:{self.strategy_version}"


@dataclass(frozen=True)
class GateRequest:
    """게이트 입력 (02 ENT-14). 신호·운영 요청·정지 정책·실행 정책 중 하나에서 만든다."""

    source: RequestSource
    request_type: RequestType
    account: str
    instrument: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    price: Decimal
    idem_key: str
    owner: str | None = None
    valid_until: datetime | None = None
    signal_id: str | None = None
    target_order_key: str | None = None

    def __post_init__(self) -> None:
        _require_decimal("quantity", self.quantity)
        _require_decimal("price", self.price)

    @property
    def is_risk_increasing(self) -> bool:
        if self.request_type in (RequestType.ENTRY, RequestType.INCREASE):
            return True
        if self.request_type is RequestType.MODIFY:
            return self.side is Side.BUY
        return False

    @classmethod
    def from_signal(cls, s: Signal) -> GateRequest:
        side = s.kind.default_side
        assert side is not None
        return cls(
            source=RequestSource.SIGNAL,
            request_type=s.kind,
            account=s.account,
            instrument=s.instrument,
            side=side,
            quantity=s.quantity,
            order_type=s.order_type,
            price=s.price,
            idem_key=s.signal_id,
            owner=s.owner,
            valid_until=s.valid_until,
            signal_id=s.signal_id,
        )

    @classmethod
    def cancel(cls, source: RequestSource, account: str, instrument: str, side: Side,
               target_order_key: str, idem_key: str, owner: str | None) -> GateRequest:
        return cls(
            source=source, request_type=RequestType.CANCEL, account=account, instrument=instrument,
            side=side, quantity=Decimal("0"), order_type=OrderType.MARKET, price=Decimal("0"),
            idem_key=idem_key, owner=owner, target_order_key=target_order_key,
        )


@dataclass(frozen=True)
class ApprovedIntent:
    """게이트가 승인하고 영속한 주문 의도. gate 패키지에서만 생성한다 (V-94)."""

    intent_id: str
    decision_id: str
    request: GateRequest


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    result: GateResult
    failed_check: Check | None
    reason: str | None
    approved: ApprovedIntent | None = None
    checks: tuple[tuple[Check, bool], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OrderRequest:
    """브로커 어댑터 입력."""

    kind: str  # NEW / CANCEL
    account: str
    instrument: str
    side: str
    quantity: Decimal
    order_type: OrderType
    price: Decimal
    target_order_no: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    outcome: SubmitOutcome
    order_no: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OrderEvent:
    """브로커 통보 (정규화). kind: FILL / CANCELLED / REJECTED."""

    kind: str
    order_no: str
    fill_id: str | None = None
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    at: datetime | None = None
