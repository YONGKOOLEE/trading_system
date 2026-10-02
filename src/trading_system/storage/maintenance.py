"""저장소 운영: 백업·복원 시험·전진 전용 마이그레이션 (R-25, 03 §9·D-060·D-089, 04 V-108).

- 백업: SQLite 온라인 백업 API로 `var/<mode>/backup/trading-<시각>.db`. 최근 N개(기본 7)만 보관한다.
  (백업 파일 정리는 원본 데이터 삭제가 아니다. 감사·주문·체결 기록은 원본 DB에서 자동 삭제하지 않는다, D-089.)
- 복원 시험: 백업을 임시 위치에 복사 → 무결성 검사 → 모드·스키마 버전 확인 → 감사 순번 연속 확인.
- 마이그레이션: 버전 n → n+1 SQL을 차례로 적용. 적용 전 자동 백업, 한 단계씩 트랜잭션. 되돌리기는 없음(백업 복원).
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .repository import SCHEMA_VERSION

KEEP_BACKUPS = 7  # 01 §2.12

# 버전 n에서 n+1로 가는 SQL. 지금은 기준선(1)뿐이다. 스키마를 바꾸면 여기에 추가하고 SCHEMA_VERSION을 올린다.
MIGRATIONS: dict[int, str] = {}


class MaintenanceError(RuntimeError):
    pass


@dataclass
class RestoreReport:
    ok: bool
    checks: dict[str, str] = field(default_factory=dict)


def backup(db: Path, backup_dir: Path, keep: int = KEEP_BACKUPS, now: datetime | None = None) -> Path:
    if not db.exists():
        raise MaintenanceError(f"DB 없음: {db}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"trading-{stamp}.db"
    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(backup_dir.glob("trading-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def restore_test(backup_file: Path, expected_mode: str) -> RestoreReport:
    rep = RestoreReport(ok=True)

    def check(name: str, ok: bool, detail: str) -> None:
        rep.checks[name] = ("통과: " if ok else "실패: ") + detail
        rep.ok = rep.ok and ok

    with tempfile.TemporaryDirectory() as tmp:
        restored = Path(tmp) / "restored.db"
        shutil.copyfile(backup_file, restored)
        conn = sqlite3.connect(restored)
        try:
            try:
                r = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:  # 파일 자체가 깨진 경우
                check("integrity", False, f"열 수 없음: {exc}")
                return rep
            check("integrity", r is not None and r[0] == "ok", str(r[0] if r else None))
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            check("mode", meta.get("mode") == expected_mode, f"{meta.get('mode')} (기대 {expected_mode})")
            check("schema_version", meta.get("schema_version") == SCHEMA_VERSION,
                  f"{meta.get('schema_version')} (코드 {SCHEMA_VERSION})")
            seqs = [s for (s,) in conn.execute("SELECT seq FROM audit_event ORDER BY seq")]
            contiguous = seqs == list(range(1, len(seqs) + 1))
            check("audit_sequence", contiguous, f"{len(seqs)}건, 연속={contiguous}")
            triggers = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            check("audit_append_only", {"audit_no_update", "audit_no_delete"} <= triggers, ",".join(sorted(triggers)))
        finally:
            conn.close()
    return rep


def schema_version_of(db: Path) -> str | None:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        r = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def migrate(db: Path, backup_dir: Path, migrations: Mapping[int, str] | None = None,
            target: int | None = None) -> list[int]:
    """전진 전용. 적용 전 백업. 적용한 버전 목록을 돌려준다. 코드보다 높은 버전·건너뛴 단계가 있으면 거부."""
    steps = dict(MIGRATIONS if migrations is None else migrations)
    goal = int(SCHEMA_VERSION) if target is None else target
    cur_text = schema_version_of(db)
    if cur_text is None or not cur_text.isdigit():
        raise MaintenanceError("스키마 버전을 알 수 없는 DB — 마이그레이션하지 않음")
    cur = int(cur_text)
    if cur > goal:
        raise MaintenanceError(f"DB 버전 {cur}가 목표 {goal}보다 높음 (되돌리기는 백업 복원으로)")
    if cur == goal:
        return []
    missing = [v for v in range(cur, goal) if v not in steps]
    if missing:
        raise MaintenanceError(f"마이그레이션 단계 없음: {missing}")
    backup(db, backup_dir)
    applied: list[int] = []
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for v in range(cur, goal):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in (s.strip() for s in steps[v].split(";")):
                    if stmt:
                        conn.execute(stmt)
                conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(v + 1),))
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise MaintenanceError(f"{v}→{v + 1} 실패, 롤백함 (적용 전 백업 있음): {exc}") from exc
            applied.append(v + 1)
    finally:
        conn.close()
    return applied
