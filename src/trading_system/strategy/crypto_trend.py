"""후보 crypto-trend v1 — BTC·ETH 현물(M2, 바이낸스) 일봉 수준 추세 추종을 60분마다 판정. 전략실 연구 후보(운영 미등록).

근거: research/2026-10-18-overnight-design.md(A·B·K·L·M·N·O·V·W), btc-trend v1(candidates/btc_trend)과 같은 수준·파라미터 값.
규칙(판정 = CAP-TRG-02 60분 주기, 매시 정각 + 확정 여유 — 운영 CP-040 D-258. 스냅샷 일봉은 확정된 UTC 일봉만(그날 진행 중 봉 제외),
판정 가격 = 판정 시점 시세 last_price(전략실 시뮬레이션은 직전 1시간 봉 종가로 근사)):
- ENTRY-1: 미보유 AND 판정 가격 > 확정 일봉 직전 entry_n개 종가 최고 AND 판정 가격 > 확정 일봉 종가 SMA(exit_n)(게이트 — 진입 즉시
  청산 상태 금지, 1시간 판정 헛매매 제거 2026-10-18 A) → 매수(지정가 — 운영이 IOC 가격을 정함, D-104).
  수량 = min(기준 × risk_ratio ÷ (stop_atr × ATR14), 기준 × max_weight ÷ 시세, order_cap ÷ (시세 × 1.006))을 종목 수량 단위로 내림.
  손절가 = 시세 − stop_atr × ATR14.
- EXIT-2(손절): 보유 AND 판정 가격 < 평균 매입가 − stop_atr × ATR14(현재 확정 일봉) → 전량 시장가. (시뮬레이션은 진입 시점 ATR 고정 —
  손절은 전체 2~3건뿐이라 영향 작음, research N. 운영 CAP-HLD-02 손절 감시를 쓰면 이 규칙 대신 시스템 감시.)
- EXIT-1(청산): 보유 AND 판정 가격 < 확정 일봉 종가 SMA(exit_n) → 전량 시장가.
- 우선순위 EXIT-2 > EXIT-1 > ENTRY-1. 종목마다 신호 1개, 종목끼리 독립(같은 계좌 현금 공유). 피라미딩 없음.
순수 함수. 파일·네트워크·현재 시각·난수 없음.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time, timedelta
from decimal import ROUND_FLOOR, Decimal

from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.models import Signal
from trading_system.domain.strategy import (
    Bar, EvaluationSnapshot, InstrumentView, ParamSpec, RuleSpec, StrategyLimits, StrategySpec)
from trading_system.strategy.base import ParamValues, make_signal

STRATEGY_ID = "crypto-trend"
VERSION = 1
# 종목 → 수량 단위(바이낸스 LOT_SIZE stepSize). BTCUSDT는 테스트넷 확인값(binance-spot.md), ETHUSDT 0.0001은 가정 — 운영 exchangeInfo로 확인
STEPS = {"BTCUSDT": Decimal("0.00001"), "ETHUSDT": Decimal("0.0001")}
PERIOD_EVALS_PER_DAY = 23          # 60분 주기 → 01:00~23:00 하루 23회(운영 CP-040 D-258·V-337, 00:00 판정 없음)
_ATR_N = 14
VALID_FOR = timedelta(minutes=30)
_INPUTS = ("CAP-DAT-01", "상태 입력")   # 판정 시세(last_price)는 조건 설명에 — 구조 규칙(선언 CAP-ID·상태 입력만)


def _sma(xs: Sequence[Decimal], n: int) -> Decimal | None:
    return sum(xs[-n:], Decimal("0")) / n if n > 0 and len(xs) >= n else None


def _atr(bars: Sequence[Bar], n: int = _ATR_N) -> Decimal | None:
    if len(bars) < n + 1:
        return None
    w = bars[-n - 1:]
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(w[:-1], w[1:])]
    return sum(trs, Decimal("0")) / n


def build_spec() -> StrategySpec:
    rules = (
        RuleSpec("EXIT-2", "EXIT", _INPUTS, "CAP-TRG-02", "보유 AND 판정 가격 < 평균 매입가 − stop_atr × ATR14(확정 일봉)",
                 "청산 가능 수량 전량 시장가(손절)", "데이터 없음·처리 중 주문", 1),
        RuleSpec("EXIT-1", "EXIT", _INPUTS, "CAP-TRG-02", "보유 AND 판정 가격 < 확정 일봉 종가 SMA(exit_n)",
                 "청산 가능 수량 전량 시장가", "데이터 없음·처리 중 주문", 2),
        RuleSpec("ENTRY-1", "ENTRY", _INPUTS, "CAP-TRG-02",
                 "미보유 AND 판정 가격 > 확정 일봉 직전 entry_n개 종가 최고 AND 판정 가격 > 확정 일봉 종가 SMA(exit_n)",
                 "S-3 매수(수량 = min(기준×risk_ratio ÷ (stop_atr×ATR14), 기준×max_weight ÷ 시세, order_cap ÷ (시세×1.006)), 손절가 = 시세 − stop_atr×ATR14)",
                 "데이터 없음·거래 상태 이상·처리 중 주문", 1),
    )
    params = (
        ParamSpec("entry_n", "int", "일", "돌파 기간(판정 가격이 확정 일봉 직전 n개 종가 최고를 넘음)", Decimal("5"), Decimal("200")),
        ParamSpec("exit_n", "int", "일", "청산 이동평균·진입 게이트 기간", Decimal("10"), Decimal("200")),
        ParamSpec("stop_atr", "decimal", "배", "손절 거리 = ATR14 × 값", Decimal("1"), Decimal("6")),
        ParamSpec("risk_ratio", "decimal", "비율", "종목별 1회 위험 = 기준 금액 × 값(2종목 반반 계좌의 5% = 0.025)", Decimal("0.001"), Decimal("0.10")),
        ParamSpec("max_weight", "decimal", "비율", "종목 상한(레버리지 없음 — 두 종목 합 ≤ 1)", Decimal("0.001"), Decimal("1")),
        ParamSpec("order_cap", "decimal", "USDT", "1회 주문 금액 상한(계좌 L7 이하로)", Decimal("5"), Decimal("1000000000")),
    )
    return StrategySpec(
        strategy_id=STRATEGY_ID, name="코인 추세 추종(일봉 수준·60분 판정·진입 게이트)", version=VERSION,
        description="확정 일봉 entry_n일 종가 최고를 판정 가격이 넘고 SMA(exit_n) 위면 매수, SMA 아래면 매도, ATR 손절 — 60분 판정, BTC·ETH — 전략실 연구 후보",
        markets=("M2",), universe_method="U-A", universe=tuple(STEPS),
        capabilities=("CAP-TRG-02", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-05"),
        data_requirements="확정 UTC 일봉 OHLC 워밍업 max(entry_n, exit_n, 15)봉 + 판정 시세", trigger="CAP-TRG-02 60분 주기(매시 정각 + 확정 여유)",
        rules=rules, params=params, signal_format="00 §2.5", validity_rule="판정 + 30분", validity_max_minutes=30,
        unfilled_policy="시스템 기본", sizing="S-3", holding="수 일~수 개월", limits=StrategyLimits())


class CryptoTrendV1:
    def __init__(self) -> None:
        self.spec = build_spec()

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        entry_n, exit_n = int(str(params["entry_n"])), int(str(params["exit_n"]))
        stop_atr, risk, maxw, cap = (Decimal(str(params[k])) for k in ("stop_atr", "risk_ratio", "max_weight", "order_cap"))
        out: list[Signal] = []
        for v in snapshot.instruments:
            if v.instrument not in STEPS or not v.data_ok or v.has_pending_order or not v.daily_bars or v.last_price is None:
                continue
            bars = v.daily_bars[-max(entry_n + 1, exit_n + 1, _ATR_N + 2):]
            xs = [b.close for b in bars]
            if len(xs) < max(entry_n, exit_n, _ATR_N + 1):
                continue
            price, atr, sma = v.last_price, _atr(bars), _sma(xs, exit_n)
            if atr is None or atr <= 0 or sma is None:
                continue
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                if price < pos.avg_price - stop_atr * atr:
                    out.append(self._sig(snapshot, v, "EXIT-2", RequestType.EXIT, pos.quantity, OrderType.MARKET))
                elif price < sma:
                    out.append(self._sig(snapshot, v, "EXIT-1", RequestType.EXIT, pos.quantity, OrderType.MARKET))
                continue
            if pos is not None and pos.quantity != 0:
                continue
            if price <= max(xs[-entry_n:]) or price <= sma or v.statuses:
                continue
            stop = price - stop_atr * atr
            if stop <= 0:
                continue
            base = snapshot.base_amount
            step = STEPS[v.instrument]
            qty = min(base * risk / (stop_atr * atr), base * maxw / price, cap / (price * Decimal("1.006")))  # 1.006 = IOC 완충 0.5% + 여유
            qty = (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if qty <= 0:
                continue
            out.append(self._sig(snapshot, v, "ENTRY-1", RequestType.ENTRY, qty, OrderType.LIMIT, stop))
        return out

    @staticmethod
    def validate_rules(params: ParamValues, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        """btc-trend F-2와 같은 서명(DP-54). 서명에 종목이 없어 '어느 종목의 수량 단위와도 같지 않으면' 거부한다 —
        종목별 대조는 운영이 서명에 종목을 넣거나 STEPS를 exchangeInfo로 맞춰야 정확해진다(인계 시 확인 항목)."""
        del params, min_notional
        if lot_step not in STEPS.values():
            return [f"거래소 LOT_SIZE stepSize {lot_step:f}가 전략 고정 수량 단위 {sorted(f'{s:f}' for s in STEPS.values())}와 다름"]
        return []

    def validate_deploy(self, params: ParamValues, eval_times: tuple[time, ...], *, market: str | None = None) -> list[str]:
        """btc-trend F-3와 같은 서명. 60분 주기 전제 → 판정 시각 23개(01:00~23:00), M2 전용. 주기 판정의 배포 표현은 운영 CP-040 구현에 맞춰 조정."""
        del params
        problems: list[str] = []
        if tuple(self.spec.markets) != ("M2",):
            problems.append(f"markets {tuple(self.spec.markets)} — 이 전략은 M2 전용")
        if market is not None and market != "M2":
            problems.append(f"배포 시장 {market!r} — 이 전략은 M2 전용")
        if len(eval_times) != PERIOD_EVALS_PER_DAY:
            problems.append(f"판정 시각은 60분 주기 하루 {PERIOD_EVALS_PER_DAY}개여야 함 — 현재 {len(eval_times)}개")
        return problems

    def _sig(self, snapshot: EvaluationSnapshot, v: InstrumentView, rule: str, kind: RequestType, qty: Decimal,
             otype: OrderType, stop: Decimal | None = None) -> Signal:
        return make_signal(self.spec, snapshot, v.instrument, rule, kind, qty, otype, v.last_price, VALID_FOR, stop_price=stop)
