"""시세 기록용 메모리 구조 (T-023, CP-027 draft D-141·D-142) — 진단 전용, 게이트 입력이 아니다.

- QuoteHistory: 종목별 최근 시세(받은 시각 기준) 고정 길이 이력. "그 시각에 시스템이 알던 시세"를 나중에 찾는다
  (전송·체결 시점 스냅샷, PV-10). 전체 틱 이력은 저장하지 않는다 — 이 이력은 메모리에만 있고 길이가 정해져 있다.
- PollStats: 시세 조회 주기·응답 지연·종목 수를 창(기본 10분)별로 요약한다(PV-15). 응답 필드는 **이름만** 남긴다.

둘 다 여러 스레드(시세 조회·실시간 수신 → 쓰기, 판정·실행 스레드의 기록기 → 읽기)가 쓰므로 잠금으로 보호한다.
여기서 나는 오류가 시세 갱신·게이트 입력을 막지 않도록 호출하는 쪽(store·feed)이 예외를 삼킨다.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_system.domain.market import Quote

QUOTE_HISTORY = 512                    # 종목별 보관 개수 (1.2초 주기·20종목이면 약 3시간 분량)
STAT_WINDOW = timedelta(minutes=10)    # 조회 통계 요약 창 [제안]


@dataclass(frozen=True)
class LoggedQuote:
    quote: Quote
    source: str          # 시세를 준 경로 (ka10001 조회, 0g 실시간 등)


class QuoteHistory:
    def __init__(self, maxlen: int = QUOTE_HISTORY) -> None:
        self._maxlen = maxlen
        self._data: dict[str, deque[LoggedQuote]] = {}
        self._lock = threading.Lock()

    def add(self, quote: Quote, source: str) -> None:
        """가격·받은 시각이 직전과 같으면(상·하한가만 갱신) 새 항목을 만들지 않는다."""
        with self._lock:
            d = self._data.setdefault(quote.instrument, deque(maxlen=self._maxlen))
            if d and d[-1].quote.at == quote.at and d[-1].quote.price == quote.price:
                return
            d.append(LoggedQuote(quote, source))

    def entries(self, instrument: str) -> list[LoggedQuote]:
        with self._lock:
            return list(self._data.get(instrument, ()))

    def at_or_before(self, instrument: str, at: datetime) -> LoggedQuote | None:
        """at 시각에 알던 마지막 시세(받은 시각 ≤ at). 모르면 None.

        이력이 가득 차 오래된 항목을 버렸고 at이 남은 가장 오래된 항목보다 앞이면, 그 사이를 모르므로 None이다."""
        with self._lock:
            d = self._data.get(instrument)
            if not d:
                return None
            if d[0].quote.at > at:
                return None
            best = None
            for e in d:                     # 받은 순서 = 시각 순서 (시계 주입 시험에서도 단조)
                if e.quote.at <= at:
                    best = e
                else:
                    break
            return best


class PollStats:
    """시세 조회 관찰 누적기. drain(now)이 창이 끝났을 때 요약 하나를 돌려주고 비운다."""

    def __init__(self, window: timedelta = STAT_WINDOW) -> None:
        self.window = window
        self.watched = 0                                  # 공급자가 맡은 종목 수
        self._lock = threading.Lock()
        self._start: datetime | None = None
        self._requests = 0
        self._failures = 0
        self._latency_ms: list[int] = []
        self._intervals: list[Decimal] = []
        self._instruments: set[str] = set()
        self._last_ok: dict[str, datetime] = {}           # 창을 넘어 유지 (종목별 직전 성공 시각)
        self._fields: tuple[str, ...] | None = None       # 이번 창에 본 필드 이름 집합
        self._fields_reported: tuple[str, ...] | None = None

    def observe(self, instrument: str, ok: bool, latency_seconds: float, at: datetime,
                fields: Iterable[str] | None = None) -> None:
        with self._lock:
            if self._start is None:
                self._start = at
            self._requests += 1
            self._instruments.add(instrument)
            self._latency_ms.append(max(0, round(latency_seconds * 1000)))
            if not ok:
                self._failures += 1
                return
            prev = self._last_ok.get(instrument)
            if prev is not None and at >= prev:
                self._intervals.append(Decimal(str((at - prev).total_seconds())))
            self._last_ok[instrument] = at
            if fields is not None:
                self._fields = tuple(sorted({str(f) for f in fields}))

    def drain(self, now: datetime) -> dict | None:
        with self._lock:
            if self._start is None or now - self._start < self.window:
                return None
            lat = sorted(self._latency_ms)
            iv = self._intervals
            fields = None
            if self._fields is not None and self._fields != self._fields_reported:
                fields = list(self._fields)
                self._fields_reported = self._fields
            out = {"window_start": self._start, "window_end": now, "watched": self.watched,
                   "instruments": len(self._instruments), "requests": self._requests, "failures": self._failures,
                   "latency_ms_p50": lat[(len(lat) - 1) // 2] if lat else None,
                   "latency_ms_max": lat[-1] if lat else None,
                   "interval_avg_seconds": _s3(sum(iv, Decimal(0)) / len(iv)) if iv else None,
                   "interval_max_seconds": _s3(max(iv)) if iv else None,
                   "response_fields": fields}
            self._start = None
            self._requests = self._failures = 0
            self._latency_ms, self._intervals, self._instruments, self._fields = [], [], set(), None
            return out


def _s3(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.001")))
