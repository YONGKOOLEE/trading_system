"""T-017 텔레그램 남은 결함 (2026-10-05 검토 M-2~4, 1일차 K11 발생).

M-2 메시지 자체가 거부(HTTP 400)되면 대기열 맨 앞에서 영구 재시도 → K11 고착 → 그 메시지만 버리고 다음으로.
M-3 429(전송 제한)는 retry_after 동안 보내지 않고 K11 실패로 세지 않는다. 연속 전송은 1초 간격, 회차당 상한.
M-4 명령 처리 중 저장 오류면 offset을 넘기지 않아 다음 폴링에서 다시 처리. 같은 update가 3번 실패하면 건너뛰고 경고.
가짜 텔레그램 서버만 쓴다. 토큰·채팅은 가상값.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t013_telegram import CHAT, FakeTelegram, api, bot, tg  # noqa: F401  (fixture)
from trading_system.ops.telegram import Notifier
from trading_system.storage.alerts import AuditAlertSink


def notifier(env: Env, t: FakeTelegram, sleeps: list[float] | None = None) -> Notifier:
    return Notifier(env.repo, api(t), CHAT, env.clock, env.halts,
                    sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def alert(env: Env, msg: str) -> None:
    AuditAlertSink(env.repo, env.clock, dedup_window=None, console=False).alert("WARNING", msg, None)


def halts(env: Env) -> list[str]:
    return [h["halt_type"] for h in env.repo.halt_rows(None)]


# --- M-2 ------------------------------------------------------------------------------------------

def test_m2_rejected_message_is_dropped_and_others_flow(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    tg.send_reply = lambda body: (400, {"ok": False, "description": "Bad Request: message is too long"}) \
        if "BAD" in body["text"] else None
    alert(env, "BAD 메시지")
    alert(env, "정상 메시지")
    n = notifier(env, tg)
    for _ in range(5):
        n.pump()
    assert [m["text"] for m in tg.sent] == ["[경고] 정상 메시지"]
    assert "K11" not in halts(env)
    assert env.repo.pending_notifications() == []
    assert any(e["kind"] == "NOTIFY_DROPPED" for e in env.repo.audit_events())


def test_m2_channel_failure_still_counts_toward_k11(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    tg.fail = True                      # 502 = 채널 장애 (메시지 탓 아님)
    alert(env, "a")
    n = notifier(env, tg)
    for _ in range(3):
        n.pump()
    assert "K11" in halts(env)
    assert len(env.repo.pending_notifications()) == 1   # 버리지 않는다 (V-30)


# --- M-3 ------------------------------------------------------------------------------------------

def test_m3_429_waits_retry_after_without_k11(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    limited = {"on": True}
    tg.send_reply = lambda body: (429, {"ok": False, "description": "Too Many Requests: retry after 30",
                                        "parameters": {"retry_after": 30}}) if limited["on"] else None
    alert(env, "x")
    n = notifier(env, tg)
    for _ in range(5):
        n.pump()
        env.clock.advance(timedelta(seconds=5))
    assert "K11" not in halts(env) and tg.sent == []
    limited["on"] = False
    env.clock.advance(timedelta(seconds=10))     # 30초 경과
    n.pump()
    assert [m["text"] for m in tg.sent] == ["[경고] x"]


def test_m3_send_spacing_and_per_round_cap(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    for i in range(25):
        alert(env, f"m{i}")
    sleeps: list[float] = []
    n = notifier(env, tg, sleeps)
    sent, failed = n.pump()
    assert (sent, failed) == (20, 0) and len(sleeps) == 19 and set(sleeps) == {1.0}
    n.pump()
    assert len(tg.sent) == 25


# --- M-4 ------------------------------------------------------------------------------------------

def test_m4_command_not_lost_when_save_fails_once(env: Env, tg: FakeTelegram, monkeypatch) -> None:  # noqa: F811,ANN001
    b = bot(env, tg)
    real = env.repo.insert_command
    calls = {"n": 0}

    def flaky(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(env.repo, "insert_command", flaky)
    tg.push(1, "/kill 테스트")
    try:
        b.poll_once(timeout=0)
    except Exception:  # noqa: BLE001 — 저장소가 StorageFailure로 감싼다
        pass
    assert env.repo.pending_commands() == []
    b.poll_once(timeout=0)                         # 같은 update를 다시 받아 처리
    assert [c["command"] for c in env.repo.pending_commands()] == ["KILL"]


def test_m4_poison_update_skipped_after_three_failures(env: Env, tg: FakeTelegram, monkeypatch) -> None:  # noqa: F811,ANN001
    b = bot(env, tg)

    def broken(*a, **k):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(env.repo, "insert_command", broken)
    tg.push(7, "/kill")
    for _ in range(3):
        try:
            b.poll_once(timeout=0)
        except Exception:  # noqa: BLE001 — 저장소가 StorageFailure로 감싼다
            pass
    monkeypatch.undo()
    tg.push(8, "/status")
    b.poll_once(timeout=0)
    msgs = [json.loads(e["summary"]).get("message", "") for e in env.repo.audit_events() if e["kind"] == "ALERT"]
    assert any("저장하지 못해 건너뜀" in m for m in msgs)
    texts = [m["text"] for m in tg.sent]
    assert any("접수하지 않았습니다" in t for t in texts)       # 사용자 채팅에 직접 회신 (재검토 H-2)
    assert texts[-1].startswith("정지")                          # 다음 명령(/status)은 정상 처리
    assert env.repo.pending_commands() == []


# --- 재검토 반영 -----------------------------------------------------------------------------------

def test_h1_chat_not_found_is_channel_failure_k11(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    tg.send_reply = lambda body: (400, {"ok": False, "description": "Bad Request: chat not found"})
    alert(env, "a")
    n = notifier(env, tg)
    for _ in range(3):
        n.pump()
    assert "K11" in halts(env) and len(env.repo.pending_notifications()) == 1


def test_h1_long_text_is_truncated_not_dropped(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    alert(env, "가" * 5000)
    notifier(env, tg).pump()
    assert len(tg.sent) == 1 and len(tg.sent[0]["text"]) <= 4000


def test_m3_long_retry_after_is_capped_and_counted(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    tg.send_reply = lambda body: (429, {"ok": False, "description": "Too Many Requests",
                                        "parameters": {"retry_after": 3600}})
    alert(env, "x")
    n = notifier(env, tg)
    for _ in range(3):
        n.pump()
        env.clock.advance(timedelta(seconds=301))
    assert "K11" in halts(env)


def test_m1_confirm_survives_storage_failure(env: Env, tg: FakeTelegram, monkeypatch) -> None:  # noqa: F811,ANN001
    from tests.scenario.test_t013_telegram import code_from
    b = bot(env, tg)
    tg.push(1, "/resume K1")
    b.poll_once(timeout=0)
    code = code_from(tg)
    real = env.repo.insert_command
    calls = {"n": 0}

    def flaky(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(env.repo, "insert_command", flaky)
    tg.push(2, f"/confirm {code}")
    try:
        b.poll_once(timeout=0)
    except Exception:  # noqa: BLE001
        pass
    b.poll_once(timeout=0)
    assert [c["command"] for c in env.repo.pending_commands()] == ["RESUME"]


def test_m4_malformed_update_does_not_block_polling(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    b = bot(env, tg)
    tg.updates.append({"update_id": 1, "message": "이상한 형식"})
    tg.push(2, "/kill")
    b.poll_once(timeout=0)
    assert [c["command"] for c in env.repo.pending_commands()] == ["KILL"]


def test_m2_stop_signal_ends_pump_quickly(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    for i in range(5):
        alert(env, f"m{i}")
    stopped = {"v": False}

    def sleep(_: float) -> None:
        stopped["v"] = True
    n = Notifier(env.repo, api(tg), CHAT, env.clock, env.halts, sleep=sleep, should_stop=lambda: stopped["v"])
    sent, _ = n.pump()
    assert sent == 1


def test_emergency_sent_before_backlog(env: Env, tg: FakeTelegram) -> None:  # noqa: F811
    for i in range(25):
        alert(env, f"m{i}")
    AuditAlertSink(env.repo, env.clock, dedup_window=None, console=False).alert("EMERGENCY", "정지 K4", None)
    notifier(env, tg).pump()
    assert tg.sent[0]["text"] == "[긴급] 정지 K4"
