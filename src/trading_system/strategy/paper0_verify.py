"""PAPER-0 인프라 검증 전략 `paper0-verify` (T-016, 04 §4.1·D-076, A-037). 수익 목적이 아니다.

- 정해진 종목(U-A)에 고정 수량(S-4, 기본 1주) 시장가로 진입하고, 보유 거래일 수가 `hold_days` 이상이면 전량 시장가 청산.
- 판정 시각(배포 설정 eval_times, 예: 09:30·14:30)마다 평가: 보유 없음 → 진입, 보유 중 → 청산 → 하루 1회 왕복.
- 데이터 품질 실패·미체결 주문 대기·거래 상태 이상이면 신호를 내지 않는다. 게이트·한도·정지는 실운영과 같다.
- 순수 함수: 스냅샷과 파라미터만 본다 (00 D-002).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.sizing import size_fixed_quantity
from trading_system.domain.strategy import (
    EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec)

from .base import ParamValues, make_signal

VALID_FOR = timedelta(minutes=10)

SPEC = StrategySpec(
    strategy_id="paper0-verify", name="PAPER-0 인프라 검증", version=1,
    description="키움 모의 환경 인프라 검증용(04 §4.1). 고정 수량 시장가 진입·청산을 반복해 주문·체결·통보·대조를 관찰한다. "
                "수익 목적 아님",
    markets=("M1",), universe_method="U-A", universe=("005930",),
    capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"),
    data_requirements="일봉 종가 1개(품질 확인용), 수정주가. 워밍업 없음",
    trigger="CAP-TRG-01 배포 설정 eval_times (KST, 예: 09:30·14:30)",
    rules=(
        RuleSpec("EXIT-1", "EXIT", ("상태 입력",), "CAP-TRG-01",
                 "보유(이 전략 소유) AND 보유 거래일 수 >= hold_days AND 미체결 없음", "청산 전량 시장가",
                 "데이터 품질 실패 또는 미체결 주문 있음", 1),
        RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                 "보유 없음 AND 미체결 없음 AND 거래 상태 이상 없음", "진입 S-4 qty 시장가",
                 "데이터 품질 실패, 미체결 주문 있음, 거래 상태(정지·VI·주의 등) 있음", 1),
    ),
    params=(
        ParamSpec("qty", "int", "주", "진입 수량(고정)", Decimal("1"), Decimal("10")),
        ParamSpec("hold_days", "int", "거래일", "청산까지 최소 보유 거래일 수(0 = 같은 날 다음 판정에서 청산)",
                  Decimal("0"), Decimal("5")),
    ),
    signal_format="00 §2.5", validity_rule="판정 기준 시각 + 10분", validity_max_minutes=10,
    unfilled_policy="시스템 기본", sizing="S-4", holding="0~5거래일 (hold_days)",
    limits=StrategyLimits(l7_max_order_amount=Decimal("1000000")),
)


class Paper0Verify:
    def __init__(self) -> None:
        self.spec = SPEC

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        hold_days = int(str(params["hold_days"]))
        for v in snapshot.instruments:
            if v.instrument not in self.spec.universe or not v.data_ok or v.has_pending_order:
                continue
            ref = v.last_price if v.last_price is not None else (v.daily_bars[-1].close if v.daily_bars else None)
            if ref is None:
                continue
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                if pos.holding_days >= hold_days:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                           pos.quantity, OrderType.MARKET, ref, VALID_FOR))
            elif pos is None or pos.quantity == 0:
                if v.statuses:
                    continue
                qty = size_fixed_quantity(Decimal(str(params["qty"])))
                if qty > 0:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                           qty, OrderType.MARKET, ref, VALID_FOR))
        return out
