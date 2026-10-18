"""M2 1분봉 이력 채우기(워밍업) — 나눔 조회·분당 상한·실패 시 신호 무효 (CP-040 D-261, T-066, 승인 A-115·A-123 ④).

**실행 경로 연결 없음**: 이 모듈은 주입한 fetch(합성 응답 또는 broker/binance/feed.minute_page_fetcher)로만 돈다. 테스트넷
klines 실제 백필 호출은 별도 승인 뒤(T-056류 관찰) app 조립에 연결한다. 연결 전에는 M2MinuteInput.history가 None —
판정 창이 저장 봉만으로 모자라면 기존 규칙(누락·결측·집계 봉 결측)으로 신호 무효다.

1. plan(repo, now): 판정 창에 필요한 1분 칸 [직전 닫힌 봉 − (window_minutes − 1), 직전 닫힌 봉] 가운데 저장되지 않은 칸만
   연속 구간으로 묶어 한 쪽 최대 1000봉으로 나눈다(domain/minute_agg.missing_ranges·split_pages). 쪽이 없으면 DONE.
2. step(now, alerts): 공급 스레드 몫. UTC 분마다 최대 per_minute쪽([제안] 20)을 조회한다 — 같은 클라이언트·WeightBudget·
   시세 우선순위(최하위). 응답은 parse_klines(필드·경계·closeTime·순서·OHLC) → 요청 범위 안·닫힌 봉만·품질
   (minute_quality_issues) 검사를 거친다(D-261 (3)). 조회 실패·보류(BinanceQueryError)·해석 실패·범위 밖 행·품질 위반 →
   그 심볼 FAILED + 정보 알림 1건, 남은 쪽은 조회하지 않는다(자동 재시도 없음 — 클라이언트 조회 재시도 최대 3회가 전부).
   거래소가 주지 않은 분(응답 안 빈 분)은 채우지 않는다 — 판정 창 규칙이 결측으로 다룬다(D-259 (4)).
3. flush(repo, now): 판정·실행 스레드 몫. 받은 봉을 같은 출처(MINUTE_SOURCE)로 추가 전용 minute_bar·minute_bar_ext에
   기록한다. 다른 출처(공개 CSV 등)로 메우는 경로는 없다(D-261 (2), 출처 전환은 CP-039).
4. status(symbol): PENDING(계획됨·조회 중) / DONE / FAILED(사유). PENDING·FAILED면 M2MinuteInput이 신호 무효.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from trading_system.broker.binance.client import BinanceQueryError
from trading_system.broker.binance.feed import minute_bar_of
from trading_system.broker.binance.market import KlineDataError, parse_klines
from trading_system.domain.minute_agg import KLINE_PAGE_MAX, missing_ranges, split_pages
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import (
    DEFAULT_CONFIRM_MARGIN, NO_ADJUSTMENT, ONE_MINUTE, last_closed_start, minute_quality_issues,
)
from trading_system.domain.sinks import AlertSink
from trading_system.storage.repository import Repository

from .m2_minutes import MINUTE_SOURCE

BACKFILL_PER_MINUTE = 20        # 분당 조회 상한 [제안] (D-261 (1))
BACKFILL_AUDIT = "MINUTE_BACKFILL"

Fetch = Callable[[str, int, int, int], object]   # (심볼, 시작 ms, 끝 ms, limit) → klines 1m 응답 본문


class BackfillState(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


class MinuteBackfill:
    def __init__(self, fetch: Fetch, symbols: Sequence[str], *, window_minutes: int,
                 margin: timedelta = DEFAULT_CONFIRM_MARGIN, per_minute: int = BACKFILL_PER_MINUTE,
                 page_max: int = KLINE_PAGE_MAX) -> None:
        if window_minutes <= 0 or per_minute <= 0 or not 1 <= page_max <= KLINE_PAGE_MAX:
            raise ValueError("window_minutes·per_minute는 1 이상, page_max는 1~1000")
        self._fetch = fetch
        self.symbols = tuple(symbols)
        self.window = window_minutes
        self.margin = margin
        self.per_minute = per_minute
        self.page_max = page_max
        self._lock = threading.Lock()
        self._pages: dict[str, list[tuple[datetime, datetime]]] = {}
        self._state: dict[str, tuple[BackfillState, str]] = {s: (BackfillState.PENDING, "계획 전") for s in symbols}
        self._got: list[MinuteBar] = []
        self._minute: datetime | None = None
        self._used = 0
        self.calls = 0

    def __repr__(self) -> str:
        return f"MinuteBackfill(symbols={list(self.symbols)}, window={self.window}, calls={self.calls})"

    # --- 계획 (판정·실행 스레드 — 저장소 읽기) ---------------------------------------------------------------
    def plan(self, repo: Repository, now: datetime) -> dict[str, int]:
        """심볼별 조회 쪽 수. 0쪽이면 DONE."""
        last = last_closed_start(now, self.margin)
        first = last - ONE_MINUTE * (self.window - 1)
        out: dict[str, int] = {}
        for s in self.symbols:
            have = [b.start for b in repo.minute_bars(s, 1, NO_ADJUSTMENT, start=first, end=last + ONE_MINUTE)]
            pages = split_pages(missing_ranges(have, first, last), self.page_max)
            with self._lock:
                self._pages[s] = pages
                self._state[s] = (BackfillState.DONE, "") if not pages else (BackfillState.PENDING, "채우기 중")
            out[s] = len(pages)
        return out

    def status(self, symbol: str) -> tuple[BackfillState, str]:
        with self._lock:
            return self._state.get(symbol, (BackfillState.PENDING, "계획 없음"))

    # --- 조회 (공급 스레드) ------------------------------------------------------------------------------------
    def step(self, now: datetime, alerts: AlertSink | None = None) -> int:
        """이번 UTC 분의 남은 몫만큼 쪽을 조회한다. 조회한 쪽 수."""
        minute = now.astimezone(UTC).replace(second=0, microsecond=0)
        if minute != self._minute:
            self._minute, self._used = minute, 0
        done = 0
        for s in self.symbols:
            while self._used < self.per_minute:
                with self._lock:
                    if self._state[s][0] is not BackfillState.PENDING or not self._pages.get(s):
                        break
                    page = self._pages[s][0]
                self._used += 1
                done += 1
                self.calls += 1
                problem = self._one(s, page, now)
                with self._lock:
                    if problem is not None:
                        self._state[s] = (BackfillState.FAILED, problem)
                        self._pages[s] = []
                    else:
                        self._pages[s].pop(0)
                        if not self._pages[s]:
                            self._state[s] = (BackfillState.DONE, "")
                if problem is not None and alerts is not None:
                    alerts.alert("INFO", f"분봉 이력 채우기 실패 {s}: {problem} — 그 심볼 장중 판정 신호 무효, 다른 출처로 "
                                         "메우지 않음 (D-261)", None)
        return done

    def _one(self, s: str, page: tuple[datetime, datetime], now: datetime) -> str | None:
        start, end = page
        start_ms, end_ms = _ms(start), _ms(end) + 59_999
        limit = int((end - start) / ONE_MINUTE) + 1
        try:
            rows = self._fetch(s, start_ms, end_ms, limit)
            ks = parse_klines(rows, symbol=s, interval="1m")
        except BinanceQueryError as exc:
            return f"조회 실패·보류: {exc}"
        except KlineDataError as exc:
            return f"응답 품질 실패: {exc}"
        bars = [minute_bar_of(k) for k in ks]
        if any(not start <= b.start <= end for b in bars):
            return "요청 범위 밖 봉"
        issues = minute_quality_issues(bars)
        if issues:
            return f"품질 실패: {'; '.join(issues[:3])}"
        limit_at = now - self.margin
        kept = [b for b in bars if b.end <= limit_at]
        with self._lock:
            self._got.extend(kept)
        return None

    # --- 저장 (판정·실행 스레드) ------------------------------------------------------------------------------
    def flush(self, repo: Repository, now: datetime) -> int:
        """받은 봉을 같은 출처로 기록 — 새로 넣은 minute_bar 수. 저장 실패는 올린다(받은 봉은 되돌림)."""
        with self._lock:
            got, self._got = self._got, []
        if not got:
            return 0
        try:
            with repo.tx():
                inserted, same, differing = repo.insert_minute_bars(got, MINUTE_SOURCE, now)
                ext = repo.insert_minute_bar_ext(got, MINUTE_SOURCE, now)
                repo.audit(now, BACKFILL_AUDIT, "marketdata", None, {
                    "bars": len(got), "inserted": inserted, "same": same, "differing": differing,
                    "ext_inserted": ext[0], "ext_differing": ext[2], "source": MINUTE_SOURCE,
                    "instruments": sorted({b.instrument for b in got}), "note": "분봉 이력 채우기 (D-261)"})
        except Exception:
            with self._lock:
                self._got = got + self._got
            raise
        return inserted
