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


def _settings_row(repo: Repository, **kw: object) -> None:
    row = {"mode": "BACKTEST", "version_hash": "h", "kind": "ACCOUNT_LIMITS", "target": "A", "values_json": "{}",
           "previous_hash": None, "direction": "LOOSENED", "submitted_at": "2026-10-01T01:00:00.000000Z",
           "effective_at": "2026-10-01T23:00:00.000000Z", "deferred": 1, "ref_time_used": "08:00",
           "source": "LOCAL", "result": "PASSED", "reason": None}
    row.update(kw)
    cols = ",".join(row)
    repo.connection.execute(f"INSERT INTO settings_version({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


def test_inv14_detects_loosening_applied_immediately(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _settings_row(repo, deferred=0, effective_at="2026-10-01T01:00:00.000000Z")
    assert any(v.startswith("INV-14") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


def test_inv14_detects_effective_before_next_reference_time(tmp_path: Path) -> None:
    repo = _repo(tmp_path)  # 제출 10:00 KST, 기준 08:00 → 다음 날 08:00(=23:00Z) 이전 적용은 위반
    _settings_row(repo, deferred=1, effective_at="2026-10-01T05:00:00.000000Z")
    assert any(v.startswith("INV-14") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


def test_inv14_accepts_correct_deferral_and_ignores_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _settings_row(repo)
    _settings_row(repo, result="REJECTED", deferred=0, effective_at="2026-10-01T01:00:00.000000Z", version_hash="h2")
    assert check_invariants(repo.connection, "BACKTEST") == []
    repo.close()


def test_detects_negative_position(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.connection.execute("INSERT INTO position VALUES('A','X','BACKTEST','-1','0')")
    assert any(v.startswith("INV-04") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


def _intent_with_history(repo: Repository, iid: str, rt: str, side: str, states: list[str], seq: int) -> None:
    c = repo.connection
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("""INSERT INTO gate_decision(decision_id, mode, created_at, seq, source, request_type, account, instrument,
                 side, quantity, price, idem_key, result, checks) VALUES(?, 'BACKTEST','t',?, 'SIGNAL',?,'A','X',?,
                 '1','1',?, 'APPROVED','[]')""", (f"d-{iid}", seq, rt, side, f"k-{iid}"))
    c.execute("""INSERT INTO intent(intent_id, mode, decision_id, idem_key, source, request_type, account, instrument,
                 side, quantity, order_type, price, state, filled_quantity, created_at, created_seq)
                 VALUES(?, 'BACKTEST', ?, ?, 'SIGNAL', ?, 'A', 'X', ?, '1', 'MARKET', '1', ?, '0', 't', ?)""",
              (iid, f"d-{iid}", f"k-{iid}", rt, side, states[-1], seq))


def _state_event(repo: Repository, iid: str, st: str) -> None:
    repo.connection.execute("INSERT INTO audit_event(mode, at, kind, actor, ref, summary) VALUES"
                            "('BACKTEST','t','INTENT_STATE','x',?,?)", (iid, f'{{"state": "{st}"}}'))


def test_inv02_detects_risk_increase_created_while_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _intent_with_history(repo, "i1", "ENTRY", "BUY", ["UNKNOWN"], 1)
    _intent_with_history(repo, "i2", "INCREASE", "BUY", ["PENDING_SEND"], 2)
    for iid, st in [("i1", "PENDING_SEND"), ("i1", "SENT"), ("i1", "UNKNOWN"), ("i2", "PENDING_SEND")]:
        _state_event(repo, iid, st)
    assert any(v.startswith("INV-02") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


def test_inv11_detects_reopened_terminal_intent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _intent_with_history(repo, "i1", "ENTRY", "BUY", ["ACCEPTED"], 1)
    for st in ["PENDING_SEND", "SENT", "REJECTED", "ACCEPTED"]:
        _state_event(repo, "i1", st)
    assert any(v.startswith("INV-11") for v in check_invariants(repo.connection, "BACKTEST"))
    repo.close()


def test_replay_accepts_normal_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _intent_with_history(repo, "i1", "ENTRY", "BUY", ["FILLED"], 1)
    for st in ["PENDING_SEND", "SENT", "ACCEPTED", "FILLED"]:
        _state_event(repo, "i1", st)
    assert check_invariants(repo.connection, "BACKTEST") == []
    repo.close()
