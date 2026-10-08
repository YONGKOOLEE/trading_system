"""PAPER-0 종단 간 장애 시나리오 — paper0-verify 하루 흐름에 장애를 주입한다 (CLAUDE.md §8 불변 원칙 확인).

test_e2e_paper0_days의 PaperWorld(fake 브로커)·KiwoomPaperWorld(로컬 kiwoom_sim)를 그대로 쓴다.
실제 조립(build_engine)·실제 설정·Paper0Verify(005930). 네트워크는 127.0.0.1 시뮬레이터뿐.

확인하는 불변 원칙:
- 같은 의도로 중복 주문 없음, 결과 불명 중 재전송 없음(§8-5·6), 주문 변경 자동 재시도 없음(§8-7)
- K8/K5/K3/K4/K1 중 위험 확대 주문 없음, 정지 유형별 허용(01 §2.6), 정지는 청산이 아님(§8-8)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.scenario.test_e2e_paper0_days as days
from tests.scenario.test_e2e_paper0_days import (
    ACCOUNT, CODE, DAYS, KiwoomPaperWorld, PaperWorld, _wait, buy_sell_of_day, kst, start_and_resume,
)
from trading_system.broker.fake.broker import FakeBehavior

D0 = DAYS[0]


# --- 보조 -----------------------------------------------------------------------------------------

def alerts(w: PaperWorld, needle: str = "") -> list[dict]:
    out = []
    for e in w.repo.audit_events():
        if e["kind"] == "ALERT":
            s = json.loads(e["summary"])
            if needle in s.get("message", ""):
                out.append(dict(e, s=s))
    return out


def audits(w: PaperWorld, kind: str, ref: str | None = None) -> list[dict]:
    return [e for e in w.repo.audit_events() if e["kind"] == kind and (ref is None or e["ref"] == ref)]


def halt_rows(w: PaperWorld, k: str) -> list[dict]:
    return [r for r in w.repo.halt_rows(None) if r["halt_type"] == k]


def new_orders(w: PaperWorld, side: str | None = None) -> list:
    return [r for r in w.broker.sent if r.kind == "NEW" and (side is None or r.side == side)]


def last_command(w: PaperWorld) -> dict:
    return w.repo.select("SELECT command, status, result FROM ops_command ORDER BY created_seq DESC LIMIT 1")[0]


def entry_with(w: PaperWorld, behavior: FakeBehavior, d: date = D0) -> None:
    """09:30 판정 직전에 브로커 동작을 바꾸고, 매수 1건 전송 직후 정상(FILL)으로 되돌린다."""
    w.run_until(kst(d, 9, 29, 30))
    assert w.sent_count() == 0 or d != D0
    w.broker.behavior = behavior
    w.run_until(kst(d, 9, 30, 0))
    assert len(new_orders(w, "BUY")) >= 1
    w.broker.behavior = FakeBehavior.FILL


def make_root_qty(qty: int):  # type: ignore[no-untyped-def]
    """paper0-verify 수량만 바꾼 임시 저장소 루트 (원본 설정 파일은 건드리지 않음 — 복사본만)."""
    orig = days.make_root

    def mk(tmp: Path) -> Path:
        # make_root는 복사 직후 등록하므로, 등록 전에 복사본을 바꾸기 위해 원본 경로 대신 임시 원본을 쓴다
        src = tmp / "src-root"
        for rel in ("config/app.paper.toml", "config/limits.paper.toml", "config/market.paper.toml",
                    "config/strategies/paper0-verify-v1.toml", "docs/capabilities.md"):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            text = (days.ROOT / rel).read_text(encoding="utf-8")
            if rel.endswith("paper0-verify-v1.toml"):
                assert "qty = 1 " in text
                text = text.replace("qty = 1 ", f"qty = {qty} ", 1)
            (src / rel).write_text(text, encoding="utf-8")
        saved = days.ROOT
        days.ROOT = src
        try:
            return orig(tmp)
        finally:
            days.ROOT = saved
    return mk


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = PaperWorld(tmp_path)
    yield w
    w.close()


# --- 1. 09:30 매수 응답 유실 ------------------------------------------------------------------------

# (수정됨: executor._apply_fill이 중복 체결을 수량 초과 검사보다 먼저 거른다 — 이전 xfail 결함 1)
def test_lost_response_found_by_query_no_second_buy_then_exit(world: PaperWorld) -> None:
    """브로커는 접수·체결, 응답 유실 → UNKNOWN → 조회로 FOUND → 재전송 없음 → 14:30 청산."""
    w = world
    start_and_resume(w)
    entry_with(w, FakeBehavior.LOST_FILL)
    its = w.intents()
    assert [(i["request_type"], i["state"]) for i in its] == [("ENTRY", "UNKNOWN")]
    w.run_until(kst(D0, 9, 32))
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "FILLED")]
    assert w.position() == 1
    assert len(new_orders(w, "BUY")) == 1                      # 같은 의도 재전송 없음
    assert "K3" not in w.active() and "K4" not in w.active(), w.active()
    w.run_until(kst(D0, 14, 31))
    assert buy_sell_of_day(w, D0) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert [r.side for r in new_orders(w)] == ["BUY", "SELL"]
    assert w.position() == 0 and w.repo.active_reservations(ACCOUNT) == []


def test_lost_response_found_without_realtime_fill_then_exit(world: PaperWorld) -> None:
    """위 결함과 분리: 실시간 체결 통보까지 유실되면(조회 체결만) FOUND → FILLED → 재전송 없음 → 14:30 청산."""
    w = world
    start_and_resume(w)
    entry_with(w, FakeBehavior.LOST_FILL)
    w.broker.drop_events()
    w.run_until(kst(D0, 9, 32))
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "FILLED")]
    assert w.position() == 1 and w.active() == set(), w.active()
    w.run_until(kst(D0, 14, 31))
    assert buy_sell_of_day(w, D0) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert [r.side for r in new_orders(w)] == ["BUY", "SELL"]
    assert w.repo.active_reservations(ACCOUNT) == []


def test_kiwoom_sim_lost_order_response_resolved_by_query_and_ws(kworld: KiwoomPaperWorld) -> None:
    """시뮬레이터가 매수(kt10000)를 처리하되 응답은 HTTP 500 → UNKNOWN → 조회로 주문 채택(키움: 조회 체결 미신뢰, RV-14)
    + 실시간 00 체결 통보로 FILLED. 재전송 없음, 14:30 청산."""
    from tests.support.kiwoom_sim import Scripted
    w = kworld
    start_and_resume(w)
    w.run_until(kst(D0, 9, 29, 30))
    w.sim.script("kt10000", Scripted("status", status=500, apply=True))
    w.clock._now = kst(D0, 9, 30)  # noqa: SLF001
    w.poller.poll_once()
    w.engine.tick()                                            # settle()은 UNKNOWN을 기다리지 않음 — 직접
    assert w.intents()[-1]["state"] in ("UNKNOWN", "FILLED"), w.intents()
    w.run_until(kst(D0, 9, 33))
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "FILLED")], w.intents()
    assert w.sent_count() == 1 and w.active() == set(), (w.active(), w.crit, [(i.get('kind'), i.get('instrument'), i.get('detail') or i.get('summary')) for i in w.repo.open_recon_items(ACCOUNT)], w.repo.select("SELECT at, trigger, result FROM reconciliation ORDER BY at")[-4:],
                                                          w.repo.select("SELECT kind, summary FROM audit_event WHERE kind IN ('UNMATCHED_EVENT','EVENT_ANOMALY','LATE_FILL','FILL') ORDER BY seq"))
    w.run_until(kst(D0, 14, 31))
    assert buy_sell_of_day(w, D0) == [("ENTRY", "FILLED"), ("EXIT", "FILLED")]
    assert w.sent_count() == 2 and w.position() == 0


def test_lost_response_five_clean_queries_escalates_k3_until_human(world: PaperWorld) -> None:
    """브로커가 받지 않았고 응답도 없음 → 조회 5회(2·4·8·16·32초) 모두 성공·후보 없음 → K3·사람 확인, 재전송 없음.
    요구사항 변경 A-084/CP-031: 자동 NOT_ACCEPTED 종결(예약 해제·재진입 허용)을 하지 않는다 — 접수 반영 지연 시 이중 포지션 위험."""
    w = world
    start_and_resume(w)
    entry_with(w, FakeBehavior.UNKNOWN)
    assert w.intents()[-1]["state"] == "UNKNOWN"
    iid = w.intents()[-1]["intent_id"]
    w.run_until(kst(D0, 9, 31, 0))                             # 대기 2·4·8·16·32초 + 30초 주기: 아직 결과 불명
    assert w.intents()[-1]["state"] == "UNKNOWN"
    w.run_until(kst(D0, 9, 35))
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "UNKNOWN")]
    assert w.position() == 0 and "K3" in w.active(), w.active()
    assert len(alerts(w, "결과 불명: 조회 5회 주문 없음 → K3")) == 1
    assert len([e for e in audits(w, "UNKNOWN_ESCALATED") if e["ref"] == iid]) == 1
    n_alerts = len(alerts(w, "결과 불명"))
    w.run_until(kst(D0, 14, 31))                                # 14:30 판정까지 사람 대기: 조회·알림 반복 없음
    assert len(alerts(w, "결과 불명")) == n_alerts, alerts(w, "결과 불명")
    assert len([e for e in audits(w, "UNKNOWN_ESCALATED") if e["ref"] == iid]) == 1
    assert len(w.broker.sent) == 1                              # 같은 의도 재발주 없음, 새 신호 진입도 없음
    assert w.repo.active_reservations(ACCOUNT) != []            # 예약 유지
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "UNKNOWN")]
    # 사람 확정(미접수) → 종결·예약 해제 → 대조 일치 뒤 로컬 RESUME
    w.command("CONFIRM_UNKNOWN", {"intent_id": iid, "accepted": False})
    w.step()
    assert last_command(w)["status"] == "DONE", last_command(w)
    assert w.intents()[-1]["state"] == "NOT_ACCEPTED"
    assert w.repo.active_reservations(ACCOUNT) == []
    w.run_until(kst(D0, 14, 36))                                # 확정 뒤 주기 대조 일치
    w.command("RESUME", {"account": None, "halt_type": "K3"})
    w.step()
    assert "K3" not in w.active(), (last_command(w), halt_rows(w, "K3"))
    assert len(w.broker.sent) == 1 and w.position() == 0


def test_lost_response_ambiguous_k3_no_spam_then_operator_confirm(world: PaperWorld) -> None:
    """조회 실패가 섞여 해소 불가 → K3·사람 대기. 30분 동안 해소기 조회·알림 반복 없음(RV-08).
    CONFIRM_UNKNOWN(미접수) → K3 재개 조건(대조 일치 + 결과 불명 없음) → 로컬 RESUME."""
    w = world
    start_and_resume(w)
    w.run_until(kst(D0, 9, 29, 30))
    w.broker.behavior = FakeBehavior.UNKNOWN
    w.run_until(kst(D0, 9, 30, 0))
    w.broker.behavior = FakeBehavior.FILL
    w.broker.query_failures = 1      # 다음 tick 첫 조회 = 해소기 1회차(resolve 단계가 대조보다 먼저) → "조회 실패 섞임"
    iid = w.intents()[-1]["intent_id"]
    w.run_until(kst(D0, 9, 35))                                 # 5회차 판정 ≈ 09:33:30 (30초 주기)
    assert [e for e in audits(w, "QUERY_FAILED") if e["ref"] == iid], "해소기 1회차 조회 실패가 주입되지 않음"
    assert w.intents()[-1]["state"] == "UNKNOWN"
    assert "K3" in w.active(), (w.active(), [a["kind"] for a in w.repo.audit_events()][-20:])
    n_alerts = len(alerts(w, "결과 불명"))
    n_resolver_audits = len([e for e in w.repo.audit_events() if e["ref"] == iid])
    assert len(alerts(w, "결과 불명 미해소 → K3")) == 1          # (발생 알림·K3 발동 알림·K3 이관 알림 각 1회)
    w.run_until(kst(D0, 10, 3))                                 # 30분 사람 대기
    assert len(alerts(w, "결과 불명")) == n_alerts, alerts(w, "결과 불명")
    assert len([e for e in w.repo.audit_events() if e["ref"] == iid]) == n_resolver_audits
    assert len(w.broker.sent) == 1 and w.intents()[-1]["state"] == "UNKNOWN"   # 재전송 없음
    # 사람 확정 전에는 재개 불가
    w.command("RESUME", {"account": None, "halt_type": "K3"})
    w.step()
    assert "K3" in w.active()
    w.command("CONFIRM_UNKNOWN", {"intent_id": iid, "accepted": False})
    w.step()
    assert last_command(w)["status"] == "DONE", last_command(w)
    assert w.intents()[-1]["state"] == "NOT_ACCEPTED"
    w.run_until(kst(D0, 10, 5))                                 # 확정 뒤 주기 대조 일치
    w.command("RESUME", {"account": None, "halt_type": "K3"})
    w.step()
    assert "K3" not in w.active(), (last_command(w), halt_rows(w, "K3"))
    assert len(w.broker.sent) == 1 and w.position() == 0
    w.run_until(kst(D0, 14, 31))                                # 14:30 새 신호로 진입 (전략 규칙: 보유 없음 → 진입)
    its = w.intents()
    assert [(i["request_type"], i["state"]) for i in its] == [("ENTRY", "NOT_ACCEPTED"), ("ENTRY", "FILLED")]
    assert its[0]["idem_key"] != its[1]["idem_key"] and len(w.broker.sent) == 2


# --- 2. 매수 부분 체결 -----------------------------------------------------------------------------

def test_partial_fill_then_exit_sells_only_held_qty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(days, "make_root", make_root_qty(3))
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        entry_with(w, FakeBehavior.ACCEPT_ONLY)
        buy = new_orders(w, "BUY")
        assert len(buy) == 1 and buy[0].quantity == 3
        (no,) = w.broker.open_order_nos()
        w.broker.fill(no, Decimal("1"))                        # 3주 중 1주 체결
        w.run_until(kst(D0, 9, 31))
        assert w.intents()[-1]["state"] == "PARTIAL", w.intents()
        assert w.position() == 1
        res = w.repo.active_reservations(ACCOUNT)
        assert len(res) == 1, res                               # 미체결 잔량 2주 예약 유지
        w.run_until(kst(D0, 10, 0))
        assert w.intents()[-1]["state"] == "PARTIAL"            # 미체결 정책 "시스템 기본" = 자동 취소 없음
        assert w.broker.expire_open() == [no]                   # 잔량 소멸 통보 (브로커 측)
        w.run_until(kst(D0, 10, 1))
        assert w.intents()[-1]["state"] == "EXPIRED", w.intents()
        assert w.repo.active_reservations(ACCOUNT) == []         # 잔량 예약 해제
        assert w.position() == 1
        w.run_until(kst(D0, 14, 31))
        sells = new_orders(w, "SELL")
        assert len(new_orders(w, "BUY")) == 1                   # 추가 매수 없음
        assert [s.quantity for s in sells] == [Decimal("1")], (sells, w.decisions()[-5:], w.evaluations()[-1])
        assert w.position() == 0
    finally:
        w.close()


# --- 3. 브로커 거부 ----------------------------------------------------------------------------------

def test_broker_reject_no_retry_no_surge_alert(world: PaperWorld) -> None:
    w = world
    start_and_resume(w)
    entry_with(w, FakeBehavior.REJECT)
    w.run_until(kst(D0, 14, 29))
    assert [(i["request_type"], i["state"]) for i in w.intents()] == [("ENTRY", "REJECTED")]
    assert len(w.broker.sent) == 1                              # 재시도 없음
    assert w.position() == 0 and w.repo.active_reservations(ACCOUNT) == []
    assert alerts(w, "거부 급증") == []
    assert w.active() == set(), w.active()
    w.run_until(kst(D0, 14, 31))                                # 14:30 새 신호(새 멱등키) 진입은 정상
    its = w.intents()
    assert [(i["request_type"], i["state"]) for i in its] == [("ENTRY", "REJECTED"), ("ENTRY", "FILLED")]
    assert its[0]["idem_key"] != its[1]["idem_key"]


# --- 4. REST 조회 장애 (로컬 시뮬레이터 5xx) ------------------------------------------------------

QUERY_APIS = {"kt00009", "ka10075", "kt00018", "kt00001", "kt00005"}


def rest_outage(w: KiwoomPaperWorld) -> dict:
    """시뮬레이터 조회 API만 HTTP 500 (주문 API·토큰은 정상). flag["on"]으로 켜고 끈다."""
    flag = {"on": False, "hits": 0}
    real = w.sim._handle  # noqa: SLF001

    def handle(api: str, body: dict, hdr: dict):  # type: ignore[no-untyped-def]
        if flag["on"] and api in QUERY_APIS:
            flag["hits"] += 1
            return 500, {}, {}
        return real(api, body, hdr)

    w.sim._handle = handle  # type: ignore[method-assign]  # noqa: SLF001
    return flag


@pytest.fixture
def kworld(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = KiwoomPaperWorld(tmp_path)
    yield w
    w.close()


def test_rest_query_outage_blocks_entry_allows_exit(kworld: KiwoomPaperWorld) -> None:
    """조회 장애 중: 대조 QUERY_FAILED, 위험 확대(09:30 매수) 전송 없음, 위험 축소(14:30 청산)는 보유일 때 전송."""
    w = kworld
    start_and_resume(w)
    flag = rest_outage(w)
    # (a) 09:15~09:40 조회 장애 → 연속 3회 실패로 K5(조회 경로) → 09:30 판정은 전역 정지로 건너뜀, 전송 없음
    #     (이전: K5 없이 G8 현금 신선도로만 거부 — 03 "조회 최대 3회 → K5"에 맞춰 수정)
    w.run_until(kst(D0, 9, 15))
    flag["on"] = True
    w.run_until(kst(D0, 9, 40))
    assert flag["hits"] > 0
    fails = w.repo.select("SELECT result FROM reconciliation WHERE result='QUERY_FAILED'")
    assert len(fails) >= 20
    assert "K5" in w.active(), w.active()
    assert w.sent_count() == 0, w.decisions()[-3:]
    assert w.decisions() == []                                  # 판정 자체를 건너뜀 (DP-21 전역 정지)
    flag["on"] = False
    w.run_until(kst(D0, 9, 45))
    assert w.active() == set()                                  # 대조 성공 → K5 자동 해제
    w.run_until(kst(D0, 14, 31))                                # 장애 해소 뒤 14:30 새 신호 진입
    assert w.position() == 1
    # (b) 다음 날 보유 중 09:29~09:35 조회 장애 → 09:30 판정 시점엔 실패 1~2회(K5 전) → 청산(위험 축소) 전송
    w.run_until(kst(DAYS[1], 9, 29))
    flag["on"] = True
    w.run_until(kst(DAYS[1], 9, 31))
    assert buy_sell_of_day(w, DAYS[1]) == [("EXIT", "FILLED")], (w.decisions()[-3:], w.active())
    flag["on"] = False
    w.run_until(kst(DAYS[1], 9, 45))
    assert w.position() == 0 and w.active() == set()
    assert w.sent_count() == 2


# (수정됨: Reconciler 연속 조회 실패 3회 → K5(link:rest) — 이전 xfail 결함 후보 2)
def test_rest_query_outage_raises_k5_then_auto_release(kworld: KiwoomPaperWorld) -> None:
    w = kworld
    start_and_resume(w)
    flag = rest_outage(w)
    w.run_until(kst(D0, 10, 0))
    flag["on"] = True
    w.run_until(kst(D0, 10, 5))                                 # 5분 지속 (주기 대조 1분 × 5)
    assert "K5" in w.active(), w.active()
    flag["on"] = False
    w.run_until(kst(D0, 10, 7))
    k5 = w.repo.select("SELECT state, released_by FROM halt WHERE halt_type='K5' ORDER BY created_at")
    assert "K5" not in w.active() and k5[-1]["released_by"] == "auto", k5


# --- 5. 지속 대조 불일치 (외부 수동 거래) → K4 → RECONCILE_RESOLVE → 재개 -----------------------------

def test_external_trade_k4_blocks_exit_then_resolve_resume(world: PaperWorld) -> None:
    """유니버스 밖 종목 외부 매수(PV-13) → 재조회 1회 후 K4. K4 중 허용은 '취소만'(01 §2.6) → 14:30 청산도 보내지 않음.
    운영자 RECONCILE_RESOLVE EXTERNAL → 재대조 일치 → 로컬 RESUME K4 → 다음 날 09:30 판정이 보유를 청산."""
    w = world
    start_and_resume(w)
    w.run_until(kst(D0, 9, 31))
    assert w.position() == 1
    ext = w.broker.add_external_order(ACCOUNT, "000660", "BUY", Decimal("1"))
    w.broker.fill(ext)
    w.run_until(kst(D0, 9, 33))
    assert "K4" in w.active(), w.active()
    recs = w.repo.select("SELECT result, summary FROM reconciliation WHERE result='MISMATCH'")
    assert any(json.loads(r["summary"]).get("requeried") for r in recs)
    sent_before = len(w.broker.sent)
    w.run_until(kst(D0, 14, 31))
    assert len(w.broker.sent) == sent_before                    # K4 중 주문 없음 (청산 포함)
    assert w.position() == 1                                    # 정지는 청산이 아님
    items = w.repo.open_recon_items(ACCOUNT)
    assert items and all(i["instrument"] == "000660" for i in items), items
    for it in items:
        w.command("RECONCILE_RESOLVE", {"item_id": it["item_id"], "method": "EXTERNAL"})
        w.step()
        assert last_command(w)["status"] == "DONE", last_command(w)
    assert w.repo.open_recon_items(ACCOUNT) == []
    w.run_until(kst(D0, 14, 35))                                # 재대조 일치
    w.command("RESUME", {"account": None, "halt_type": "K4"})
    w.step()
    assert "K4" not in w.active(), (last_command(w), halt_rows(w, "K4"))
    assert w.repo.position(ACCOUNT, "000660").quantity == 1
    w.run_until(kst(DAYS[1], 9, 31))
    assert w.position() == 0
    assert buy_sell_of_day(w, DAYS[1]) == [("EXIT", "FILLED")]
    sells = new_orders(w, "SELL")
    assert [s.instrument for s in sells] == [CODE]              # 수동 소유 종목에 전략 주문 없음
    assert w.active() == set()


# --- 6. 09:31 KILL, 매수 미체결 → K1 + 취소 정확히 1회 (감시 스레드 fast_kill 경로) --------------------

def test_kill_with_open_buy_cancels_once_via_supervisor_fast_kill(world: PaperWorld) -> None:
    from trading_system.app.supervisor import Supervisor

    class Mono:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    w = world
    start_and_resume(w)
    w.broker.behavior = FakeBehavior.ACCEPT_ONLY
    w.run_until(kst(D0, 9, 31))
    assert w.intents()[-1]["state"] == "ACCEPTED" and len(w.broker.open_order_nos()) == 1
    mono = Mono()
    w.engine.mono = mono
    w.engine.last_progress = mono()
    sup = Supervisor(w.engine, heartbeat=w.root / "var" / "paper" / "heartbeat",
                     fallback=w.root / "var" / "paper" / "logs" / "fallback.log", mono=mono)
    w.command("KILL", {"account": None, "reason": "시험 킬"})
    mono.t += 0.5
    sup.step(waited=0.5)                                        # 감시 스레드: K1 행 즉시 기록, 취소는 안 함
    assert "K1" in w.active()
    assert [r.kind for r in w.broker.sent] == ["NEW"]
    for _ in range(3):                                          # 판정·실행 스레드: KILL 처리 → 취소 1회
        mono.t += 1.0
        w.clock._now = w.clock.now() + timedelta(seconds=1)  # noqa: SLF001
        sup.step(waited=1.0)
        w.step()
    assert [r.kind for r in w.broker.sent] == ["NEW", "CANCEL"], w.broker.sent
    assert len(halt_rows(w, "K1")) == 1
    assert last_command(w)["status"] == "DONE"
    w.run_until(kst(D0, 14, 31))
    assert [r.kind for r in w.broker.sent] == ["NEW", "CANCEL"]  # K1 중 신규 주문·추가 취소 없음
    assert w.broker.open_order_nos() == [] and w.position() == 0
    assert w.repo.active_reservations(ACCOUNT) == []
    w.command("RESUME", {"account": None, "halt_type": "K1"})
    w.step()
    assert "K1" not in w.active()
    w.broker.behavior = FakeBehavior.FILL
    w.run_until(kst(DAYS[1], 9, 31))
    assert buy_sell_of_day(w, DAYS[1]) == [("ENTRY", "FILLED")]
    assert len([r for r in w.broker.sent if r.kind == "CANCEL"]) == 1
