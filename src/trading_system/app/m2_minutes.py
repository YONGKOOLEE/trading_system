"""M2 1분봉 판정 입력 — 저장(추가 전용 minute_bar)·판정 창·지문·누락 경고 (CP-037 D-220, T-051, 승인 A-113).

판정·실행 스레드에서만 부른다(저장소 쓰기, CP-036 D-215). 공급(binance-feed → marketdata/spot_minute.SpotMinuteBook)과
판정 사이의 경계:
1. flush: 버퍼의 닫힌 1m 봉을 기존 `minute_bar` 표(M2 DB, 키 (심볼, 1, 봉 시작 UTC, adjusted="none"), source =
   "binance-testnet")에 추가한다. 같은 키 재수신은 기존 값 유지 — 값이 다른 재수신이 있으면 건수를 감사
   MINUTE_BARS_STORED로 남긴다(V-293). 새 스키마 버전 없음(T-028 추가 전용 표 재사용). SpotProfile.reconcile_step이
   tick마다 부른다(판정 전략이 없어도 기록은 쌓인다).
2. inputs: 판정 예정 시각 at의 창(직전 닫힌 봉부터 lookback개)을 **저장 봉에서** 읽어 판정한다 — 같은 at이면 재수신 값과
   무관하게 같은 입력(재현, 04 §4.2 신호 일치율). OK가 아니면 그 심볼 판정 신호 무효(InstrumentView.data_ok False,
   minute_bars ()): LAST_MISSING(누락)·GAP(창 안 결측)·QUALITY(공급 품질 실패, K10은 버퍼가 발동). 판정마다 감사
   MINUTE_INPUT(ref = 판정 ID)에 심볼별 상태·기대 범위·봉 수·지문을 남긴다(D-220 "판정 스냅샷에 사용 봉 범위·지문").
3. 누락 경고 [DP-73, DP-67 방식]: 같은 심볼 LAST_MISSING 3회 연속이면 경고 1건, 계속되면 직전 알림 뒤 30분이 지난 다음
   누락 때 요약 1건, 경고 뒤 첫 OK 판정에서 회복 1건(INFO). GAP·QUALITY는 연속 수를 바꾸지 않는다(품질은 K10 알림).
CAP-DAT-02를 선언한 전략만 분봉 입력을 받는다. 선언했는데 분봉 공급이 꺼져 있으면(설정 [minute] 꺼짐) 모든 심볼
DISABLED = 신호 무효(불확실하면 주문하지 않음). M2 열 CAP-DAT-02는 capabilities.md에서 아직 "미지원"이라 등록이
거부된다 — 개정은 T-052(승인 + M1 run 꺼진 시간). 판정 입력 규칙 [DP-75], 저장 값(adjusted·source) [DP-70].
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_system.domain.minute_series import (
    DEFAULT_CONFIRM_MARGIN, NO_ADJUSTMENT, ONE_MINUTE, WindowStatus, check_window, last_closed_start,
)
from trading_system.domain.sinks import AlertSink
from trading_system.domain.strategy import Bar
from trading_system.marketdata.spot_minute import SpotMinuteBook
from trading_system.storage.repository import Repository

MINUTE_CAPABILITY = "CAP-DAT-02"
MINUTE_SOURCE = "binance-testnet"
MINUTE_INPUT_AUDIT = "MINUTE_INPUT"
MINUTE_STORED_AUDIT = "MINUTE_BARS_STORED"
MISSING_ALERT_AFTER = 3
MISSING_SUMMARY_EVERY = timedelta(minutes=30)


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


def _record(status: str, first: datetime, last: datetime, *, count: int = 0, missing: int = 0, fingerprint: str = "",
            reason: str = "") -> dict:
    rec: dict = {"status": status, "first": first.isoformat(), "last": last.isoformat(), "count": count,
                 "missing": missing, "fingerprint": fingerprint}
    if reason:
        rec["reason"] = reason
    return rec


class M2MinuteInput:
    def __init__(self, book: SpotMinuteBook, *, lookback_bars: int,
                 margin: timedelta = DEFAULT_CONFIRM_MARGIN) -> None:
        if lookback_bars <= 0:
            raise ValueError("lookback_bars는 1 이상")
        self.book = book
        self.lookback = lookback_bars
        self.margin = margin
        self._missing: dict[str, _Missing] = {}

    def __repr__(self) -> str:
        return f"M2MinuteInput(lookback={self.lookback}, margin={self.margin}, book={self.book!r})"

    # --- 저장 ------------------------------------------------------------------------------------------
    def flush(self, repo: Repository, alerts: AlertSink | None, now: datetime) -> tuple[int, int, int]:
        """버퍼 → minute_bar. (새로 넣음, 같은 값 재수신, 다른 값 재수신 — 기존 값 유지)."""
        bars = self.book.drain()
        if not bars:
            return 0, 0, 0
        with repo.tx():
            inserted, same, differing = repo.insert_minute_bars(bars, MINUTE_SOURCE, now)
            if differing:
                repo.audit(now, MINUTE_STORED_AUDIT, "marketdata", None, {
                    "inserted": inserted, "same": same, "differing": differing,
                    "instruments": sorted({b.instrument for b in bars}),
                    "note": "같은 봉 재수신 값이 다름 — 기존 값 유지(추가 전용, D-220·V-293)"})
        return inserted, same, differing

    # --- 판정 입력 -------------------------------------------------------------------------------------
    def inputs(self, repo: Repository, alerts: AlertSink | None, symbols: Sequence[str], at: datetime, now: datetime,
               ev_id: str) -> dict[str, MinuteInput]:
        self.flush(repo, alerts, now)
        out = {s: self._one(repo, alerts, s, at, now, ev_id) for s in symbols}
        with repo.tx():
            repo.audit(now, MINUTE_INPUT_AUDIT, "engine", ev_id, {
                "at": at.isoformat(), "lookback": self.lookback, "margin_s": self.margin.total_seconds(),
                "instruments": {s: r.record for s, r in out.items()}})
        return out

    def _one(self, repo: Repository, alerts: AlertSink | None, s: str, at: datetime, now: datetime,
             ev_id: str) -> MinuteInput:
        last = last_closed_start(at, self.margin)
        first = last - ONE_MINUTE * (self.lookback - 1)
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
