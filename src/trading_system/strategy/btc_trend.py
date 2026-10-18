"""후보 btc-trend v1 — 비트코인 현물(M2, 바이낸스) 일봉 추세 추종. 전략실 연구 후보(운영 미등록).

근거: research/2026-10-17-crypto-design.md(cryptosim 탐색), 사용자 결정 2026-10-17(1회 위험 5%, 피라미딩 없음).
규칙(판정 매일 UTC 하루 1회 — 운영 M2 백테스트 기본 00:15 UTC, 스냅샷에는 확정된 UTC 일봉만, 판정 시세 = 그날 시가):
- ENTRY-1: 미보유 AND 전일(마지막 확정 봉) 종가 > 그 전 entry_n개 봉 종가 최고 → 매수(지정가 — 운영이 IOC 가격을 정함, D-104).
  수량 = min(기준 금액 × risk_ratio ÷ (stop_atr × ATR14), 기준 금액 × max_weight ÷ 시세)을 lot_step으로 내림. 손절가 = 시세 − stop_atr × ATR14.
- EXIT-1(청산): 보유 AND 전일 종가 < SMA(exit_n) → 청산 가능 수량 전량 시장가.
- EXIT-2(손절): 보유 AND 전일 종가 < 평균 매입가 − stop_atr × ATR14(현재) → 전량 시장가. (cryptosim은 진입 판정일 ATR로 고정 — 손절은
  탐색에서 결과에 거의 영향이 없었다: 50일선 청산이 먼저 작동. 무상태라 현재 ATR 사용, 차이는 대조 실행에서 확인.)
- 우선순위: EXIT-2 > EXIT-1 > ENTRY-1. 종목마다 신호 1개. 피라미딩 없음.
순수 함수. 파일·네트워크·현재 시각·난수 없음.
[운영 보강 F-2·F-3, A-114 ③ 2026-10-18] 시작 검사 validate_rules(DP-54 형식 — LOT_STEP = 거래소 LOT_SIZE stepSize)와
배포 검사 validate_deploy(판정 시각 정확히 1개·M2 전용)를 추가했다. 판정·수량·신호 논리는 바꾸지 않았다.
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

STRATEGY_ID = "btc-trend"
VERSION = 1
SYMBOL = "BTCUSDT"
LOT_STEP = Decimal("0.00001")      # 바이낸스 BTCUSDT LOT_SIZE stepSize(테스트넷 확인값, binance-spot.md) — 운영이 다시 맞춘다
_ATR_N = 14
VALID_FOR = timedelta(minutes=30)
_INPUTS = ("CAP-DAT-01", "상태 입력")


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
        RuleSpec("EXIT-2", "EXIT", _INPUTS, "CAP-TRG-01", "보유 AND 전일 종가 < 평균 매입가 − stop_atr × ATR14",
                 "청산 가능 수량 전량 시장가(손절)", "데이터 없음·처리 중 주문", 1),
        RuleSpec("EXIT-1", "EXIT", _INPUTS, "CAP-TRG-01", "보유 AND 전일 종가 < SMA(exit_n)",
                 "청산 가능 수량 전량 시장가", "데이터 없음·처리 중 주문", 2),
        RuleSpec("ENTRY-1", "ENTRY", _INPUTS, "CAP-TRG-01", "미보유 AND 전일 종가 > 그 전 entry_n개 봉 종가 최고",
                 "S-3 매수(수량 = min(기준×risk_ratio ÷ (stop_atr×ATR14), 기준×max_weight ÷ 시세, order_cap ÷ (시세×1.006)), 손절가 = 시세 − stop_atr×ATR14)",
                 "데이터 없음·거래 상태 이상·처리 중 주문", 1),
    )
    params = (
        ParamSpec("entry_n", "int", "일", "돌파 기간(전일 종가가 그 전 n일 종가 최고를 넘음)", Decimal("5"), Decimal("200")),
        ParamSpec("exit_n", "int", "일", "청산 이동평균 기간", Decimal("10"), Decimal("200")),
        ParamSpec("stop_atr", "decimal", "배", "손절 거리 = ATR14 × 값", Decimal("1"), Decimal("6")),
        ParamSpec("risk_ratio", "decimal", "비율", "1회 위험 = 기준 금액 × 값(사용자 5%)", Decimal("0.001"), Decimal("0.10")),
        ParamSpec("max_weight", "decimal", "비율", "최대 비중(레버리지 없음 ≤ 1)", Decimal("0.001"), Decimal("1")),
        ParamSpec("order_cap", "decimal", "USDT", "1회 주문 금액 상한(지정가 완충 포함 전 시세 기준, 계좌 L7 이하로)", Decimal("5"), Decimal("1000000000")),
    )
    return StrategySpec(
        strategy_id=STRATEGY_ID, name="BTC 일봉 추세 추종(돌파·이동평균 청산)", version=VERSION,
        description="UTC 일봉 종가가 entry_n일 고점을 넘으면 매수, SMA(exit_n) 아래면 매도, ATR 손절 — 전략실 연구 후보",
        markets=("M2",), universe_method="U-A", universe=(SYMBOL,),
        # [운영 수정 F-1, 2026-10-17] 전략실 원본 CAP-INS-01(국내 상장주식 — M2 "해당 없음", 등록 거부) → CAP-INS-05(크립토 현물).
        # 메타데이터만 — 규칙·계산 변경 없음 (docs/strategies/btc-trend/validation-v1.md §1)
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-05"),
        data_requirements="UTC 일봉 OHLC 워밍업 max(entry_n+1, exit_n, 15)", trigger="CAP-TRG-01 매일 UTC 00:15(하루 1회)",
        rules=rules, params=params, signal_format="00 §2.5", validity_rule="판정 + 30분", validity_max_minutes=30,
        unfilled_policy="시스템 기본", sizing="S-3", holding="수 주~수 개월", limits=StrategyLimits())


class BtcTrendV1:
    def __init__(self) -> None:
        self.spec = build_spec()

    @staticmethod
    def validate_rules(params: ParamValues, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        """[F-2, A-114 ③] 거래소 규칙(exchangeInfo 적재값)과의 일치 검사. M2 시작 때 app.wiring.spot_rule_problems가 유니버스
        심볼마다 부른다(CP-036 D-209·DP-54 형식: params, LOT_SIZE stepSize, minNotional — m2_verify와 같은 서명). 문제가 있으면
        시작 거부(주문 0). 이 전략은 수량 내림 단위를 모듈 고정값 LOT_STEP으로 쓰므로 그것이 stepSize와 같아야 한다.
        min_notional은 서명 호환용으로 받는다 — 수량이 변동이라 사전 고정 검사가 없다(내림 뒤 미달 주문은 게이트·거래소 거부)."""
        del params, min_notional
        if LOT_STEP != lot_step:
            return [f"전략 고정 LOT_STEP {LOT_STEP:f} ≠ 거래소 LOT_SIZE stepSize {lot_step:f}"]
        return []

    def validate_deploy(self, params: ParamValues, eval_times: tuple[time, ...], *, market: str | None = None) -> list[str]:
        """[F-3, A-114 ③] 시작 전 배포 검사: 판정은 UTC 하루 1회 전제(전일 확정 일봉·그날 시가) → 판정 시각 정확히 1개.
        M2(바이낸스 현물) 전용 → 명세 markets가 정확히 ("M2",)이고, market을 주면 "M2"여야 한다.
        (현재 호출부 app.main·backtest_m2_cli는 market을 넘기지 않는다 — 그때는 명세 markets 검사만 적용.)"""
        del params
        problems: list[str] = []
        if tuple(self.spec.markets) != ("M2",):
            problems.append(f"markets {tuple(self.spec.markets)} — 이 전략은 M2 전용")
        if market is not None and market != "M2":
            problems.append(f"배포 시장 {market!r} — 이 전략은 M2 전용")
        if len(eval_times) != 1:
            problems.append(f"판정 시각은 정확히 1개여야 함 (UTC 하루 1회 판정) — 현재 {len(eval_times)}개")
        return problems

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        entry_n, exit_n = int(str(params["entry_n"])), int(str(params["exit_n"]))
        stop_atr, risk, maxw, cap = (Decimal(str(params[k])) for k in ("stop_atr", "risk_ratio", "max_weight", "order_cap"))
        out: list[Signal] = []
        for v in snapshot.instruments:
            if v.instrument != SYMBOL or not v.data_ok or v.has_pending_order or not v.daily_bars or v.last_price is None:
                continue
            bars = v.daily_bars[-max(entry_n + 2, exit_n + 1, _ATR_N + 2):]
            xs = [b.close for b in bars]
            if len(xs) < max(entry_n + 1, exit_n, _ATR_N + 1):
                continue
            close, atr = xs[-1], _atr(bars)
            if atr is None or atr <= 0:
                continue
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                sma = _sma(xs, exit_n)
                if close < pos.avg_price - stop_atr * atr:
                    out.append(self._sig(snapshot, v, "EXIT-2", RequestType.EXIT, pos.quantity, OrderType.MARKET))
                elif sma is not None and close < sma:
                    out.append(self._sig(snapshot, v, "EXIT-1", RequestType.EXIT, pos.quantity, OrderType.MARKET))
                continue
            if pos is not None and pos.quantity != 0:
                continue
            if close <= max(xs[-entry_n - 1:-1]) or v.statuses:
                continue
            price = v.last_price
            stop = price - stop_atr * atr
            if stop <= 0:
                continue
            base = snapshot.base_amount
            qty = min(base * risk / (stop_atr * atr), base * maxw / price, cap / (price * Decimal("1.006")))  # 1.006 = IOC 완충 0.5% + 여유
            qty = (qty / LOT_STEP).to_integral_value(rounding=ROUND_FLOOR) * LOT_STEP
            if qty <= 0:
                continue
            out.append(self._sig(snapshot, v, "ENTRY-1", RequestType.ENTRY, qty, OrderType.LIMIT, stop))
        return out

    def _sig(self, snapshot: EvaluationSnapshot, v: InstrumentView, rule: str, kind: RequestType, qty: Decimal,
             otype: OrderType, stop: Decimal | None = None) -> Signal:
        return make_signal(self.spec, snapshot, v.instrument, rule, kind, qty, otype, v.last_price, VALID_FOR, stop_price=stop)
