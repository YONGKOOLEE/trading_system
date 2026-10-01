"""불변 조건 검사기 자체가 위반을 잡는지 확인한다 (04 D-069 전제)."""

from __future__ import annotations

from pathlib import Path

from tests.support.invariants import check_invariants
from trading_system.domain.enums import Mode
from trading_system.storage.repository import Repository


def _repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "inv.db", Mode.BACKTEST)


def test_clean_db_has_no_violations(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert check_invariants(repo.connection, "BACKTEST") == []
    repo.close()


def test_detects_two_active_ownerships(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for i in range(2):
        repo.connection.execute(
            "INSERT INTO ownership(ownership_id, mode, account, instrument, owner, state, created_at) "
            "VALUES(?, 'BACKTEST', 'A', 'X', 's', 'CONFIRMED', 't')", (f"o{i}",))
    assert any(v.startswith("INV-05") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


def test_detects_intent_without_approved_decision_and_mode_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    c = repo.connection
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("""INSERT INTO intent(intent_id, mode, decision_id, idem_key, source, request_type, account, instrument,
                 side, quantity, order_type, price, state, filled_quantity, created_at, created_seq)
                 VALUES('i1','PAPER','missing','k','SIGNAL','ENTRY','A','X','BUY','1','MARKET','1','PENDING_SEND','0','t',1)""")
    found = check_invariants(c, "BACKTEST")
    assert any(v.startswith("INV-01") for v in found)
    assert any(v.startswith("INV-12") for v in found)
    repo.close()


def test_detects_negative_position(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.connection.execute("INSERT INTO position VALUES('A','X','BACKTEST','-1','0')")
    assert any(v.startswith("INV-04") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()
