"""M2 1분봉 수신 버퍼 — 시세 공급 스레드 → 판정·실행 스레드 (CP-037 D-220, T-051, 승인 A-113). 네트워크·저장소 없음.

`binance-feed`(broker/binance/feed, 공통 계약 모양 MinuteSink)가 판정 격자 시각마다 받은 1m 봉을 넣고, 판정·실행 스레드
(app/m2_minutes.M2MinuteInput)가 꺼내 기존 추가 전용 `minute_bar` 표(M2 DB)에 기록한다 — 저장소 쓰기는 판정·실행 스레드만
(CP-036 D-215와 같은 원칙). 모든 읽기·쓰기는 잠금 안.

- 닫힌 봉만 넘긴다: 봉 끝 ≤ 받은 시각 − 확정 여유. 진행 중 봉은 값이 바뀌므로 추가 전용 표에 넣지 않는다.
- 품질(domain/minute_series.minute_quality_issues — 중복·역순·음수·0 이하 가격·고저 모순)이나 응답 해석 실패
  (minute_quality_failed, 공급자가 klines 품질 실패를 알림) → 그 묶음은 버리고 그 심볼 "품질 실패" 상태 + 품질 사건
  하나를 쌓는다. **정지(K10 등)는 발동하지 않는다** [DP-72 개정, T-051 재검토]: K10은 종목 범위라 분봉을 쓰지 않는 전략의
  진입까지 막고 일봉 data_ok 보고(UTC 하루 1회)로만 풀린다. 품질 실패 상태는 CAP-DAT-02 선언 전략의 그 심볼 분봉 입력만
  무효로 만들고(app/m2_minutes._one QUALITY), 사건은 판정·실행 스레드가 꺼내 감사·묶음 경고한다. 다음 정상 수신이 품질
  상태를 지운다.
- 쌓이기만 하고 꺼내지 않는 경우를 막는 상한 max_pending(오래된 것부터 버리고 dropped로 센다). 저장 실패로 되돌린 봉
  (requeue)도 같은 상한 [DP-76].
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import DEFAULT_CONFIRM_MARGIN, closed_bars, minute_quality_issues

MAX_PENDING = 20_000
MAX_QUALITY_EVENTS = 1_000


@dataclass(frozen=True)
class MinuteQualityEvent:
    symbol: str
    reason: str
    at: datetime


class SpotMinuteBook:
    def __init__(self, universe: Sequence[str], *, margin: timedelta = DEFAULT_CONFIRM_MARGIN,
                 account: str | None = None, max_pending: int = MAX_PENDING) -> None:
        self.universe = tuple(universe)
        self.margin = margin
        self.account = account
        self._pending: deque[MinuteBar] = deque()
        self._max = max_pending
        self._quality: dict[str, str] = {}
        self._events: deque[MinuteQualityEvent] = deque(maxlen=MAX_QUALITY_EVENTS)
        self._fetched: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self.dropped = 0

    def __repr__(self) -> str:
        return f"SpotMinuteBook(universe={list(self.universe)}, pending={len(self._pending)}, dropped={self.dropped})"

    # --- 공급 스레드 (MinuteSink) ---------------------------------------------------------------
    def put_minute_bars(self, symbol: str, bars: Sequence[MinuteBar], fetched_at: datetime) -> None:
        """받은 순서(과거→최신)의 1m 봉. 품질 위반이면 버리고 품질 실패 상태, 아니면 닫힌 봉만 저장 대기열에."""
        if symbol not in self.universe:
            return
        issues = minute_quality_issues(bars) + [f"{b.instrument} 심볼 불일치" for b in bars if b.instrument != symbol]
        if issues:
            self.minute_quality_failed(symbol, "; ".join(issues[:3]), fetched_at)
            return
        kept = closed_bars(bars, fetched_at, self.margin)
        with self._lock:
            self._quality.pop(symbol, None)
            self._fetched[symbol] = fetched_at
            self._pending.extend(kept)
            self._trim()

    def minute_quality_failed(self, symbol: str, reason: str, at: datetime) -> None:
        """정지 없음 (DP-72 개정) — 그 심볼 분봉 입력 무효 상태 + 감사·경고용 사건만."""
        if symbol not in self.universe:
            return
        with self._lock:
            self._quality[symbol] = reason
            self._fetched[symbol] = at
            self._events.append(MinuteQualityEvent(symbol, reason, at))

    # --- 판정·실행 스레드 -------------------------------------------------------------------------
    def drain(self) -> list[MinuteBar]:
        with self._lock:
            out = list(self._pending)
            self._pending.clear()
            return out

    def requeue(self, bars: Sequence[MinuteBar]) -> None:
        """저장 실패로 기록하지 못한 봉을 대기열 앞에 되돌린다(다음 flush 재시도, DP-76). 상한을 넘으면 오래된 것부터 버린다."""
        if not bars:
            return
        with self._lock:
            self._pending.extendleft(reversed(list(bars)))
            self._trim()

    def drain_quality_events(self) -> list[MinuteQualityEvent]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
            return out

    def requeue_quality_events(self, events: Sequence[MinuteQualityEvent]) -> None:
        if not events:
            return
        with self._lock:
            newer = list(self._events)
            self._events.clear()
            self._events.extend([*events, *newer])

    def quality_problem(self, symbol: str) -> str | None:
        with self._lock:
            return self._quality.get(symbol)

    def fetched_at(self, symbol: str) -> datetime | None:
        """마지막 수신(정상·품질 실패 모두) 시각 — 주기 판정(T-052)이 그 판정 시각의 조회가 끝났는지 볼 때 쓴다."""
        with self._lock:
            return self._fetched.get(symbol)

    def _trim(self) -> None:
        over = len(self._pending) - self._max
        for _ in range(max(over, 0)):
            self._pending.popleft()
        self.dropped += max(over, 0)
