"""시험용 표본 전략 (실제 매매전략 아님, 가상 규칙·가상 종목).

`t-cross`: 일봉 종가가 진입 기준선보다 높고 보유가 없으면 진입(S-4 고정 수량), 보유 중 종가가 청산선보다 낮으면 청산.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.sizing import size_fixed_quantity
from trading_system.domain.strategy import EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec
from trading_system.strategy.base import ParamValues, make_signal

CAPS_ALL_SUPPORTED = {cap: {"M1": "지원", "M2": "미지원", "M3": "미지원"} for cap in (
    "CAP-TRG-01", "CAP-TRG-02", "CAP-DAT-01", "CAP-DAT-02", "CAP-ORD-01", "CAP-ORD-02", "CAP-ORD-03",
    "CAP-INS-01", "CAP-INS-02")}


def cross_spec(strategy_id: str = "t-cross", version: int = 1, **over: object) -> StrategySpec:
    spec = StrategySpec(
        strategy_id=strategy_id, name="시험용 교차 전략", description="가상 규칙. 시험 전용", version=version,
        markets=("M1",), universe_method="U-A", universe=("TST-A", "TST-B", "TST-C", "TST-D", "TST-E"),
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"),
        data_requirements="일봉 종가 2개, 수정주가", trigger="CAP-TRG-01 매 거래일 09:05 KST",
        rules=(
            RuleSpec("EXIT-1", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01", "보유 AND 종가 < exit_level",
                     "청산 전량", "데이터 품질 실패", 1),
            RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01", "보유 없음 AND 종가 > entry_level",
                     "진입 S-4 qty", "데이터 품질 실패", 1),
        ),
        params=(
            ParamSpec("entry_level", "decimal", "KRW", "진입 기준선", Decimal("1"), Decimal("1000000")),
            ParamSpec("exit_level", "decimal", "KRW", "청산 기준선", Decimal("1"), Decimal("1000000")),
            ParamSpec("qty", "int", "주", "진입 수량", Decimal("1"), Decimal("10000")),
        ),
        signal_format="00 §2.5", validity_rule="판정 기준 시각 + 30분", validity_max_minutes=30,
        unfilled_policy="시스템 기본", sizing="S-4", holding="1~20거래일", limits=StrategyLimits(),
    )
    return replace(spec, **over)  # type: ignore[arg-type]


PARAMS = {"entry_level": "9000", "exit_level": "8000", "qty": 10}


class CrossStrategy:
    def __init__(self, spec: StrategySpec | None = None) -> None:
        self.spec = spec or cross_spec()

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        for v in snapshot.instruments:
            if not v.data_ok or not v.daily_bars:
                continue
            close = v.daily_bars[-1].close
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                if close < params["exit_level"]:  # type: ignore[operator]
                    out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                           pos.quantity, OrderType.MARKET, close, timedelta(minutes=30)))
            elif close > params["entry_level"]:  # type: ignore[operator]
                qty = size_fixed_quantity(Decimal(params["qty"]))  # type: ignore[arg-type]
                if qty > 0:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                           qty, OrderType.MARKET, close, timedelta(minutes=30)))
        return out
