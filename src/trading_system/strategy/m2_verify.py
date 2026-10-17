"""M2 PAPER-0 인프라 검증 전략 `m2-verify` v1 (T-M2-15 확정안, **미승인**). 수익 목적이 아니다.
전략 승인과 T-M2-15 테스트넷 주문 전송 승인은 각각 별도로 필요하다(A-110 ①).

명세: docs/strategies/m2-verify/strategy.md. M1 paper0-verify v2(같은 날 왕복, split_time으로 진입·청산 판정 분리)를
M2(바이낸스 Spot 테스트넷, BTCUSDT)에 맞춘 것이다. 실행 경로는 CP-036(A-106): build_engine(market="M2")·SpotProfile 판정 입력
(D-207 — 보유 = 청산 가능 수량, D-208 내림, dust는 보유 없음), 시작 시 validate_rules(D-209, DP-54 형식), 등록은
`scripts/strategy.py --market M2`와 M2 PAPER-0 검증 예외(D-212).
- split_time(UTC) 전 판정: 무보유면 고정 금액(S-1 `notional` USDT) 진입 신호 1개. 수량 = notional ÷ 최근가를 `qty_step`
  (LOT_SIZE stepSize)으로 **내림**. 0이면 신호 없음.
- split_time 이후 판정: 이 전략 소유 보유가 있으면 전량 청산 신호.
- 주문 유형·가격은 전략이 정하지 않는다(CP-024 D-104·D-120): 의도를 만들 때 시스템이 매수 = IOC 지정가(최근가 × (1 + 완충)
  tick 내림), 매도 = 시세 정상이면 수량 지정 시장가 / 지연이면 IOC 지정가로 바꾼다(domain.trading_rules.apply_spot_terms).
  신호의 주문 유형(진입 LIMIT, 청산 MARKET)과 가격(최근가)은 명목값이다.
- 수동 보유(소유자 manual — 예: 테스트넷 지급 BTC)나 다른 전략 보유가 있으면 아무 신호도 내지 않는다(02 INV-16, 게이트 G6과 같음).
- 데이터 품질 실패·미체결 대기·심볼 상태 이상·최근가 없음이면 진입하지 않는다. 게이트·한도·정지는 실운영과 같다.
- 순수 함수: 스냅샷과 파라미터만 본다 (00 D-002). 시계·브로커·저장소·네트워크 접근 없음.
"""

from __future__ import annotations

from datetime import UTC, time, timedelta
from decimal import Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.sizing import size_fixed_amount
from trading_system.domain.strategy import (
    EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec)

from .base import ParamValues, make_signal
from .paper0_verify import parse_split_time

VALID_FOR = timedelta(minutes=10)

SPEC = StrategySpec(
    strategy_id="m2-verify", name="M2 PAPER-0 인프라 검증", version=1,
    description="바이낸스 Spot 테스트넷 인프라 검증용(04 2-M2.6, T-M2-15). 하루 1회 고정 금액 IOC 지정가 매수, 같은 UTC 날짜 "
                "뒤 판정에서 전량 시장가 매도로 주문·체결·사용자 스트림·대조·dust 경로를 관찰한다. 수익 목적 아님",
    markets=("M2",), universe_method="U-A", universe=("BTCUSDT",),
    # CAP-INS-05 크립토 현물(00 v0.5 §2.3, capabilities.md M2 "검증 중" — CP-036 D-212). 다섯 능력 = M2 PAPER-0 예외 허용 범위
    capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05"),
    data_requirements="최근가 1개(시세 신선도 60초는 게이트 G11), UTC 1d 확정 봉(품질 확인·최근가 없을 때 청산 기준가). "
                      "워밍업 없음, 수정 개념 없음(크립토)",
    trigger="CAP-TRG-01 배포 설정 eval_times (UTC, 예: 01:00·04:00), split_time(UTC) 전 판정은 진입만·이후 판정은 청산만",
    rules=(
        RuleSpec("EXIT-1", "EXIT", ("상태 입력",), "CAP-TRG-01",
                 "판정 시각 >= split_time AND 이 전략 소유 보유(청산 가능 수량 > 0, dust 아님) AND 미체결 없음",
                 "청산 전량(청산 가능 수량) — 시세 정상 시 시장가, 지연 시 IOC 지정가(시스템 규칙 D-104·D-120)",
                 "데이터 품질 실패, 미체결 주문 있음, 기준가(최근가·확정 봉) 없음", 1),
        RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                 "판정 시각 < split_time AND 보유 없음(dust는 없음으로 봄) AND 미체결 없음 AND 심볼 상태 이상 없음",
                 "진입 S-1 notional USDT ÷ 최근가, qty_step 내림 — IOC 지정가(시스템 규칙 D-104)",
                 "데이터 품질 실패, 미체결 주문 있음, 최근가 없음, 심볼 상태 이상, 수동·다른 전략 보유 있음, 수량 0", 1),
    ),
    params=(
        ParamSpec("notional", "decimal", "USDT", "진입 목표 금액(고정). 10 이상 = minNotional 5의 2배 → 내림 뒤에도 5 초과",
                  Decimal("10"), Decimal("50")),
        ParamSpec("qty_step", "decimal", "BTC", "수량 내림 단위 = 거래소 LOT_SIZE stepSize(시작 점검에서 일치 확인, validate_rules)",
                  Decimal("0.00000001"), Decimal("1")),
        ParamSpec("split_time", "str", "HH:MM (UTC)", "이 시각 전 판정은 진입만, 이후 판정은 청산만. 두 판정 시각 사이에 둔다"),
    ),
    signal_format="00 §2.5", validity_rule="판정 기준 시각 + 10분", validity_max_minutes=10,
    unfilled_policy="시스템 기본", sizing="S-1",
    holding="약 3시간(같은 UTC 날짜 진입·청산). 청산 실패 시 다음 청산 판정(다음 날)까지 보유",
    # notional 상한 50 × (1 + 매수 완충 0.5%) = 50.25 위 여유. 계좌 L7 100 USDT(A-064) 이하
    limits=StrategyLimits(l7_max_order_amount=Decimal("60")),
)


class M2Verify:
    def __init__(self) -> None:
        self.spec = SPEC

    @staticmethod
    def validate_deploy(params: ParamValues, eval_times: tuple[time, ...]) -> list[str]:
        """시작 전 배포 검사: split_time 형식, split_time 전 판정 시각이 정확히 1개(하루 진입 1회), 이후 1개 이상, 같은 시각 금지."""
        try:
            split = parse_split_time(params["split_time"])
        except (KeyError, ValueError) as exc:
            return [f"split_time: {exc}"]
        problems = []
        if any(t == split for t in eval_times):
            problems.append("split_time이 판정 시각과 같음 — 두 판정 시각 사이에 둔다")
        before = [t for t in eval_times if t < split]
        if len(before) != 1:
            problems.append(f"split_time 전 판정 시각은 정확히 1개여야 함 (하루 진입 1회) — 현재 {len(before)}개")
        if not any(t > split for t in eval_times):
            problems.append("split_time 이후 판정 시각이 1개 이상 있어야 함 (청산 판정)")
        return problems

    @staticmethod
    def validate_rules(params: ParamValues, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        """거래소 규칙(exchangeInfo 적재값)과의 일치 검사. M2 시작 때 app.wiring.spot_rule_problems가 유니버스 심볼마다
        부른다(CP-036 D-209·DP-54 형식: params, LOT_SIZE stepSize, minNotional). 문제가 있으면 시작 거부(주문 0).

        qty_step = LOT_SIZE stepSize, notional ≥ 2 × minNotional (내림 수량 k·step에서 k ≥ 1이면 k·step·가격 > notional/2)."""
        problems = []
        step = Decimal(str(params["qty_step"]))
        notional = Decimal(str(params["notional"]))
        if step != lot_step:
            problems.append(f"qty_step {step} ≠ 거래소 LOT_SIZE stepSize {lot_step}")
        if notional < 2 * min_notional:
            problems.append(f"notional {notional} < 2 × minNotional {min_notional} — 내림 뒤 최소 주문 금액 미달 가능")
        return problems

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        split = parse_split_time(params["split_time"])
        entry_phase = snapshot.as_of.astimezone(UTC).time() < split
        notional = Decimal(str(params["notional"]))
        step = Decimal(str(params["qty_step"]))
        out: list[Signal] = []
        for v in snapshot.instruments:
            if v.instrument not in self.spec.universe or not v.data_ok or v.has_pending_order:
                continue
            pos = v.position
            if pos is not None and pos.quantity > 0:
                if entry_phase or not pos.owned_by_me:
                    continue   # 진입 판정에서는 청산 안 함 / 수동·다른 전략 보유는 건드리지 않음 (INV-16)
                ref = v.last_price if v.last_price is not None else (v.daily_bars[-1].close if v.daily_bars else None)
                if ref is None or ref <= 0:
                    continue
                out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                       pos.quantity, OrderType.MARKET, ref, VALID_FOR))
                continue
            if not entry_phase or v.statuses or v.last_price is None or v.last_price <= 0:
                continue
            qty = size_fixed_amount(notional, v.last_price, step)
            if qty > 0:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       qty, OrderType.LIMIT, v.last_price, VALID_FOR))
        return out
