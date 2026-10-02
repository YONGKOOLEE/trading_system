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
  order_type TEXT NOT NULL, price TEXT NOT NULL, owner TEXT, target_order_key TEXT, stop_price TEXT,
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
  state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  scope_instrument TEXT, scope_owner TEXT, variant TEXT, conditions TEXT NOT NULL DEFAULT '{}',
  released_at TEXT, released_by TEXT);
CREATE TABLE IF NOT EXISTS ops_command (
  command_id TEXT PRIMARY KEY, mode TEXT NOT NULL, channel TEXT NOT NULL, actor TEXT NOT NULL,
  command TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
  created_at TEXT NOT NULL, created_seq INTEGER NOT NULL, done_at TEXT);
CREATE TABLE IF NOT EXISTS settings_version (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, version_hash TEXT NOT NULL, kind TEXT NOT NULL,
  target TEXT NOT NULL, values_json TEXT NOT NULL, previous_hash TEXT, direction TEXT NOT NULL,
  submitted_at TEXT NOT NULL, effective_at TEXT NOT NULL, deferred INTEGER NOT NULL, ref_time_used TEXT,
  source TEXT NOT NULL, result TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS strategy_registration (
  reg_id TEXT PRIMARY KEY, mode TEXT NOT NULL, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
  spec_hash TEXT NOT NULL, spec_json TEXT NOT NULL, params_hash TEXT, params_json TEXT,
  target_mode TEXT NOT NULL, result TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_state (
  strategy_id TEXT NOT NULL, version INTEGER NOT NULL, market TEXT NOT NULL, mode TEXT NOT NULL,
  state TEXT NOT NULL, stop_reason TEXT, prev_state TEXT, spec_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (strategy_id, version, market));
CREATE TABLE IF NOT EXISTS strategy_transition (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
  market TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, approval_id TEXT NOT NULL, evidence TEXT,
  stop_reason TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconciliation (
  recon_id TEXT PRIMARY KEY, mode TEXT NOT NULL, account TEXT NOT NULL, trigger TEXT NOT NULL,
  result TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL, created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS recon_item (
  item_id TEXT PRIMARY KEY, mode TEXT NOT NULL, recon_id TEXT NOT NULL REFERENCES reconciliation(recon_id),
  account TEXT NOT NULL, kind TEXT NOT NULL, instrument TEXT, ref TEXT, internal_value TEXT, broker_value TEXT,
  detail TEXT, state TEXT NOT NULL, method TEXT, resolver TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS clock_check (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL,
  offset_seconds TEXT, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_report (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, account TEXT NOT NULL, trade_day TEXT NOT NULL,
  version INTEGER NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL, settings_hash TEXT, created_at TEXT NOT NULL);
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


class SchemaMismatch(RuntimeError):
    """저장소 스키마 버전이 코드와 다름. 마이그레이션(T-011) 없이 열지 않는다."""


SCHEMA_VERSION = "1"  # 기준선 (CP-018). 바꾸면 T-011 마이그레이션 필요


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
    def __init__(self, path: Path, mode: Mode, busy_timeout: float = 5.0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._lock = threading.RLock()
        self._depth = 0
        self._tx_thread: int | None = None
        self.fail_next_transaction = False  # 테스트용 저장 실패 주입 (V-26)
        # busy_timeout: 다른 프로세스(scripts)가 쓰기 잠금을 쥐고 있을 때 기다리는 최대 초 (CP-018 L-3)
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False,
                                          timeout=busy_timeout)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self._bind_mode()

    def _bind_mode(self) -> None:
        """DB 파일을 처음 만든 모드·스키마 버전에 묶는다. 다르면 거부한다 (R-01, INV-12, CP-018)."""
        row = self.connection.execute("SELECT value FROM meta WHERE key = 'mode'").fetchone()
        ver = self.connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO meta(key, value) VALUES('mode', ?)", (self.mode.value,))
            self.connection.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
            return
        if row[0] != self.mode.value:
            self.connection.close()
            raise ModeMismatch(f"이 저장소는 {row[0]} 모드로 만들어졌다 (요청 모드 {self.mode.value})")
        if ver is None or ver[0] != SCHEMA_VERSION:
            self.connection.close()
            raise SchemaMismatch(f"저장소 스키마 버전 {ver[0] if ver else '없음'} ≠ 코드 {SCHEMA_VERSION} "
                                 f"(scripts/migrate.py로 백업 후 마이그레이션)")

    @property
    def in_transaction(self) -> bool:
        """현재 스레드가 저장 단위(tx) 안에 있는지. 브로커 전송은 트랜잭션 밖에서만 한다 (INV-03)."""
        return bool(self._depth) and self._tx_thread == threading.get_ident()

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
            self._tx_thread = threading.get_ident()
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
                self._tx_thread = None

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

    def intents_in_state(self, state: IntentState) -> list[IntentRow]:
        return [self._intent(r) for r in self._x("SELECT * FROM intent WHERE state=? ORDER BY created_seq",
                                                 (state.value,))]

    def set_intent_terms(self, intent_id: str, quantity: Decimal, price: Decimal) -> None:
        """정정 확인 시 의도의 수량·가격을 정정 후 조건으로 바꾼다 (체결 누계는 유지)."""
        self._x("UPDATE intent SET quantity=?, price=? WHERE intent_id=?", (str(quantity), str(price), intent_id))

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

    def attempts_awaiting_response(self) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM attempt WHERE state='AWAITING_RESPONSE' ORDER BY created_seq")]

    def attempt_for(self, intent_id: str) -> dict | None:
        r = self._x("SELECT * FROM attempt WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(r) if r else None

    def recent_attempt_results(self, n: int) -> list[tuple[str, str | None]]:
        """최근 응답 확정된 전송 시도 n개의 (결과, 사유), 최신 순."""
        return [(r["outcome"], r["reason"]) for r in self._x(
            "SELECT outcome, reason FROM attempt WHERE outcome IS NOT NULL ORDER BY created_seq DESC LIMIT ?", (n,))]

    def finish_attempt(self, attempt_id: str, state: str, outcome: str, reason: str | None, at: datetime) -> None:
        self._x("UPDATE attempt SET state=?, outcome=?, reason=?, result_at=? WHERE attempt_id=?",
                (state, outcome, reason, to_iso(at), attempt_id))

    # --- 브로커 주문·체결 --------------------------------------------------
    @staticmethod
    def trade_date_of(at: datetime) -> str:
        return at.astimezone(KST).date().isoformat()

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

    def broker_order_by_no(self, order_no: str, trade_date: str, account: str | None = None) -> BrokerOrderRow | None:
        """주문번호는 거래일·계좌 안에서만 유일하다고 본다 (03 D-046). 계좌를 모르는데 여러 계좌에 있으면 None."""
        sql, params = "SELECT * FROM broker_order WHERE order_no=? AND trade_date=?", (order_no, trade_date)
        if account is not None:
            sql, params = sql + " AND account=?", params + (account,)
        rows = list(self._x(sql, params))
        return self._order(rows[0]) if len(rows) == 1 else None

    def open_orders(self, account: str, side: str | None = None) -> list[BrokerOrderRow]:
        sql = ("SELECT * FROM broker_order WHERE account=? AND state IN ('ACCEPTED','PARTIAL') "
               "AND origin IN ('INTERNAL_NEW','INTERNAL_MODIFY')")
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

    def insert_ownership(self, account: str, instrument: str, owner: str, intent_id: str | None,
                         at: datetime) -> None:
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

    def add_to_reservation(self, reservation_id: str, amount: Decimal, cash_amount: Decimal) -> None:
        r = self._x("SELECT amount, cash_amount FROM reservation WHERE reservation_id=?", (reservation_id,)).fetchone()
        self._x("UPDATE reservation SET amount=?, cash_amount=? WHERE reservation_id=?",
                (str(Decimal(r["amount"]) + amount), str(Decimal(r["cash_amount"]) + cash_amount), reservation_id))

    def known_order_nos(self, account: str, trade_date: str) -> set[str]:
        return {r["order_no"] for r in self._x("SELECT order_no FROM broker_order WHERE account=? AND trade_date=?",
                                               (account, trade_date))}

    def latest_halt(self, halt_type: str) -> dict | None:
        r = self._x("SELECT * FROM halt WHERE halt_type=? ORDER BY created_at DESC LIMIT 1", (halt_type,)).fetchone()
        return dict(r) if r else None

    def audit_count_since(self, kind: str, since: str) -> int:
        return int(self._x("SELECT COUNT(*) FROM audit_event WHERE kind=? AND at>=?", (kind, since)).fetchone()[0])

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
    def insert_halt(self, halt_type: str, account: str | None, actor: str, reason: str | None, at: datetime,
                    *, instrument: str | None = None, owner: str | None = None, variant: str | None = None) -> str:
        halt_id = new_id("halt")
        self._x("INSERT INTO halt(halt_id, mode, halt_type, account, state, actor, reason, created_at, "
                "scope_instrument, scope_owner, variant) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (halt_id, self.mode.value, halt_type, account, "ACTIVE", actor, reason, to_iso(at),
                 instrument, owner, variant))
        return halt_id

    def active_halts(self, account: str) -> list[str]:
        """해제되지 않은(활성·재개 대기) 정지 유형 목록."""
        return [r["halt_type"] for r in self.halt_rows(account)]

    def halt_rows(self, account: str | None = None) -> list[dict]:
        """해제되지 않은 정지. account=None이면 전체."""
        if account is None:
            sql, params = "SELECT * FROM halt WHERE state<>'RELEASED' ORDER BY created_at", ()
        else:
            sql = "SELECT * FROM halt WHERE state<>'RELEASED' AND (account IS NULL OR account=?) ORDER BY created_at"
            params = (account,)
        return [dict(r) for r in self._x(sql, params)]

    def halt(self, halt_id: str) -> dict | None:
        r = self._x("SELECT * FROM halt WHERE halt_id=?", (halt_id,)).fetchone()
        return dict(r) if r else None

    def update_halt(self, halt_id: str, *, state: str | None = None, conditions: dict | None = None,
                    released_at: datetime | None = None, released_by: str | None = None) -> None:
        if state is not None:
            self._x("UPDATE halt SET state=? WHERE halt_id=?", (state, halt_id))
        if conditions is not None:
            self._x("UPDATE halt SET conditions=? WHERE halt_id=?",
                    (json.dumps(conditions, ensure_ascii=False, sort_keys=True), halt_id))
        if released_at is not None:
            self._x("UPDATE halt SET released_at=?, released_by=? WHERE halt_id=?",
                    (to_iso(released_at), released_by, halt_id))

    # --- 전략 등록·상태 (02 ENT-05·06) ------------------------------------------------
    def insert_strategy_registration(self, row: dict) -> str:
        reg_id = new_id("reg")
        row = {**row, "reg_id": reg_id, "mode": self.mode.value}
        cols = ",".join(row)
        self._x(f"INSERT INTO strategy_registration({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        return reg_id

    def latest_registration(self, strategy_id: str, version: int) -> dict | None:
        r = self._x("SELECT * FROM strategy_registration WHERE strategy_id=? AND version=? AND result='PASSED' "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", (strategy_id, version)).fetchone()
        return dict(r) if r else None

    def strategy_state(self, strategy_id: str, version: int, market: str) -> dict | None:
        r = self._x("SELECT * FROM strategy_state WHERE strategy_id=? AND version=? AND market=?",
                    (strategy_id, version, market)).fetchone()
        return dict(r) if r else None

    def upsert_strategy_state(self, strategy_id: str, version: int, market: str, state: str, stop_reason: str | None,
                              prev_state: str | None, spec_hash: str, at: datetime) -> None:
        self._x("""INSERT INTO strategy_state(strategy_id, version, market, mode, state, stop_reason, prev_state,
                   spec_hash, updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(strategy_id, version, market) DO UPDATE SET state=excluded.state,
                   stop_reason=excluded.stop_reason, prev_state=excluded.prev_state, updated_at=excluded.updated_at""",
                (strategy_id, version, market, self.mode.value, state, stop_reason, prev_state, spec_hash, to_iso(at)))

    def insert_strategy_transition(self, strategy_id: str, version: int, market: str, from_state: str | None,
                                   to_state: str, approval_id: str, evidence: str | None, stop_reason: str | None,
                                   at: datetime) -> None:
        self._x("INSERT INTO strategy_transition(mode, strategy_id, version, market, from_state, to_state, approval_id, "
                "evidence, stop_reason, at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.mode.value, strategy_id, version, market, from_state, to_state, approval_id, evidence,
                 stop_reason, to_iso(at)))

    def strategy_transitions(self, strategy_id: str, version: int) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM strategy_transition WHERE strategy_id=? AND version=? "
                                         "ORDER BY seq", (strategy_id, version))]

    # --- 대조 (02 ENT-24·24a) --------------------------------------------------------
    def insert_reconciliation(self, account: str, trigger: str, result: str, summary: dict, at: datetime) -> str:
        recon_id = new_id("rec")
        self._x("INSERT INTO reconciliation(recon_id, mode, account, trigger, result, summary, created_at, created_seq) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (recon_id, self.mode.value, account, trigger, result, json.dumps(summary, ensure_ascii=False),
                 to_iso(at), self.next_seq("recon")))
        return recon_id

    def insert_recon_item(self, recon_id: str, account: str, kind: str, instrument: str | None, ref: str | None,
                          internal_value: str | None, broker_value: str | None, detail: str) -> str:
        item_id = new_id("rci")
        self._x("INSERT INTO recon_item(item_id, mode, recon_id, account, kind, instrument, ref, internal_value, "
                "broker_value, detail, state) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, self.mode.value, recon_id, account, kind, instrument, ref, internal_value, broker_value,
                 detail, "OPEN"))
        return item_id

    def open_recon_items(self, account: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM recon_item WHERE state='OPEN'", ()
        if account is not None:
            sql, params = sql + " AND account=?", (account,)
        return [dict(r) for r in self._x(sql + " ORDER BY rowid", params)]

    def recon_item(self, item_id: str) -> dict | None:
        r = self._x("SELECT * FROM recon_item WHERE item_id=?", (item_id,)).fetchone()
        return dict(r) if r else None

    def resolve_recon_item(self, item_id: str, method: str, resolver: str, at: datetime) -> None:
        self._x("UPDATE recon_item SET state='RESOLVED', method=?, resolver=?, resolved_at=? WHERE item_id=?",
                (method, resolver, to_iso(at), item_id))

    def latest_reconciliation(self, account: str) -> dict | None:
        r = self._x("SELECT * FROM reconciliation WHERE account=? ORDER BY created_seq DESC LIMIT 1",
                    (account,)).fetchone()
        return dict(r) if r else None

    def fill_exists(self, order_key: str, fill_id: str) -> bool:
        return self._x("SELECT 1 FROM fill WHERE fill_key=?", (f"{order_key}|{fill_id}",)).fetchone() is not None

    def integrity_ok(self) -> bool:
        with self._lock:
            r = self.connection.execute("PRAGMA quick_check").fetchone()
        return r is not None and r[0] == "ok"

    # --- 감시·보고 (02 ENT-27·28) -------------------------------------------------------
    def insert_clock_check(self, at: datetime, source: str, offset: Decimal | None, result: str) -> None:
        self._x("INSERT INTO clock_check(mode, at, source, offset_seconds, result) VALUES(?,?,?,?,?)",
                (self.mode.value, to_iso(at), source, None if offset is None else str(offset), result))

    def insert_daily_report(self, account: str, trade_day: object, status: str, content: dict, at: datetime,
                            settings_hash: str | None) -> int:
        day = str(trade_day)
        r = self._x("SELECT COALESCE(MAX(version), 0) FROM daily_report WHERE account=? AND trade_day=?",
                    (account, day)).fetchone()
        version = int(r[0]) + 1
        self._x("INSERT INTO daily_report(mode, account, trade_day, version, status, content, settings_hash, "
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (self.mode.value, account, day, version, status, json.dumps(content, ensure_ascii=False, default=str),
                 settings_hash, to_iso(at)))
        return version

    def daily_reports(self, account: str) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM daily_report WHERE account=? ORDER BY seq", (account,))]

    def decisions_since(self, account: str, since: datetime) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM gate_decision WHERE account=? AND created_at>=? ORDER BY seq",
                                         (account, to_iso(since)))]

    def select(self, sql: str, params: tuple = ()) -> list[dict]:
        """읽기 전용 질의 (보고서용). SELECT만 허용한다."""
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("select()는 SELECT만")
        return [dict(r) for r in self._x(sql, params)]

    # --- 운영 명령 (03 §6.6) -------------------------------------------------------
    def insert_command(self, command_id: str, channel: str, actor: str, command: str, args: dict,
                       at: datetime) -> bool:
        """명령 기록. 같은 명령 ID(멱등키)가 이미 있으면 False (02 §4, V-57)."""
        cur = self._x("INSERT OR IGNORE INTO ops_command(command_id, mode, channel, actor, command, args, status, "
                      "created_at, created_seq) VALUES(?,?,?,?,?,?,?,?,?)",
                      (command_id, self.mode.value, channel, actor, command,
                       json.dumps(args, ensure_ascii=False, sort_keys=True), "PENDING", to_iso(at), self.next_seq("cmd")))
        return cur.rowcount == 1

    def pending_commands(self) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM ops_command WHERE status='PENDING' ORDER BY created_seq")]

    def command(self, command_id: str) -> dict | None:
        r = self._x("SELECT * FROM ops_command WHERE command_id=?", (command_id,)).fetchone()
        return dict(r) if r else None

    def finish_command(self, command_id: str, status: str, result: str, at: datetime) -> None:
        self._x("UPDATE ops_command SET status=?, result=?, done_at=? WHERE command_id=?",
                (status, result, to_iso(at), command_id))
