"""시장 데이터 품질 검사와 전처리 (R-22, 03 D-088, 02 ENT-09, 04 V-76·V-112).

- 전처리는 이 모듈의 `preprocess_daily`만 쓴다. 백테스트와 실시간 경로가 같은 함수를 부른다 (D-088).
  결과에는 전처리 버전을 붙여 판정 스냅샷에 기록한다.
- 품질 실패(결측·중복·이상치·형식)인 종목은 그 판정에서 신호를 내지 않는다(기본값으로 채우지 않음, 00 §2.2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from trading_system.domain.strategy import Bar

PREPROCESS_VERSION = "pp-1"  # 규칙을 바꾸면 올린다 (백테스트 재현성, D-074)
DEFAULT_MAX_DAILY_MOVE = Decimal("0.30")  # KRX 가격제한폭 ±30% (krx-market §3)


@dataclass(frozen=True)
class QualityReport:
    ok: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prepared:
    bars: tuple[Bar, ...]
    report: QualityReport
    version: str = PREPROCESS_VERSION
    data_version: str = ""


@dataclass(frozen=True)
class RawDaily:
    """원시 일봉 (공급자가 준 그대로). corporate_action이 참이면 그날 가격 급변은 이상치로 보지 않는다."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    corporate_action: bool = False


@dataclass
class _Issues:
    items: list[str] = field(default_factory=list)

    def add(self, s: str) -> None:
        self.items.append(s)


def preprocess_daily(raw: Sequence[RawDaily], trading_days: Callable[[date, date], list[date]] | None = None,
                     max_move: Decimal = DEFAULT_MAX_DAILY_MOVE, data_version: str = "") -> Prepared:
    """원시 일봉 → 검사된 일봉. 문제가 하나라도 있으면 ok=False(봉은 정렬만 해서 돌려주되 쓰면 안 됨)."""
    issues = _Issues()
    rows = sorted(raw, key=lambda r: r.day)
    seen: set[date] = set()
    for r in rows:
        if r.day in seen:
            issues.add(f"{r.day}: 중복 봉")
        seen.add(r.day)
        values = (r.open, r.high, r.low, r.close, r.volume)
        if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
            issues.add(f"{r.day}: 숫자 형식 오류")
            continue
        if min(r.open, r.high, r.low, r.close) <= 0 or r.volume < 0:
            issues.add(f"{r.day}: 0 이하 가격 또는 음수 거래량")
        if not (r.low <= min(r.open, r.close) and max(r.open, r.close) <= r.high):
            issues.add(f"{r.day}: 고가·저가 모순")
    for prev, cur in zip(rows, rows[1:], strict=False):
        if prev.close > 0 and not cur.corporate_action:
            move = abs(cur.close - prev.close) / prev.close
            if move > max_move:
                issues.add(f"{cur.day}: 전일 대비 {move:.2%} 변동 — 이상치(권리 변동 표시 없음)")
    if trading_days is not None and len(rows) >= 2:
        expected = set(trading_days(rows[0].day, rows[-1].day))
        missing = sorted(expected - seen)
        if missing:
            issues.add(f"결측 거래일 {len(missing)}개 (첫 {missing[0]})")
        extra = sorted(seen - expected)
        if extra:
            issues.add(f"휴장일 봉 {len(extra)}개 (첫 {extra[0]})")
    bars = tuple(Bar(r.day, r.open, r.high, r.low, r.close, r.volume) for r in rows)
    return Prepared(bars, QualityReport(not issues.items, tuple(issues.items)), PREPROCESS_VERSION, data_version)
