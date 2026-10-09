"""T-013 텔레그램 알림·원격 명령 (05-plan T-013 + CP-018 추가, 04 V-30·V-32·V-57·V-107·V-118·V-119).

로컬 가짜 텔레그램 서버(http.server)로만 시험한다. 토큰·채팅 ID는 가상값이다. 실제 봇 연결은 사용자 승인 후.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.domain.enums import RequestType
from trading_system.domain.models import GateRequest
from trading_system.ops.commands import OpsProcessor
from trading_system.ops.telegram import CommandBot, Notifier, TelegramApi, TelegramError
from trading_system.storage.alerts import AuditAlertSink

TOKEN = "123456:virtual-token-ABC"
CHAT = "777"


class FakeTelegram:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.fail = False
        self.send_reply = None   # T-017: sendMessage 본문 → (HTTP 코드, 응답) 또는 None(정상)
        self.paths: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:  # 조용히
                return

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                fake.paths.append(self.path)
                if not self.path.startswith(f"/bot{TOKEN}/"):
                    return self._send(401, {"ok": False, "description": "Unauthorized"})
                if fake.fail:
                    return self._send(502, {"ok": False, "description": "Bad Gateway"})
                method = self.path.rsplit("/", 1)[-1]
                if method == "sendMessage" and fake.send_reply is not None:
                    special = fake.send_reply(body)
                    if special is not None:
                        return self._send(*special)
                if method == "sendMessage":
                    fake.sent.append(body)
                    return self._send(200, {"ok": True, "result": {"message_id": len(fake.sent)}})
                if method == "getUpdates":
                    off = body.get("offset", 0)
                    return self._send(200, {"ok": True, "result": [u for u in fake.updates if u["update_id"] >= off]})
                return self._send(404, {"ok": False})

            def _send(self, code: int, obj: dict) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def push(self, uid: int, text: str, chat: str = CHAT, date: int | None = None) -> None:
        msg: dict = {"chat": {"id": int(chat)}, "text": text}
        if date is not None:
            msg["date"] = date
        self.updates.append({"update_id": uid, "message": msg})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tg() -> FakeTelegram:
    f = FakeTelegram()
    yield f
    f.close()


def api(tg: FakeTelegram, token: str = TOKEN) -> TelegramApi:
    return TelegramApi(lambda: token, tg.url)


def bot(env: Env, tg: FakeTelegram) -> CommandBot:
    sink = AuditAlertSink(env.repo, env.clock)
    return CommandBot(env.repo, api(tg), CHAT, ACCOUNT, env.clock, env.kill_file, lambda lvl, m: sink.alert(lvl, m, None))


def ops(env: Env) -> OpsProcessor:
    return OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock, kill_file=env.kill_file)


def code_from(tg: FakeTelegram) -> str:
    text = tg.sent[-1]["text"]
    return text.rsplit("/confirm ", 1)[1].strip()


# --- 원격 명령 ---------------------------------------------------------------------------------

def test_kill_is_immediate_and_processed(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(1, "/kill 급락")
    b.poll_once(timeout=0)
    [o] = ops(env).process_pending()
    assert o.status == "DONE" and env.repo.active_halts(ACCOUNT) == ["K1"]
    assert env.repo.command("tg:1")["channel"] == "REMOTE"


def test_v57_duplicate_update_processed_once(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(5, "/kill")
    b.poll_once(timeout=0)
    tg.updates.clear()
    tg.push(5, "/kill")  # 같은 update_id 재전달
    b2 = bot(env, tg)    # 재시작해 offset을 잃어도
    b2.poll_once(timeout=0)
    assert len(ops(env).process_pending()) == 1
    assert "이미 받은 명령" in tg.sent[-1]["text"]


def test_v107_unknown_chat_rejected_recorded_alerted(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(1, "/kill", chat="999")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == []
    kinds = [e["kind"] for e in env.repo.audit_events()]
    assert "REMOTE_REJECTED" in kinds
    assert any("허용되지 않은 채팅" in json.loads(e["summary"]).get("message", "") for e in env.repo.audit_events()
               if e["kind"] == "ALERT")
    assert tg.sent == []  # 모르는 채팅에는 답하지 않음


def test_v107_confirmation_code_required_wrong_and_expired(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(1, "/resume K1")
    b.poll_once(timeout=0)
    good = code_from(tg)
    tg.push(2, "/confirm deadbe")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == [] and "틀렸거나 만료" in tg.sent[-1]["text"]
    env.clock.advance(timedelta(seconds=61))
    tg.push(3, f"/confirm {good}")
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == []  # 만료
    tg.push(4, "/resume K1")
    b.poll_once(timeout=0)
    tg.push(5, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    assert [c["command"] for c in env.repo.pending_commands()] == ["RESUME"]


def test_v32_remote_resume_of_k3_rejected_and_v119_kill_file_shown(env: Env, tg: FakeTelegram) -> None:
    from trading_system.domain.enums import HaltType
    env.halts.activate(HaltType.K3, "resolver", "x", ACCOUNT)
    env.halts.activate(HaltType.K1, "local", "kill")
    env.kill_file.write_text("KILL")
    b = bot(env, tg)
    for i, k in enumerate(("K3", "K1"), start=1):
        tg.push(i * 10, f"/resume {k}")
        b.poll_once(timeout=0)
        tg.push(i * 10 + 1, f"/confirm {code_from(tg)}")
        b.poll_once(timeout=0)
    r3, r1 = ops(env).process_pending()
    assert r3.status == "REJECTED" and "로컬" in r3.result
    assert r1.status == "DONE" and "KILL 파일 있음" in r1.result
    tg.push(99, "/status")
    b.poll_once(timeout=0)
    assert "KILL 파일: 있음" in tg.sent[-1]["text"]


def test_operator_exit_via_telegram_goes_through_gate(env: Env, tg: FakeTelegram) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity="10")))
    env.executor.submit(d.approved)
    env.executor.process_events()
    b = bot(env, tg)
    tg.push(1, "/exit TST-A 10")
    b.poll_once(timeout=0)
    tg.push(2, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    [o] = ops(env).process_pending()
    env.executor.process_events()
    assert o.status == "DONE" and env.repo.position(ACCOUNT, "TST-A").quantity == 0
    tg.push(3, "/entry TST-A 10")  # 신규 진입 명령은 없다 (R-21)
    b.poll_once(timeout=0)
    assert env.repo.pending_commands() == [] and "명령:" in tg.sent[-1]["text"]


# --- 알림 (V-30, V-118) -------------------------------------------------------------------------

def test_notifier_sends_alerts_and_v30_k11_with_backlog(env: Env, tg: FakeTelegram) -> None:
    sink = AuditAlertSink(env.repo, env.clock)
    n = Notifier(env.repo, api(tg), CHAT, env.clock, env.halts)
    sink.alert("EMERGENCY", "결과 불명", None)
    assert n.pump() == (1, 0) and tg.sent[-1]["text"].startswith("[M1] [긴급]")  # [M1] 표시: T-M2-13 D-117 A-065 Q5
    tg.fail = True
    sink.alert("WARNING", "경고 1", None)
    for _ in range(3):
        n.pump()
    assert "K11" in env.repo.active_halts(ACCOUNT)
    blocked = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="k11")))
    assert blocked.result.value == "REJECTED"
    sink.alert("WARNING", "경고 2", None)
    tg.fail = False
    sent, _ = n.pump()
    texts = [m["text"] for m in tg.sent]
    assert any("경고 1" in t for t in texts) and any("경고 2" in t for t in texts)  # 밀린 알림까지 전송
    assert "K11" not in env.repo.active_halts(ACCOUNT)


def test_v118_rejected_risk_reduction_alerts_over_telegram(env: Env, tg: FakeTelegram) -> None:
    b = bot(env, tg)
    tg.push(1, "/exit TST-A 5")  # 보유 없음 → 게이트 거부
    b.poll_once(timeout=0)
    tg.push(2, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    ops(env).process_pending()
    Notifier(env.repo, api(tg), CHAT, env.clock, env.halts).pump()
    assert any("[긴급]" in m["text"] and "운영 요청 거부" in m["text"] for m in tg.sent)


def test_token_never_appears_in_errors(tg: FakeTelegram) -> None:
    bad = TelegramApi(lambda: "wrong:token-XYZ", tg.url)
    with pytest.raises(TelegramError) as e:
        bad.send_message(CHAT, "x")
    assert "token-XYZ" not in str(e.value)
    dead = TelegramApi(lambda: TOKEN, "http://127.0.0.1:9")
    with pytest.raises(TelegramError) as e2:
        dead.send_message(CHAT, "x")
    assert TOKEN not in str(e2.value) and "virtual-token" not in str(e2.value)


def test_outgoing_text_is_masked(env: Env, tg: FakeTelegram) -> None:
    AuditAlertSink(env.repo, env.clock).alert("INFO", "계좌 1234567890 확인", None)
    Notifier(env.repo, api(tg), CHAT, env.clock, env.halts).pump()
    assert "1234567890" not in tg.sent[-1]["text"]
    assert Path and Decimal  # 표시용


# --- 꺼져 있던 동안 보낸 명령 (2026-10-05 독립 검토 M-1) ---------------------------------------------------

def test_stale_commands_sent_before_start_are_not_executed(env: Env, tg: FakeTelegram) -> None:
    """시작 시각보다 2분 넘게 오래된 메시지(꺼져 있는 동안 보낸 /kill 등)는 실행하지 않고 다시 보내라고만 답한다."""
    now = int(env.clock.now().timestamp())
    b = bot(env, tg)
    tg.push(1, "/kill 오래전", date=now - 3600)
    tg.push(2, "/status", date=now - 300)
    tg.push(3, "hi", date=now - 7200)            # 명령이 아닌 글은 조용히 무시
    assert b.poll_once(timeout=0) == 3
    assert ops(env).process_pending() == [] and env.repo.active_halts(ACCOUNT) == []
    texts = [m["text"] for m in tg.sent]
    assert len(texts) == 2 and all("꺼져 있던 동안" in t for t in texts)
    assert sum(1 for e in env.repo.audit_events() if e["kind"] == "REMOTE_STALE") == 3


def test_fresh_commands_near_start_still_work(env: Env, tg: FakeTelegram) -> None:
    now = int(env.clock.now().timestamp())
    b = bot(env, tg)
    tg.push(1, "/kill 방금", date=now - 60)       # 시작 직전 1분 — 허용 범위
    b.poll_once(timeout=0)
    assert [o.status for o in ops(env).process_pending()] == ["DONE"]


def test_stale_check_applies_after_chat_check(env: Env, tg: FakeTelegram) -> None:
    now = int(env.clock.now().timestamp())
    b = bot(env, tg)
    tg.push(1, "/kill", chat="999", date=now - 3600)
    b.poll_once(timeout=0)
    kinds = [e["kind"] for e in env.repo.audit_events()]
    assert "REMOTE_REJECTED" in kinds and "REMOTE_STALE" not in kinds and tg.sent == []


# --- A-084 ③ 비인가 채팅 홍수 상한 (R-21) -------------------------------------------------------------------

def _rejected_audits(env: Env) -> int:
    return sum(1 for e in env.repo.audit_events() if e["kind"] == "REMOTE_REJECTED")


def test_a084_unauthorized_chat_flood_capped_per_hour(env: Env, tg: FakeTelegram) -> None:
    """시간당 처음 3건만 감사 기록 + WARNING. 그 뒤는 조용히 세고, 한 시간이 지나면 요약 WARNING 1건.

    알림 호출 자체를 세려고 기록용 alert를 넣는다 (AuditAlertSink의 같은 문구 억제 창과 별개의 상한)."""
    calls: list[tuple[str, str]] = []
    b = CommandBot(env.repo, api(tg), CHAT, ACCOUNT, env.clock, env.kill_file, lambda lvl, m: calls.append((lvl, m)))

    def warnings(needle: str) -> list[str]:
        return [m for lvl, m in calls if lvl == "WARNING" and needle in m]

    for uid in range(1, 6):
        tg.push(uid, "/kill", chat="999")
    assert b.poll_once(timeout=0) == 5
    assert _rejected_audits(env) == 3
    assert len(warnings("허용되지 않은 채팅")) == 3
    assert warnings("비인가 메시지") == []                   # 같은 시간 창 안에서는 요약 없음
    assert tg.sent == [] and env.repo.pending_commands() == []
    tg.updates.clear()
    env.clock.advance(timedelta(minutes=59))
    tg.push(6, "/status", chat="999")
    b.poll_once(timeout=0)
    assert _rejected_audits(env) == 3 and warnings("비인가 메시지") == []
    tg.updates.clear()
    env.clock.advance(timedelta(minutes=2))                  # 시간 창 경과
    tg.push(7, "/kill", chat="999")
    b.poll_once(timeout=0)
    assert warnings("비인가 메시지") == ["비인가 메시지 3건 무시 (최근 1시간, 처음 3건만 기록)"]
    assert _rejected_audits(env) == 4                        # 새 시간 창의 첫 건은 다시 기록
    assert len(warnings("허용되지 않은 채팅")) == 4
    tg.updates.clear()
    env.clock.advance(timedelta(hours=1, minutes=1))         # 조용히 센 건이 없으면 요약도 없다
    assert b.poll_once(timeout=0) == 0
    assert len(warnings("비인가 메시지")) == 1
