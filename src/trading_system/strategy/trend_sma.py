"""첫 실제 전략 trend-sma v1 — 이동평균 추세추종 (T-018, A-071). **초안, 사용자 미승인.**

명세: docs/strategies/trend-sma/strategy.md (규칙 표와 이 코드가 일치해야 한다). 파라미터: config/strategies/trend-sma-v1.toml.
출발점은 후보 cand-trend-sma(candidates.py)이며, 계산 도우미(SMA·ATR·크기·신호 생성)를 그대로 재사용한다.
후보 대비 추가: 재진입 제한 REENTRY-1(직전 청산 뒤 확정 일봉 reentry_wait개 전에는 진입 금지), fast < slow 검사.

규칙 (판정 1일 1회, 확정 일봉 종가 기준. 종목마다 신호 최대 1개, 청산 > 진입 — D-006):
- EXIT-STOP (청산 1순위): 보유 AND 최근 확정 종가 <= 평균가 − atr_mult × ATR(atr_n). ATR은 판정 시점 값.
- EXIT-1   (청산 2순위): 보유 AND SMA(fast) <= SMA(slow).
- REENTRY-1 (재진입 제한): 직전 청산(사유 무관)이 있고, 청산일(KST) 뒤 확정 일봉 수 < reentry_wait → 진입 안 함.
- ENTRY-1  : 보유 없음 AND SMA(fast) > SMA(slow) AND 거래 상태 정상 AND REENTRY-1 아님.
             손절가 = 최근 확정 종가 − atr_mult × ATR (원 단위 내림 — L1 계산이 보수적). 크기 = S-3 위험·비중·L7(상한가 기준) 중 최소.
순수 함수: 스냅샷과 파라미터만 본다 (00 D-002). 시계·브로커·저장소·네트워크를 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal

from trading_system.domain.clock import KST
from trading_system.domain.models import Signal
from trading_system.domain.strategy import EvaluationSnapshot, InstrumentView, ParamSpec, RuleSpec, StrategySpec

from .base import ParamValues
from .candidates import _atr, _closes, _entry, _exit, _held, _ready, _sma, _spec

STRATEGY_ID = "trend-sma"
_INPUTS = ("CAP-DAT-01", "상태 입력")


def _rules() -> tuple[RuleSpec, ...]:
    return (
        RuleSpec("EXIT-STOP", "EXIT", _INPUTS, "CAP-TRG-01",
                 "보유 AND 최근 확정 종가 <= 평균가 − atr_mult×ATR(atr_n)", "청산 전량 시장가", "데이터 품질 실패·처리 중 주문", 1),
        RuleSpec("EXIT-1", "EXIT", _INPUTS, "CAP-TRG-01",
                 "보유 AND SMA(fast) <= SMA(slow)", "청산 전량 시장가", "데이터 품질 실패·처리 중 주문", 2),
        RuleSpec("REENTRY-1", "REENTRY_BLOCK", ("상태 입력",), "CAP-TRG-01",
                 "직전 청산 있음 AND 청산일(KST) 뒤 확정 일봉 수 < reentry_wait", "ENTRY-1 신호를 내지 않음",
                 "직전 청산 없음", 1),
        RuleSpec("ENTRY-1", "ENTRY", _INPUTS, "CAP-TRG-01",
                 "보유 없음 AND SMA(fast) > SMA(slow) AND REENTRY-1 아님",
                 "진입 S-3 시장가(손절가 = 종가 − atr_mult×ATR)", "데이터 품질 실패·거래 상태 이상·처리 중 주문", 1),
    )


def _params() -> tuple[ParamSpec, ...]:
    return (
        ParamSpec("fast", "int", "거래일", "단기 이동평균 (fast < slow)", Decimal("5"), Decimal("50")),
        ParamSpec("slow", "int", "거래일", "장기 이동평균 (fast < slow)", Decimal("20"), Decimal("200")),
        ParamSpec("atr_n", "int", "거래일", "ATR 기간", Decimal("5"), Decimal("30")),
        ParamSpec("atr_mult", "decimal", "배", "손절 ATR 배수", Decimal("1"), Decimal("5")),
        ParamSpec("reentry_wait", "int", "거래일", "청산 뒤 재진입 대기 확정 일봉 수 (0 = 제한 없음)",
                  Decimal("0"), Decimal("20")),
    )


def build_spec() -> StrategySpec:
    base = _spec(STRATEGY_ID, "이동평균 추세추종", "SMA(fast)>SMA(slow) 동안 보유, ATR 손절, 청산 뒤 재진입 대기",
                 _rules(), _params(), "일봉 OHLC 수정주가, 워밍업 max(slow, atr_n+1)거래일", "수일~수개월(최대 보유 제한 없음)")
    # 후보 표기("제안(미등록, T-018 결정 전)")를 초안 표기로 바꾼다
    return replace(base, description="SMA(fast)>SMA(slow) 동안 보유, ATR 손절, 청산 뒤 재진입 대기 — 초안(사용자 미승인, T-018)")


def check_params(params: ParamValues) -> tuple[int, int, int, Decimal, int]:
    """교차 조건 검사 (범위 검사는 파라미터 로더). 위반이면 ValueError → 판정 실패로 기록, 신호 없음."""
    fast, slow = int(str(params["fast"])), int(str(params["slow"]))
    if fast >= slow:
        raise ValueError("trend-sma: fast < slow 이어야 한다")
    return fast, slow, int(str(params["atr_n"])), Decimal(str(params["atr_mult"])), int(str(params["reentry_wait"]))


def reentry_blocked(v: InstrumentView, wait: int) -> bool:
    """REENTRY-1: 직전 청산일(KST) 뒤의 확정 일봉 수가 wait 미만이면 True. 스냅샷의 봉만 센다."""
    if wait <= 0 or v.last_exit_at is None:
        return False
    exit_day = v.last_exit_at.astimezone(KST).date()
    after = sum(1 for b in v.daily_bars if b.at > exit_day)   # type: ignore[operator]  # 일봉은 date
    return after < wait


class TrendSmaV1:
    def __init__(self) -> None:
        self.spec = build_spec()

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        fast, slow, atr_n, mult, wait = check_params(params)
        out: list[Signal] = []
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
            elif v.position is None or v.position.quantity == 0:
                if f > s and not reentry_blocked(v, wait):
                    stop = (xs[-1] - mult * atr).quantize(Decimal("1"), rounding=ROUND_FLOOR)
                    sig = _entry(self.spec, snapshot, params, v, "ENTRY-1", stop)
                    if sig is not None:
                        out.append(sig)
        return out
