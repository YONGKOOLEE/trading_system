"""SQLite 저장소 (03 D-039, 02-data-model).

- 모드별 DB 파일. 모든 레코드에 모드를 기록하고 저장소 모드와 다르면 거부한다 (INV-12).
- 여러 엔티티를 한 저장 단위로 기록한다: `with repo.tx(): ...` (BEGIN IMMEDIATE, 직렬화).
- 감사 이벤트는 추가만 가능하다 (트리거로 UPDATE·DELETE 금지, INV-10).
- 금액·수량은 TEXT(Decimal 문자열), 시각은 UTC ISO 8601.
T-001 범위의 스키마다. 마이그레이션 체계는 T-011.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system.domain.clock import KST, to_iso
from trading_system.domain.enums import ACTIVE_INTENT_STATES, IntentState, Mode
from trading_system.domain.models import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counter (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS account (
  alias TEXT PRIMARY KEY, mode TEXT NOT NULL, broker_env TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_decision (
  decision_id TEXT PRIMARY KEY, mode TEXT NOT NULL, created_at TEXT NOT NULL, seq INTEGER NOT NULL,
  source TEXT NOT NULL, request_type TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
  side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, idem_key TEXT NOT NULL,
  signal_id TEXT, owner TEXT, target_order_key TEXT,
  result TEXT NOT NULL, failed_check TEXT, reason TEXT, checks TEXT NOT NULL,
  symbol_after TEXT, symbol_limit TEXT, total_after TEXT, total_limit TEXT,
  settings_hash TEXT, limit_snapshot TEXT);
CREATE TABLE IF NOT EXISTS intent (
  intent_id TEXT PRIMARY KEY, mode TEXT NOT NULL, decision_id TEXT NOT NULL REFERENCES gate_decision(decision_id),
  idem_key TEXT NOT NULL UNIQUE, signal_id TEXT, source TEXT NOT NULL, request_type TEXT NOT NULL,
  account TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
  order_type TEXT NOT NULL, price TEXT NOT NULL, owner TEXT, target_order_key TEXT,
  state TEXT NOT NULL, filled_quantity TEXT NOT NULL, broker_order_key TEXT,
  created_at TEXT NOT NULL, created_seq INTEGER NOT NULL, closed_reason TEXT);
CREATE TABLE IF NOT EXISTS attempt (
  attempt_id TEXT PRIMARY KEY, mode TEXT NOT NULL, intent_id TEXT NOT NULL UNIQUE REFERENCES intent(intent_id),
  state TEXT NOT NULL, outcome TEXT, reason TEXT,
  created_at TEXT NOT NULL, created_seq INTEGER NOT NULL, result_at TEXT);
CREATE TABLE IF NOT EXISTS broker_order (
  order_key TEXT PRIMARY KEY, mode TEXT NOT NULL, account TEXT NOT NULL, trade_date TEXT NOT NULL,
  order_no TEXT NOT NULL, intent_id TEXT, instrument TEXT NOT NULL, side TEXT NOT NULL,
  quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL, state TEXT NOT NULL, origin TEXT NOT NULL,
  target_order_key TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fill (
  fill_key TEXT PRIMARY KEY, mode TEXT NOT NULL, order_key TEXT NOT NULL REFERENCES broker_order(order_key),
  fill_id TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS position (
  account TEXT NOT NULL, instrument TEXT NOT NULL, mode TEXT NOT NULL,
  quantity TEXT NOT NULL, avg_price TEXT NOT NULL, PRIMARY KEY (account, instrument));
CREATE TABLE IF NOT EXISTS ownership (
  ownership_id TEXT PRIMARY KEY, mode TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
  owner TEXT NOT NULL, state TEXT NOT NULL, intent_id TEXT, created_at TEXT NOT NULL, released_reason TEXT);
CREATE TABLE IF NOT EXISTS reservation (
  reservation_id TEXT PRIMARY KEY, mode TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES intent(intent_id),
  account TEXT NOT NULL, instrument TEXT NOT NULL, amount TEXT NOT NULL, consumed TEXT NOT NULL,
  cash_amount TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS halt (
  halt_id TEXT PRIMARY KEY, mode TEXT NOT NULL, halt_type TEXT NOT NULL, account TEXT,
  state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings_version (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, version_hash TEXT NOT NULL, kind TEXT NOT NULL,
  target TEXT NOT NULL, values_json TEXT NOT NULL, previous_hash TEXT, direction TEXT NOT NULL,
  submitted_at TEXT NOT NULL, effective_at TEXT NOT NULL, deferred INTEGER NOT NULL, ref_time_used TEXT,
  source TEXT NOT NULL, result TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS audit_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
  actor TEXT NOT NULL, ref TEXT, summary TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_event
  BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_event
  BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END;
"""


class StorageFailure(RuntimeError):
    """저장 실패. 이 예외가 나면 브로커로 전송하지 않는다 (R-25, G15)."""


class ModeMismatch(RuntimeError):
    """저장소 모드와 다른 모드의 접근 (R-01, INV-12)."""


@dataclass(frozen=True)
class IntentRow:
    intent_id: str
    decision_id: str
    state: IntentState
    source: str
    request_type: str
    account: str
    instrument: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal
    price: Decimal
    order_type: str
    owner: str | None
    target_order_key: str | None
    broker_order_key: str | None


@dataclass(frozen=True)
class OwnershipRow:
    ownership_id: str
    owner: str
    state: str
    intent_id: str | None


@dataclass(frozen=True)
class ReservationRow:
    reservation_id: str
    intent_id: str
    instrument: str
    amount: Decimal       # 예약 노출 금액 (× |레버리지 배수|, 02 ENT-21)
    consumed: Decimal     # 체결로 포지션에 옮겨 간 노출 금액
    cash_amount: Decimal  # 예약 현금 (주문 금액 + 추정 비용, 03 §6.2)

    @property
    def fraction_left(self) -> Decimal:
        return Decimal("0") if self.amount == 0 else (self.amount - self.consumed) / self.amount

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.consumed

    @property
    def cash_remaining(self) -> Decimal:
        return self.cash_amount * self.fraction_left


@dataclass(frozen=True)
class PositionRow:
    quantity: Decimal
    avg_price: Decimal


@dataclass(frozen=True)
class BrokerOrderRow:
    order_key: str
    order_no: str
    intent_id: str | None
    account: str
    instrument: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal
    state: str


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class _Result:
    def __init__(self, rows: list[sqlite3.Row], rowcount: int, lastrowid: int | None) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[sqlite3.Row]:
        return iter(self._rows)


class Repository:
    def __init__(self, path: Path, mode: Mode) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._lock = threading.RLock()
        self._depth = 0
        self.fail_next_transaction = False  # 테스트용 저장 실패 주입 (V-26)
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self._bind_mode()

    def _bind_mode(self) -> None:
        """DB 파일을 처음 만든 모드에 묶는다. 다른 모드로 다시 열면 거부한다 (R-01, INV-12)."""
        row = self.connection.execute("SELECT value FROM meta WHERE key = 'mode'").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO meta(key, value) VALUES('mode', ?)", (self.mode.value,))
        elif row[0] != self.mode.value:
            self.connection.close()
            raise ModeMismatch(f"이 저장소는 {row[0]} 모드로 만들어졌다 (요청 모드 {self.mode.value})")

    def close(self) -> None:
        self.connection.close()

    # --- 트랜잭션 ---------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[None]:
        """하나의 저장 단위. 중첩 호출은 바깥 트랜잭션에 합쳐진다."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            if self.fail_next_transaction:
                self.fail_next_transaction = False
                raise StorageFailure("주입된 저장 실패")
            try:
                self.connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageFailure(str(exc)) from exc
            self._depth = 1
            try:
                yield
                self.connection.execute("COMMIT")
            except sqlite3.Error as exc:
                self.connection.execute("ROLLBACK")
                raise StorageFailure(str(exc)) from exc
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def _x(self, sql: str, params: tuple = ()) -> _Result:
        """모든 읽기·쓰기를 같은 잠금 아래에서 끝낸다.

        연결 하나를 여러 스레드가 공유하므로, 잠금 없이 읽으면 다른 스레드의 커밋 전(롤백될 수 있는) 행을 본다.
        결과는 잠금 안에서 모두 가져온다.
        """
        with self._lock:
            cur = self.connection.execute(sql, params)
            return _Result(cur.fetchall(), cur.rowcount, cur.lastrowid)

    def _check_mode(self, mode: Mode) -> None:
        if mode is not self.mode:
            raise ModeMismatch(f"저장소 모드 {self.mode} ≠ {mode}")

    def next_seq(self, name: str = "global") -> int:
        self._x("INSERT INTO counter(name, value) VALUES(?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
        return int(self._x("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()[0])

    # --- 계좌 ---------------------------------------------------------------
    def register_account(self, account: Account) -> None:
        self._check_mode(account.mode)
        with self.tx():
            self._x("INSERT INTO account(alias, mode, broker_env) VALUES(?,?,?)",
                    (account.alias, account.mode.value, account.broker_env))

    def accounts(self) -> list[str]:
        return [r["alias"] for r in self._x("SELECT alias FROM account ORDER BY alias")]

    def account(self, alias: str) -> Account | None:
        r = self._x("SELECT * FROM account WHERE alias = ?", (alias,)).fetchone()
        return Account(r["alias"], Mode(r["mode"]), r["broker_env"]) if r else None

    # --- 감사 ---------------------------------------------------------------
    def audit(self, at: datetime, kind: str, actor: str, ref: str | None, summary: dict) -> None:
        self._x("INSERT INTO audit_event(mode, at, kind, actor, ref, summary) VALUES(?,?,?,?,?,?)",
                (self.mode.value, to_iso(at), kind, actor, ref, json.dumps(summary, ensure_ascii=False, default=str)))

    def audit_events(self) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM audit_event ORDER BY seq")]

    # --- 게이트 판정 --------------------------------------------------------
    def insert_decision(self, row: dict) -> None:
        row = {**row, "mode": self.mode.value, "seq": self.next_seq("decision")}
        cols = ",".join(row)
        self._x(f"INSERT INTO gate_decision({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    def decision(self, decision_id: str) -> dict | None:
        r = self._x("SELECT * FROM gate_decision WHERE decision_id = ?", (decision_id,)).fetchone()
        return dict(r) if r else None

    # --- 의도 ---------------------------------------------------------------
    def insert_intent(self, row: dict) -> None:
        row = {**row, "mode": self.mode.value, "created_seq": self.next_seq()}
        cols = ",".join(row)
        self._x(f"INSERT INTO intent({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    @staticmethod
    def _intent(r: sqlite3.Row) -> IntentRow:
        return IntentRow(
            r["intent_id"], r["decision_id"], IntentState(r["state"]), r["source"], r["request_type"],
            r["account"], r["instrument"], r["side"], Decimal(r["quantity"]), Decimal(r["filled_quantity"]),
            Decimal(r["price"]), r["order_type"], r["owner"], r["target_order_key"], r["broker_order_key"],
        )

    def intent(self, intent_id: str) -> IntentRow:
        return self._intent(self._x("SELECT * FROM intent WHERE intent_id = ?", (intent_id,)).fetchone())

    def intents(self) -> list[IntentRow]:
        return [self._intent(r) for r in self._x("SELECT * FROM intent ORDER BY created_seq")]

    def intent_by_idem(self, idem_key: str) -> IntentRow | None:
        r = self._x("SELECT * FROM intent WHERE idem_key = ?", (idem_key,)).fetchone()
        return self._intent(r) if r else None

    def decision_exists(self, idem_key: str) -> bool:
        """같은 멱등키의 판정(승인·거부 무관)이 이미 있는지 (01 D-016, R-05)."""
        return self._x("SELECT 1 FROM gate_decision WHERE idem_key = ? LIMIT 1", (idem_key,)).fetchone() is not None

    def _active_states(self) -> str:
        return ",".join(f"'{s.value}'" for s in ACTIVE_INTENT_STATES)

    def active_new_intents(self, account: str, instrument: str, side: str) -> list[IntentRow]:
        sql = (f"SELECT * FROM intent WHERE account=? AND instrument=? AND side=? AND state IN ({self._active_states()}) "
               "AND request_type IN ('ENTRY','INCREASE','REDUCE','EXIT')")
        return [self._intent(r) for r in self._x(sql, (account, instrument, side))]

    def active_cancel_for(self, target_order_key: str) -> list[IntentRow]:
        sql = (f"SELECT * FROM intent WHERE target_order_key=? AND state IN ({self._active_states()}) "
               "AND request_type IN ('CANCEL','MODIFY')")
        return [self._intent(r) for r in self._x(sql, (target_order_key,))]

    def unknown_intents(self, account: str, instrument: str) -> list[IntentRow]:
        """결과 불명 의도 (01 D-018, INV-02)."""
        return [self._intent(r) for r in self._x(
            "SELECT * FROM intent WHERE account=? AND instrument=? AND state='UNKNOWN'", (account, instrument))]

    def risk_increasing_order_count(self, account: str, since: datetime) -> int:
        """`since` 이후 만든 위험 확대 의도 중 브로커 주문이 되었거나 될 수 있는 것의 수 (01 L5).

        브로커가 받지 않은 것(전송 전 실패·미전송·명시 거부·미접수 확정)은 세지 않는다. 결과 불명은 센다.
        """
        r = self._x("SELECT COUNT(*) FROM intent WHERE account=? AND request_type IN ('ENTRY','INCREASE') "
                    "AND created_at >= ? AND state NOT IN ('NOT_SENT','REJECTED','NOT_ACCEPTED')",
                    (account, to_iso(since))).fetchone()
        return int(r[0])

    def set_intent_state(self, intent_id: str, state: IntentState, reason: str | None = None) -> None:
        current = self.intent(intent_id).state
        if current.is_terminal:
            raise ValueError(f"종결된 의도는 다시 열 수 없다 (INV-11): {intent_id} {current}")
        self._x("UPDATE intent SET state = ?, closed_reason = COALESCE(?, closed_reason) WHERE intent_id = ?",
                (state.value, reason, intent_id))

    def set_intent_filled(self, intent_id: str, filled: Decimal) -> None:
        self._x("UPDATE intent SET filled_quantity = ? WHERE intent_id = ?", (str(filled), intent_id))

    def link_intent_order(self, intent_id: str, order_key: str) -> None:
        self._x("UPDATE intent SET broker_order_key = ? WHERE intent_id = ?", (order_key, intent_id))

    # --- 전송 시도 ----------------------------------------------------------
    def insert_attempt(self, intent_id: str, at: datetime) -> str:
        attempt_id = new_id("att")
        self._x("INSERT INTO attempt(attempt_id, mode, intent_id, state, created_at, created_seq) VALUES(?,?,?,?,?,?)",
                (attempt_id, self.mode.value, intent_id, "AWAITING_RESPONSE", to_iso(at), self.next_seq()))
        return attempt_id

    def finish_attempt(self, attempt_id: str, state: str, outcome: str, reason: str | None, at: datetime) -> None:
        self._x("UPDATE attempt SET state=?, outcome=?, reason=?, result_at=? WHERE attempt_id=?",
                (state, outcome, reason, to_iso(at), attempt_id))

    # --- 브로커 주문·체결 --------------------------------------------------
    @staticmethod
    def order_key(account: str, at: datetime, order_no: str) -> str:
        """(계좌, 거래일, 주문번호) — 03 D-046."""
        return f"{account}|{at.astimezone(KST).date().isoformat()}|{order_no}"

    def insert_broker_order(self, *, account: str, order_no: str, at: datetime, intent_id: str | None,
                            instrument: str, side: str, quantity: Decimal, origin: str,
                            target_order_key: str | None = None) -> str:
        key = self.order_key(account, at, order_no)
        self._x("""INSERT INTO broker_order(order_key, mode, account, trade_date, order_no, intent_id, instrument,
                   side, quantity, filled_quantity, state, origin, target_order_key, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, self.mode.value, account, at.astimezone(KST).date().isoformat(), order_no, intent_id,
                 instrument, side, str(quantity), "0", "ACCEPTED", origin, target_order_key, to_iso(at)))
        return key

    @staticmethod
    def _order(r: sqlite3.Row) -> BrokerOrderRow:
        return BrokerOrderRow(r["order_key"], r["order_no"], r["intent_id"], r["account"], r["instrument"],
                              r["side"], Decimal(r["quantity"]), Decimal(r["filled_quantity"]), r["state"])

    def broker_order(self, order_key: str) -> BrokerOrderRow | None:
        r = self._x("SELECT * FROM broker_order WHERE order_key = ?", (order_key,)).fetchone()
        return self._order(r) if r else None

    def broker_order_by_no(self, order_no: str) -> BrokerOrderRow | None:
        r = self._x("SELECT * FROM broker_order WHERE order_no = ? ORDER BY trade_date DESC LIMIT 1",
                    (order_no,)).fetchone()
        return self._order(r) if r else None

    def open_orders(self, account: str, side: str | None = None) -> list[BrokerOrderRow]:
        sql = "SELECT * FROM broker_order WHERE account=? AND state IN ('ACCEPTED','PARTIAL') AND origin='INTERNAL_NEW'"
        params: tuple = (account,)
        if side:
            sql += " AND side=?"
            params += (side,)
        return [self._order(r) for r in self._x(sql, params)]

    def update_broker_order(self, order_key: str, filled: Decimal, state: str) -> None:
        self._x("UPDATE broker_order SET filled_quantity=?, state=? WHERE order_key=?", (str(filled), state, order_key))

    def insert_fill(self, order_key: str, fill_id: str, quantity: Decimal, price: Decimal, at: datetime) -> bool:
        """체결 기록. 이미 있는 체결이면 False (중복 통보 무시)."""
        cur = self._x("INSERT OR IGNORE INTO fill(fill_key, mode, order_key, fill_id, quantity, price, at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (f"{order_key}|{fill_id}", self.mode.value, order_key, fill_id, str(quantity), str(price), to_iso(at)))
        return cur.rowcount == 1

    # --- 포지션 -------------------------------------------------------------
    def position(self, account: str, instrument: str) -> PositionRow:
        r = self._x("SELECT * FROM position WHERE account=? AND instrument=?", (account, instrument)).fetchone()
        return PositionRow(Decimal(r["quantity"]), Decimal(r["avg_price"])) if r else PositionRow(Decimal("0"), Decimal("0"))

    def positions(self, account: str) -> dict[str, PositionRow]:
        return {r["instrument"]: PositionRow(Decimal(r["quantity"]), Decimal(r["avg_price"]))
                for r in self._x("SELECT * FROM position WHERE account=?", (account,))}

    def set_position(self, account: str, instrument: str, quantity: Decimal, avg_price: Decimal) -> None:
        if quantity < 0:
            raise ValueError("포지션 수량은 음수가 될 수 없다 (INV-04)")
        self._x("""INSERT INTO position(account, instrument, mode, quantity, avg_price) VALUES(?,?,?,?,?)
                   ON CONFLICT(account, instrument) DO UPDATE SET quantity=excluded.quantity, avg_price=excluded.avg_price""",
                (account, instrument, self.mode.value, str(quantity), str(avg_price)))

    # --- 소유권 -------------------------------------------------------------
    def active_ownership(self, account: str, instrument: str) -> OwnershipRow | None:
        r = self._x("SELECT * FROM ownership WHERE account=? AND instrument=? AND state IN ('RESERVED','CONFIRMED')",
                    (account, instrument)).fetchone()
        return OwnershipRow(r["ownership_id"], r["owner"], r["state"], r["intent_id"]) if r else None

    def insert_ownership(self, account: str, instrument: str, owner: str, intent_id: str, at: datetime) -> None:
        self._x("INSERT INTO ownership(ownership_id, mode, account, instrument, owner, state, intent_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (new_id("own"), self.mode.value, account, instrument, owner, "RESERVED", intent_id, to_iso(at)))

    def set_ownership_state(self, ownership_id: str, state: str, reason: str | None = None) -> None:
        self._x("UPDATE ownership SET state=?, released_reason=COALESCE(?, released_reason) WHERE ownership_id=?",
                (state, reason, ownership_id))

    # --- 한도 예약 ----------------------------------------------------------
    def insert_reservation(self, intent_id: str, account: str, instrument: str, amount: Decimal,
                           cash_amount: Decimal) -> None:
        self._x("INSERT INTO reservation(reservation_id, mode, intent_id, account, instrument, amount, consumed, "
                "cash_amount, state) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("rsv"), self.mode.value, intent_id, account, instrument, str(amount), "0", str(cash_amount),
                 "ACTIVE"))

    @staticmethod
    def _reservation(r: sqlite3.Row) -> ReservationRow:
        return ReservationRow(r["reservation_id"], r["intent_id"], r["instrument"], Decimal(r["amount"]),
                              Decimal(r["consumed"]), Decimal(r["cash_amount"]))

    def active_reservations(self, account: str) -> list[ReservationRow]:
        return [self._reservation(r)
                for r in self._x("SELECT * FROM reservation WHERE account=? AND state='ACTIVE'", (account,))]

    def reservation_for(self, intent_id: str) -> ReservationRow | None:
        r = self._x("SELECT * FROM reservation WHERE intent_id=? AND state='ACTIVE'", (intent_id,)).fetchone()
        return self._reservation(r) if r else None

    def consume_reservation(self, reservation_id: str, consumed: Decimal) -> None:
        self._x("UPDATE reservation SET consumed=? WHERE reservation_id=?", (str(consumed), reservation_id))

    def release_reservation(self, reservation_id: str) -> None:
        self._x("UPDATE reservation SET state='RELEASED' WHERE reservation_id=?", (reservation_id,))

    # --- 설정 버전 (02 ENT-08) ---------------------------------------------------
    def insert_settings_version(self, row: dict) -> int:
        row = {**row, "mode": self.mode.value}
        cols = ",".join(row)
        cur = self._x(f"INSERT INTO settings_version({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return int(cur.lastrowid)

    def settings_rows(self, kind: str, target: str) -> list[dict]:
        return [dict(r) for r in self._x(
            "SELECT * FROM settings_version WHERE kind=? AND target=? ORDER BY seq", (kind, target))]

    def effective_settings_row(self, kind: str, target: str, at: datetime) -> dict | None:
        """적용 시각이 지난 통과 버전 중 가장 나중에 제출된 것. 나중 제출이 대기 중인 이전 완화를 덮는다."""
        r = self._x(
            "SELECT * FROM settings_version WHERE kind=? AND target=? AND result='PASSED' AND effective_at <= ? "
            "ORDER BY seq DESC LIMIT 1", (kind, target, to_iso(at))).fetchone()
        return dict(r) if r else None

    def latest_passed_settings_row(self, kind: str, target: str) -> dict | None:
        r = self._x("SELECT * FROM settings_version WHERE kind=? AND target=? AND result='PASSED' "
                    "ORDER BY seq DESC LIMIT 1", (kind, target)).fetchone()
        return dict(r) if r else None

    # --- 정지 ---------------------------------------------------------------
    def insert_halt(self, halt_type: str, account: str | None, actor: str, reason: str | None, at: datetime) -> str:
        halt_id = new_id("halt")
        self._x("INSERT INTO halt(halt_id, mode, halt_type, account, state, actor, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (halt_id, self.mode.value, halt_type, account, "ACTIVE", actor, reason, to_iso(at)))
        return halt_id

    def active_halts(self, account: str) -> list[str]:
        return [r["halt_type"] for r in self._x(
            "SELECT halt_type FROM halt WHERE state='ACTIVE' AND (account IS NULL OR account=?)", (account,))]
