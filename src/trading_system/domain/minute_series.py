"""M2 분봉(1m) 순수 규칙 — 닫힌 봉·판정 창·품질·지문·주기 격자 (CP-037 D-220·D-219, T-051, 승인 A-113). 표준 라이브러리만.

시장 공통 값 객체 MinuteBar(domain/minute_bar, CP-032 T-028)를 그대로 쓴다. M1(키움 ka10080) 규칙(marketdata/minute)은
바꾸지 않는다 — M1은 KST·정규장·확정 여유 5초, M2는 UTC·24/7·확정 여유 2초다.

- 닫힌 봉 (D-220): 봉 시작 + 60초 ≤ 판정 시각 − 확정 여유(기본 2초 [제안], 실측 PV-M2-14로 조정). 진행 중 봉은 쓰지 않는다.
- 판정 창: 판정 시각 직전 닫힌 봉(마지막 봉)부터 거슬러 lookback개의 1분 칸. 마지막 봉이 없으면 LAST_MISSING(누락),
  창 안에 빈 분이 있으면 GAP(결측) — 둘 다 그 판정 신호 무효. 바이낸스 결측 분이 "거래 없음"인지 "데이터 없음"인지는
  근거가 없어 [미확인] 보수적으로 무효로 본다(M1 정규장 빈 칸 = 거래 없음과 다름).
- 품질 (CP-032 D-177과 같은 항목): 같은 시각 중복, 역순(받은 순서는 과거→최신이어야 함), 음수 거래량, 0 이하 가격,
  고가·저가 모순. 위반이면 호출자가 그 심볼 분봉 입력(신호) 무효 — 정지(K10)는 걸지 않는다(DP-72 개정).
- 지문: 봉(시작 UTC·OHLCV, Decimal은 정규화한 값)의 SHA-256 — 판정 스냅샷 기록과 저장 봉 재현 확인용(V-293).
- 주기 격자 (D-219 [제안] 값): 판정 시각 = UTC 00:00 + k×N분 + 확정 여유, N은 1440의 약수, 격자 시각(여유 전)이 주문 창
  (A-064 UTC 00:10~23:50, 양끝 포함) 안일 때만. 늦은 판정 유예 = min(60초, N분의 절반). T-051은 분봉 조회 일정에만 쓰고,
  엔진 주기 판정(CAP-TRG-02)은 T-052가 같은 격자로 연결한다. 격자 시각이 주문 창 안에 하나도 없는 N(예: 1440 → 00:00뿐)은
  판정·조회가 영원히 일어나지 않으므로 거부한다 [DP-77] (boundaries_in_window).
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from functools import cached_property

from .clock import require_aware
from .minute_bar import MinuteBar

NO_ADJUSTMENT = "none"                       # 수정주가 개념 없음(크립토) — minute_bar.adjusted 값 (D-220 "T-051에서 정함")
ONE_MINUTE = timedelta(minutes=1)
DEFAULT_CONFIRM_MARGIN = timedelta(seconds=2)   # D-220 [제안], PV-M2-14 실측으로 조정
ORDER_WINDOW = (time(0, 10), time(23, 50))      # A-064 주문 창 (UTC)
DAY_MINUTES = 1440


def _floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def last_closed_start(decision_at: datetime, margin: timedelta = DEFAULT_CONFIRM_MARGIN) -> datetime:
    """판정 시각에 닫힌 마지막 1분 봉의 시작(UTC): 시작 + 60초 ≤ 판정 시각 − 여유."""
    require_aware(decision_at)
    return _floor_minute((decision_at - margin).astimezone(UTC)) - ONE_MINUTE


def closed_bars(bars: Iterable[MinuteBar], at: datetime, margin: timedelta = DEFAULT_CONFIRM_MARGIN) -> list[MinuteBar]:
    """at 시점에 닫힌 봉만(봉 끝 ≤ at − 여유). 순서 유지."""
    require_aware(at)
    limit = at - margin
    return [b for b in bars if b.end <= limit]


# --- 품질 -------------------------------------------------------------------------------------------------

def minute_quality_issues(received: Sequence[MinuteBar]) -> list[str]:
    """받은 순서(과거→최신)의 봉 → 문제 목록. 빈 목록이면 통과. 값은 시각만 적는다."""
    issues: list[str] = []
    counts = Counter(b.start for b in received)
    for start, n in counts.items():
        if n > 1:
            issues.append(f"{_hm(start)} 같은 시각 중복")
    for prev, cur in zip(received, received[1:], strict=False):
        if cur.start < prev.start:
            issues.append(f"{_hm(cur.start)} 역순(과거→최신 순서 아님)")
    for b in received:
        if b.volume < 0:
            issues.append(f"{_hm(b.start)} 음수 거래량")
        if min(b.open, b.high, b.low, b.close) <= 0:
            issues.append(f"{_hm(b.start)} 0 이하 가격")
        if not (b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high):
            issues.append(f"{_hm(b.start)} 고가·저가 모순")
    return issues


def _hm(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


# --- 지문 -------------------------------------------------------------------------------------------------

def _num(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


def bar_fingerprint(bars: Iterable[MinuteBar]) -> str:
    """봉 묶음의 SHA-256(16진). 같은 값이면 Decimal 자릿수 표기와 무관하게 같다. 순서를 반영한다."""
    h = hashlib.sha256()
    for b in bars:
        line = "|".join((b.instrument, str(b.minutes), b.start.astimezone(UTC).isoformat(),
                         *(_num(x) for x in (b.open, b.high, b.low, b.close, b.volume))))
        h.update(line.encode("utf-8") + b"\n")
    return h.hexdigest()


# --- 판정 창 ----------------------------------------------------------------------------------------------

class WindowStatus(str, Enum):
    OK = "OK"
    LAST_MISSING = "LAST_MISSING"    # 판정 직전 닫힌 봉 없음 (누락)
    GAP = "GAP"                      # 창 안 결측 분


@dataclass(frozen=True)
class MinuteWindow:
    status: WindowStatus
    bars: tuple[MinuteBar, ...]      # OK일 때만 lookback개(시각 오름차순), 아니면 ()
    first_start: datetime            # 창의 기대 범위 (첫 봉 시작)
    last_start: datetime             # 창의 기대 범위 (마지막 닫힌 봉 시작)
    missing: int                     # 창 안 빈 분 수
    fingerprint: str                 # OK일 때 bars 지문, 아니면 ""


def check_window(bars: Iterable[MinuteBar], decision_at: datetime, lookback: int,
                 margin: timedelta = DEFAULT_CONFIRM_MARGIN) -> MinuteWindow:
    """판정 시각의 1분 봉 창 판정. 1분 봉·닫힌 봉만 본다(진행 중 봉·다른 길이는 무시)."""
    if lookback <= 0:
        raise ValueError("lookback은 1 이상")
    last = last_closed_start(decision_at, margin)
    first = last - ONE_MINUTE * (lookback - 1)
    present = {b.start.astimezone(UTC): b for b in closed_bars(bars, decision_at, margin) if b.minutes == 1}
    expected = [first + ONE_MINUTE * i for i in range(lookback)]
    missing = sum(1 for s in expected if s not in present)
    if last not in present:
        return MinuteWindow(WindowStatus.LAST_MISSING, (), first, last, missing, "")
    if missing:
        return MinuteWindow(WindowStatus.GAP, (), first, last, missing, "")
    window = tuple(present[s] for s in expected)
    return MinuteWindow(WindowStatus.OK, window, first, last, 0, bar_fingerprint(window))


# --- 주기 격자 --------------------------------------------------------------------------------------------

def boundaries_in_window(period_minutes: int, start: time = ORDER_WINDOW[0], end: time = ORDER_WINDOW[1]) -> int:
    """하루 격자 시각(UTC 00:00 + k×N분, 여유 전) 가운데 주문 창 [start, end) 안인 개수 — 끝 제외, 게이트 L8과 같음(A-124 ①). N은 1440의 약수."""
    if isinstance(period_minutes, bool) or not isinstance(period_minutes, int) or period_minutes <= 0:
        raise ValueError("period_minutes는 양의 정수")
    lo, hi = start.hour * 60 + start.minute, end.hour * 60 + end.minute
    return sum(1 for m in range(0, DAY_MINUTES, period_minutes) if lo <= m < hi)


@dataclass(frozen=True)
class PeriodGrid:
    period_minutes: int
    margin: timedelta = DEFAULT_CONFIRM_MARGIN
    window_start: time = ORDER_WINDOW[0]
    window_end: time = ORDER_WINDOW[1]

    def __post_init__(self) -> None:
        n = self.period_minutes
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= DAY_MINUTES or DAY_MINUTES % n:
            raise ValueError("period_minutes는 1440의 약수인 1~1440 정수")
        if self.margin < timedelta(0) or self.margin >= ONE_MINUTE:
            raise ValueError("확정 여유는 0초 이상 60초 미만")
        if boundaries_in_window(n, self.window_start, self.window_end) == 0:
            raise ValueError(f"period_minutes {n}: 격자 시각(UTC 00:00 + k×{n}분)이 주문 창 "
                             f"{self.window_start:%H:%M}~{self.window_end:%H:%M} 안에 하나도 없다 — 판정·조회가 일어나지 "
                             "않는다 (DP-77)")

    @property
    def grace(self) -> timedelta:
        """늦은 판정 유예 = min(60초, N분의 절반) (D-219 [제안])."""
        return min(timedelta(seconds=60), timedelta(seconds=30 * self.period_minutes))

    @property
    def fetch_wait(self) -> timedelta:
        """주기 판정이 그 판정 시각의 1분봉 조회를 기다리는 상한 = 유예의 절반 [제안, DP-79] (N=1 → 15초, N≥2 → 30초).
        유예보다 짧아 판정·실행 루프(최대 5초)가 기다림 뒤에도 유예 안에서 판정한다 — 늦은 판정은 만들지 않는다."""
        return self.grace / 2

    @cached_property
    def _offsets(self) -> tuple[int, ...]:
        lo = self.window_start.hour * 60 + self.window_start.minute
        hi = self.window_end.hour * 60 + self.window_end.minute
        return tuple(m for m in range(0, DAY_MINUTES, self.period_minutes) if lo <= m < hi)

    def offsets(self) -> tuple[int, ...]:
        """하루 격자 시각(UTC 00:00부터 분) 가운데 주문 창 [시작, 끝) 안인 것, 오름차순 (끝 제외 — A-124 ①)."""
        return self._offsets

    def day_times(self, day: date) -> tuple[datetime, ...]:
        """그 UTC 날짜의 판정 시각(격자 시각 + 여유) 전부, 오름차순 (백테스트 일정 — 실운영과 같은 격자, D-221)."""
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return tuple(midnight + ONE_MINUTE * m + self.margin for m in self.offsets())

    def next_after(self, at: datetime) -> datetime:
        """at 다음 판정 시각(주문 창 안 격자, 필요하면 다음 날 첫 격자) — 신호 기한 상한(00 §2.6)."""
        require_aware(at)
        t = at.astimezone(UTC)
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
        offs = self._offsets
        # 판정 시각 midnight + o분 + 여유 > t  ⇔  o > (t − 여유 − midnight)/1분  (이분 탐색 — 1분 격자에서도 판정마다 상수 시간)
        k = bisect_right(offs, (t - self.margin - midnight) / ONE_MINUTE)
        if k < len(offs):
            return midnight + ONE_MINUTE * offs[k] + self.margin
        return midnight + timedelta(days=1) + ONE_MINUTE * offs[0] + self.margin

    def count_between(self, earlier: datetime, later: datetime) -> int:
        """earlier < 판정 시각 < later 인 판정 시각 수 (루프 지연으로 건너뛴 판정 집계용)."""
        n, t = 0, self.next_after(earlier)
        while t < later:
            n += 1
            t = self.next_after(t)
        return n

    def latest_at_or_before(self, now: datetime) -> datetime | None:
        """now 이전(같음 포함) 가장 늦은 격자 판정 시각(UTC). 그 격자 시각이 주문 창 밖이면 None."""
        require_aware(now)
        base = _floor_minute((now - self.margin).astimezone(UTC))
        midnight = base.replace(hour=0, minute=0)
        k = int((base - midnight) / ONE_MINUTE) // self.period_minutes
        boundary = midnight + ONE_MINUTE * (k * self.period_minutes)
        if not self.window_start <= boundary.time() < self.window_end:
            return None
        return boundary + self.margin
