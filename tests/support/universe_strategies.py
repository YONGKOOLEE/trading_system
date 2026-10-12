"""시험용 U-B 전략 (CP-034 T-041). 실제 매매전략 아님, 가상 규칙.

`t-ub`: 스냅샷의 종목 중 보유 없음·미체결 없음이면 진입(S-4 고정 수량), 이 전략 보유면 청산(전량).
`increase=True` 파라미터 대신 클래스 속성으로 보유 종목에 증액 신호를 내는 변형을 만든다(목록 밖 증액 거부 시험).
판정에 받은 스냅샷을 기록한다(등재 시각 확인용).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.strategy import EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec
from trading_system.strategy.base import ParamValues, make_signal

LIST_ID = "kr-picks"


def ub_spec(strategy_id: str = "t-ub", version: int = 1, **over: object) -> StrategySpec:
    spec = StrategySpec(
        strategy_id=strategy_id, name="시험용 U-B 전략", description="가상 규칙. 시험 전용", version=version,
        markets=("M1",), universe_method="U-B", universe=(), universe_list=LIST_ID,
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"),
        data_requirements="일봉 종가 1개", trigger="CAP-TRG-01 배포 설정 eval_times",
        rules=(
            RuleSpec("EXIT-1", "EXIT", ("상태 입력",), "CAP-TRG-01", "이 전략 보유", "청산 전량",
                     "데이터 품질 실패", 1),
            RuleSpec("INC-1", "INCREASE", ("상태 입력",), "CAP-TRG-01", "이 전략 보유 AND increase 변형",
                     "증액 qty", "데이터 품질 실패", 1),
            RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01", "보유 없음 AND 미체결 없음",
                     "진입 S-4 qty", "데이터 품질 실패", 1),
        ),
        params=(ParamSpec("qty", "int", "주", "진입 수량", Decimal("1"), Decimal("100")),),
        signal_format="00 §2.5", validity_rule="판정 기준 시각 + 30분", validity_max_minutes=30,
        unfilled_policy="시스템 기본", sizing="S-4", holding="1거래일", limits=StrategyLimits(),
    )
    return replace(spec, **over)  # type: ignore[arg-type]


class UbProbe:
    increase = False

    def __init__(self, spec: StrategySpec | None = None) -> None:
        self.spec = spec or ub_spec()
        self.snapshots: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        self.snapshots.append(snapshot)
        out: list[Signal] = []
        qty = Decimal(str(params["qty"]))
        for v in snapshot.instruments:
            if not v.data_ok or v.has_pending_order or not v.daily_bars:
                continue
            px = v.last_price or v.daily_bars[-1].close
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                kind, rule, q = ((RequestType.INCREASE, "INC-1", qty) if self.increase
                                 else (RequestType.EXIT, "EXIT-1", pos.quantity))
                out.append(make_signal(self.spec, snapshot, v.instrument, rule, kind, q, OrderType.MARKET, px,
                                       timedelta(minutes=30)))
            elif pos is None:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, qty,
                                       OrderType.MARKET, px, timedelta(minutes=30)))
        return out


class UbIncrease(UbProbe):
    increase = True
