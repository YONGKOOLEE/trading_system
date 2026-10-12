"""시험용 배분 규칙 (CP-034 T-042). 실제 운용 규칙 아님, 가상 규칙·가상 지수.

`t-regime`: 지수 마지막 확정 종가 > 최근 sma_days일 단순 평균이면 지정 전략 신규 진입 허용.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tests.support.sample_strategies import CAPS_ALL_SUPPORTED
from trading_system.domain.allocation import AllocationDecision, IndexView
from trading_system.domain.strategy import ParamSpec, RuleSpec, StrategyLimits, StrategySpec

CAPS_WITH_INDEX = {**CAPS_ALL_SUPPORTED, "CAP-DAT-05d": {"M1": "지원", "M2": "미지원", "M3": "미지원"}}


def regime_spec(targets: tuple[str, ...] = ("t-cross",), **over: object) -> StrategySpec:
    spec = StrategySpec(
        strategy_id="t-regime", name="시험용 국면 배분 규칙", description="가상 규칙. 시험 전용", version=1,
        markets=("M1",), universe_method="U-A", universe=(), kind="ALLOCATION", allocation_targets=targets,
        capabilities=("CAP-TRG-01", "CAP-DAT-05d"),
        data_requirements="지수 일봉 sma_days개 (CAP-DAT-05d, 전 거래일까지 확정값)",
        trigger="CAP-TRG-01 지정 전략 판정 직전",
        rules=(RuleSpec("ALLOC-1", "ALLOCATION", ("CAP-DAT-05d",), "CAP-TRG-01",
                        "지수 종가 > 지수 SMA(sma_days)", "지정 전략 신규 진입 허용", "지수 결측·지연·품질 실패", 1),),
        params=(ParamSpec("sma_days", "int", "거래일", "지수 단순 이동평균 기간", Decimal("2"), Decimal("300")),),
        signal_format="배분 결정(허용 여부)", validity_rule="해당 없음", validity_max_minutes=1,
        unfilled_policy="시스템 기본", sizing="해당 없음", holding="해당 없음", limits=StrategyLimits(),
    )
    return replace(spec, **over)  # type: ignore[arg-type]


class IndexAboveSma:
    index_code = "001"

    def __init__(self, spec: StrategySpec | None = None) -> None:
        self.spec = spec or regime_spec()
        self.views: list[IndexView] = []

    def decide(self, index: IndexView, params: dict) -> AllocationDecision:
        self.views.append(index)
        n = int(str(params["sma_days"]))
        if len(index.bars) < n:
            return AllocationDecision(False, f"지수 일봉 {len(index.bars)}개 < {n}")
        closes = [b.close for b in index.bars[-n:]]
        sma = sum(closes, Decimal("0")) / n
        last = index.bars[-1].close
        return AllocationDecision(last > sma, "종가 > SMA" if last > sma else "종가 <= SMA",
                                  {"close": str(last), "sma": str(sma)})


class BrokenRule(IndexAboveSma):
    def decide(self, index: IndexView, params: dict) -> AllocationDecision:
        raise RuntimeError("규칙 오류(시험)")
