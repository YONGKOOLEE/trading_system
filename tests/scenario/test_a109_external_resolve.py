"""A-109 ④ 외부 주문 인정(EXTERNAL) 결함 수정 (2026-10-16 PV-11 run 관찰).

결함: 미확인 브로커 주문을 EXTERNAL로 해소하면 브로커가 이미 FILLED로 알린 주문도 ACCEPTED 0/1로 기록돼
status에 유령 미체결("0056724 005930 BUY 0/1 ACCEPTED EXTERNAL")로 보였다.
수정: 브로커 상태·체결 수량으로 기록한다(체결 행·포지션·소유권은 만들지 않음 — 수량은 QTY_MISMATCH 항목으로 따로).
이미 기록된 행은 사람이 실행하는 1회 정정 명령(scripts/fix-external-order.py)으로 고친다.
모든 값은 가상이다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.support.invariants import check_invariants
from trading_system.app.external_fix import fix_external_main
from trading_system.app.instance_lock import AUTOPILOT_LOCK, acquire_instance_lock
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.ops.cli import status_main
from trading_system.ops.reconcile import ReconcileResolver, ResolveError, correct_external_filled
from trading_system.reconciliation.reconciler import ItemKind, Reconciler
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import Repository


def rec(env: Env) -> Reconciler:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock))


def resolve_all(env: Env) -> None:
    r = ReconcileResolver(env.repo, env.executor, env.clock)
    for it in env.repo.open_recon_items(ACCOUNT):
        r.resolve(it["item_id"], "EXTERNAL", "u")


def ext_row(env: Env, no: str) -> dict:
    [row] = env.repo.select("SELECT * FROM broker_order WHERE order_no=? AND origin='EXTERNAL'", (no,))
    return row


def fills_of(env: Env, order_key: str) -> list[dict]:
    return env.repo.select("SELECT * FROM fill WHERE order_key=?", (order_key,))


# --- 해소 경로: 브로커 상태로 기록 ---------------------------------------------------------------

def test_external_filled_order_recorded_as_filled_not_phantom_open(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("30"))
    env.broker.fill(no)
    env.broker.drop_events()
    assert rec(env).run(ACCOUNT, "PERIODIC").result == "MISMATCH"
    resolve_all(env)
    row = ext_row(env, no)
    assert (row["state"], Decimal(row["filled_quantity"]), Decimal(row["quantity"])) == ("FILLED", 30, 30)
    assert fills_of(env, row["order_key"]) == []                    # 체결 행은 만들지 않음
    assert env.repo.position(ACCOUNT, "TST-C").quantity == 30       # 수량은 QTY_MISMATCH 해소로만
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"


def test_external_partial_then_expired_records_broker_filled_quantity(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("10"))
    env.broker.fill(no, Decimal("4"))
    env.broker.expire_open()
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    resolve_all(env)
    row = ext_row(env, no)
    assert (row["state"], Decimal(row["filled_quantity"])) == ("EXPIRED", 4)
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"


def test_external_cancelled_order_recorded_as_cancelled(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("5"))
    env.broker._set(no, state="CANCELLED")   # 증권사 앱에서 접수 후 취소 (체결 없음)
    rec(env).run(ACCOUNT, "PERIODIC")
    assert [i["kind"] for i in env.repo.open_recon_items(ACCOUNT)] == [ItemKind.UNKNOWN_BROKER_ORDER]
    resolve_all(env)
    row = ext_row(env, no)
    assert (row["state"], Decimal(row["filled_quantity"])) == ("CANCELLED", 0)
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"


def test_external_partial_order_stays_open_but_external(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("10"))
    env.broker.fill(no, Decimal("3"))
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    resolve_all(env)
    row = ext_row(env, no)
    assert (row["state"], Decimal(row["filled_quantity"])) == ("PARTIAL", 3)
    assert env.repo.open_orders(ACCOUNT) == []                      # 내부 미체결(게이트·대조)에는 여전히 없음
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"


def test_legacy_item_without_broker_state_uses_broker_value(env: Env) -> None:
    """수정 전에 열린 항목(detail에 broker_state 없음)도 broker_value "BUY 1 FILLED"로 종결 상태를 안다."""
    now = env.clock.now()
    with env.repo.tx():
        rid = env.repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MISMATCH", {}, now)
        iid = env.repo.insert_recon_item(rid, ACCOUNT, ItemKind.UNKNOWN_BROKER_ORDER, "TST-C", "0056724", None,
                                         "BUY 1 FILLED",
                                         json.dumps({"side": "BUY", "quantity": "1",
                                                     "trade_date": env.repo.trade_date_of(now),
                                                     "detected_at": now.isoformat()}))
    ReconcileResolver(env.repo, env.executor, env.clock).resolve(iid, "EXTERNAL", "u")
    row = ext_row(env, "0056724")
    assert (row["state"], Decimal(row["filled_quantity"])) == ("FILLED", 1)


def test_m2_spot_external_filled_order_recorded_as_filled(tmp_path: Path) -> None:
    """M2(심볼 범위 식별, 소수 수량): 같은 해소 경로 — 대조 항목 detail의 브로커 상태로 기록."""
    from tests.scenario.test_tm2_09_v157 import spot_reconciler
    from tests.support.m2_fakes import ACCOUNT as M2_ACCOUNT
    from tests.support.m2_fakes import build_m2_env
    from trading_system.reconciliation.spot import SpotItemKind
    e = build_m2_env(tmp_path / "m2")
    try:
        no = e.broker.add_external_order(M2_ACCOUNT, "TSTA-USDT", "BUY", Decimal("0.0500"), Decimal("100"),
                                         client_order_id="web_ext000009")
        e.broker.fill(no)
        e.broker.drop_events()
        spot_reconciler(e).run(M2_ACCOUNT, "PERIODIC")
        [it] = [i for i in e.repo.open_recon_items(M2_ACCOUNT) if i["kind"] == SpotItemKind.UNKNOWN_BROKER_ORDER]
        ReconcileResolver(e.repo, e.executor, e.clock).resolve(it["item_id"], "EXTERNAL", "u")
        [row] = e.repo.select("SELECT * FROM broker_order WHERE order_no=? AND origin='EXTERNAL'", (no,))
        assert (row["state"], Decimal(row["filled_quantity"])) == ("FILLED", Decimal("0.0500"))
        assert check_invariants(e.repo.connection, Mode.BACKTEST.value) == []
    finally:
        e.repo.close()


def test_status_does_not_list_terminal_external_orders(env: Env, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    filled = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("2"))
    env.broker.fill(filled)
    partial = env.broker.add_external_order(ACCOUNT, "TST-D", "BUY", Decimal("5"))
    env.broker.fill(partial, Decimal("1"))
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    resolve_all(env)
    assert status_main(["--mode", "BACKTEST", "--root", str(tmp_path), "--db", str(tmp_path / "trading.db")],
                       clock=env.clock) == 0
    out = capsys.readouterr().out
    assert "미체결 주문: 1건" in out, out
    assert f"주문번호 {partial} TST-D BUY 1/5 PARTIAL EXTERNAL" in out
    assert f"주문번호 {filled} " not in out


# --- 1회 정정: 이미 기록된 유령 미체결 행 ---------------------------------------------------------

def _legacy_external(repo: Repository, now: datetime, no: str = "0056724", broker_value: str = "BUY 1 FILLED",
                     account: str = "KR-PAPER-01", inst: str = "005930", qty: str = "1") -> str:
    """수정 전 해소가 남긴 상태: 항목 RESOLVED(EXTERNAL)·감사 RECONCILE_RESOLVE·주문 ACCEPTED 0/수량."""
    with repo.tx():
        rid = repo.insert_reconciliation(account, "PERIODIC", "MISMATCH", {}, now)
        iid = repo.insert_recon_item(rid, account, ItemKind.UNKNOWN_BROKER_ORDER, inst, no, None, broker_value,
                                     json.dumps({"side": "BUY", "quantity": qty,
                                                 "trade_date": repo.trade_date_of(now),
                                                 "detected_at": now.isoformat()}))
        key = repo.insert_broker_order(account=account, order_no=no, at=now, intent_id=None, instrument=inst,
                                       side="BUY", quantity=Decimal(qty), origin="EXTERNAL")
        repo.resolve_recon_item(iid, "EXTERNAL", "local", now)
        repo.audit(now, "RECONCILE_RESOLVE", "local", iid,
                   {"kind": "UNKNOWN_BROKER_ORDER", "method": "EXTERNAL", "note": "외부 주문으로 인정"})
    return key


def test_correction_then_reconcile_still_matches(env: Env) -> None:
    """수정 전 경로를 재현(ACCEPTED 0/2로 되돌림) → 정정 → 대조 일치·불변 조건."""
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("2"))
    env.broker.fill(no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    resolve_all(env)
    key = ext_row(env, no)["order_key"]
    with env.repo.tx():
        env.repo.update_broker_order(key, Decimal("0"), "ACCEPTED")   # 수정 전 기록과 같은 상태
    with env.repo.tx():
        note = correct_external_filled(env.repo, no, ACCOUNT, "u", env.clock.now())
    assert "FILLED" in note
    row = ext_row(env, no)
    assert (row["state"], Decimal(row["filled_quantity"])) == ("FILLED", 2)
    assert fills_of(env, key) == [] and env.repo.position(ACCOUNT, "TST-C").quantity == 2
    [a] = [e for e in env.repo.audit_events() if e["kind"] == "EXTERNAL_ORDER_CORRECTED"]
    assert a["ref"] == key
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"
    with pytest.raises(ResolveError):                                   # 두 번째는 대상 없음
        correct_external_filled(env.repo, no, ACCOUNT, "u", env.clock.now())


@pytest.fixture
def paper_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        _legacy_external(repo, datetime(2026, 10, 16, 1, 0, tzinfo=UTC))
    finally:
        repo.close()
    return root


def _paper_rows(root: Path) -> tuple[list[dict], list[dict]]:
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        return (repo.select("SELECT state, filled_quantity, origin FROM broker_order"),
                [e for e in repo.audit_events() if e["kind"] == "EXTERNAL_ORDER_CORRECTED"])
    finally:
        violations = check_invariants(repo.connection, Mode.PAPER.value)
        repo.close()
        assert violations == [], violations


ARGS = ["--mode", "PAPER", "--account", "KR-PAPER-01"]


def test_fix_command_corrects_legacy_row_and_status_shows_none(paper_root: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    status_main(["--mode", "PAPER", "--root", str(paper_root)])
    assert "미체결 주문: 1건" in capsys.readouterr().out             # 결함 재현: 유령 미체결
    assert fix_external_main([*ARGS, "--root", str(paper_root), "0056724"]) == 0
    out = capsys.readouterr().out
    assert "0056724" in out and "FILLED" in out
    rows, audits = _paper_rows(paper_root)
    assert rows == [{"state": "FILLED", "filled_quantity": "1", "origin": "EXTERNAL"}]
    assert len(audits) == 1
    status_main(["--mode", "PAPER", "--root", str(paper_root)])
    assert "미체결 주문: 0건" in capsys.readouterr().out
    assert fix_external_main([*ARGS, "--root", str(paper_root), "0056724"]) == 2   # 이미 정정됨 → 거부


@pytest.mark.parametrize("lock_name", ["run.lock", AUTOPILOT_LOCK])
def test_fix_command_refuses_while_run_or_autopilot_lock_held(paper_root: Path, lock_name: str,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    lock = acquire_instance_lock(paper_root / "var" / "paper", lock_name)
    try:
        assert fix_external_main([*ARGS, "--root", str(paper_root), "0056724"]) == 2
    finally:
        lock.release()
    assert "실행 중" in capsys.readouterr().out
    rows, audits = _paper_rows(paper_root)
    assert rows[0]["state"] == "ACCEPTED" and audits == []


def test_fix_command_refuses_unknown_order_and_non_filled_broker_value(tmp_path: Path,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "r"
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        _legacy_external(repo, datetime(2026, 10, 16, 1, 0, tzinfo=UTC), no="0000077", broker_value="BUY 1 ACCEPTED")
        repo_key = repo.insert_broker_order(account="KR-PAPER-01", order_no="0000088",
                                            at=datetime(2026, 10, 16, 1, 0, tzinfo=UTC), intent_id=None,
                                            instrument="005930", side="BUY", quantity=Decimal("1"),
                                            origin="INTERNAL_NEW")
        assert repo_key
    finally:
        repo.close()
    base = [*ARGS, "--root", str(root)]
    assert fix_external_main([*base, "0099999"]) == 2                   # 없는 주문
    assert fix_external_main([*base, "0000077"]) == 2                   # 브로커 값이 FILLED 아님
    assert fix_external_main([*base, "0000088"]) == 2                   # 내부 주문은 대상 아님
    out = capsys.readouterr().out
    assert out.count("거부") >= 3
    rows, audits = _paper_rows(root)
    assert sorted(r["state"] for r in rows) == ["ACCEPTED", "ACCEPTED"] and audits == []


def test_fix_command_refuses_live_and_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fix_external_main(["--mode", "LIVE", "--root", str(tmp_path), "0056724"]) == 2
    assert fix_external_main(["--mode", "PAPER", "--root", str(tmp_path), "0056724"]) == 2
    assert not (tmp_path / "var" / "live").exists() and not (tmp_path / "var" / "paper" / "trading.db").exists()
    assert "거부" in capsys.readouterr().out


# --- 관찰 순서 무관: 먼저 미체결로, 나중에 체결로 본 외부 주문 (독립 검토) ------------------------------

def _open_then_filled_items(env: Env, qty: str = "2") -> tuple[str, dict[str, str]]:
    """같은 외부 주문을 접수 상태로 한 번, 체결 뒤 한 번 대조 → 브로커 값이 달라 미확인 항목이 2개 열린다."""
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal(qty))
    rec(env).run(ACCOUNT, "PERIODIC")
    env.broker.fill(no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    items = {i["broker_value"].split()[-1]: i["item_id"] for i in env.repo.open_recon_items(ACCOUNT)
             if i["kind"] == ItemKind.UNKNOWN_BROKER_ORDER}
    assert sorted(items) == ["ACCEPTED", "FILLED"], items
    return no, items


@pytest.mark.parametrize("order", [("ACCEPTED", "FILLED"), ("FILLED", "ACCEPTED")])
def test_open_then_filled_items_record_filled_in_any_resolve_order(env: Env, order: tuple[str, str]) -> None:
    no, items = _open_then_filled_items(env)
    r = ReconcileResolver(env.repo, env.executor, env.clock)
    notes = [r.resolve(items[s], "EXTERNAL", "u") for s in order]
    row = ext_row(env, no)
    assert (row["state"], Decimal(row["filled_quantity"]), Decimal(row["quantity"])) == ("FILLED", 2, 2), notes
    if order[0] == "ACCEPTED":
        assert "FILLED" in notes[1] and "상향" in notes[1], notes      # 이미 기록된 행을 종결 상태로 올렸다고 알린다
    else:
        assert "원장 변경 없음" in notes[1] and "상향" not in notes[1], notes   # 낮추지 않는다
    assert fills_of(env, row["order_key"]) == []                       # 체결 행·포지션은 만들지 않음
    assert env.repo.position(ACCOUNT, "TST-C").quantity == 0
    resolve_all(env)                                                    # 남은 QTY_MISMATCH
    assert env.repo.position(ACCOUNT, "TST-C").quantity == 2
    assert rec(env).run(ACCOUNT, "MANUAL").result == "MATCH"
    assert check_invariants(env.repo.connection, Mode.BACKTEST.value) == []


def test_already_recorded_upgrade_never_touches_internal_rows(env: Env) -> None:
    """이미 기록된 행이 내부 주문이면 항목이 FILLED라고 해도 바꾸지 않는다."""
    now = env.clock.now()
    with env.repo.tx():
        key = env.repo.insert_broker_order(account=ACCOUNT, order_no="0000555", at=now, intent_id=None,
                                           instrument="TST-C", side="BUY", quantity=Decimal("2"), origin="INTERNAL_NEW")
        rid = env.repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MISMATCH", {}, now)
        iid = env.repo.insert_recon_item(rid, ACCOUNT, ItemKind.UNKNOWN_BROKER_ORDER, "TST-C", "0000555", None,
                                         "BUY 2 FILLED",
                                         json.dumps({"side": "BUY", "quantity": "2", "broker_state": "FILLED",
                                                     "filled_quantity": "2", "trade_date": env.repo.trade_date_of(now),
                                                     "detected_at": now.isoformat()}))
    note = ReconcileResolver(env.repo, env.executor, env.clock).resolve(iid, "EXTERNAL", "u")
    assert "원장 변경 없음" in note and "상향" not in note
    assert env.repo.broker_order(key).state == "ACCEPTED"


def _resolved_item(repo: Repository, now: datetime, no: str, broker_value: str, qty: str = "1",
                   trade_date: str | None = None, inst: str = "005930") -> str:
    """EXTERNAL로 해소된 미확인 주문 항목(감사 RECONCILE_RESOLVE 포함)만 추가한다 — 주문 행은 만들지 않음."""
    with repo.tx():
        rid = repo.insert_reconciliation("KR-PAPER-01", "PERIODIC", "MISMATCH", {}, now)
        iid = repo.insert_recon_item(rid, "KR-PAPER-01", ItemKind.UNKNOWN_BROKER_ORDER, inst, no, None, broker_value,
                                     json.dumps({"side": "BUY", "quantity": qty,
                                                 "trade_date": trade_date or repo.trade_date_of(now),
                                                 "detected_at": now.isoformat()}))
        repo.resolve_recon_item(iid, "EXTERNAL", "local", now)
        repo.audit(now, "RECONCILE_RESOLVE", "local", iid,
                   {"kind": "UNKNOWN_BROKER_ORDER", "method": "EXTERNAL", "note": "외부 주문으로 인정"})
    return iid


def _correct(repo: Repository, no: str) -> str:
    with repo.tx():
        return correct_external_filled(repo, no, "KR-PAPER-01", "u", datetime(2026, 10, 17, 1, 0, tzinfo=UTC))


def test_correction_accepts_filled_evidence_despite_earlier_accepted_item(tmp_path: Path) -> None:
    now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    repo = Repository(tmp_path / "trading.db", Mode.PAPER)
    try:
        key = _legacy_external(repo, now, broker_value="BUY 1 ACCEPTED")    # 먼저 접수 상태로 본 항목
        _resolved_item(repo, now, "0056724", "BUY 1 FILLED")                 # 나중에 체결로 본 항목
        assert "FILLED" in _correct(repo, "0056724")
        row = repo.broker_order(key)
        assert row is not None and (row.state, row.filled_quantity) == ("FILLED", 1)
        assert check_invariants(repo.connection, Mode.PAPER.value) == []
    finally:
        repo.close()


@pytest.mark.parametrize("contradiction", ["BUY 2 FILLED", "BUY 1 CANCELLED", "BUY 1 REJECTED"])
def test_correction_refuses_contradicting_evidence(tmp_path: Path, contradiction: str) -> None:
    now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    repo = Repository(tmp_path / "trading.db", Mode.PAPER)
    try:
        key = _legacy_external(repo, now)                                   # BUY 1 FILLED
        _resolved_item(repo, now, "0056724", contradiction, qty=contradiction.split()[1])
        with pytest.raises(ResolveError):
            _correct(repo, "0056724")
        row = repo.broker_order(key)
        assert row is not None and row.state == "ACCEPTED"
    finally:
        repo.close()


def test_correction_ignores_evidence_from_another_trade_date(tmp_path: Path) -> None:
    """주문번호는 거래일 안에서만 유일 (D-046) — 다른 거래일 같은 번호의 FILLED 항목은 근거가 아니다."""
    now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    repo = Repository(tmp_path / "trading.db", Mode.PAPER)
    try:
        with repo.tx():                                                     # 10-16 외부 주문 행 (ACCEPTED 0/1)
            key = repo.insert_broker_order(account="KR-PAPER-01", order_no="0056724", at=now, intent_id=None,
                                           instrument="005930", side="BUY", quantity=Decimal("1"), origin="EXTERNAL")
        _resolved_item(repo, now, "0056724", "BUY 1 FILLED", trade_date="2026-10-15")   # 전날 같은 번호의 근거
        with pytest.raises(ResolveError):
            _correct(repo, "0056724")
        row = repo.broker_order(key)
        assert row is not None and row.state == "ACCEPTED"
    finally:
        repo.close()
