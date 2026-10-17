"""시험 전용 1분봉 예제 전략 (CP-037 T-053). **후보 전략이 아니다** — 등록·PAPER·성과 판단에 쓰지 않는다.

`SessionRangeBreakout` (t-session-range v1): CAP-TRG-02(주기 판정) + CAP-DAT-02(1분봉) 경로를 끝까지 통과시키기 위한 단순 규칙.
- 판정 시각마다 1분봉 창(판정 직전 닫힌 봉까지 lookback개)에서 **같은 UTC 날짜**의 직전 range_minutes개 봉(마지막 닫힌 봉 제외)의
  최고가·최저가로 세션 구간을 정한다. 구간 봉이 모자라거나 날짜가 넘어가면 진입하지 않는다.
- ENTRY-1: 무보유, 분봉 입력 유효(minute_ok), 마지막 닫힌 봉 종가 > 구간 최고가, 판정 시각 < flat_time(UTC) → 고정 금액
  (notional USDT ÷ 최근가, qty_step 내림) 진입.
- EXIT-1: 내 소유 보유 중 분봉 입력 무효 → 청산(감시 입력을 잃음 — 위험 축소, DP-78로 엔진이 허용).
- EXIT-2: 내 소유 보유 중 마지막 종가 < 구간 최저가 → 청산. EXIT-3: 판정 시각 ≥ flat_time → 청산.
- 신호 기한 = min(판정 + N분/2, 다음 예정 판정 − 1초). 주문 유형·가격은 시스템이 정한다(D-104).
순수 함수: 스냅샷·파라미터만 본다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, time, timedelta
from decimal import Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.sizing import size_fixed_amount
from trading_system.domain.strategy import (
    EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec,
)
from trading_system.strategy.base import ParamValues, make_signal

D = Decimal
CAPS = ("CAP-TRG-02", "CAP-DAT-02", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05")
_IN = ("CAP-DAT-02", "상태 입력")

SPEC = StrategySpec(
    strategy_id="t-session-range", name="시험 전용 세션 구간 돌파(1분봉)", version=1,
    description="CP-037 T-053 시험 전용 예제 — 후보 전략 아님. 같은 UTC 날짜 직전 N분 구간 돌파 진입, 구간 이탈·마감 시각 청산",
    markets=("M2",), universe_method="U-A", universe=("BTCUSDT",), capabilities=CAPS,
    data_requirements="1분봉 닫힌 봉 lookback개(같은 UTC 날짜 range_minutes + 1개 이상 필요), 최근가. 수정 개념 없음",
    trigger="CAP-TRG-02 period_minutes N분 격자 (UTC 00:00 + k×N분 + 확정 여유, 주문 창 00:10~23:50)",
    rules=(
        RuleSpec("EXIT-1", "EXIT", _IN, "CAP-TRG-02", "내 소유 보유 AND 분봉 입력 무효", "청산 전량", "보유 없음", 1),
        RuleSpec("EXIT-2", "EXIT", _IN, "CAP-TRG-02", "내 소유 보유 AND 마지막 종가 < 구간 최저가", "청산 전량",
                 "구간 봉 부족", 2),
        RuleSpec("EXIT-3", "EXIT", _IN, "CAP-TRG-02", "내 소유 보유 AND 판정 시각 >= flat_time", "청산 전량", "보유 없음", 3),
        RuleSpec("ENTRY-1", "ENTRY", _IN, "CAP-TRG-02",
                 "무보유 AND 분봉 유효 AND 마지막 종가 > 같은 UTC 날짜 직전 range_minutes 봉 최고가 AND 판정 시각 < flat_time",
                 "진입 S-1 notional ÷ 최근가, qty_step 내림", "분봉 무효, 구간 봉 부족, 최근가 없음, 미체결 있음", 1),
    ),
    params=(
        ParamSpec("period_minutes", "int", "분", "주기 판정 격자 N분 (1440의 약수)", D("1"), D("720")),
        ParamSpec("range_minutes", "int", "분", "세션 구간 길이(같은 UTC 날짜 직전 봉 수)", D("2"), D("990")),
        ParamSpec("notional", "decimal", "USDT", "1회 진입 금액", D("10"), D("100000")),
        ParamSpec("qty_step", "decimal", "기초 자산", "수량 내림 단위 = LOT_SIZE stepSize", D("0.00000001"), D("1")),
        ParamSpec("flat_time", "str", "HH:MM (UTC)", "이 시각 이후 진입 없음·보유 청산"),
    ),
    signal_format="00 §2.5", validity_rule="min(판정 + N분/2, 다음 예정 판정 − 1초)", validity_max_minutes=360,
    unfilled_policy="시스템 기본", sizing="S-1", holding="같은 UTC 날짜 안 (flat_time 청산)", limits=StrategyLimits())


def _hhmm(text: object) -> time:
    hh, mm = str(text).split(":")
    return time(int(hh), int(mm))


class SessionRangeBreakout:
    def __init__(self, spec: StrategySpec = SPEC) -> None:
        self.spec = spec

    @staticmethod
    def validate_rules(params: ParamValues, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        out = []
        if D(str(params["qty_step"])) != lot_step:
            out.append(f"qty_step {params['qty_step']} ≠ 거래소 LOT_SIZE stepSize {lot_step}")
        if D(str(params["notional"])) < 2 * min_notional:
            out.append(f"notional {params['notional']} < 2 × minNotional {min_notional}")
        return out

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        n = int(str(params["period_minutes"]))
        rng = int(str(params["range_minutes"]))
        flat = _hhmm(params["flat_time"])
        at = snapshot.as_of.astimezone(UTC)
        valid = timedelta(seconds=30 * n)
        if snapshot.next_evaluation_at is not None:
            valid = min(valid, snapshot.next_evaluation_at - snapshot.as_of - timedelta(seconds=1))
        if valid <= timedelta(0):
            return []
        out: list[Signal] = []
        for v in snapshot.instruments:
            if v.instrument not in self.spec.universe or not v.data_ok or v.has_pending_order:
                continue
            bars = v.minute_bars
            window = bars[-(rng + 1):-1] if len(bars) >= rng + 1 else ()
            same_day = bool(window) and all(b.at.astimezone(UTC).date() == at.date() for b in window)  # type: ignore[union-attr]
            pos = v.position
            ref = v.last_price
            if pos is not None and pos.quantity > 0:
                if not pos.owned_by_me or ref is None or ref <= 0:
                    continue
                rule = None
                if not v.minute_ok:
                    rule = "EXIT-1"
                elif same_day and bars[-1].close < min(b.low for b in window):
                    rule = "EXIT-2"
                elif at.time() >= flat:
                    rule = "EXIT-3"
                if rule is not None:
                    out.append(make_signal(self.spec, snapshot, v.instrument, rule, RequestType.EXIT, pos.quantity,
                                           OrderType.MARKET, ref, valid))
                continue
            if not v.minute_ok or not same_day or at.time() >= flat or v.statuses or ref is None or ref <= 0:
                continue
            if bars[-1].close > max(b.high for b in window):
                qty = size_fixed_amount(D(str(params["notional"])), ref, D(str(params["qty_step"])))
                if qty > 0:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, qty,
                                           OrderType.LIMIT, ref, valid))
        return out


def session_range_for(symbol: str) -> SessionRangeBreakout:
    """같은 규칙, 다른 심볼(가상 심볼 시험용)."""
    return SessionRangeBreakout(replace(SPEC, universe=(symbol,)))
