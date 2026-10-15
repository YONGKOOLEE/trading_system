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
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from trading_system.domain.clock import KST, from_iso, to_iso
from trading_system.domain.daily_base import DailyBase, DailyBaseCorrupt
from trading_system.domain.enums import ACTIVE_INTENT_STATES, IntentState, Mode
from trading_system.domain.fin import FIN_BASIS, FIN_SOURCE
from trading_system.domain.investor_flow import INVESTOR_KEYS, UNIT_AMOUNT, FlowRevision, FlowWrite, InvestorFlow
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.models import Account
from trading_system.domain.universe import SUBMIT_CHANNEL, ListEntry, ListVersion

DAILY_BASE_KIND = "DAILY_BASE"   # 거래일별 일일 기준 감사 기록 (monitoring.watchers.DAILY_BASE와 같은 값)
DUST_RELEASE_REASON = "dust 확정"  # 소유권 해제 사유 (CP-024 D-102, 01 R-28·02 ENT-20 개정 방향)

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
  settings_hash TEXT, limit_snapshot TEXT, rule_id TEXT);
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
  owner TEXT NOT NULL, state TEXT NOT NULL, intent_id TEXT, created_at TEXT NOT NULL, released_reason TEXT,
  released_at TEXT);
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
CREATE TABLE IF NOT EXISTS notification (
  alert_seq INTEGER PRIMARY KEY, mode TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL,
  channel TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, last_attempt_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS audit_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
  actor TEXT NOT NULL, ref TEXT, summary TEXT NOT NULL);
-- 시세 기록 (T-023, CP-027 draft D-141·D-142·D-144): 진단 전용 추가 표. 기존 표·의미를 바꾸지 않으므로 스키마
-- 버전을 올리지 않는다 — 기존 저장소도 열 때 생성(아래 인덱스와 같은 방식). 주문 경로는 이 표를 읽지 않는다
CREATE TABLE IF NOT EXISTS quote_snapshot (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, event TEXT NOT NULL, ref TEXT NOT NULL,
  account TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL, intent_id TEXT,
  event_at TEXT NOT NULL, event_price TEXT, event_quantity TEXT,
  quote_price TEXT, quote_at TEXT, quote_source TEXT, quote_age_seconds TEXT, tick TEXT,
  recorded_at TEXT NOT NULL, UNIQUE (event, ref));
CREATE TABLE IF NOT EXISTS quote_poll_stat (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  watched INTEGER NOT NULL, instruments INTEGER NOT NULL, requests INTEGER NOT NULL, failures INTEGER NOT NULL,
  latency_ms_p50 INTEGER, latency_ms_max INTEGER, interval_avg_seconds TEXT, interval_max_seconds TEXT,
  response_fields TEXT, recorded_at TEXT NOT NULL);
-- 체결 수수료·거래소 보고 금액 (M2, CP-024 D-101·D-118, T-M2-04): 체결 1건당 0~1행의 추가 표. 기존 표·열·의미를
-- 바꾸지 않으므로 스키마 버전을 올리지 않는다(quote_snapshot과 같은 방식, A-072 ⑤) — 기존 M1 저장소도 열 때 생성되고
-- M1 체결(수수료 자산·보고 금액 없음)은 이 표에 쓰지 않는다. quote_status: PROVISIONAL(fills만 — price × qty 임시값) /
-- CONFIRMED(executionReport Y·myTrades quoteQty). 확정값으로만 USDT 대조 (D-103)
CREATE TABLE IF NOT EXISTS fill_fee (
  fill_key TEXT PRIMARY KEY REFERENCES fill(fill_key), mode TEXT NOT NULL,
  commission TEXT, commission_asset TEXT, quote_qty TEXT NOT NULL, quote_status TEXT NOT NULL,
  recorded_at TEXT NOT NULL, confirmed_at TEXT);
-- 고객 주문 ID (M2 newClientOrderId, CP-024 D-110, T-M2-05): 의도별 유일·재사용 금지(기본 키·유일 제약). 전송 시도 기록과
-- 같은 저장 단위에서 전송 전에 쓴다. 추가 표라 스키마 버전 불변(위와 같음). M1(키움)은 쓰지 않는다
-- M2 대조 결과의 자산별 값 (CP-024 D-103, 02 ENT-24 M2, T-M2-06): 대조 1회당 자산별 내부값·거래소 free·locked·합·대조 범위
-- 여부. 추가 표라 스키마 버전 불변. M1 대조는 쓰지 않는다
CREATE TABLE IF NOT EXISTS recon_asset (
  recon_id TEXT NOT NULL REFERENCES reconciliation(recon_id), mode TEXT NOT NULL, account TEXT NOT NULL,
  asset TEXT NOT NULL, internal TEXT, broker_free TEXT NOT NULL, broker_locked TEXT NOT NULL, broker_total TEXT NOT NULL,
  in_scope INTEGER NOT NULL, PRIMARY KEY (recon_id, asset));
CREATE TABLE IF NOT EXISTS client_order (
  client_order_id TEXT PRIMARY KEY, mode TEXT NOT NULL, intent_id TEXT NOT NULL UNIQUE,
  account TEXT NOT NULL, instrument TEXT NOT NULL, created_at TEXT NOT NULL);
-- U-B 사람 목록 버전 (CP-034 D-198, T-041): 목록 ID별 일련번호·내용 해시·제출 시각·채널·제출자·종목(JSON). 추가 전용
-- 표라 스키마 버전을 올리지 않는다(A-086 ③·A-072 ⑤와 같은 방식) — 기존 저장소도 열 때 생성. 고치거나 지우지 않는다
-- (아래 트리거). 제출 채널은 LOCAL만(A-092 Q-8, 저장 함수가 검사). 주문 경로는 판정 입력으로만 읽는다
CREATE TABLE IF NOT EXISTS universe_list_version (
  list_id TEXT NOT NULL, version INTEGER NOT NULL, mode TEXT NOT NULL, content_hash TEXT NOT NULL,
  submitted_at TEXT NOT NULL, channel TEXT NOT NULL, actor TEXT NOT NULL, codes TEXT NOT NULL, memos TEXT NOT NULL,
  PRIMARY KEY (list_id, version));
CREATE TRIGGER IF NOT EXISTS universe_list_no_update BEFORE UPDATE ON universe_list_version
  BEGIN SELECT RAISE(ABORT, 'universe_list_version is append-only'); END;
CREATE TRIGGER IF NOT EXISTS universe_list_no_delete BEFORE DELETE ON universe_list_version
  BEGIN SELECT RAISE(ABORT, 'universe_list_version is append-only'); END;
-- 분봉 (CP-032 D-177, T-028): 키 (종목, 봉 길이, 봉 시작 시각 UTC, 수정주가 구분). 추가 전용 표라 스키마 버전을
-- 올리지 않는다(A-086 ③·A-072 ⑤와 같은 방식) — 기존 저장소도 열 때 생성. 고치거나 지우지 않는다(아래 트리거, 자동 삭제
-- 없음). 같은 키가 다시 오면 기존 값을 유지한다. 주문 경로·판정은 아직 읽지 않는다(주기 판정 배선은 T-029)
CREATE TABLE IF NOT EXISTS minute_bar (
  instrument TEXT NOT NULL, bar_minutes INTEGER NOT NULL, bar_start TEXT NOT NULL, adjusted TEXT NOT NULL,
  mode TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
  volume TEXT NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY (instrument, bar_minutes, bar_start, adjusted));
CREATE TRIGGER IF NOT EXISTS minute_bar_no_update BEFORE UPDATE ON minute_bar
  BEGIN SELECT RAISE(ABORT, 'minute_bar is append-only'); END;
CREATE TRIGGER IF NOT EXISTS minute_bar_no_delete BEFORE DELETE ON minute_bar
  BEGIN SELECT RAISE(ABORT, 'minute_bar is append-only'); END;
-- 투자자별 일별 순매수 (CAP-DAT-05a, CP-033 D-190·02 ENT-33, T-035): 키 (종목, 일자, 단위, 회차, 투자자 구분). 추가 전용
-- 표라 스키마 버전을 올리지 않는다(D-144·A-086 ③과 같은 방식) — 기존 저장소도 열 때 생성. 고치거나 지우지 않는다(트리거,
-- 자동 삭제 없음). 재수집 값이 다르면 새 회차 행 + 감사 INVESTOR_FLOW_CORRECTED, 같으면 관측(investor_flow_seen)만 추가.
-- 관측 시각은 확정 판정(D-189)에 쓴다. 주문 경로는 이 표를 읽지 않는다(05a 선언 전략의 판정 입력으로만)
CREATE TABLE IF NOT EXISTS investor_flow (
  instrument TEXT NOT NULL, trade_date TEXT NOT NULL, unit TEXT NOT NULL, revision INTEGER NOT NULL,
  investor TEXT NOT NULL, mode TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL,
  collected_at TEXT NOT NULL, collect_run TEXT NOT NULL,
  PRIMARY KEY (instrument, trade_date, unit, revision, investor));
CREATE TABLE IF NOT EXISTS investor_flow_seen (
  instrument TEXT NOT NULL, trade_date TEXT NOT NULL, unit TEXT NOT NULL, revision INTEGER NOT NULL,
  seen_at TEXT NOT NULL, mode TEXT NOT NULL, collect_run TEXT NOT NULL,
  PRIMARY KEY (instrument, trade_date, unit, revision, seen_at));
CREATE TRIGGER IF NOT EXISTS investor_flow_no_update BEFORE UPDATE ON investor_flow
  BEGIN SELECT RAISE(ABORT, 'investor_flow is append-only'); END;
CREATE TRIGGER IF NOT EXISTS investor_flow_no_delete BEFORE DELETE ON investor_flow
  BEGIN SELECT RAISE(ABORT, 'investor_flow is append-only'); END;
CREATE TRIGGER IF NOT EXISTS investor_flow_seen_no_update BEFORE UPDATE ON investor_flow_seen
  BEGIN SELECT RAISE(ABORT, 'investor_flow_seen is append-only'); END;
CREATE TRIGGER IF NOT EXISTS investor_flow_seen_no_delete BEFORE DELETE ON investor_flow_seen
  BEGIN SELECT RAISE(ABORT, 'investor_flow_seen is append-only'); END;
-- 재무 스냅샷 (CAP-DAT-05b, CP-033 D-194·02 ENT-34, T-037): 키 (종목, 수집일). 축적만 — 판정·백테스트 입력에 쓰지 않는다.
-- 추가 전용(트리거, 자동 삭제 없음, 스키마 번호 유지). fields = ka10001 필드 JSON, unchanged = 직전 스냅샷과 같음,
-- basis = 기준 기간 미확인 표시. 같은 날 다시 받으면 처음 값 유지
CREATE TABLE IF NOT EXISTS fin_snapshot (
  instrument TEXT NOT NULL, collected_day TEXT NOT NULL, mode TEXT NOT NULL, fields TEXT NOT NULL,
  unparsed TEXT NOT NULL, unchanged INTEGER NOT NULL, basis TEXT NOT NULL, source TEXT NOT NULL,
  collected_at TEXT NOT NULL, collect_run TEXT NOT NULL, PRIMARY KEY (instrument, collected_day));
CREATE TRIGGER IF NOT EXISTS fin_snapshot_no_update BEFORE UPDATE ON fin_snapshot
  BEGIN SELECT RAISE(ABORT, 'fin_snapshot is append-only'); END;
CREATE TRIGGER IF NOT EXISTS fin_snapshot_no_delete BEFORE DELETE ON fin_snapshot
  BEGIN SELECT RAISE(ABORT, 'fin_snapshot is append-only'); END;
-- 조회 인덱스: 데이터 의미 불변이라 스키마 버전을 올리지 않는다 — 기존 저장소도 열 때 생성 (review C절, DP-38)
CREATE INDEX IF NOT EXISTS ix_intent_state ON intent(state);
CREATE INDEX IF NOT EXISTS ix_ops_command_status ON ops_command(status);
CREATE INDEX IF NOT EXISTS ix_audit_kind_ref ON audit_event(kind, ref);
CREATE INDEX IF NOT EXISTS ix_audit_kind_at ON audit_event(kind, at);
CREATE INDEX IF NOT EXISTS ix_gate_decision_account_at ON gate_decision(account, created_at);
CREATE INDEX IF NOT EXISTS ix_broker_order_account_date ON broker_order(account, trade_date);
CREATE INDEX IF NOT EXISTS ix_fill_order_key ON fill(order_key);
CREATE INDEX IF NOT EXISTS ix_recon_item_state ON recon_item(state);
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


SCHEMA_VERSION = "2"  # 1: 기준선 (CP-018). 2: 신호 규칙 ID·소유 해제 시각 (거래 이력). maintenance.MIGRATIONS


class OrderIdentity(StrEnum):
    """브로커 주문 식별 범위 (시장별, CP-024 D-110).

    - TRADE_DATE (M1, 기본): (계좌, 거래일(KST), 주문번호) — 03 D-046. 키 `계좌|KST 날짜|주문번호`. 기존 동작 그대로.
    - SYMBOL (M2): (계좌, 심볼, orderId) — orderId는 심볼 안에서만 유일(B-U17), 거래일을 식별에 쓰지 않는다.
      키 `계좌|전송 UTC 날짜|심볼|orderId`(날짜는 참고용 — 조회는 열로 하며 날짜를 보지 않는다). trade_date 열 = 전송
      시각의 UTC 날짜(M2 거래일, D-108). 같은 (계좌, 심볼, orderId)는 두 번 만들지 않는다."""

    TRADE_DATE = "TRADE_DATE"
    SYMBOL = "SYMBOL"


def open_read_only(path: Path, mode: Mode, busy_timeout: float = 5.0) -> sqlite3.Connection:
    """조회 도구용 읽기 전용 연결 (confirm-unknown --list). 스키마 생성·모드 기록·쓰기를 하지 않는다.

    -wal 파일이 없으면(쓰는 프로세스 없음) immutable로 열어 -wal·-shm도 만들지 않는다. 있으면 mode=ro (WAL 공유
    메모리를 함께 읽어 쓰는 중인 run의 커밋을 본다). 모드·스키마 버전이 다르면 거부한다 (R-01, INV-12).
    열기·읽기 실패(잠김·DB 아님·meta 없음)는 StorageFailure."""
    from urllib.parse import quote
    p = path.resolve()
    if not p.is_file():
        raise StorageFailure(f"DB 없음: {p}")
    ro = "" if p.with_name(p.name + "-wal").exists() else "&immutable=1"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{quote(p.as_posix(), safe='/:')}?mode=ro{ro}", uri=True, timeout=busy_timeout)
        conn.row_factory = sqlite3.Row
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('mode','schema_version')").fetchall())
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise StorageFailure(f"읽기 전용 열기 실패 ({type(exc).__name__}: {exc}) — 잠김이면 잠시 뒤 다시 실행") from exc
    if meta.get("mode") != mode.value:
        conn.close()
        raise ModeMismatch(f"이 저장소는 {meta.get('mode') or '모드 기록 없음'} 모드로 만들어졌다 (요청 모드 {mode.value})")
    if meta.get("schema_version") != SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatch(f"저장소 스키마 버전 {meta.get('schema_version') or '없음'} ≠ 코드 {SCHEMA_VERSION}")
    return conn


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
    idem_key: str | None = None


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
    def __init__(self, path: Path, mode: Mode, busy_timeout: float = 5.0, *,
                 order_identity: OrderIdentity = OrderIdentity.TRADE_DATE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.order_identity = order_identity   # 브로커 주문 식별 범위 (M1 기본 TRADE_DATE, M2 SYMBOL — D-110)
        self._lock = threading.RLock()
        self._depth = 0
        self._tx_thread: int | None = None
        self._after_tx: list[Callable[[bool], None]] = []   # 가장 바깥 저장 단위 종료 뒤 부를 일 (CP-028 D-148)
        self.fail_next_transaction = False  # 테스트용 저장 실패 주입 (V-26)
        # busy_timeout: 다른 프로세스(scripts)가 쓰기 잠금을 쥐고 있을 때 기다리는 최대 초 (CP-018 L-3)
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False,
                                          timeout=busy_timeout)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")   # 커밋 내구성: 빌드 기본값에 기대지 않음 (INV-03 전송 전 기록)
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
        """하나의 저장 단위. 중첩 호출은 바깥 트랜잭션에 합쳐진다.

        가장 바깥 저장 단위가 끝나면(커밋·롤백 모두) call_after_tx로 맡긴 일을 같은 스레드에서 잠금 밖에서 부른다
        (CP-028 D-148: 저장 단위 안에서 낸 알림의 기록)."""
        after: list[Callable[[bool], None]] = []
        committed = False
        try:
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
                    committed = True
                except sqlite3.Error as exc:
                    self.connection.execute("ROLLBACK")
                    raise StorageFailure(str(exc)) from exc
                except BaseException:
                    self.connection.execute("ROLLBACK")
                    raise
                finally:
                    self._depth = 0
                    self._tx_thread = None
                    after, self._after_tx = self._after_tx, []
        finally:
            for fn in after:
                try:
                    fn(committed)
                except Exception as exc:  # noqa: BLE001 — 종료 뒤 일의 실패가 저장 단위 결과(예외)를 가리지 않게
                    sys.stderr.write(f"저장 단위 종료 뒤 처리 실패: {type(exc).__name__}\n")

    def call_after_tx(self, fn: Callable[[bool], None]) -> bool:
        """현재 스레드가 저장 단위 안이면 fn을 가장 바깥 저장 단위가 끝난 뒤 부르도록 맡기고 True.

        fn(committed): 커밋이면 True, 롤백(커밋 실패 포함)이면 False. 같은 스레드에서 저장소 잠금 밖에서 부른다.
        저장 단위 밖이면 아무것도 하지 않고 False — 호출자가 바로 처리한다 (CP-028 D-148)."""
        if not self.in_transaction:
            return False
        self._after_tx.append(fn)
        return True

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

    # --- U-B 목록 (CP-034 D-198) ------------------------------------------------
    def insert_universe_version(self, list_id: str, entries: Sequence[ListEntry], content_hash: str, at: datetime,
                                channel: str, actor: str) -> int:
        """새 목록 버전을 추가하고 번호를 돌려준다(목록 ID별 1부터). 저장 단위 안에서 부른다.

        제출 채널은 로컬만(A-092 Q-8) — 그 밖이면 ValueError. 목록 ID·종목 형식 검증은 호출자(제출 도구)가 먼저 한다."""
        if channel != SUBMIT_CHANNEL:
            raise ValueError(f"U-B 목록 제출은 로컬 도구만 가능 (채널 {channel} 거부, A-092 Q-8)")
        if not self.in_transaction:
            raise ValueError("insert_universe_version은 저장 단위(tx) 안에서")
        row = self._x("SELECT COALESCE(MAX(version), 0) FROM universe_list_version WHERE list_id=?", (list_id,))
        version = int(row.fetchone()[0]) + 1
        self._x("INSERT INTO universe_list_version(list_id, version, mode, content_hash, submitted_at, channel, actor, "
                "codes, memos) VALUES(?,?,?,?,?,?,?,?,?)",
                (list_id, version, self.mode.value, content_hash, to_iso(at), channel, actor,
                 json.dumps([e.code for e in entries]), json.dumps([e.memo for e in entries], ensure_ascii=False)))
        return version

    def universe_versions(self, list_id: str) -> list[ListVersion]:
        rows = self._x("SELECT * FROM universe_list_version WHERE list_id=? ORDER BY version", (list_id,))
        return [ListVersion(r["list_id"], int(r["version"]), r["content_hash"], from_iso(r["submitted_at"]),
                            r["channel"], tuple(json.loads(r["codes"]))) for r in rows]

    def universe_list_ids(self) -> list[str]:
        return [r["list_id"] for r in self._x("SELECT DISTINCT list_id FROM universe_list_version ORDER BY list_id")]

    def universe_memos(self, list_id: str, version: int) -> tuple[str, ...]:
        r = self._x("SELECT memos FROM universe_list_version WHERE list_id=? AND version=?",
                    (list_id, version)).fetchone()
        return tuple(json.loads(r["memos"])) if r else ()

    # --- 분봉 (CP-032 D-177, T-028) ---------------------------------------------
    def insert_minute_bars(self, bars: Sequence[MinuteBar], source: str, at: datetime) -> tuple[int, int, int]:
        """분봉을 추가한다. (새로 넣음, 이미 있고 같음, 이미 있고 값이 다름 — 기존 값 유지). 저장 단위 안에서 부른다."""
        if not self.in_transaction:
            raise ValueError("insert_minute_bars는 저장 단위(tx) 안에서")
        inserted = same = differing = 0
        for b in bars:
            key = (b.instrument, b.minutes, to_iso(b.start), b.adjusted)
            values = (str(b.open), str(b.high), str(b.low), str(b.close), str(b.volume))
            cur = self._x("INSERT OR IGNORE INTO minute_bar(instrument, bar_minutes, bar_start, adjusted, mode, open, "
                          "high, low, close, volume, source, recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (*key, self.mode.value, *values, source, to_iso(at)))
            if cur.rowcount == 1:
                inserted += 1
                continue
            old = self._x("SELECT open, high, low, close, volume FROM minute_bar WHERE instrument=? AND bar_minutes=? "
                          "AND bar_start=? AND adjusted=?", key).fetchone()
            if tuple(Decimal(x) for x in old) == tuple(Decimal(x) for x in values):
                same += 1
            else:
                differing += 1
        return inserted, same, differing

    def minute_bars(self, instrument: str, minutes: int, adjusted: str = "1", *, start: datetime | None = None,
                    end: datetime | None = None) -> list[MinuteBar]:
        """봉 시작 시각 오름차순. start ≤ 봉 시작 < end (주면)."""
        sql = "SELECT * FROM minute_bar WHERE instrument=? AND bar_minutes=? AND adjusted=?"
        params: list[object] = [instrument, minutes, adjusted]
        if start is not None:
            sql += " AND bar_start >= ?"
            params.append(to_iso(start))
        if end is not None:
            sql += " AND bar_start < ?"
            params.append(to_iso(end))
        rows = self._x(sql + " ORDER BY bar_start", tuple(params))
        return [MinuteBar(r["instrument"], int(r["bar_minutes"]), from_iso(r["bar_start"]), Decimal(r["open"]),
                          Decimal(r["high"]), Decimal(r["low"]), Decimal(r["close"]), Decimal(r["volume"]),
                          r["adjusted"]) for r in rows]

    # --- 투자자별 순매수 (CAP-DAT-05a, CP-033 D-190, T-035) -----------------------------
    def _latest_flow(self, instrument: str, day: str, unit: str) -> tuple[int, dict[str, Decimal]] | None:
        r = self._x("SELECT MAX(revision) FROM investor_flow WHERE instrument=? AND trade_date=? AND unit=?",
                    (instrument, day, unit)).fetchone()
        if r is None or r[0] is None:
            return None
        rev = int(r[0])
        rows = self._x("SELECT investor, value FROM investor_flow WHERE instrument=? AND trade_date=? AND unit=? "
                       "AND revision=?", (instrument, day, unit, rev))
        return rev, {x["investor"]: Decimal(x["value"]) for x in rows}

    def record_investor_flows(self, flows: Sequence[InvestorFlow], source: str, at: datetime,
                              run_id: str) -> FlowWrite:
        """투자자별 순매수를 추가한다 (D-190). 저장 단위 안에서 부른다.

        같은 (종목, 일자, 단위)가 처음이면 회차 1, 마지막 회차와 값이 같으면 관측(seen)만 추가, 다르면 새 회차 행 +
        감사 INVESTOR_FLOW_CORRECTED(바뀐 투자자 구분 이름만 — 값은 표에 있음). 이전 행은 바꾸지 않는다."""
        if not self.in_transaction:
            raise ValueError("record_investor_flows는 저장 단위(tx) 안에서")
        stamp = to_iso(at)
        new = same = corrected = 0
        corrections: list[tuple[str, date]] = []
        for f in flows:
            day = f.day.isoformat()
            values = dict(f.values)
            latest = self._latest_flow(f.instrument, day, f.unit)
            if latest is not None and latest[1] == values:
                rev = latest[0]
                same += 1
            else:
                rev = 1 if latest is None else latest[0] + 1
                for k, v in f.values:
                    self._x("INSERT INTO investor_flow(instrument, trade_date, unit, revision, investor, mode, value, "
                            "source, collected_at, collect_run) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (f.instrument, day, f.unit, rev, k, self.mode.value, str(v), source, stamp, run_id))
                if latest is None:
                    new += 1
                else:
                    corrected += 1
                    corrections.append((f.instrument, f.day))
                    changed = sorted(k for k in set(values) | set(latest[1]) if values.get(k) != latest[1].get(k))
                    self.audit(at, "INVESTOR_FLOW_CORRECTED", "collector", f.instrument,
                               {"day": day, "unit": f.unit, "previous_revision": latest[0], "revision": rev,
                                "changed": changed, "source": source, "run": run_id})
            self._x("INSERT OR IGNORE INTO investor_flow_seen(instrument, trade_date, unit, revision, seen_at, mode, "
                    "collect_run) VALUES(?,?,?,?,?,?,?)", (f.instrument, day, f.unit, rev, stamp, self.mode.value,
                                                           run_id))
        return FlowWrite(new, same, corrected, tuple(corrections))

    def investor_flow_revisions(self, instrument: str, unit: str = UNIT_AMOUNT, *, since: date | None = None,
                                until: date | None = None) -> list[FlowRevision]:
        """저장된 회차 전부 (일자·회차 오름차순). since ≤ 일자 ≤ until (주면). 투자자 구분 순서는 INVESTOR_KEYS."""
        sql, params = "SELECT * FROM investor_flow WHERE instrument=? AND unit=?", [instrument, unit]
        if since is not None:
            sql += " AND trade_date >= ?"
            params.append(since.isoformat())
        if until is not None:
            sql += " AND trade_date <= ?"
            params.append(until.isoformat())
        groups: dict[tuple[str, int], list[sqlite3.Row]] = {}
        for r in self._x(sql + " ORDER BY trade_date, revision", tuple(params)):
            groups.setdefault((r["trade_date"], int(r["revision"])), []).append(r)
        seen: dict[tuple[str, int], list[datetime]] = {}
        sql2 = sql.replace("FROM investor_flow ", "FROM investor_flow_seen ", 1)
        for r in self._x(sql2 + " ORDER BY seen_at", tuple(params)):
            seen.setdefault((r["trade_date"], int(r["revision"])), []).append(from_iso(r["seen_at"]))
        order = {k: i for i, k in enumerate(INVESTOR_KEYS)}
        out: list[FlowRevision] = []
        for (day, rev), rows in groups.items():
            vals = sorted(((r["investor"], Decimal(r["value"])) for r in rows), key=lambda kv: order.get(kv[0], 99))
            flow = InvestorFlow(instrument, date.fromisoformat(day), unit, tuple(vals))
            out.append(FlowRevision(flow, rev, from_iso(rows[0]["collected_at"]), tuple(seen.get((day, rev), ()))))
        return out

    # --- 재무 스냅샷 (CAP-DAT-05b, CP-033 D-194, T-037) — 축적만 ------------------------------
    def record_fin_snapshot(self, instrument: str, day: date, values: object, at: datetime, run_id: str) -> str:
        """재무 스냅샷 1건 추가. "NEW" / "UNCHANGED"(직전 스냅샷과 같음 — 표시만, 값도 저장) / "DUPLICATE_DAY"(같은 날
        이미 있음 — 처음 값 유지). values: marketdata.fin_snapshot.FinValues(as_dict·unparsed). 저장 단위 안에서 부른다."""
        if not self.in_transaction:
            raise ValueError("record_fin_snapshot는 저장 단위(tx) 안에서")
        fields = dict(values.as_dict())  # type: ignore[attr-defined]
        unparsed = list(values.unparsed)  # type: ignore[attr-defined]
        if self._x("SELECT 1 FROM fin_snapshot WHERE instrument=? AND collected_day=?",
                   (instrument, day.isoformat())).fetchone():
            return "DUPLICATE_DAY"
        prev = self._x("SELECT fields FROM fin_snapshot WHERE instrument=? AND collected_day<? "
                       "ORDER BY collected_day DESC LIMIT 1", (instrument, day.isoformat())).fetchone()
        unchanged = prev is not None and json.loads(prev["fields"]) == fields
        self._x("INSERT INTO fin_snapshot(instrument, collected_day, mode, fields, unparsed, unchanged, basis, source, "
                "collected_at, collect_run) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (instrument, day.isoformat(), self.mode.value, json.dumps(fields, ensure_ascii=False, sort_keys=True),
                 json.dumps(unparsed, ensure_ascii=False), int(unchanged), FIN_BASIS, FIN_SOURCE, to_iso(at), run_id))
        return "UNCHANGED" if unchanged else "NEW"

    def fin_snapshot_first_day(self) -> date | None:
        """축적 시작일 (가장 이른 수집일). 없으면 None."""
        r = self._x("SELECT MIN(collected_day) FROM fin_snapshot").fetchone()
        return date.fromisoformat(r[0]) if r is not None and r[0] else None

    def investor_flow_instruments(self, unit: str = UNIT_AMOUNT) -> list[str]:
        return [r["instrument"] for r in self._x("SELECT DISTINCT instrument FROM investor_flow WHERE unit=? "
                                                 "ORDER BY instrument", (unit,))]

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

    def audited_refs(self, kinds: tuple[str, ...], refs: list[str]) -> set[str]:
        """주어진 종류의 감사 기록이 있는 참조(ref)들 (색인 ix_audit_kind_ref). 읽기만 한다 (T-044 재판정 방지)."""
        if not kinds or not refs:
            return set()
        sql = (f"SELECT DISTINCT ref FROM audit_event WHERE kind IN ({','.join('?' * len(kinds))}) "
               f"AND ref IN ({','.join('?' * len(refs))})")
        return {str(r["ref"]) for r in self._x(sql, (*kinds, *refs))}

    def daily_base(self, account: str, trade_date: str) -> DailyBase | None:
        """거래일의 마지막 DAILY_BASE 감사 기록 (A-084 ①: 일일 손실 추적·요약·보고가 같은 기준 금액을 읽는다).

        기록이 없으면 None. 같은 거래일에 여러 번 기록됐으면 마지막 것. 그 거래일의 마지막 기록이 깨졌으면
        DailyBaseCorrupt — "기록 없음"으로 보면 호출자가 기준을 새로 고정해 그날 앞선 손실이 사라진다 (재검토 M-1).
        요약에서 거래일을 읽지 못하는 행은 기록 시각(at)의 거래일로 판단한다."""
        for r in self._x("SELECT at, summary FROM audit_event WHERE kind=? AND ref=? ORDER BY seq DESC",
                         (DAILY_BASE_KIND, account)):
            try:
                rec = json.loads(r["summary"])
            except ValueError:
                rec = None
            if isinstance(rec, dict) and rec.get("market") not in (None, "M1"):
                # 다른 시장의 일일 기준(M2 monitoring.spot_cash — 요약에 market, 거래일 = UTC 날짜)은 M1 기준이 아니다.
                # 같은 별칭·같은 날짜 문자열이어도 읽지 않는다 (통합 시험 적응, V-152 M1 동작 불변)
                continue
            day = rec.get("trade_date") if isinstance(rec, dict) else None
            if not isinstance(day, str):
                try:
                    day = self.trade_date_of(from_iso(r["at"]))
                except (TypeError, ValueError):
                    day = trade_date   # 어느 날 기록인지도 모르면 손상으로 본다 (보수적)
            if day != trade_date:
                continue
            if not isinstance(rec, dict):
                raise DailyBaseCorrupt(f"DAILY_BASE {trade_date} 요약을 읽을 수 없음")
            try:
                return DailyBase.from_summary(rec)
            except ValueError as exc:
                raise DailyBaseCorrupt(f"DAILY_BASE {trade_date} {exc}") from exc
        return None

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
            r["idem_key"],
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

    def broker_order_key(self, account: str, at: datetime, order_no: str, instrument: str) -> str:
        """식별 범위(OrderIdentity)에 맞는 브로커 주문 키. TRADE_DATE는 order_key와 같다(M1 불변)."""
        if self.order_identity is OrderIdentity.SYMBOL:
            return f"{account}|{at.astimezone(UTC).date().isoformat()}|{instrument}|{order_no}"
        return self.order_key(account, at, order_no)

    def insert_broker_order(self, *, account: str, order_no: str, at: datetime, intent_id: str | None,
                            instrument: str, side: str, quantity: Decimal, origin: str,
                            target_order_key: str | None = None) -> str:
        key = self.broker_order_key(account, at, order_no, instrument)
        if self.order_identity is OrderIdentity.SYMBOL:
            trade_date = at.astimezone(UTC).date().isoformat()
            if self._x("SELECT 1 FROM broker_order WHERE account=? AND instrument=? AND order_no=?",
                       (account, instrument, order_no)).fetchone() is not None:
                raise ValueError(f"이미 있는 브로커 주문 (계좌, 심볼, orderId) — D-110: {instrument} {order_no}")
        else:
            trade_date = at.astimezone(KST).date().isoformat()
        self._x("""INSERT INTO broker_order(order_key, mode, account, trade_date, order_no, intent_id, instrument,
                   side, quantity, filled_quantity, state, origin, target_order_key, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, self.mode.value, account, trade_date, order_no, intent_id,
                 instrument, side, str(quantity), "0", "ACCEPTED", origin, target_order_key, to_iso(at)))
        return key

    @staticmethod
    def _order(r: sqlite3.Row) -> BrokerOrderRow:
        return BrokerOrderRow(r["order_key"], r["order_no"], r["intent_id"], r["account"], r["instrument"],
                              r["side"], Decimal(r["quantity"]), Decimal(r["filled_quantity"]), r["state"])

    def broker_order(self, order_key: str) -> BrokerOrderRow | None:
        r = self._x("SELECT * FROM broker_order WHERE order_key = ?", (order_key,)).fetchone()
        return self._order(r) if r else None

    def broker_order_by_no(self, order_no: str, trade_date: str, account: str | None = None, *,
                           instrument: str | None = None) -> BrokerOrderRow | None:
        """주문번호는 거래일·계좌 안에서만 유일하다고 본다 (03 D-046). 계좌를 모르는데 여러 계좌에 있으면 None.

        SYMBOL 범위(M2, D-110): 거래일을 보지 않고 (계좌, 심볼, orderId)로 찾는다. 심볼을 모르는데 여러 건이면 None.
        TRADE_DATE 범위(M1)는 instrument를 쓰지 않는다 — 기존 질의 그대로."""
        if self.order_identity is OrderIdentity.SYMBOL:
            sql, params = "SELECT * FROM broker_order WHERE order_no=?", (order_no,)
            if instrument is not None:
                sql, params = sql + " AND instrument=?", params + (instrument,)
        else:
            sql, params = "SELECT * FROM broker_order WHERE order_no=? AND trade_date=?", (order_no, trade_date)
        if account is not None:
            sql, params = sql + " AND account=?", params + (account,)
        rows = list(self._x(sql, params))
        return self._order(rows[0]) if len(rows) == 1 else None

    def order_nos_of(self, account: str, instrument: str) -> set[str]:
        """이 (계좌, 심볼)의 알려진 브로커 주문번호 전체 (SYMBOL 범위 M2 — 날짜 무관, D-110·D-111)."""
        return {r["order_no"] for r in self._x("SELECT order_no FROM broker_order WHERE account=? AND instrument=?",
                                               (account, instrument))}

    # --- 고객 주문 ID (M2, D-110) ---------------------------------------------------
    def insert_client_order(self, client_order_id: str, intent_id: str, account: str, instrument: str,
                            at: datetime) -> bool:
        """고객 주문 ID를 의도에 묶는다. 같은 의도에 같은 ID면 True(재시작 후 재확인), 다른 의도가 이미 쓰면 False(재사용 금지)."""
        r = self._x("SELECT intent_id FROM client_order WHERE client_order_id=?", (client_order_id,)).fetchone()
        if r is not None:
            return bool(r["intent_id"] == intent_id)
        if self._x("SELECT 1 FROM client_order WHERE intent_id=?", (intent_id,)).fetchone() is not None:
            return False   # 이 의도에 이미 다른 ID — 의도당 ID 하나
        self._x("INSERT INTO client_order(client_order_id, mode, intent_id, account, instrument, created_at) "
                "VALUES(?,?,?,?,?,?)", (client_order_id, self.mode.value, intent_id, account, instrument, to_iso(at)))
        return True

    def client_order_of(self, intent_id: str) -> str | None:
        r = self._x("SELECT client_order_id FROM client_order WHERE intent_id=?", (intent_id,)).fetchone()
        return r["client_order_id"] if r else None

    def intent_of_client_order(self, client_order_id: str) -> str | None:
        r = self._x("SELECT intent_id FROM client_order WHERE client_order_id=?", (client_order_id,)).fetchone()
        return r["intent_id"] if r else None

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

    def insert_fill_fee(self, fill_key: str, commission: Decimal | None, commission_asset: str | None,
                        quote_qty: Decimal, confirmed: bool, at: datetime) -> None:
        """체결 수수료·보고 금액 기록 (M2, D-101·D-118). 체결 행을 기록한 같은 저장 단위에서 부른다."""
        self._x("INSERT INTO fill_fee(fill_key, mode, commission, commission_asset, quote_qty, quote_status, "
                "recorded_at, confirmed_at) VALUES(?,?,?,?,?,?,?,?)",
                (fill_key, self.mode.value, None if commission is None else str(commission), commission_asset,
                 str(quote_qty), "CONFIRMED" if confirmed else "PROVISIONAL", to_iso(at),
                 to_iso(at) if confirmed else None))

    def fill_fee(self, fill_key: str) -> dict | None:
        r = self._x("SELECT * FROM fill_fee WHERE fill_key=?", (fill_key,)).fetchone()
        return dict(r) if r else None

    def confirm_fill_quote(self, fill_key: str, quote_qty: Decimal, at: datetime) -> None:
        """임시 금액(PROVISIONAL)을 거래소 보고 금액으로 확정한다 (D-118)."""
        self._x("UPDATE fill_fee SET quote_qty=?, quote_status='CONFIRMED', confirmed_at=? "
                "WHERE fill_key=? AND quote_status='PROVISIONAL'", (str(quote_qty), to_iso(at), fill_key))

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

    def active_ownerships_of(self, owner: str) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM ownership WHERE owner=? AND state IN ('RESERVED','CONFIRMED')",
                                         (owner,))]

    def insert_ownership(self, account: str, instrument: str, owner: str, intent_id: str | None,
                         at: datetime) -> None:
        self._x("INSERT INTO ownership(ownership_id, mode, account, instrument, owner, state, intent_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (new_id("own"), self.mode.value, account, instrument, owner, "RESERVED", intent_id, to_iso(at)))

    def set_ownership_state(self, ownership_id: str, state: str, reason: str | None = None,
                            at: datetime | None = None) -> None:
        self._x("UPDATE ownership SET state=?, released_reason=COALESCE(?, released_reason), "
                "released_at=COALESCE(?, released_at) WHERE ownership_id=?",
                (state, reason, to_iso(at) if at is not None and state == "RELEASED" else None, ownership_id))

    def dust_instruments(self, account: str) -> set[str]:
        """dust 확정으로 소유권이 해제된 뒤 잔량만 남은 종목 (M2, D-102: 포지션 없음 취급·L6 제외).

        종목별 마지막 소유권 행이 RELEASED + 사유 "dust 확정"이고 보유 수량 > 0인 것. 그 뒤 재진입하면 새 소유권이 생겨 빠진다."""
        latest: dict[str, sqlite3.Row] = {}
        for r in self._x("SELECT * FROM ownership WHERE account=? ORDER BY created_at, rowid", (account,)):
            latest[r["instrument"]] = r
        return {inst for inst, r in latest.items()
                if r["state"] == "RELEASED" and r["released_reason"] == DUST_RELEASE_REASON
                and self.position(account, inst).quantity > 0}

    def strategy_history(self, account: str, owner: str, instrument: str) -> dict:
        """전략·종목 거래 이력 원자료 (00 §2.2 거래 이력). 체결 0주로 끝난 진입은 진입으로 세지 않는다."""
        lots = [dict(r) for r in self._x(
            "SELECT * FROM ownership WHERE account=? AND instrument=? AND owner=? "
            "AND NOT (state='RELEASED' AND released_reason='체결 0주 종결') ORDER BY created_at, rowid",
            (account, instrument, owner))]
        entry_at = None
        active = next((o for o in lots if o["state"] in ("RESERVED", "CONFIRMED")), None)
        if active is not None and active["intent_id"]:
            r = self._x("SELECT MIN(f.at) FROM fill f JOIN broker_order b ON f.order_key=b.order_key "
                        "WHERE b.intent_id=?", (active["intent_id"],)).fetchone()
            entry_at = r[0] if r and r[0] else None
        released = [o for o in lots if o["state"] == "RELEASED" and o["released_at"]]
        last_exit_at = max((o["released_at"] for o in released), default=None)
        reason = None
        if last_exit_at is not None:
            r = self._x("SELECT i.source, d.rule_id FROM intent i JOIN gate_decision d ON i.decision_id=d.decision_id "
                        "WHERE i.account=? AND i.instrument=? AND i.side='SELL' AND i.filled_quantity<>'0' "
                        "AND i.created_at<=? ORDER BY i.created_seq DESC LIMIT 1",
                        (account, instrument, last_exit_at)).fetchone()
            if r is not None:
                reason = r["rule_id"] if r["source"] == "SIGNAL" and r["rule_id"] else r["source"]
        return {"entry_at": entry_at, "last_exit_at": last_exit_at, "last_exit_reason": reason,
                "entries": [o["created_at"] for o in lots]}

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
                    released_at: datetime | None = None, released_by: str | None = None,
                    variant: str | None = None) -> None:
        if variant is not None:
            self._x("UPDATE halt SET variant=? WHERE halt_id=?", (variant, halt_id))
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

    def strategy_states(self) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM strategy_state ORDER BY strategy_id, version, market")]

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

    def insert_recon_assets(self, recon_id: str, account: str,
                            rows: list[tuple[str, str | None, Decimal, Decimal, bool]]) -> None:
        """M2 대조의 자산별 값 (자산, 내부값, 거래소 free, locked, 대조 범위) — D-103."""
        for asset, internal, free, locked, in_scope in rows:
            self._x("INSERT INTO recon_asset(recon_id, mode, account, asset, internal, broker_free, broker_locked, "
                    "broker_total, in_scope) VALUES(?,?,?,?,?,?,?,?,?)",
                    (recon_id, self.mode.value, account, asset, internal, str(free), str(locked), str(free + locked),
                     1 if in_scope else 0))

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

    # --- 시세 기록 (T-023, CP-027 draft) — 진단 전용, 주문 경로에서 부르지 않는다 ----------------
    def last_rowid(self, table: str) -> int:
        if table not in ("attempt", "fill"):
            raise ValueError(f"허용하지 않는 표 {table}")
        return int(self._x(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0])

    def attempts_after(self, rowid: int, account: str) -> list[dict]:
        """rowid 뒤의 전송 시도 + 의도 요약 (계좌 한정, 순서대로)."""
        return [dict(r) for r in self._x(
            "SELECT a.rowid AS rid, a.attempt_id, a.created_at, i.intent_id, i.instrument, i.side, i.request_type, "
            "i.price FROM attempt a JOIN intent i ON i.intent_id = a.intent_id "
            "WHERE a.rowid > ? AND i.account = ? ORDER BY a.rowid", (rowid, account))]

    def fills_after(self, rowid: int, account: str) -> list[dict]:
        """rowid 뒤의 체결 + 브로커 주문 요약 (계좌 한정, 순서대로)."""
        return [dict(r) for r in self._x(
            "SELECT f.rowid AS rid, f.fill_key, f.quantity, f.price, f.at, b.instrument, b.side, b.intent_id "
            "FROM fill f JOIN broker_order b ON b.order_key = f.order_key "
            "WHERE f.rowid > ? AND b.account = ? ORDER BY f.rowid", (rowid, account))]

    def insert_quote_snapshot(self, row: dict) -> None:
        """같은 (사건, 참조)는 한 번만 (INSERT OR IGNORE)."""
        def t(v: object) -> object:
            return to_iso(v) if isinstance(v, datetime) else (None if v is None else str(v))
        self._x("INSERT OR IGNORE INTO quote_snapshot(mode, event, ref, account, instrument, side, intent_id, event_at, "
                "event_price, event_quantity, quote_price, quote_at, quote_source, quote_age_seconds, tick, "
                "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.mode.value, row["event"], row["ref"], row["account"], row["instrument"], row["side"],
                 row.get("intent_id"), t(row["event_at"]), t(row.get("event_price")), t(row.get("event_quantity")),
                 t(row.get("quote_price")), t(row.get("quote_at")), row.get("quote_source"),
                 t(row.get("quote_age_seconds")), t(row.get("tick")), t(row["recorded_at"])))

    def insert_poll_stat(self, row: dict) -> None:
        fields = row.get("response_fields")
        self._x("INSERT INTO quote_poll_stat(mode, window_start, window_end, watched, instruments, requests, failures, "
                "latency_ms_p50, latency_ms_max, interval_avg_seconds, interval_max_seconds, response_fields, "
                "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.mode.value, to_iso(row["window_start"]), to_iso(row["window_end"]), int(row["watched"]),
                 int(row["instruments"]), int(row["requests"]), int(row["failures"]), row.get("latency_ms_p50"),
                 row.get("latency_ms_max"), row.get("interval_avg_seconds"), row.get("interval_max_seconds"),
                 None if fields is None else json.dumps(list(fields), ensure_ascii=False),
                 to_iso(row["recorded_at"])))

    def select(self, sql: str, params: tuple = ()) -> list[dict]:
        """읽기 전용 질의 (보고서용). SELECT만 허용한다."""
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("select()는 SELECT만")
        return [dict(r) for r in self._x(sql, params)]

    # --- 알림 전송 상태 (02 ENT-26) ---------------------------------------------------
    def enqueue_new_alerts(self, channel: str = "telegram") -> int:
        """아직 대기열에 없는 감사 ALERT를 넣는다. 넣은 수."""
        r = self._x("SELECT COALESCE(MAX(alert_seq), 0) FROM notification").fetchone()
        rows = list(self._x("SELECT seq, summary FROM audit_event WHERE kind='ALERT' AND seq>? ORDER BY seq",
                            (int(r[0]),)))
        for row in rows:
            summary = json.loads(row["summary"])
            self._x("INSERT INTO notification(alert_seq, mode, level, message, channel, status, attempts) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (row["seq"], self.mode.value, summary.get("level", "INFO"), summary.get("message", ""), channel,
                     "PENDING", 0))
        return len(rows)

    def skip_old_alerts_on_first_enable(self, channel: str = "telegram") -> int:
        """알림 채널을 처음 켤 때(대기열 기록이 하나도 없을 때) 그 전의 감사 ALERT를 보내지 않도록 마지막 ALERT를
        SKIPPED로 넣는다(enqueue_new_alerts는 최대 alert_seq 이후만 넣음). 건너뛴 수. 이미 기록이 있으면 0 —
        연결 이후 밀린 알림은 그대로 보낸다(V-30)."""
        if self._x("SELECT 1 FROM notification LIMIT 1").fetchone() is not None:
            return 0
        r = self._x("SELECT COUNT(*), MAX(seq) FROM audit_event WHERE kind='ALERT'").fetchone()
        if not r[0]:
            return 0
        self._x("INSERT INTO notification(alert_seq, mode, level, message, channel, status, attempts) "
                "VALUES(?,?,?,?,?,?,?)",
                (int(r[1]), self.mode.value, "INFO", f"채널 연결 이전 알림 {r[0]}건 건너뜀", channel, "SKIPPED", 0))
        return int(r[0])

    def pending_notifications(self) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM notification WHERE status='PENDING' ORDER BY alert_seq")]

    def mark_notification(self, alert_seq: int, ok: bool, at: datetime, error: str | None = None,
                          count_failure: bool = True) -> None:
        """성공이면 SENT, 실패면 시도 횟수를 늘린다. 밀린 알림은 버리지 않는다(복구 시 전송, V-30)."""
        if ok:
            self._x("UPDATE notification SET status='SENT', attempts=attempts+1, last_attempt_at=?, last_error=NULL "
                    "WHERE alert_seq=?", (to_iso(at), alert_seq))
            self._x("INSERT INTO meta(key, value) VALUES('notify_fail_streak', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value='0'")
            return
        self._x("UPDATE notification SET attempts=attempts+1, last_attempt_at=?, last_error=? WHERE alert_seq=?",
                (to_iso(at), (error or "")[:200], alert_seq))
        if not count_failure:   # 429 전송 제한 — 채널 장애가 아니라 K11 연속 실패로 세지 않는다 (T-017 M-3)
            return
        self._x("INSERT INTO meta(key, value) VALUES('notify_fail_streak', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)")

    def drop_notification(self, alert_seq: int, at: datetime, error: str) -> None:
        """메시지 자체가 거부된 알림을 대기열에서 뺀다(DROPPED). 연속 실패 수는 건드리지 않는다 (T-017 M-2)."""
        self._x("UPDATE notification SET status='DROPPED', attempts=attempts+1, last_attempt_at=?, last_error=? "
                "WHERE alert_seq=?", (to_iso(at), error[:200], alert_seq))

    def notification_fail_streak(self) -> int:
        r = self._x("SELECT value FROM meta WHERE key='notify_fail_streak'").fetchone()
        return int(r[0]) if r else 0

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
