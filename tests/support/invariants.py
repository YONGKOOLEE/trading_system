"""불변 조건 검사기 (02-data-model §6, 04 D-069).

모든 시나리오 테스트가 끝날 때 실행한다. 위반 목록을 문자열로 돌려준다.
T-001 범위: INV-01·03·04·05·06·07·08·09·10·12·15·17, T-002: INV-14, T-004: INV-02·11 (감사 이력 재생).
T-007: INV-13. INV-16(수동 소유 종목)은 T-008에서 추가한다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import time as dtime
from decimal import Decimal

from trading_system.domain.clock import from_iso, next_kst_time_after

ACTIVE_INTENT_STATES = ("PENDING_SEND", "SENT", "UNKNOWN", "ACCEPTED", "PARTIAL")
NEW_ORDER_TYPES = ("ENTRY", "INCREASE", "REDUCE", "EXIT")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))


def check_invariants(conn: sqlite3.Connection, mode: str) -> list[str]:
    conn.row_factory = sqlite3.Row
    v: list[str] = []
    act = ",".join(f"'{s}'" for s in ACTIVE_INTENT_STATES)
    new = ",".join(f"'{t}'" for t in NEW_ORDER_TYPES)

    # INV-01 의도는 승인된 게이트 판정을 가진다
    for r in _rows(conn, """SELECT i.intent_id FROM intent i LEFT JOIN gate_decision d
                            ON d.decision_id = i.decision_id
                            WHERE d.decision_id IS NULL OR d.result <> 'APPROVED'"""):
        v.append(f"INV-01 승인 판정 없는 의도 {r['intent_id']}")

    # INV-03 시도는 의도보다 나중에 기록된다
    for r in _rows(conn, """SELECT a.attempt_id FROM attempt a JOIN intent i ON i.intent_id = a.intent_id
                            WHERE a.created_seq <= i.created_seq"""):
        v.append(f"INV-03 의도보다 먼저 기록된 시도 {r['attempt_id']}")

    # INV-04 포지션 수량 ≥ 0, 체결 누계 ≤ 주문 수량
    for r in _rows(conn, "SELECT account, instrument, quantity FROM position"):
        if Decimal(r["quantity"]) < 0:
            v.append(f"INV-04 음수 포지션 {r['account']}/{r['instrument']}")
    for r in _rows(conn, "SELECT order_key, quantity, filled_quantity FROM broker_order"):
        if Decimal(r["filled_quantity"]) > Decimal(r["quantity"]):
            v.append(f"INV-04 주문 수량 초과 체결 {r['order_key']}")
    for r in _rows(conn, """SELECT o.order_key, o.filled_quantity, f.total FROM broker_order o
                            JOIN (SELECT order_key, SUM(CAST(quantity AS REAL)) AS total FROM fill GROUP BY order_key) f
                            ON f.order_key = o.order_key"""):
        if Decimal(str(r["total"])) != Decimal(r["filled_quantity"]):
            v.append(f"INV-04 체결 합계 불일치 {r['order_key']}")

    # INV-05 (계좌, 종목)당 활성 소유권 최대 1
    for r in _rows(conn, """SELECT account, instrument, COUNT(*) c FROM ownership
                            WHERE state IN ('RESERVED','CONFIRMED') GROUP BY account, instrument HAVING c > 1"""):
        v.append(f"INV-05 활성 소유권 {r['c']}개 {r['account']}/{r['instrument']}")

    # INV-06 활성 매도 의도 수량 ≤ 포지션 수량
    for r in _rows(conn, f"""SELECT i.account, i.instrument, SUM(CAST(i.quantity AS REAL)) q,
                                    COALESCE(p.quantity, '0') pos
                             FROM intent i LEFT JOIN position p
                               ON p.account = i.account AND p.instrument = i.instrument
                             WHERE i.side = 'SELL' AND i.request_type IN ({new}) AND i.state IN ({act})
                             GROUP BY i.account, i.instrument"""):
        if Decimal(str(r["q"])) > Decimal(r["pos"]):
            v.append(f"INV-06 매도 의도가 보유 초과 {r['account']}/{r['instrument']}")

    # INV-07 승인 시점 한도 스냅샷이 한도 이하
    for r in _rows(conn, """SELECT decision_id, symbol_after, symbol_limit, total_after, total_limit
                            FROM gate_decision WHERE result = 'APPROVED' AND symbol_after IS NOT NULL"""):
        if Decimal(r["symbol_after"]) > Decimal(r["symbol_limit"]) or Decimal(r["total_after"]) > Decimal(r["total_limit"]):
            v.append(f"INV-07 한도 초과 승인 {r['decision_id']}")

    # INV-08 의도당 전송 시도 최대 1
    for r in _rows(conn, "SELECT intent_id, COUNT(*) c FROM attempt GROUP BY intent_id HAVING c > 1"):
        v.append(f"INV-08 시도 {r['c']}개 {r['intent_id']}")

    # INV-09 같은 신호로 승인된 의도 최대 1 (멱등키 유일은 DB 제약)
    for r in _rows(conn, """SELECT signal_id, COUNT(*) c FROM intent WHERE signal_id IS NOT NULL
                            GROUP BY signal_id HAVING c > 1"""):
        v.append(f"INV-09 같은 신호의 의도 {r['c']}개 {r['signal_id']}")

    # INV-10 감사 순번 연속
    seqs = [r["seq"] for r in _rows(conn, "SELECT seq FROM audit_event ORDER BY seq")]
    if seqs and seqs != list(range(1, len(seqs) + 1)):
        v.append("INV-10 감사 순번 불연속")

    # INV-12 모든 레코드의 모드 = 저장소 모드
    for table in ("account", "gate_decision", "intent", "attempt", "broker_order", "fill",
                  "position", "ownership", "reservation", "halt", "audit_event", "settings_version", "ops_command",
                  "strategy_registration", "strategy_state", "strategy_transition"):
        for r in _rows(conn, f"SELECT COUNT(*) c FROM {table} WHERE mode <> ?", (mode,)):
            if r["c"]:
                v.append(f"INV-12 모드 불일치 {table} {r['c']}건")

    # INV-14 완화·혼합·기준 시각 변경은 제출 후 첫 일일 기준 시각 이후에 적용
    for r in _rows(conn, "SELECT * FROM settings_version WHERE result = 'PASSED' AND direction <> 'INITIAL'"):
        if r["direction"] in ("LOOSENED", "MIXED") and not r["deferred"]:
            v.append(f"INV-14 완화·혼합이 즉시 적용됨 seq={r['seq']}")
        if r["deferred"]:
            hh, mm = (r["ref_time_used"] or "00:00").split(":")
            earliest = next_kst_time_after(from_iso(r["submitted_at"]), dtime(int(hh), int(mm)))
            if from_iso(r["effective_at"]) < earliest:
                v.append(f"INV-14 기준 시각 전에 적용 seq={r['seq']}")

    # INV-15 (계좌, 종목, 방향)당 활성 새 주문 의도 최대 1
    for r in _rows(conn, f"""SELECT account, instrument, side, COUNT(*) c FROM intent
                             WHERE request_type IN ({new}) AND state IN ({act})
                             GROUP BY account, instrument, side HAVING c > 1"""):
        v.append(f"INV-15 활성 새 주문 {r['c']}개 {r['account']}/{r['instrument']}/{r['side']}")

    v.extend(_replay_intent_states(conn))

    # INV-13 전략 상태 전이에는 승인 A-ID가 있어야 한다 (A-숫자 형식)
    for r in _rows(conn, "SELECT seq, approval_id FROM strategy_transition"):
        if not re.fullmatch(r"A-\d{3,}", r["approval_id"] or ""):
            v.append(f"INV-13 승인 A-ID 없는 전략 상태 전이 seq={r['seq']}")

    # INV-17 대상 주문당 활성 취소·정정 의도 최대 1
    for r in _rows(conn, f"""SELECT target_order_key, COUNT(*) c FROM intent
                             WHERE request_type IN ('CANCEL','MODIFY') AND state IN ({act})
                             GROUP BY target_order_key HAVING c > 1"""):
        v.append(f"INV-17 활성 취소·정정 {r['c']}개 {r['target_order_key']}")
    return v


TERMINAL = {"FILLED", "CANCELLED", "EXPIRED", "REJECTED", "NOT_SENT", "NOT_ACCEPTED", "DONE"}


def _replay_intent_states(conn: sqlite3.Connection) -> list[str]:
    """감사 이벤트의 의도 상태 기록을 순서대로 재생해 INV-02·INV-11을 검사한다."""
    v: list[str] = []
    meta = {r["intent_id"]: r for r in _rows(conn, "SELECT intent_id, account, instrument, side, request_type FROM intent")}
    state: dict[str, str] = {}
    for e in _rows(conn, "SELECT seq, ref, summary FROM audit_event WHERE kind = 'INTENT_STATE' ORDER BY seq"):
        iid, new = e["ref"], json.loads(e["summary"])["state"]
        m = meta.get(iid)
        if m is None:
            v.append(f"감사 기록의 의도가 없음 {iid}")
            continue
        old = state.get(iid)
        # INV-11 종결 상태 의도는 다시 열리지 않는다
        if old in TERMINAL:
            v.append(f"INV-11 종결 후 상태 변경 {iid} {old}→{new} (seq {e['seq']})")
        if old is None and new == "PENDING_SEND":
            # INV-02 결과 불명이 있는 종목에는 위험 확대 의도를 만들 수 없다. 결과 불명 매도가 있으면 매도도 불가
            unknown = [o for o, s in state.items() if s == "UNKNOWN" and o != iid
                       and meta[o]["account"] == m["account"] and meta[o]["instrument"] == m["instrument"]]
            rt, side = m["request_type"], m["side"]
            increasing = rt in ("ENTRY", "INCREASE") or (rt == "MODIFY" and side == "BUY")
            if unknown and increasing:
                v.append(f"INV-02 결과 불명 중 위험 확대 의도 {iid}")
            if side == "SELL" and rt in NEW_ORDER_TYPES and any(
                    meta[o]["side"] == "SELL" and meta[o]["request_type"] in NEW_ORDER_TYPES for o in unknown):
                v.append(f"INV-02 결과 불명 매도 중 매도 의도 {iid}")
        state[iid] = new
    for iid in meta:
        if iid not in state:
            v.append(f"상태 감사 기록 없는 의도 {iid}")
    return v
