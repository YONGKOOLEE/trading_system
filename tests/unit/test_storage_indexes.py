"""저장소 조회 인덱스 (review-2026-10-03 C절): tick마다 도는 상태·시각 조회가 전체 스캔하지 않게.

인덱스는 데이터 의미를 바꾸지 않으므로 스키마 버전을 올리지 않고 `CREATE INDEX IF NOT EXISTS`로 둔다 —
기존 PAPER 저장소도 다음 열기에서 자동 생성된다(마이그레이션 단계 없음, DP-38).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_system.domain.enums import Mode
from trading_system.storage.repository import SCHEMA_VERSION, Repository

QUERIES = [
    ("SELECT * FROM intent WHERE state=?", ("UNKNOWN",), "intent"),
    ("SELECT * FROM ops_command WHERE status=?", ("PENDING",), "ops_command"),
    ("SELECT * FROM audit_event WHERE kind=? AND ref=?", ("DAILY_BASE", "A"), "audit_event"),
    ("SELECT * FROM gate_decision WHERE account=? AND created_at>=?", ("A", "2026"), "gate_decision"),
    ("SELECT * FROM broker_order WHERE account=? AND trade_date=?", ("A", "2026-10-06"), "broker_order"),
    ("SELECT * FROM fill WHERE order_key=?", ("k",), "fill"),
    ("SELECT * FROM recon_item WHERE state='OPEN'", (), "recon_item"),
]


@pytest.mark.parametrize("sql,params,table", QUERIES)
def test_hot_queries_use_an_index(tmp_path: Path, sql: str, params: tuple, table: str) -> None:
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    try:
        plan = " ".join(str(r[-1]) for r in repo.connection.execute("EXPLAIN QUERY PLAN " + sql, params))
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan
    finally:
        repo.close()


def test_indexes_added_to_existing_db_without_version_change(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    Repository(db, Mode.BACKTEST).close()
    repo = Repository(db, Mode.BACKTEST)     # 다시 열어도 같은 버전, 인덱스 유지
    try:
        names = {r[0] for r in repo.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "ix_intent_state" in names
        ver = repo.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == SCHEMA_VERSION
    finally:
        repo.close()
