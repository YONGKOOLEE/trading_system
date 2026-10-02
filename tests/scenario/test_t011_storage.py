"""T-011 저장소 운영·감사·로그 (05-plan T-011 + CP-018 추가, 04 V-104·V-106·V-108, 03 §9·D-059·D-060·D-089).

시험의 계좌번호·토큰은 형식만 흉내 낸 가상값이다.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.domain.enums import Mode, RequestType
from trading_system.domain.models import Account, GateRequest
from trading_system.storage.jsonlog import JsonLogger, Masker, archive_logs
from trading_system.storage.maintenance import MaintenanceError, backup, migrate, restore_test, schema_version_of
from trading_system.storage.repository import Repository, SchemaMismatch

ROOT = Path(__file__).resolve().parents[2]


def _activity(env: Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity="10")))
    env.executor.submit(d.approved)
    env.executor.process_events()


# --- V-108 백업·복원 --------------------------------------------------------------------------

def test_v108_backup_and_restore_test_pass(env: Env, tmp_path: Path) -> None:
    _activity(env)
    b = backup(tmp_path / "trading.db", tmp_path / "backup")
    rep = restore_test(b, "BACKTEST")
    assert rep.ok, rep.checks
    assert set(rep.checks) == {"integrity", "mode", "schema_version", "audit_sequence", "audit_append_only"}


def test_v108_restore_test_detects_tampering_and_wrong_mode(env: Env, tmp_path: Path) -> None:
    _activity(env)
    b = backup(tmp_path / "trading.db", tmp_path / "backup")
    assert not restore_test(b, "PAPER").ok
    conn = sqlite3.connect(b)
    conn.execute("DROP TRIGGER audit_no_delete")
    conn.execute("DELETE FROM audit_event WHERE seq = 2")
    conn.commit()
    conn.close()
    rep = restore_test(b, "BACKTEST")
    assert not rep.ok
    assert rep.checks["audit_sequence"].startswith("실패") and rep.checks["audit_append_only"].startswith("실패")


def test_backup_keeps_only_latest_n(env: Env, tmp_path: Path) -> None:
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(9):
        backup(tmp_path / "trading.db", tmp_path / "backup", keep=7, now=base + timedelta(days=i))
    names = sorted(p.name for p in (tmp_path / "backup").glob("trading-*.db"))
    assert len(names) == 7 and names[0].startswith("trading-20261003")


def test_backup_while_main_process_holds_write_lock(env: Env, tmp_path: Path) -> None:
    done = threading.Event()

    def hold() -> None:
        with env.repo.tx():
            env.repo.audit(env.clock.now(), "TEST_LOCK", "t", None, {})
            time.sleep(0.5)
        done.set()

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.1)
    b = backup(tmp_path / "trading.db", tmp_path / "backup")
    t.join()
    assert restore_test(b, "BACKTEST").ok


# --- 마이그레이션 (D-060) ---------------------------------------------------------------------

def _db(tmp_path: Path) -> Path:
    p = tmp_path / "m.db"
    r = Repository(p, Mode.BACKTEST)
    r.register_account(Account("KR-BT-01", Mode.BACKTEST, "fake"))
    r.close()
    return p


def test_migration_forward_with_backup(tmp_path: Path) -> None:
    p = _db(tmp_path)
    applied = migrate(p, tmp_path / "bk", {1: "CREATE TABLE extra(a TEXT); ALTER TABLE halt ADD COLUMN note TEXT"},
                      target=2)
    assert applied == [2] and schema_version_of(p) == "2"
    assert list((tmp_path / "bk").glob("trading-*.db"))  # 적용 전 백업
    with pytest.raises(SchemaMismatch):
        Repository(p, Mode.BACKTEST)  # 코드(1)와 DB(2) 버전이 다르면 열지 않음


def test_migration_failure_rolls_back_and_refuses_bad_paths(tmp_path: Path) -> None:
    p = _db(tmp_path)
    with pytest.raises(MaintenanceError, match="롤백"):
        migrate(p, tmp_path / "bk", {1: "CREATE TABLE ok1(a); THIS IS NOT SQL"}, target=2)
    assert schema_version_of(p) == "1"
    conn = sqlite3.connect(p)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='ok1'").fetchone()[0] == 0
    conn.close()
    with pytest.raises(MaintenanceError, match="단계 없음"):
        migrate(p, tmp_path / "bk", {}, target=2)
    assert migrate(p, tmp_path / "bk", {}, target=1) == []
    with pytest.raises(MaintenanceError, match="높음"):
        migrate(p, tmp_path / "bk", {}, target=0)


def test_older_schema_refused_with_guidance(tmp_path: Path) -> None:
    p = _db(tmp_path)
    conn = sqlite3.connect(p)
    conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaMismatch, match="migrate"):
        Repository(p, Mode.BACKTEST)


# --- V-104 마스킹, V-106 추가 전용 --------------------------------------------------------------

def test_v104_masking(tmp_path: Path) -> None:
    secret = "APPKEY-virtual-SECRETVALUE"
    log = JsonLogger(tmp_path / "logs", "app", Masker([secret]))
    log.write({"msg": f"계좌 1234567890 / 12345678-90 키 {secret} 응답", "acnt_no": "5555555555",
               "token": "eyJhbGciOi.virtual", "nested": {"appkey": "x", "note": "acct 9876543210"},
               "items": ["0000000001x", 1234567890], "order_no": "0000123", "price": "10000", "qty": 15},
              at=datetime(2026, 10, 1, tzinfo=UTC))
    text = (tmp_path / "logs" / "app-20261001.jsonl").read_text(encoding="utf-8")
    for leaked in ("1234567890", "12345678-90", secret, "5555555555", "eyJhbGciOi", "9876543210"):
        assert leaked not in text, leaked
    rec = json.loads(text)
    assert rec["order_no"] == "0000123" and rec["price"] == "10000" and rec["qty"] == 15
    assert rec["acnt_no"] == "***" and rec["nested"]["appkey"] == "***"


def test_v106_audit_append_only(env: Env) -> None:
    _activity(env)
    with pytest.raises(sqlite3.DatabaseError):
        env.repo.connection.execute("UPDATE audit_event SET summary='{}' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError):
        env.repo.connection.execute("DELETE FROM audit_event WHERE seq=1")


# --- D-089 로그 보관 -----------------------------------------------------------------------------

def test_archive_logs_only_old_files_verified_before_delete(tmp_path: Path) -> None:
    d = tmp_path / "logs"
    d.mkdir()
    for day in ("20260601", "20260602", "20260920"):
        (d / f"app-{day}.jsonl").write_bytes(f'{{"day": "{day}"}}\n'.encode())
    done = archive_logs(d, 90, date(2026, 10, 1))
    assert done == ["app-20260601.jsonl", "app-20260602.jsonl"]
    assert sorted(p.name for p in d.glob("*.jsonl")) == ["app-20260920.jsonl"]
    [z] = list((d / "archive").glob("*.zip"))
    with zipfile.ZipFile(z) as zf:
        assert zf.read("app-20260601.jsonl") == b'{"day": "20260601"}\n'


# --- 로컬 도구 ---------------------------------------------------------------------------------

def test_backup_and_restore_scripts(env: Env, tmp_path: Path) -> None:
    _activity(env)
    db = tmp_path / "trading.db"
    a = subprocess.run([sys.executable, str(ROOT / "scripts" / "backup.py"), "--mode", "BACKTEST", "--db", str(db)],
                       capture_output=True, text=True, timeout=60)
    assert a.returncode == 0, a.stdout + a.stderr
    b = subprocess.run([sys.executable, str(ROOT / "scripts" / "restore-test.py"), "--mode", "BACKTEST", "--db",
                        str(db)], capture_output=True, text=True, timeout=60)
    assert b.returncode == 0 and "통과" in b.stdout, b.stdout + b.stderr
    assert ACCOUNT  # 표시용


def test_v108_restore_test_detects_corrupted_backup(env: Env, tmp_path: Path) -> None:
    _activity(env)
    b = backup(tmp_path / "trading.db", tmp_path / "backup")
    data = bytearray(b.read_bytes())
    for i in range(4096, min(len(data), 4096 * 3)):
        data[i] = 0xFF  # 2·3번째 페이지 손상
    b.write_bytes(bytes(data))
    rep = restore_test(b, "BACKTEST")
    assert not rep.ok and rep.checks["integrity"].startswith("실패")


def test_archive_keeps_originals_when_zip_verification_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "logs"
    d.mkdir()
    (d / "app-20260601.jsonl").write_bytes(b"original-content")
    real_read = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda self, name, pwd=None: real_read(self, name) + b"x")
    with pytest.raises(RuntimeError, match="압축 확인 실패"):
        archive_logs(d, 90, date(2026, 10, 1))
    assert (d / "app-20260601.jsonl").read_bytes() == b"original-content"


def test_v108_restore_test_reports_integrity_errors_found_by_check(env: Env, tmp_path: Path) -> None:
    """무결성 검사가 예외 없이 "문제 있음"을 돌려주는 손상도 실패로 판정한다."""
    with env.repo.tx():
        for i in range(1500):
            env.repo.audit(env.clock.now(), "FILLER", "t", f"r{i}", {"i": i})
    b = backup(tmp_path / "trading.db", tmp_path / "backup")
    original = b.read_bytes()
    for page in range(2, len(original) // 4096):
        data = bytearray(original)
        data[page * 4096 + 200: page * 4096 + 240] = b"\x00" * 40
        b.write_bytes(bytes(data))
        try:
            result = sqlite3.connect(b).execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError:
            continue
        if result != "ok":
            break
    else:
        pytest.fail("검사가 보고하는 손상을 만들지 못함")
    rep = restore_test(b, "BACKTEST")
    assert not rep.ok and rep.checks["integrity"].startswith("실패")
