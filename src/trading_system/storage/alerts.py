"""알림 기록 (03 D-093, 01 §2.10, 검토 RV-16).

알림은 감사 이벤트(ALERT)로 남기고, 전송(텔레그램)은 T-013이 이 기록을 읽어 보낸다.
저장소에 쓸 수 없으면(K7 상황) 저장소와 독립된 로컬 파일에 남긴다. 그것도 안 되면 표준 오류로 출력한다.
메시지에 비밀정보를 넣지 않는다 (호출자 책임, 계좌번호 대신 별칭).

RV-16 가시성:
- WARNING 이상은 콘솔(stderr)에 `[KST 시각] 등급 Kx 문구`로 바로 보인다(가림 처리).
- 모든 알림을 운영 로그 `<log_dir>/ops-YYYYMMDD.jsonl`에 남긴다. log_dir 기본값은 fallback 파일의 폴더(var/<mode>/logs).
- 같은 (등급·문구·참조) 알림은 억제 창(기본 60초) 안에서 처음 한 번만 기록·출력하고 나머지는 센다.
  창이 닫히면(다음 알림 또는 flush() 때) 억제 건수 요약을 한 번 기록한다. 첫 발생은 항상 DB 감사에 남는다.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from trading_system.domain.clock import Clock, to_iso

from .oplog import console_line, halt_type_of, write_console, write_ops
from .repository import Repository, StorageFailure

CONSOLE_LEVELS = frozenset({"WARNING", "EMERGENCY"})
DEDUP_WINDOW = timedelta(seconds=60)


@dataclass
class _Window:
    started: datetime
    suppressed: int = 0


class AuditAlertSink:
    def __init__(self, repo: Repository, clock: Clock, fallback: Path | None = None, actor: str = "system", *,
                 log_dir: Path | None = None, console: bool = True,
                 dedup_window: timedelta | None = DEDUP_WINDOW) -> None:
        self._repo = repo
        self._clock = clock
        self._fallback = fallback
        self._actor = actor
        self._log_dir = log_dir if log_dir is not None else (fallback.parent if fallback is not None else None)
        self._console = console
        self._window = dedup_window
        self._open: dict[tuple[str, str, str | None], _Window] = {}
        self._lock = threading.Lock()

    def alert(self, level: str, message: str, ref: str | None) -> None:
        now = self._clock.now()
        key = (level, message, ref)
        with self._lock:  # 잠금 안에서는 상태만 바꾼다. 기록(저장소 잠금)은 잠금 밖에서 (교착 방지)
            summaries = self._close_expired(now)
            suppressed = False
            if self._window is not None:
                w = self._open.get(key)
                if w is not None:
                    w.suppressed += 1
                    suppressed = True
                else:
                    self._open[key] = _Window(now)
        self._emit_summaries(now, summaries)
        if not suppressed:
            self._emit(now, level, message, ref, {})

    def flush(self) -> None:
        """억제 창이 닫힌 알림의 억제 건수 요약을 기록한다. 본 루프가 tick마다 불러도 된다."""
        now = self._clock.now()
        with self._lock:
            summaries = self._close_expired(now)
        self._emit_summaries(now, summaries)

    def _close_expired(self, now: datetime) -> list[tuple[tuple[str, str, str | None], int]]:
        if self._window is None:
            return []
        done = []
        for key in [k for k, w in self._open.items() if now - w.started >= self._window]:
            w = self._open.pop(key)
            if w.suppressed:
                done.append((key, w.suppressed))
        return done

    def _emit_summaries(self, now: datetime, summaries: list[tuple[tuple[str, str, str | None], int]]) -> None:
        assert self._window is not None or not summaries
        for (level, message, ref), n in summaries:
            secs = int(self._window.total_seconds()) if self._window else 0
            self._emit(now, level, f"반복 억제 요약: \"{message}\" — {secs}초 동안 추가 {n}건", ref, {"suppressed": n})

    def _emit(self, now: datetime, level: str, message: str, ref: str | None, extra: dict) -> None:
        if self._console and level in CONSOLE_LEVELS:
            write_console(console_line(now, level, message, ref))
        write_ops(self._log_dir, {"kind": "ALERT", "level": level, "halt_type": halt_type_of(message),
                                  "message": message, "ref": ref, **extra}, now)
        try:
            with self._repo.tx():
                self._repo.audit(now, "ALERT", self._actor, ref, {"level": level, "message": message, **extra})
            return
        except (StorageFailure, sqlite3.Error):
            pass
        line = f"{to_iso(now)}\t{level}\t{ref or '-'}\t{message}\n"
        try:
            if self._fallback is not None:
                self._fallback.parent.mkdir(parents=True, exist_ok=True)
                with self._fallback.open("a", encoding="utf-8") as f:
                    f.write(line)
                return
        except OSError:
            pass
        sys.stderr.write("ALERT(저장 실패) " + line)
