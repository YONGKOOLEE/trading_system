"""전략 인터페이스 (00 D-002·D-005, 03 §5.1 `Strategy.evaluate`).

전략은 판정 스냅샷과 파라미터만 보고 신호 목록을 돌려준다. 브로커·저장소·네트워크·현재 시각에 접근하지 않는다
(tests/architecture가 import로 강제). 같은 입력이면 같은 출력이어야 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.strategy import EvaluationSnapshot, StrategySpec

ParamValues = Mapping[str, object]


class Strategy(Protocol):
    spec: StrategySpec

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]: ...


def make_signal(spec: StrategySpec, snapshot: EvaluationSnapshot, instrument: str, rule_id: str,
                kind: RequestType, quantity: Decimal, order_type: OrderType, price: Decimal,
                valid_for: timedelta, stop_price: Decimal | None = None) -> Signal:
    """전략 구현이 신호를 만들 때 쓰는 도우미. 기한은 판정 기준 시각 + valid_for (상한은 판정 실행기가 검사)."""
    valid_until: datetime = snapshot.as_of + valid_for
    return Signal(
        strategy_id=spec.strategy_id, strategy_version=spec.version, evaluation_id=snapshot.evaluation_id,
        account=snapshot.account, instrument=instrument, rule_id=rule_id, kind=kind, quantity=quantity,
        order_type=order_type, price=price, as_of=snapshot.as_of, valid_until=valid_until, stop_price=stop_price,
    )
