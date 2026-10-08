"""PAPER-0 종단 간 장애 시나리오 2 — test_e2e_paper0_faults가 다루지 않은 빈틈 (CLAUDE.md §8 불변 원칙).

test_e2e_paper0_days의 PaperWorld(fake 브로커)·KiwoomPaperWorld(로컬 kiwoom_sim, 실제 KiwoomAdapter·실시간)를 그대로
쓴다. 실제 조립(build_engine)·실제 설정·Paper0Verify(005930). 네트워크는 127.0.0.1 시뮬레이터뿐.

1. 결과 불명 + 후보 주문 여럿(모호) → K3·사람 대기·반복 없음 → CONFIRM_UNKNOWN(접수, 주문번호 채택) → 체결 → K3 재개
2. kiwoom_sim(실제 어댑터): 매수 미체결 → KILL → 취소 정확히 1회, 시뮬레이터 취소 확인, 추가 주문 없음
3. K3·K4·K5 중 운영 요청 CANCEL / EXIT / ENTRY — 01 §2.6 "불확실할 때 허용" 열 그대로
4. 토큰 발급(au10001) 연속 실패 → K5(link:rest) → 발급 회복 + 대조 성공 → K5 자동 해제, 그동안 주문 없음
5. 부분 체결 잔량 + 미체결 정책 "시스템 기본"(01 §2.7 "장 마감까지 유지") — 하루 동안의 실제 동작
6. 실시간 체결 통보 중복(같은 체결번호 2회, 앞자리 0 개수만 다른 체결번호) → 1회만 반영, K4 없음
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e   # A-084 ⑥: 빠른 확인은 -m "not rehearsal and not e2e"

import tests.scenario.test_e2e_paper0_days as days
from tests.scenario.test_e2e_paper0_days import (
    ACCOUNT, CODE, DAYS, KiwoomPaperWorld, PaperWorld, _wait, buy_sell_of_day, kst, start_and_resume,
)
from tests.scenario.test_e2e_paper0_faults import alerts, audits, halt_rows, last_command, make_root_qty, new_orders
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType

D0 = DAYS[0]
ORDER_APIS = ("kt10000", "kt10001", "kt10002", "kt10003")


# --- 보조 -----------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = PaperWorld(tmp_path)
    yield w
    w.close()


def run_command(w: PaperWorld, cmd: str, args: dict) -> dict:
    w.command(cmd, args)
    w.step()
    return last_command(w)


def sim_calls(w: KiwoomPaperWorld, api: str) -> list[dict]:
    return [body for a, body, _ in w.sim.log if a == api]


class KiwoomOpenWorld(KiwoomPaperWorld):
    """시뮬레이터 자동 체결을 끈 변형 — 접수된(미체결) 주문이 열린 채 남을 수 있게 settle은 전송 확정까지만 기다린다."""

    def make_broker(self):  # type: ignore[no-untyped-def]
        b = super().make_broker()
        self.sim.auto_fill = False
        return b

    def settle(self) -> None:
        def done() -> bool:
            busy = self.repo.select("SELECT 1 FROM intent WHERE state IN ('SENDING','SENT')")
            if busy:
                self.engine.tick()
            return not busy
        assert _wait(done, 5.0), self.intents()[-2:]


# --- 1. 결과 불명 + 후보 여럿 → K3 → 사람 확정(접수·주문번호) → 체결 → 재개 ------------------------------

def test_unknown_ambiguous_candidates_k3_then_confirm_accepted_with_order_no(world: PaperWorld) -> None:
    """09:30 매수: 브로커 접수·응답 유실(LOST_ACCEPT, 미체결). 같은 시각 같은 조건(005930 매수 1주)의 외부 주문이 있어
    조회 후보가 2건 → 해소기 모호 판정(resolver._match exact 2건) → K3 + 긴급 알림 1회, 사람 대기(조회·알림 반복 없음).
    운영자 CONFIRM_UNKNOWN accepted=True + 주문번호 → 의도에 주문 채택 → 실시간 체결로 FILLED → 외부 주문은 K4
    해소(RECONCILE_RESOLVE) → K3·K4 로컬 재개 → 14:30 청산. 같은 의도 재전송 없음."""
    w = world
    start_and_resume(w)
    w.run_until(kst(D0, 9, 29, 30))
    w.broker.behavior = FakeBehavior.LOST_ACCEPT
    w.run_until(kst(D0, 9, 30, 0))
    w.broker.behavior = FakeBehavior.FILL
    (mine,) = w.broker.open_order_nos()                          # 시스템이 보낸 주문 (응답 유실 — 내부는 번호 모름)
    ext = w.broker.add_external_order(ACCOUNT, CODE, "BUY", Decimal("1"))   # 같은 조건의 외부(앱) 주문
    iid = w.intents()[-1]["intent_id"]
    assert w.intents()[-1]["state"] == "UNKNOWN"
    w.run_until(kst(D0, 9, 31))
    assert w.intents()[-1]["state"] == "UNKNOWN"                 # 후보가 여럿이면 하나를 골라 채택하지 않는다
    assert "K3" in w.active(), w.active()
    esc = [e for e in audits(w, "UNKNOWN_ESCALATED") if e["ref"] == iid]
    assert len(esc) == 1
    assert len(alerts(w, "결과 불명 미해소 → K3")) == 1
    assert "일치 2건" in alerts(w, "결과 불명 미해소 → K3")[0]["s"]["message"]
    n_alerts, n_queries = len(alerts(w, "결과 불명")), len([e for e in w.repo.audit_events() if e["ref"] == iid])
    w.run_until(kst(D0, 10, 1))                                  # 30분 사람 대기: 해소기 조회·알림 반복 없음 (RV-08)
    assert len(alerts(w, "결과 불명")) == n_alerts
    assert len([e for e in w.repo.audit_events() if e["ref"] == iid]) == n_queries
    assert len(w.broker.sent) == 1 and w.intents()[-1]["state"] == "UNKNOWN"
    # 사람 확정: 접수, 주문번호 = 시스템 주문 (증권사 앱에서 확인했다고 가정)
    r = run_command(w, "CONFIRM_UNKNOWN", {"intent_id": iid, "accepted": True, "order_no": mine})
    assert r["status"] == "DONE", r
    assert w.intents()[-1]["state"] == "ACCEPTED", w.intents()
    w.broker.fill(mine)                                          # 이후 체결 → 실시간 통보로 반영
    w.run_until(kst(D0, 10, 3))
    assert w.intents()[-1]["state"] == "FILLED" and w.position() == 1, w.intents()
    # 대조: 결과 불명 중에는 같은 조건의 두 주문 모두 해소기에 맡겨지고(3차 검토 F-3), 확정 뒤 우리 주문은
    # 알려진 주문이 된다 → 외부 주문만 미확인 브로커 주문 → K4
    assert "K4" in w.active()
    items = w.repo.open_recon_items(ACCOUNT)
    assert sorted((i["kind"], i["ref"]) for i in items) == [("UNKNOWN_BROKER_ORDER", ext)], items
    for it in items:
        r = run_command(w, "RECONCILE_RESOLVE", {"item_id": it["item_id"], "method": "EXTERNAL"})
        assert r["status"] == "DONE", r
        if it["ref"] == mine:                                    # 채택된 우리 주문 — 원장 변경 없음
            assert "이미 기록된 주문" in r["result"], r
    w.run_until(kst(D0, 10, 6))
    for k in ("K4", "K3"):
        r = run_command(w, "RESUME", {"account": None, "halt_type": k})
        assert r["status"] == "DONE", (k, r, halt_rows(w, k))
    assert w.active() == set(), w.active()
    assert len(new_orders(w, "BUY")) == 1                        # 같은 의도 재전송 없음
    w.run_until(kst(D0, 14, 31))
    assert buy_sell_of_day(w, D0) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert w.position() == 0 and w.repo.active_reservations(ACCOUNT) == []


# --- 2. kiwoom_sim: 매수 미체결 → KILL → 취소 정확히 1회 ------------------------------------------------

@pytest.fixture
def oworld(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = KiwoomOpenWorld(tmp_path)
    yield w
    w.close()


def test_kiwoom_sim_kill_cancels_open_buy_once(oworld: KiwoomOpenWorld) -> None:
    """01 §2.6 K1: '매수 미체결 취소, 보유 유지(자동 청산 없음)'. 실제 KiwoomAdapter → kt10003 1회 →
    시뮬레이터 '확인' 매수취소 통보 → 의도 CANCELLED. K1 중 14:30 판정·주문 없음."""
    w = oworld
    start_and_resume(w)
    w.run_until(kst(D0, 9, 31))
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "ACCEPTED")], w.intents()
    (sim_no,) = [no for no, o in w.sim.orders.items() if o.open]
    assert w.sent_count() == 1
    w.command("KILL", {"account": None, "reason": "시험 킬"})
    w.step()
    assert "K1" in w.active()
    def entry_state() -> str:
        return [i for i in w.intents() if i["request_type"] == "ENTRY"][0]["state"]

    assert _wait(lambda: (w.engine.tick() or True) and entry_state() == "CANCELLED", 5.0), \
        (w.intents(), [a for a, _, _ in w.sim.log if a in ORDER_APIS])
    # 정지 정책의 매수 미체결 취소도 중앙 경로(게이트 → 의도)를 거친다 (01 §2.6 아래 주석)
    assert [(i["request_type"], i["side"]) for i in w.intents()] == [("ENTRY", "BUY"), ("CANCEL", "BUY")]
    cancels = sim_calls(w, "kt10003")
    assert len(cancels) == 1 and cancels[0]["orig_ord_no"] == sim_no, cancels
    assert not w.sim.orders[sim_no].open
    assert [o.kind for o in w.sim.orders.values()] == ["NEW", "CANCEL"]
    w.run_until(kst(D0, 14, 31))
    assert [a for a, _, _ in w.sim.log if a in ORDER_APIS] == ["kt10000", "kt10003"]   # 추가 주문·추가 취소 없음
    assert w.position() == 0 and w.repo.active_reservations(ACCOUNT) == []
    assert w.repo.open_orders(ACCOUNT) == []
    assert "K1" in w.active() and len(halt_rows(w, "K1")) == 1
    assert w.crit == []


# --- 3. K3·K4·K5 중 운영 요청 (01 §2.6 "불확실할 때 허용" 열) ---------------------------------------------

def _hold_with_open_sell(w: PaperWorld) -> str:
    """보유 1주 + 14:30 청산 매도가 미체결(접수만)인 상태를 만든다. 열린 매도 주문 키를 돌려준다."""
    start_and_resume(w)
    w.run_until(kst(D0, 9, 31))
    assert w.position() == 1
    w.run_until(kst(D0, 14, 29, 30))
    w.broker.behavior = FakeBehavior.ACCEPT_ONLY
    w.run_until(kst(D0, 14, 30, 30))
    w.broker.behavior = FakeBehavior.FILL
    (o,) = w.repo.open_orders(ACCOUNT)
    assert o.side == "SELL" and o.instrument == CODE
    return o.order_key


def _operator_requests(w: PaperWorld, order_key: str) -> dict[str, dict]:
    """운영 요청 3종을 차례로 낸다: 신규 진입(ENTRY) → 청산(EXIT 1주) → 취소(열린 매도)."""
    out = {}
    out["ENTRY"] = run_command(w, "ENTRY", {"account": ACCOUNT, "instrument": CODE, "quantity": 1})
    out["EXIT"] = run_command(w, "EXIT", {"account": ACCOUNT, "instrument": CODE, "quantity": 1})
    out["CANCEL"] = run_command(w, "CANCEL", {"order_key": order_key})
    return out


def _assert_entry_rejected(r: dict) -> None:
    # 01 §2.9·R-21: 운영 요청은 취소·감액·청산만 — 신규 진입은 명령 단계에서 거부 (게이트·의도까지 가지 않음)
    assert r["status"] == "REJECTED" and "알 수 없는 명령" in r["result"], r


def _sent_kinds(w: PaperWorld) -> list[str]:
    return [f"{r.kind}:{r.side}" for r in w.broker.sent]


@pytest.mark.parametrize("scope", [CODE, "000660"], ids=["same-instrument", "other-instrument"])
def test_operator_requests_under_k3(world: PaperWorld, scope: str) -> None:
    """01 §2.6 K3 행 "불확실할 때 허용 = 해당 종목 외의 취소만", 신규·증액 차단.
    K3 범위(결과 불명 종목)는 주입한다: scope=005930이면 005930 취소 거부, scope=000660이면 005930 취소 허용.
    (K3 발동 자체의 실제 경로는 1번 시험·test_e2e_paper0_faults가 덮는다.)"""
    w = world
    key = _hold_with_open_sell(w)
    w.engine.halts.activate(HaltType.K3, "resolver", "시험 주입: 결과 불명 미해소", ACCOUNT, instrument=scope)
    assert "K3" in w.active()
    sent0 = len(w.broker.sent)
    r = _operator_requests(w, key)
    _assert_entry_rejected(r["ENTRY"])
    # K3 행: 청산(위험 축소)은 허용 목록에 없음 → G2 거부
    assert r["EXIT"]["status"] == "REJECTED" and "G2" in r["EXIT"]["result"], r["EXIT"]
    if scope == CODE:
        # K3 행 "해당 종목 외의 취소만" → 결과 불명 종목(005930)의 취소는 거부
        assert r["CANCEL"]["status"] == "REJECTED" and "G2" in r["CANCEL"]["result"], r["CANCEL"]
        assert len(w.broker.sent) == sent0
        assert len(w.repo.open_orders(ACCOUNT)) == 1
    else:
        # K3 행 "해당 종목 외의 취소만" → 다른 종목(005930) 주문 취소는 허용·전송
        assert r["CANCEL"]["status"] == "DONE", r["CANCEL"]
        assert _sent_kinds(w)[sent0:] == ["CANCEL:SELL"]
        assert w.repo.open_orders(ACCOUNT) == []
    assert w.position() == 1                                     # 정지는 청산이 아님 (§8-8)
    assert len(alerts(w, "운영 요청 거부")) == (2 if scope == CODE else 1)   # 위험 축소 거부는 알림 (01 §2.5)


def test_operator_requests_under_k4(world: PaperWorld) -> None:
    """01 §2.6 K4 행 "불확실할 때 허용 = 취소만". 실제 경로: 외부 수동 매수(000660) → 재조회 후 K4."""
    w = world
    key = _hold_with_open_sell(w)
    ext = w.broker.add_external_order(ACCOUNT, "000660", "BUY", Decimal("1"))
    w.broker.fill(ext)
    w.run_until(kst(D0, 14, 33))
    assert "K4" in w.active(), w.active()
    sent0 = len(w.broker.sent)
    r = _operator_requests(w, key)
    _assert_entry_rejected(r["ENTRY"])
    assert r["EXIT"]["status"] == "REJECTED" and "G2" in r["EXIT"]["result"], r["EXIT"]   # K4 행: 청산 불허
    assert r["CANCEL"]["status"] == "DONE", r["CANCEL"]                                  # K4 행: 취소만 허용
    assert _sent_kinds(w)[sent0:] == ["CANCEL:SELL"]
    w.step()
    assert w.repo.open_orders(ACCOUNT) == [] and w.position() == 1
    assert "K4" in w.active()


def test_operator_requests_under_k5(world: PaperWorld) -> None:
    """01 §2.6 K5 행 "불확실할 때 허용 = 없음 (연결 복구 후 대조)". 실제 경로: 브로커 단절 → 대조 조회 연속 3회 실패 → K5.
    주: 01 §2.5 '취소' 행의 '정지 중: 허용'과 §2.6 K5 '없음'이 겹친다 — 구현·이 시험은 §2.6(정지별 표)을 따른다."""
    w = world
    key = _hold_with_open_sell(w)
    w.broker.disconnected = True
    w.run_until(kst(D0, 14, 35))
    assert "K5" in w.active(), w.active()
    sent0 = len(w.broker.sent)
    r = _operator_requests(w, key)
    _assert_entry_rejected(r["ENTRY"])
    assert r["EXIT"]["status"] == "REJECTED" and "G2" in r["EXIT"]["result"], r["EXIT"]
    assert r["CANCEL"]["status"] == "REJECTED" and "G2" in r["CANCEL"]["result"], r["CANCEL"]
    assert len(w.broker.sent) == sent0
    w.broker.disconnected = False
    w.run_until(kst(D0, 14, 38))
    assert "K5" not in w.active(), w.active()                   # 연결 복구 + 대조 일치 → 자동 재개
    assert run_command(w, "CANCEL", {"order_key": key})["status"] == "DONE"
    assert _sent_kinds(w)[sent0:] == ["CANCEL:SELL"]
    assert w.position() == 1


# --- 4. 토큰 발급 실패 → K5 → 회복 ------------------------------------------------------------------

@pytest.fixture
def kworld(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = KiwoomPaperWorld(tmp_path)
    yield w
    w.close()


def test_kiwoom_sim_token_issue_failures_k5_then_recover(kworld: KiwoomPaperWorld) -> None:
    """09:15 토큰 무효화 + au10001 연속 실패 → 조회 전송 전 실패(대조 QUERY_FAILED) 3회 → K5(link:rest).
    09:30 판정은 전역 정지로 건너뜀, 주문 전송 없음. 발급 회복 → 대조 성공 → K5 자동 해제(released_by=auto) → 14:30 진입."""
    w = kworld
    tokens = w.broker.tokens
    tokens._sleep = lambda s: None  # noqa: SLF001 — 발급 재시도 대기(1·2초)만 생략, 시도 횟수는 그대로
    start_and_resume(w)
    w.run_until(kst(D0, 9, 15))
    issued0 = len(sim_calls(w, "au10001"))
    w.sim.token_fail = 10 ** 6
    w.sim.valid_tokens.clear()                                   # 서버 쪽 토큰 만료 → 8005 → 어댑터 무효화·재발급 시도
    tokens.invalidate()
    w.run_until(kst(D0, 9, 40))
    assert len(sim_calls(w, "au10001")) - issued0 >= 3
    assert "K5" in w.active(), (w.active(), w.crit)
    k5 = halt_rows(w, "K5")
    assert len(k5) == 1 and "link:rest" in json.loads(k5[0]["conditions"]), k5
    assert w.sent_count() == 0 and w.intents() == []             # 장애 중 주문 없음
    assert w.decisions() == []                                   # 09:30 판정은 전역 정지로 건너뜀
    w.sim.token_fail = 0
    w.run_until(kst(D0, 9, 43))
    k5 = w.repo.select("SELECT state, released_by FROM halt WHERE halt_type='K5'")
    assert "K5" not in w.active() and k5[-1]["released_by"] == "auto", (k5, w.active())
    ok = w.repo.select("SELECT created_at FROM reconciliation WHERE result='MATCH' ORDER BY created_seq")
    assert ok and datetime.fromisoformat(ok[-1]["created_at"]) >= kst(D0, 9, 40)   # 해제 근거 = 회복 뒤 대조 일치
    w.run_until(kst(D0, 14, 31))
    assert buy_sell_of_day(w, D0) == [("ENTRY", "FILLED")]
    assert w.sent_count() == 1


# --- 5. 부분 체결 잔량 + 미체결 정책 "시스템 기본" ------------------------------------------------------

def test_partial_fill_remainder_system_default_policy_over_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """01 §2.7 부분 체결 행: '체결분 반영, 잔량은 미체결 정책 또는 시스템 기본: 장 마감까지 유지'.
    paper0-verify는 CAP-ORD-03(미체결 정책) 미선언 → 시스템 기본. 3주 매수 중 1주 체결, 브로커는 잔량을 소멸시키지 않음.
    관찰: 잔량 2주는 장 마감까지 열린 채(자동 취소·정정 없음), 예약 유지 → 01 문구와 일치.
    14:30에는 청산하지 않는다 — 승인 전략 규칙 EXIT-1 "미체결 없음" 조건(paper0_verify.SPEC)으로 신호 없음 → 보유 1주를 안고
    하루를 넘긴다 (자동 청산 없음과 일관)."""
    monkeypatch.setattr(days, "make_root", make_root_qty(3))
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(D0, 9, 29, 30))
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY
        w.run_until(kst(D0, 9, 30))
        w.broker.behavior = FakeBehavior.FILL
        (no,) = w.broker.open_order_nos()
        w.broker.fill(no, Decimal("1"))
        w.run_until(kst(D0, 14, 29))
        assert w.intents()[-1]["state"] == "PARTIAL" and w.position() == 1
        assert len(w.repo.active_reservations(ACCOUNT)) == 1
        assert [r.kind for r in w.broker.sent] == ["NEW"]          # 장중 자동 취소·정정 없음 = "장 마감까지 유지"
        w.run_until(kst(D0, 14, 31))
        assert new_orders(w, "SELL") == []                           # EXIT-1: 미체결 있음 → 청산 신호 없음
        evs = w.evaluations()
        assert [e["s"]["signals"] for e in evs] == [1, 0], [e["s"] for e in evs]
        assert [r.kind for r in w.broker.sent] == ["NEW"]
        w.run_until(kst(D0, 15, 30))
        assert w.broker.open_order_nos() == [no]                     # 장 마감 시점까지 잔량 유지 (시스템이 취소하지 않음)
        assert w.active() == set(), w.active()
    finally:
        w.close()


def test_partial_fill_remainder_silently_expired_at_close_closes_intent_without_k4(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(days, "make_root", make_root_qty(3))
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(D0, 9, 29, 30))
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY
        w.run_until(kst(D0, 9, 30))
        w.broker.behavior = FakeBehavior.FILL
        (no,) = w.broker.open_order_nos()
        w.broker.fill(no, Decimal("1"))
        w.run_until(kst(D0, 15, 30))
        w.broker._set(no, state="EXPIRED")  # noqa: SLF001 — 브로커 마감 소멸, 실시간 통보 없음 (K-U3 미확인 쪽)
        w.run_until(kst(D0, 15, 45))                                 # 마감 후 대조 1회
        buy = [i for i in w.intents() if i["request_type"] == "ENTRY"][0]
        assert buy["state"] == "EXPIRED", w.intents()
        assert "K4" not in w.active(), (w.active(), w.repo.open_recon_items(ACCOUNT))
    finally:
        w.close()


def test_partial_fill_remainder_still_open_after_close_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(days, "make_root", make_root_qty(3))
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(D0, 9, 29, 30))
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY
        w.run_until(kst(D0, 9, 30))
        w.broker.behavior = FakeBehavior.FILL
        (no,) = w.broker.open_order_nos()
        w.broker.fill(no, Decimal("1"))
        w.run_until(kst(D0, 15, 30))
        n = len(alerts(w))
        w.run_until(kst(D0, 16, 1))                                  # 마감 후 대조 1회 + 일일 보고
        after = [a["s"]["message"] for a in alerts(w)][n:]
        assert any("미체결" in m for m in after), after
    finally:
        w.close()


def test_partial_fill_remainder_never_expired_by_broker_next_day_k4_stale(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """03 §6.4 둘째 줄 '남아 있으면 알림하고 다음 거래일 시작 전 사용자 확인 대상'. 브로커가 잔량 소멸을 전혀 알리지 않으면
    (fake: 다음 거래일 당일 조회에 전일 주문이 보이지 않음) 다음 날 장중 대조가 STALE_ORDER → K4 → 진입 없음.
    마감 후(전일) 알림이 있었는지도 기록한다."""
    monkeypatch.setattr(days, "make_root", make_root_qty(3))
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(D0, 9, 29, 30))
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY
        w.run_until(kst(D0, 9, 30))
        w.broker.behavior = FakeBehavior.FILL
        (no,) = w.broker.open_order_nos()
        w.broker.fill(no, Decimal("1"))
        w.run_until(kst(D0, 16, 1))
        assert w.active() == set(), w.active()                       # 전일 마감 후 대조: 브로커도 열림 → 일치
        w.run_until(kst(DAYS[1], 9, 31))
        items = w.repo.open_recon_items(ACCOUNT)
        assert "K4" in w.active(), w.active()
        assert any(i["kind"] == "STALE_ORDER" for i in items), items
        assert buy_sell_of_day(w, DAYS[1]) == []                     # K4 중 진입 없음
    finally:
        w.close()


# --- 6. 실시간 체결 통보 중복 (kiwoom_sim, 실제 파서) ------------------------------------------------------

def test_kiwoom_sim_duplicate_fill_notifications_counted_once(kworld: KiwoomPaperWorld) -> None:
    """같은 체결(909)을 두 번 더, 그리고 앞자리 0 개수만 다른 체결번호로 한 번 더 보낸다 → parse.fill_no 정규화 →
    executor 체결 키 중복 제거 → 체결 1건, 보유 1주, K4·이상 통보 없음."""
    w = kworld
    start_and_resume(w)
    w.run_until(kst(D0, 9, 31))
    assert w.position() == 1
    (no,) = list(w.sim.orders)
    o = w.sim.orders[no]
    (_, cntr, q, px, _tm) = w.sim.fills[0]
    assert cntr == "0000001"

    def push(fill_no: str) -> None:
        w.sim._push00(o, "체결", "+매수", {"909": fill_no, "910": str(px), "911": str(o.filled),  # noqa: SLF001
                                          "914": str(px), "915": str(q), "902": "0"})

    seen: list = []
    real_poll = w.broker.poll_events

    def poll():  # type: ignore[no-untyped-def]
        evs = real_poll()
        seen.extend(evs)
        return evs

    w.broker.poll_events = poll  # type: ignore[method-assign] — 실행기에 실제로 전달된 통보 기록 (동작은 그대로)
    for fill_no in (cntr, cntr, "1", "001"):
        push(fill_no)
    n_fill_audit = len(audits(w, "FILL"))
    assert _wait(lambda: w.sim.ws_out.empty(), 3.0)

    def delivered() -> bool:
        w.engine.executor.process_events()
        return len([e for e in seen if e.kind == "FILL"]) >= 4
    assert _wait(delivered, 5.0), seen
    # 중복 통보 4건이 실제로 실행기까지 왔고, 체결번호는 모두 같은 값으로 정규화됨 (parse.fill_no)
    assert {e.fill_id for e in seen if e.kind == "FILL"} == {"0000001"}, seen
    w.run_until(kst(D0, 9, 35))
    fills = w.repo.select("SELECT fill_key, quantity FROM fill")
    assert len(fills) == 1, fills
    assert len(audits(w, "FILL")) == n_fill_audit
    assert w.position() == 1
    assert w.active() == set(), (w.active(), w.repo.open_recon_items(ACCOUNT))
    assert audits(w, "EVENT_ANOMALY") == [] and audits(w, "LATE_FILL") == []
    w.run_until(kst(D0, 14, 31))
    assert buy_sell_of_day(w, D0) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert w.position() == 0 and w.crit == []


def test_fake_duplicate_fill_event_same_id_counted_once(world: PaperWorld) -> None:
    """fake 브로커: 대기 통보를 그대로 중복(duplicate_events) → 같은 체결번호 2회 → 1회 반영, K4 없음."""
    from trading_system.domain.models import OrderEvent
    w = world
    start_and_resume(w)
    w.run_until(kst(D0, 9, 29, 30))
    w.broker.behavior = FakeBehavior.ACCEPT_ONLY
    w.run_until(kst(D0, 9, 30))
    w.broker.behavior = FakeBehavior.FILL
    (no,) = w.broker.open_order_nos()
    w.broker.fill(no)
    w.broker.duplicate_events()
    w.step()
    w.broker.inject(OrderEvent(kind="FILL", order_no=no, fill_id="F0000001", quantity=Decimal("1"),
                               price=Decimal("70000")))
    w.run_until(kst(D0, 9, 35))
    assert len(w.repo.select("SELECT 1 FROM fill")) == 1
    assert w.position() == 1 and w.active() == set(), (w.active(), w.repo.open_recon_items(ACCOUNT))
    assert w.intents()[-1]["state"] == "FILLED"
