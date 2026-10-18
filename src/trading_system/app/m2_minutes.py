"""M2 1분봉 판정 입력 — 저장(추가 전용 minute_bar)·판정 창·지문·누락 경고 (CP-037 D-220, T-051, 승인 A-113).

판정·실행 스레드에서만 부른다(저장소 쓰기, CP-036 D-215). 공급(binance-feed → marketdata/spot_minute.SpotMinuteBook)과
판정 사이의 경계:
1. flush: 버퍼의 닫힌 1m 봉을 기존 `minute_bar` 표(M2 DB, 키 (심볼, 1, 봉 시작 UTC, adjusted="none"), source =
   "binance-testnet")에 추가한다. 같은 키 재수신은 기존 값 유지 — 값이 다른 재수신이 있으면 건수를 감사
   MINUTE_BARS_STORED로 남긴다(V-293). 새 스키마 버전 없음(T-028 추가 전용 표 재사용). SpotProfile.reconcile_step이
   tick마다 부른다(판정 전략이 없어도 기록은 쌓인다). 버퍼가 쌓은 품질 사건은 같은 저장 단위에서 감사 MINUTE_QUALITY.
   **저장 실패 격리 [DP-76]**: flush의 저장 실패(StorageFailure·sqlite3.Error)는 엔진 단계로 올리지 않는다(올리면
   _step_failed → 계좌 전역 K8). 꺼낸 봉·사건은 되돌려 다음 flush에 다시 기록하고, 성공 전까지 CAP-DAT-02 전략의 분봉
   입력은 STORE_FAILED(신호 무효), 묶음 경고(첫 1건·30분 요약·회복 INFO).
2. inputs: 판정 예정 시각 at의 창(직전 닫힌 봉부터 lookback개)을 **저장 봉에서** 읽어 판정한다 — 같은 at이면 재수신 값과
   무관하게 같은 입력(재현, 04 §4.2 신호 일치율). OK가 아니면 그 심볼 판정 신호 무효(InstrumentView.data_ok False,
   minute_bars ()): LAST_MISSING(누락)·GAP(창 안 결측)·QUALITY(공급 품질 실패 — 정지 없음, DP-72 개정)·
   STORE_FAILED(분봉 저장 실패, DP-76). 판정마다 감사
   MINUTE_INPUT(ref = 판정 ID)에 심볼별 상태·기대 범위·봉 수·지문을 남긴다(D-220 "판정 스냅샷에 사용 봉 범위·지문").
3. 누락 경고 [DP-73, DP-67 방식]: 같은 심볼 LAST_MISSING 3회 연속이면 경고 1건, 계속되면 직전 알림 뒤 30분이 지난 다음
   누락 때 요약 1건, 경고 뒤 첫 OK 판정에서 회복 1건(INFO). GAP·QUALITY는 연속 수를 바꾸지 않는다. 품질 실패는 심볼별로
   같은 방식의 별도 묶음 경고(첫 실패 1건·30분 요약·정상 수신 뒤 회복 INFO, DP-72·DP-73).
CAP-DAT-02를 선언한 전략만 분봉 입력을 받는다. 선언했는데 분봉 공급이 꺼져 있으면(설정 [minute] 꺼짐) 모든 심볼
DISABLED = 신호 무효(불확실하면 주문하지 않음). M2 열 CAP-DAT-02는 T-052에서 "검증 중"(BACKTEST 등록만, PAPER는 "지원" 뒤).
판정 입력 규칙 [DP-75], 저장 값(adjusted·source) [DP-70].
4. 주기 판정 대기 [T-052, DP-79]: 엔진은 CAP-TRG-02·CAP-DAT-02 전략의 판정 시각 at에 pending_fetch로 그 시각 조회가 끝났는지
   보고 PeriodGrid.fetch_wait까지 기다린다. 넘으면 그 심볼은 FETCH_TIMEOUT(신호 무효)으로 판정한다(늦은 판정은 만들지 않음).
[DP-78 — DP-75 (6) 해소, CLAUDE.md §8-3] 분봉 입력이 무효여도 data_ok는 분봉 외 입력(규칙·심볼 상태·1d 봉)만 반영한다.
무효는 InstrumentView.minute_ok False + minute_bars ()로 전달하고, 판정 실행기가 그 종목 신규 진입·증액만 버린다 — 전략은
보유·최근가로 청산·감액 신호를 낼 수 있다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_system.domain.minute_series import (
    DEFAULT_CONFIRM_MARGIN, NO_ADJUSTMENT, ONE_MINUTE, WindowStatus, check_window, last_closed_start,
)
from trading_system.domain.sinks import AlertSink
from trading_system.domain.strategy import Bar
from trading_system.marketdata.spot_minute import MAX_QUALITY_EVENTS, MinuteQualityEvent, SpotMinuteBook
from trading_system.storage.repository import Repository, StorageFailure

MINUTE_CAPABILITY = "CAP-DAT-02"
MINUTE_SOURCE = "binance-testnet"
MINUTE_INPUT_AUDIT = "MINUTE_INPUT"
MINUTE_STORED_AUDIT = "MINUTE_BARS_STORED"
MINUTE_QUALITY_AUDIT = "MINUTE_QUALITY"
MISSING_ALERT_AFTER = 3
MISSING_SUMMARY_EVERY = timedelta(minutes=30)
CONDITION_SUMMARY_EVERY = MISSING_SUMMARY_EVERY   # 품질·저장 실패 묶음 요약 간격 (DP-72·DP-76 [제안])


@dataclass(frozen=True)
class MinuteInput:
    bars: tuple[Bar, ...]     # OK일 때만 lookback개(봉 시작 UTC 오름차순)
    ok: bool
    record: dict              # 감사 기록용 (상태·기대 범위·봉 수·지문)


@dataclass
class _Missing:
    count: int = 0
    alerted_at: datetime | None = None
    suppressed: int = 0


@dataclass
class _Condition:
    alerted_at: datetime | None = None
    suppressed: int = 0


class _ConditionAlerts:
    """조건 키별 묶음 경고 (DP-67·DP-73 방식): 첫 실패 1건 → 계속되면 직전 알림 뒤 30분이 지난 다음 실패 때 요약 1건 →
    회복 1건(INFO). 알림 대상이 없어도 상태는 센다."""

    def __init__(self, every: timedelta = CONDITION_SUMMARY_EVERY) -> None:
        self._every = every
        self._st: dict[str, _Condition] = {}

    def active(self) -> list[str]:
        return [k for k, st in self._st.items() if st.alerted_at is not None]

    def failed(self, key: str, alerts: AlertSink | None, now: datetime, first: str, summary: str) -> None:
        st = self._st.setdefault(key, _Condition())
        if st.alerted_at is None:
            text = first
        elif now - st.alerted_at >= self._every:
            text = f"{summary} (직전 알림 뒤 묶은 {st.suppressed}건)"
        else:
            st.suppressed += 1
            return
        st.alerted_at, st.suppressed = now, 0
        if alerts is not None:
            alerts.alert("WARNING", text, None)

    def recovered(self, key: str, alerts: AlertSink | None, text: str) -> None:
        st = self._st.pop(key, None)
        if st is not None and st.alerted_at is not None and alerts is not None:
            alerts.alert("INFO", f"{text} (묶은 알림 {st.suppressed}건)", None)


def _record(status: str, first: datetime, last: datetime, *, count: int = 0, missing: int = 0, fingerprint: str = "",
            reason: str = "") -> dict:
    rec: dict = {"status": status, "first": first.isoformat(), "last": last.isoformat(), "count": count,
                 "missing": missing, "fingerprint": fingerprint}
    if reason:
        rec["reason"] = reason
    return rec


class M2MinuteInput:
    def __init__(self, book: SpotMinuteBook, *, lookback_bars: int,
                 margin: timedelta = DEFAULT_CONFIRM_MARGIN, period_minutes: int | None = None) -> None:
        if lookback_bars <= 0:
            raise ValueError("lookback_bars는 1 이상")
        self.book = book
        self.lookback = lookback_bars
        self.margin = margin
        self.period_minutes = period_minutes   # 1m klines 조회 격자 N분 (설정 [minute]) — 주기 판정 격자 검사용 (T-052)
        self._missing: dict[str, _Missing] = {}
        self._conditions = _ConditionAlerts()
        self._store_problem: str | None = None           # 분봉 저장 실패 중이면 사유 (DP-76) — 성공한 flush가 지운다
        self._unaudited: list[MinuteQualityEvent] = []   # 저장 실패로 감사하지 못한 품질 사건 (다음 flush 재시도)

    def __repr__(self) -> str:
        return f"M2MinuteInput(lookback={self.lookback}, margin={self.margin}, book={self.book!r})"

    # --- 저장 ------------------------------------------------------------------------------------------
    def flush(self, repo: Repository, alerts: AlertSink | None, now: datetime) -> tuple[int, int, int]:
        """버퍼 → minute_bar. (새로 넣음, 같은 값 재수신, 다른 값 재수신 — 기존 값 유지).

        저장 실패는 올리지 않는다 [DP-76] — 봉·사건을 되돌리고 (0, 0, 0). 분봉 저장만의 실패로 계좌 전역 K8을 걸지 않는다."""
        fresh = self.book.drain_quality_events()
        self._quality_alerts(fresh, alerts, now)
        events = (self._unaudited + fresh)[-MAX_QUALITY_EVENTS:]
        self._unaudited = []
        bars = self.book.drain()
        if not bars and not events:
            self._store_ok(alerts)
            return 0, 0, 0
        inserted = same = differing = 0
        try:
            with repo.tx():
                if bars:
                    inserted, same, differing = repo.insert_minute_bars(bars, MINUTE_SOURCE, now)
                if differing:
                    repo.audit(now, MINUTE_STORED_AUDIT, "marketdata", None, {
                        "inserted": inserted, "same": same, "differing": differing,
                        "instruments": sorted({b.instrument for b in bars}),
                        "note": "같은 봉 재수신 값이 다름 — 기존 값 유지(추가 전용, D-220·V-293)"})
                for ev in events:
                    repo.audit(now, MINUTE_QUALITY_AUDIT, "marketdata", None, {
                        "instrument": ev.symbol, "reason": ev.reason, "at": ev.at.isoformat(),
                        "note": "1분봉 품질 실패 — 묶음 버림, CAP-DAT-02 전략의 그 심볼 분봉 입력 무효(정지 없음, DP-72)"})
        except (StorageFailure, sqlite3.Error) as exc:
            self.book.requeue(bars)
            self._unaudited = events
            self._store_failed(type(exc).__name__, len(bars), alerts, now)
            return 0, 0, 0
        self._store_ok(alerts)
        return inserted, same, differing

    def _store_failed(self, kind: str, n: int, alerts: AlertSink | None, now: datetime) -> None:
        self._store_problem = f"분봉 저장 실패 {kind}"
        self._conditions.failed(
            "store", alerts, now,
            f"분봉 저장 실패: {kind} — 받은 봉 {n}개는 다음 tick에 다시 기록, 그때까지 CAP-DAT-02 전략 분봉 입력 무효 "
            "(계좌 정지 아님, DP-76)",
            f"분봉 저장 실패 계속: {kind} — CAP-DAT-02 전략 분봉 입력 무효 (DP-76)")

    def _store_ok(self, alerts: AlertSink | None) -> None:
        self._store_problem = None
        self._conditions.recovered("store", alerts, "분봉 저장 회복: 대기 봉 기록됨 (DP-76)")

    def _quality_alerts(self, events: Sequence[MinuteQualityEvent], alerts: AlertSink | None, now: datetime) -> None:
        """심볼별 품질 실패 묶음 경고(정지 없음, DP-72·DP-73) — 정상 수신으로 품질 상태가 지워지면 회복 1건."""
        for ev in events:
            self._conditions.failed(
                f"quality {ev.symbol}", alerts, now,
                f"분봉 품질 실패 {ev.symbol}: {ev.reason} — CAP-DAT-02 전략의 그 심볼 분봉 입력 무효 (정지 없음, DP-72)",
                f"분봉 품질 실패 계속 {ev.symbol}: 최근 {ev.reason} — 분봉 입력 무효 (DP-72·DP-73)")
        for key in self._conditions.active():
            sym = key.removeprefix("quality ")
            if key.startswith("quality ") and self.book.quality_problem(sym) is None:
                self._conditions.recovered(key, alerts, f"분봉 품질 회복 {sym}: 정상 수신")

    # --- 판정 입력 -------------------------------------------------------------------------------------
    def pending_fetch(self, symbols: Sequence[str], at: datetime) -> frozenset[str]:
        """판정 시각 at의 1m klines 조회(정상·품질 실패 수신 모두)가 아직 끝나지 않은 심볼 (T-052 주기 판정 대기, DP-79).
        공급 스레드는 격자 시각(= at) 이후 조회해 받은 시각을 남긴다 — 받은 시각 < at이면 이전 격자의 조회다."""
        out = []
        for s in symbols:
            got = self.book.fetched_at(s)
            if got is None or got < at:
                out.append(s)
        return frozenset(out)

    def inputs(self, repo: Repository, alerts: AlertSink | None, symbols: Sequence[str], at: datetime, now: datetime,
               ev_id: str, unfetched: frozenset[str] = frozenset()) -> dict[str, MinuteInput]:
        """unfetched: 기다림 상한(PeriodGrid.fetch_wait)이 지나도 그 판정 시각 조회가 끝나지 않은 심볼 → FETCH_TIMEOUT
        (신호 무효 — 판정 직전 닫힌 봉을 받지 못한 것이므로 누락 연속 수에 센다, DP-73·DP-79)."""
        self.flush(repo, alerts, now)
        out = {s: self._one(repo, alerts, s, at, now, ev_id, s in unfetched) for s in symbols}
        with repo.tx():
            repo.audit(now, MINUTE_INPUT_AUDIT, "engine", ev_id, {
                "at": at.isoformat(), "lookback": self.lookback, "margin_s": self.margin.total_seconds(),
                "instruments": {s: r.record for s, r in out.items()}})
        return out

    def _one(self, repo: Repository, alerts: AlertSink | None, s: str, at: datetime, now: datetime,
             ev_id: str, unfetched: bool = False) -> MinuteInput:
        last = last_closed_start(at, self.margin)
        first = last - ONE_MINUTE * (self.lookback - 1)
        if unfetched:   # 그 판정 시각의 조회가 기다림 상한 안에 끝나지 않음 (T-052, DP-79)
            self._track(s, WindowStatus.LAST_MISSING, alerts, now, ev_id)
            return MinuteInput((), False, _record("FETCH_TIMEOUT", first, last,
                                                  reason="판정 시각 1m klines 조회가 기다림 상한 안에 끝나지 않음"))
        if self._store_problem is not None:   # 저장 실패 중 — 저장 봉 창이 받은 봉을 반영하지 못함 (DP-76)
            return MinuteInput((), False, _record("STORE_FAILED", first, last, reason=self._store_problem))
        problem = self.book.quality_problem(s)
        if problem is not None:
            return MinuteInput((), False, _record("QUALITY", first, last, reason=problem))
        stored = repo.minute_bars(s, 1, NO_ADJUSTMENT, start=first, end=last + ONE_MINUTE)
        w = check_window(stored, at, self.lookback, self.margin)
        self._track(s, w.status, alerts, now, ev_id)
        rec = _record(w.status.value, first, last, count=len(stored), missing=w.missing, fingerprint=w.fingerprint)
        if w.status is not WindowStatus.OK:
            return MinuteInput((), False, rec)
        return MinuteInput(tuple(Bar(b.start, b.open, b.high, b.low, b.close, b.volume) for b in w.bars), True, rec)

    def _track(self, s: str, status: WindowStatus, alerts: AlertSink | None, now: datetime, ev_id: str) -> None:
        st = self._missing.setdefault(s, _Missing())
        if status is WindowStatus.OK:
            if st.alerted_at is not None and alerts is not None:
                alerts.alert("INFO", f"분봉 누락 회복 {s}: 판정 직전 닫힌 1분 봉 다시 수신 ({st.count}회 연속 누락 뒤, "
                                     f"묶은 알림 {st.suppressed}건)", ev_id)
            self._missing[s] = _Missing()
            return
        if status is not WindowStatus.LAST_MISSING:
            return
        st.count += 1
        if st.count < MISSING_ALERT_AFTER:
            return
        if st.alerted_at is None:
            text = (f"분봉 누락 {s}: 판정 직전 닫힌 1분 봉이 {st.count}회 연속 없음 — 그 판정 신호 무효 (D-220)")
        elif now - st.alerted_at >= MISSING_SUMMARY_EVERY:
            text = (f"분봉 누락 계속 {s}: {st.count}회 연속 — 판정 신호 무효 (직전 알림 뒤 묶은 {st.suppressed}건, "
                    "D-220·DP-73)")
        else:
            st.suppressed += 1
            return
        st.alerted_at, st.suppressed = now, 0
        if alerts is not None:
            alerts.alert("WARNING", text, ev_id)


def disabled_inputs(repo: Repository, symbols: Sequence[str], at: datetime, now: datetime, ev_id: str,
                    margin: timedelta = DEFAULT_CONFIRM_MARGIN) -> dict[str, MinuteInput]:
    """CAP-DAT-02 선언 전략인데 분봉 공급이 꺼짐 → 모든 심볼 신호 무효 + 감사 (불확실하면 주문하지 않음)."""
    last = last_closed_start(at, margin)
    out = {s: MinuteInput((), False, _record("DISABLED", last, last,
                                             reason="분봉 공급 꺼짐(config/m2/market.<mode>.toml [minute])"))
           for s in symbols}
    with repo.tx():
        repo.audit(now, MINUTE_INPUT_AUDIT, "engine", ev_id, {
            "at": at.isoformat(), "instruments": {s: r.record for s, r in out.items()}})
    return out
