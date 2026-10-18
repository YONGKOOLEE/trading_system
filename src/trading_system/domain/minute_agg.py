"""M2 집계 봉(15분·60분)·이력 나눔 계획 — 순수 규칙 (CP-040 D-259·D-261, T-066, 승인 A-115·A-123 ④). 표준 라이브러리만.

집계 봉 (D-259):
- 원천 = 저장된 1분 봉(같은 출처). 거래소 1h·15m klines는 쓰지 않는다. 집계 봉은 저장하지 않는다(읽을 때 계산, 결정적).
- 정렬 = UTC: 60분 [HH:00, HH+1:00), 15분 [HH:00·15·30·45, +15분).
- 값 = open 첫 분 시가, high 최댓값, low 최솟값, close 마지막 분 종가, volume·quote_volume·trades·taker_buy_base·
  taker_buy_quote 합. 확장 열은 구성 분이 모두 확장 열을 가질 때만(아니면 그 열만 None — OHLCV는 그대로).
- 확정 = 집계 봉 끝 ≤ 판정 시각 − 확정 여유 AND 구성 1분 봉(닫힌 봉)이 **전부** 있음. 하나라도 없으면 그 집계 봉은 결측 —
  채우거나 만들지 않는다. 흐름 열 사용 선언(flow)이면 확장 열이 없는 구성 분도 결측으로 센다(D-260).
- 판정 창: 직전 닫힌 집계 봉 필수(없으면 LAST_MISSING — 신호 무효), 더 오래된 결측은 목록으로 넘기고 결측 수 ≤
  max_missing(기본 0 = DP-75와 같은 보수)이면 OK, 넘으면 TOO_MANY_MISSING(신호 무효).
- 지문 = 집계 규칙 버전 + 집계 길이 + flow + 사용한 1분 봉 지문(OHLCV, minute_series.bar_fingerprint) + 확장 열 지문.

이력 나눔 계획 (D-261): 판정 창에 필요한 1분 칸 가운데 저장되지 않은 칸만 연속 구간으로 묶고, 한 쪽 최대 1000봉
(klines limit 상한, BN S6)으로 나눈다. 조회·저장은 app(m2_backfill)이 한다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from .clock import require_aware
from .minute_bar import MinuteBar
from .minute_series import DEFAULT_CONFIRM_MARGIN, ONE_MINUTE, bar_fingerprint
from .strategy import Bar

AGG_RULE_VERSION = "m2-agg-v1"        # 집계 규칙 버전 (D-259 (6)) — 규칙을 바꾸면 올린다
AGG_MINUTES = (15, 60)                # 허용 집계 길이 (D-258·D-259, Q-1 (나))
KLINE_PAGE_MAX = 1000                 # klines 한 쪽 상한 (BN S6)


class AggStatus(str, Enum):
    OK = "OK"
    LAST_MISSING = "LAST_MISSING"            # 직전 닫힌 집계 봉 결측
    TOO_MANY_MISSING = "TOO_MANY_MISSING"    # 창 안 오래된 결측 > max_missing


@dataclass(frozen=True)
class AggWindow:
    status: AggStatus
    bars: tuple[Bar, ...]             # OK일 때만: 결측을 뺀 집계 봉(시작 오름차순), 아니면 ()
    missing: tuple[datetime, ...]     # 창 안 결측 집계 봉 시작(오름차순) — 상태와 무관하게 기록용
    first_start: datetime             # 창 기대 범위 (첫 집계 봉 시작)
    last_start: datetime              # 창 기대 범위 (직전 닫힌 집계 봉 시작)
    used_minutes: int                 # 사용한 1분 봉 수 (OK일 때)
    fingerprint: str                  # OK일 때 지문, 아니면 ""


def _check_args(minutes: int, lookback: int, max_missing: int) -> None:
    if isinstance(minutes, bool) or minutes not in AGG_MINUTES:
        raise ValueError(f"집계 봉 길이는 {AGG_MINUTES} 중 하나 (D-258·D-259)")
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0:
        raise ValueError("집계 봉 창은 1 이상 정수")
    if isinstance(max_missing, bool) or not isinstance(max_missing, int) or max_missing < 0:
        raise ValueError("max_missing은 0 이상 정수")


def last_agg_start(decision_at: datetime, minutes: int, margin: timedelta = DEFAULT_CONFIRM_MARGIN) -> datetime:
    """판정 시각에 닫힌 마지막 집계 봉의 시작(UTC): 끝 = (판정 시각 − 여유)를 집계 길이로 내림."""
    require_aware(decision_at)
    t = (decision_at - margin).astimezone(UTC)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int((t - midnight) / timedelta(minutes=minutes))
    return midnight + timedelta(minutes=minutes) * k - timedelta(minutes=minutes)


def _sum(values: Sequence[Decimal | None]) -> Decimal | None:
    if any(v is None for v in values):
        return None
    return sum((v for v in values if v is not None), Decimal(0))


def _aggregate(start: datetime, parts: Sequence[MinuteBar]) -> Bar:
    ext = all(b.has_ext for b in parts)
    return Bar(start, parts[0].open, max(b.high for b in parts), min(b.low for b in parts), parts[-1].close,
               sum((b.volume for b in parts), Decimal(0)),
               quote_volume=_sum([b.quote_volume for b in parts]) if ext else None,
               trades=sum(b.trades or 0 for b in parts) if ext else None,
               taker_buy_base=_sum([b.taker_buy_base for b in parts]) if ext else None,
               taker_buy_quote=_sum([b.taker_buy_quote for b in parts]) if ext else None)


def _ext_num(d: Decimal | int | None) -> str:
    if d is None:
        return "-"
    if isinstance(d, int):
        return str(d)
    return format(d.normalize(), "f") if d else "0"


def ext_fingerprint(bars: Iterable[MinuteBar]) -> str:
    """확장 열 지문(없는 값은 "-"). 순서를 반영한다."""
    h = hashlib.sha256()
    for b in bars:
        line = "|".join((b.start.astimezone(UTC).isoformat(), *(_ext_num(x) for x in (
            b.quote_volume, b.trades, b.taker_buy_base, b.taker_buy_quote))))
        h.update(line.encode("utf-8") + b"\n")
    return h.hexdigest()


def agg_fingerprint(used: Sequence[MinuteBar], minutes: int, flow: bool) -> str:
    text = "|".join((AGG_RULE_VERSION, str(minutes), "flow" if flow else "ohlcv", bar_fingerprint(used),
                     ext_fingerprint(used)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_agg_window(bars_1m: Iterable[MinuteBar], decision_at: datetime, minutes: int, lookback: int,
                     max_missing: int = 0, margin: timedelta = DEFAULT_CONFIRM_MARGIN, *,
                     flow: bool = False) -> AggWindow:
    """판정 시각의 집계 봉 창 판정 (D-259 (4)·(5)). 1분 봉·닫힌 봉만 본다(진행 중 봉·다른 길이는 무시). 입력 순서 무관."""
    _check_args(minutes, lookback, max_missing)
    step = timedelta(minutes=minutes)
    last = last_agg_start(decision_at, minutes, margin)
    first = last - step * (lookback - 1)
    limit = decision_at - margin
    present = {b.start.astimezone(UTC): b for b in bars_1m if b.minutes == 1 and b.end <= limit}
    out: list[Bar] = []
    used: list[MinuteBar] = []
    missing: list[datetime] = []
    for i in range(lookback):
        start = first + step * i
        parts = [present.get(start + ONE_MINUTE * k) for k in range(minutes)]
        full = [p for p in parts if p is not None]
        if len(full) != minutes or (flow and not all(p.has_ext for p in full)):
            missing.append(start)
            continue
        out.append(_aggregate(start, full))
        used.extend(full)
    if last in missing:
        return AggWindow(AggStatus.LAST_MISSING, (), tuple(missing), first, last, 0, "")
    if len(missing) > max_missing:
        return AggWindow(AggStatus.TOO_MANY_MISSING, (), tuple(missing), first, last, 0, "")
    return AggWindow(AggStatus.OK, tuple(out), tuple(missing), first, last, len(used),
                     agg_fingerprint(used, minutes, flow))


# --- 이력 나눔 계획 (D-261) --------------------------------------------------------------------------------------

def missing_ranges(present: Iterable[datetime], first: datetime, last: datetime) -> list[tuple[datetime, datetime]]:
    """[first, last] 1분 칸 가운데 present에 없는 칸의 연속 구간들(양끝 포함, 시작 오름차순)."""
    require_aware(first)
    require_aware(last)
    have = {t.astimezone(UTC) for t in present}
    out: list[tuple[datetime, datetime]] = []
    run_start: datetime | None = None
    t = first.astimezone(UTC)
    end = last.astimezone(UTC)
    while t <= end:
        if t in have:
            if run_start is not None:
                out.append((run_start, t - ONE_MINUTE))
                run_start = None
        elif run_start is None:
            run_start = t
        t += ONE_MINUTE
    if run_start is not None:
        out.append((run_start, end))
    return out


def split_pages(ranges: Iterable[tuple[datetime, datetime]],
                page_max: int = KLINE_PAGE_MAX) -> list[tuple[datetime, datetime]]:
    """구간들을 한 쪽 최대 page_max봉(양끝 포함)으로 나눈다."""
    if page_max <= 0:
        raise ValueError("page_max는 1 이상")
    out: list[tuple[datetime, datetime]] = []
    for start, end in ranges:
        t = start
        while t <= end:
            stop = min(end, t + ONE_MINUTE * (page_max - 1))
            out.append((t, stop))
            t = stop + ONE_MINUTE
    return out
