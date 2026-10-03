"""저장 내구성 설정 명시 (review-2026-10-03 C절): 빌드 기본값에 기대지 않고 synchronous=FULL·WAL."""

from __future__ import annotations

from pathlib import Path

from trading_system.domain.enums import Mode
from trading_system.storage.repository import Repository


def test_repository_sets_wal_and_synchronous_full(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    try:
        assert repo.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        repo.connection.execute("PRAGMA synchronous=NORMAL")   # 다른 값이어도 새 연결은 FULL로 연다
    finally:
        repo.close()
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    try:
        assert repo.connection.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL
    finally:
        repo.close()
