"""실제 전략 후보 (T-018 준비, A-044 3단계) — **제안, 미등록**. 사용자 결정(T-018)·등록·승인 전에는 PAPER에 쓰지 않는다.

오프라인 백테스트 비교용이다(docs/strategies/candidates-2026-10-03.md). 세 후보 모두 일봉 종가 기준, 판정 1일 1회,
손절가를 신호에 실어 L1 = (진입가 − 손절가) × 수량으로 계산되게 한다(00 §2.5, A-029 Q3). 크기는 S-3 위험 기반
(기준 금액 × risk_ratio)·종목 비중 상한(기준 금액 × max_weight)·1회 주문 금액 L7(100만 원, A-034) 중 작은 값. 손절은 판정 시각의 종가 판정
(장중 손절 주문 없음 — CAP-ORD 미지원)이므로 실제 손실은 손절가보다 클 수 있다(갭).

- TrendSMA (cand-trend-sma): SMA(fast) > SMA(slow) 진입, 반대면 청산. 손절 = 진입 평균가 − atr_mult × ATR.
- Breakout (cand-breakout): 전일까지 entry_n일 최고 종가 돌파 진입, exit_n일 최저 종가 이탈 청산(손절 겸용).
- MeanRevRSI (cand-meanrev-rsi): 종가 > SMA(trend_n)에서 RSI(2) < rsi_entry 진입, 종가 > SMA(5) 또는
  hold_max 거래일 경과 또는 손절(평균가 × (1 − stop_pct)) 청산.
순수 함수: 스냅샷과 파라미터만 본다 (00 D-002).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.sizing import size_fixed_amount, size_ratio, size_risk
from trading_system.domain.strategy import (
    Bar, EvaluationSnapshot, InstrumentView, ParamSpec, RuleSpec, StrategyLimits, StrategySpec)

from .base import ParamValues, make_signal

VALID_FOR = timedelta(minutes=10)
WORST_PRICE_FACTOR = Decimal("1.3")   # 시장가 주문 금액 = 상한가 기준(가격제한폭 30%, market.paper.toml)
L7_AMOUNT = Decimal("1000000")   # 1회 주문 금액 상한 = PAPER 계좌 L7 (A-034) — 크기 산정에도 적용
_COMMON_PARAMS = (
    ParamSpec("risk_ratio", "decimal", "비율", "1회 위험 = 기준 금액 × 값 (L1 0.5% 이하)", Decimal("0.001"), Decimal("0.005")),
    ParamSpec("max_weight", "decimal", "비율", "종목 비중 상한 (L2 10% 이하)", Decimal("0.01"), Decimal("0.10")),
)


def _spec(sid: str, name: str, desc: str, rules: tuple[RuleSpec, ...], params: tuple[ParamSpec, ...],
          data: str, holding: str) -> StrategySpec:
    return StrategySpec(
        strategy_id=sid, name=name, version=1, description=desc + " — 제안(미등록, T-018 결정 전)",
        markets=("M1",), universe_method="U-A", universe=("005930",),
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"),
        data_requirements=data, trigger="CAP-TRG-01 배포 설정 eval_times (1일 1회)", rules=rules,
        params=params + _COMMON_PARAMS, signal_format="00 §2.5", validity_rule="판정 기준 시각 + 10분",
        validity_max_minutes=10, unfilled_policy="시스템 기본", sizing="S-3", holding=holding,
        limits=StrategyLimits(l7_max_order_amount=L7_AMOUNT),
    )


def _closes(v: InstrumentView) -> list[Decimal]:
    return [b.close for b in v.daily_bars]


def _sma(xs: list[Decimal], n: int) -> Decimal | None:
    return sum(xs[-n:], Decimal("0")) / n if len(xs) >= n else None


def _atr(bars: tuple[Bar, ...], n: int) -> Decimal | None:
    if len(bars) < n + 1:
        return None
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs, Decimal("0")) / n


def _rsi(xs: list[Decimal], n: int) -> Decimal | None:
    if len(xs) < n + 1:
        return None
    ch = [b - a for a, b in zip(xs[-n - 1:-1], xs[-n:])]
    up = sum((c for c in ch if c > 0), Decimal("0"))
    down = -sum((c for c in ch if c < 0), Decimal("0"))
    if down == 0:
        return Decimal("100")
    rs = up / down
    return Decimal("100") - Decimal("100") / (1 + rs)


def _size(snapshot: EvaluationSnapshot, params: ParamValues, price: Decimal, stop: Decimal) -> Decimal:
    base = snapshot.base_amount
    by_risk = size_risk(base * Decimal(str(params["risk_ratio"])), price, stop)
    by_weight = size_ratio(base, Decimal(str(params["max_weight"])), price)
    by_l7 = size_fixed_amount(L7_AMOUNT, price * WORST_PRICE_FACTOR)   # 게이트는 시장가를 상한가로 평가
    return min(by_risk, by_weight, by_l7)


def _ready(v: InstrumentView, universe: tuple[str, ...]) -> bool:
    return v.instrument in universe and v.data_ok and not v.has_pending_order and bool(v.daily_bars)


def _held(v: InstrumentView) -> bool:
    return v.position is not None and v.position.owned_by_me and v.position.quantity > 0


def _entry(spec: StrategySpec, snapshot: EvaluationSnapshot, params: ParamValues, v: InstrumentView,
           rule: str, stop: Decimal) -> Signal | None:
    if v.statuses:
        return None
    price = v.last_price if v.last_price is not None else v.daily_bars[-1].close
    if stop <= 0 or stop >= price:
        return None
    qty = _size(snapshot, params, price, stop)
    if qty <= 0:
        return None
    return make_signal(spec, snapshot, v.instrument, rule, RequestType.ENTRY, qty, OrderType.MARKET, price, VALID_FOR,
                       stop_price=stop)


def _exit(spec: StrategySpec, snapshot: EvaluationSnapshot, v: InstrumentView, rule: str) -> Signal:
    price = v.last_price if v.last_price is not None else v.daily_bars[-1].close
    assert v.position is not None
    return make_signal(spec, snapshot, v.instrument, rule, RequestType.EXIT, v.position.quantity, OrderType.MARKET,
                       price, VALID_FOR)


class TrendSMA:
    def __init__(self) -> None:
        self.spec = _spec(
            "cand-trend-sma", "이동평균 추세", "SMA(fast)>SMA(slow) 동안 보유",
            (RuleSpec("EXIT-STOP", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 AND 종가 <= 평균가 − atr_mult×ATR", "청산 전량 시장가", "데이터 품질 실패", 1),
             RuleSpec("EXIT-1", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01", "보유 AND SMA(fast) <= SMA(slow)",
                      "청산 전량 시장가", "데이터 품질 실패", 2),
             RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 없음 AND SMA(fast) > SMA(slow)", "진입 S-3 시장가(손절가 포함)", "데이터 품질 실패·거래 상태 이상", 3)),
            (ParamSpec("fast", "int", "거래일", "단기 이동평균", Decimal("5"), Decimal("50")),
             ParamSpec("slow", "int", "거래일", "장기 이동평균", Decimal("20"), Decimal("200")),
             ParamSpec("atr_n", "int", "거래일", "ATR 기간", Decimal("5"), Decimal("30")),
             ParamSpec("atr_mult", "decimal", "배", "손절 ATR 배수", Decimal("1"), Decimal("5"))),
            "일봉 OHLC, 워밍업 slow+1거래일", "추세 지속 기간(수일~수개월)")

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        fast, slow, atr_n = int(str(params["fast"])), int(str(params["slow"])), int(str(params["atr_n"]))
        mult = Decimal(str(params["atr_mult"]))
        for v in snapshot.instruments:
            if not _ready(v, self.spec.universe):
                continue
            xs = _closes(v)
            f, s, atr = _sma(xs, fast), _sma(xs, slow), _atr(v.daily_bars, atr_n)
            if f is None or s is None or atr is None:
                continue
            if _held(v):
                assert v.position is not None
                if xs[-1] <= v.position.avg_price - mult * atr:
                    out.append(_exit(self.spec, snapshot, v, "EXIT-STOP"))
                elif f <= s:
                    out.append(_exit(self.spec, snapshot, v, "EXIT-1"))
            elif (v.position is None or v.position.quantity == 0) and f > s:
                sig = _entry(self.spec, snapshot, params, v, "ENTRY-1", (xs[-1] - mult * atr).quantize(Decimal("1")))
                if sig is not None:
                    out.append(sig)
        return out


class Breakout:
    def __init__(self) -> None:
        self.spec = _spec(
            "cand-breakout", "가격 돌파", "entry_n일 최고 종가 돌파 진입, exit_n일 최저 종가 이탈 청산",
            (RuleSpec("EXIT-1", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 AND 종가 < 전일까지 exit_n일 최저 종가", "청산 전량 시장가", "데이터 품질 실패", 1),
             RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 없음 AND 종가 > 전일까지 entry_n일 최고 종가", "진입 S-3 시장가(손절가 = exit_n일 최저)",
                      "데이터 품질 실패·거래 상태 이상", 2)),
            (ParamSpec("entry_n", "int", "거래일", "돌파 기준 기간", Decimal("10"), Decimal("60")),
             ParamSpec("exit_n", "int", "거래일", "이탈 기준 기간", Decimal("5"), Decimal("30"))),
            "일봉 종가, 워밍업 entry_n+1거래일", "추세 지속 기간")

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        en, ex = int(str(params["entry_n"])), int(str(params["exit_n"]))
        for v in snapshot.instruments:
            if not _ready(v, self.spec.universe):
                continue
            xs = _closes(v)
            if len(xs) < max(en, ex) + 1:
                continue
            hi, lo, last = max(xs[-en - 1:-1]), min(xs[-ex - 1:-1]), xs[-1]
            if _held(v):
                if last < lo:
                    out.append(_exit(self.spec, snapshot, v, "EXIT-1"))
            elif (v.position is None or v.position.quantity == 0) and last > hi:
                sig = _entry(self.spec, snapshot, params, v, "ENTRY-1", lo)
                if sig is not None:
                    out.append(sig)
        return out


class MeanRevRSI:
    def __init__(self) -> None:
        self.spec = _spec(
            "cand-meanrev-rsi", "단기 평균회귀", "상승 추세 중 RSI(2) 과매도 진입, 단기 반등 청산",
            (RuleSpec("EXIT-STOP", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 AND 종가 <= 평균가×(1−stop_pct)", "청산 전량 시장가", "데이터 품질 실패", 1),
             RuleSpec("EXIT-1", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 AND (종가 > SMA(5) OR 보유 거래일 >= hold_max)", "청산 전량 시장가", "데이터 품질 실패", 2),
             RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01",
                      "보유 없음 AND 종가 > SMA(trend_n) AND RSI(2) < rsi_entry", "진입 S-3 시장가(손절가 포함)",
                      "데이터 품질 실패·거래 상태 이상", 3)),
            (ParamSpec("trend_n", "int", "거래일", "추세 필터 이동평균", Decimal("50"), Decimal("200")),
             ParamSpec("rsi_entry", "decimal", "점", "RSI(2) 진입 상한", Decimal("1"), Decimal("30")),
             ParamSpec("stop_pct", "decimal", "비율", "손절 비율", Decimal("0.02"), Decimal("0.10")),
             ParamSpec("hold_max", "int", "거래일", "최대 보유", Decimal("1"), Decimal("10"))),
            "일봉 종가, 워밍업 trend_n거래일", "1~hold_max거래일")

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        tn, hold_max = int(str(params["trend_n"])), int(str(params["hold_max"]))
        rsi_entry, stop_pct = Decimal(str(params["rsi_entry"])), Decimal(str(params["stop_pct"]))
        for v in snapshot.instruments:
            if not _ready(v, self.spec.universe):
                continue
            xs = _closes(v)
            trend, sma5, rsi = _sma(xs, tn), _sma(xs, 5), _rsi(xs, 2)
            if trend is None or sma5 is None or rsi is None:
                continue
            if _held(v):
                assert v.position is not None
                if xs[-1] <= v.position.avg_price * (1 - stop_pct):
                    out.append(_exit(self.spec, snapshot, v, "EXIT-STOP"))
                elif xs[-1] > sma5 or v.position.holding_days >= hold_max:
                    out.append(_exit(self.spec, snapshot, v, "EXIT-1"))
            elif (v.position is None or v.position.quantity == 0) and xs[-1] > trend and rsi < rsi_entry:
                price = v.last_price if v.last_price is not None else xs[-1]
                sig = _entry(self.spec, snapshot, params, v, "ENTRY-1", (price * (1 - stop_pct)).quantize(Decimal("1")))
                if sig is not None:
                    out.append(sig)
        return out
