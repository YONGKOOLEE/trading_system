"""알림 기록 (03 D-093, 01 §2.10).

알림은 감사 이벤트(ALERT)로 남기고, 전송(텔레그램)은 T-013이 이 기록을 읽어 보낸다.
저장소에 쓸 수 없으면(K7 상황) 저장소와 독립된 로컬 파일에 남긴다. 그것도 안 되면 표준 오류로 출력한다.
메시지에 비밀정보를 넣지 않는다 (호출자 책임, 계좌번호 대신 별칭).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from trading_system.domain.clock import Clock, to_iso

from .repository import Repository, StorageFailure


class AuditAlertSink:
    def __init__(self, repo: Repository, clock: Clock, fallback: Path | None = None, actor: str = "system") -> None:
        self._repo = repo
        self._clock = clock
        self._fallback = fallback
        self._actor = actor

    def alert(self, level: str, message: str, ref: str | None) -> None:
        now = self._clock.now()
        try:
            with self._repo.tx():
                self._repo.audit(now, "ALERT", self._actor, ref, {"level": level, "message": message})
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
