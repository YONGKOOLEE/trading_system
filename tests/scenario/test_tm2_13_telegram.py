"""T-M2-13 운영 도구·텔레그램 시장 구분 (CP-024 D-117, A-065 Q5). V-149·V-152.

- /exit·/reduce 수량: 시장의 수량 단위가 소수일 때만(M2) 소수 허용, M1은 정수만(기존 그대로).
- 메시지 시장 표시: 보내는 모든 텔레그램 메시지 앞에 `[M1]`/`[M2]`(전송 계층 한 곳에서). M1의 `[M1]` 표시는 이 작업에서
  추가한다(A-065 Q5) — 기존 M1 시험의 문구 기대값은 이 이유(T-M2-13, D-117, V-149)로 `[M1] ` 접두어를 더했다.
- M2 봇 토큰은 M2 체크아웃의 비밀 설정 파일에만(D-117) — 코드는 체크아웃 루트의 같은 키 이름을 읽는다. 시험 값은 가상.
로컬 가짜 텔레그램 서버로만 시험한다. 실제 M2 봇 송수신은 텔레그램 연결 승인 뒤.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t013_telegram import CHAT, TOKEN, FakeTelegram, code_from
from tests.support.m2_fakes import ACCOUNT as M2_ACCOUNT
from tests.support.m2_fakes import M2Env, build_m2_env
from trading_system.ops.telegram import MARKET_TAGS, MAX_TEXT, CommandBot, Notifier, TelegramApi
from trading_system.storage.alerts import AuditAlertSink


@pytest.fixture
def tg() -> FakeTelegram:
    f = FakeTelegram()
    yield f
    f.close()


@pytest.fixture
def m2(tmp_path: Path) -> M2Env:
    e = build_m2_env(tmp_path)
    yield e
    e.repo.close()


def m2_bot(e: M2Env, tg: FakeTelegram) -> CommandBot:
    sink = AuditAlertSink(e.repo, e.clock)
    return CommandBot(e.repo, TelegramApi(lambda: TOKEN, tg.url, market="M2"), CHAT, M2_ACCOUNT, e.clock,
                      e.kill_file, lambda lvl, m: sink.alert(lvl, m, None), market="M2")


def m1_bot(e: M2Env, tg: FakeTelegram) -> CommandBot:
    sink = AuditAlertSink(e.repo, e.clock)
    return CommandBot(e.repo, TelegramApi(lambda: TOKEN, tg.url), CHAT, "KR-BT-01", e.clock, e.kill_file,
                      lambda lvl, m: sink.alert(lvl, m, None))


# --- V-149 수량 해석 --------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,qty", [("/exit", "0.0100"), ("/reduce", "0.5"), ("/exit", "3"), ("/reduce", "12.000")])
def test_v149_m2_decimal_quantity_accepted(m2: M2Env, tg: FakeTelegram, cmd: str, qty: str) -> None:
    b = m2_bot(m2, tg)
    tg.push(1, f"{cmd} TSTA-USDT {qty}")
    b.poll_once(timeout=0)
    assert "/confirm" in tg.sent[-1]["text"]
    tg.push(2, f"/confirm {code_from(tg)}")
    b.poll_once(timeout=0)
    [c] = m2.repo.pending_commands()
    assert c["command"] == cmd[1:].upper()
    args = json.loads(c["args"])
    assert args == {"account": M2_ACCOUNT, "instrument": "TSTA-USDT", "quantity": qty}
    assert Decimal(args["quantity"]) == Decimal(qty)


@pytest.mark.parametrize("qty", ["1e-3", "-0.1", "0", "0.0", "0.000", ".5", "1.", "abc", "1,000", "+1", "0x10",
                                 "NaN", "Infinity", "１"])
def test_v149_m2_bad_quantity_refused(m2: M2Env, tg: FakeTelegram, qty: str) -> None:
    b = m2_bot(m2, tg)
    tg.push(1, f"/exit TSTA-USDT {qty}")
    b.poll_once(timeout=0)
    assert "형식 오류" in tg.sent[-1]["text"] and "소수" in tg.sent[-1]["text"]
    assert m2.repo.pending_commands() == []


@pytest.mark.parametrize("qty", ["0.5", "1.0", "1e2", "-1"])
def test_v149_m1_decimal_quantity_still_refused(m2: M2Env, tg: FakeTelegram, qty: str) -> None:
    b = m1_bot(m2, tg)
    tg.push(1, f"/reduce 005930 {qty}")
    b.poll_once(timeout=0)
    assert "형식 오류" in tg.sent[-1]["text"] and "수량(정수)" in tg.sent[-1]["text"]
    assert m2.repo.pending_commands() == []


def test_m1_integer_quantity_unchanged(m2: M2Env, tg: FakeTelegram) -> None:
    b = m1_bot(m2, tg)
    tg.push(1, "/exit 005930 10")
    b.poll_once(timeout=0)
    assert "EXIT" in tg.sent[-1]["text"] and '"quantity": "10"' in tg.sent[-1]["text"]


def test_bot_market_value_checked(m2: M2Env, tg: FakeTelegram) -> None:
    with pytest.raises(ValueError):
        CommandBot(m2.repo, TelegramApi(lambda: TOKEN, tg.url), CHAT, M2_ACCOUNT, m2.clock, m2.kill_file,
                   lambda lvl, m: None, market="M3")


# --- V-149 시장 표시 --------------------------------------------------------------------------

def test_v149_market_tag_on_every_message(tg: FakeTelegram) -> None:
    assert MARKET_TAGS == {"M1": "[M1]", "M2": "[M2]"}
    TelegramApi(lambda: TOKEN, tg.url, market="M2").send_message(CHAT, "[긴급] 정지 K4")
    TelegramApi(lambda: TOKEN, tg.url).send_message(CHAT, "[경고] x")          # 기본 = M1
    TelegramApi(lambda: TOKEN, tg.url, market="M1").send_message(CHAT, "정지 0건")
    assert [m["text"] for m in tg.sent] == ["[M2] [긴급] 정지 K4", "[M1] [경고] x", "[M1] 정지 0건"]


def test_market_tag_not_doubled_and_long_text_still_cut(tg: FakeTelegram) -> None:
    a = TelegramApi(lambda: TOKEN, tg.url, market="M2")
    a.send_message(CHAT, "[M2] 이미 표시됨")
    a.send_message(CHAT, "가" * 5000)
    assert tg.sent[0]["text"] == "[M2] 이미 표시됨"
    assert tg.sent[1]["text"].startswith("[M2] ") and len(tg.sent[1]["text"]) <= MAX_TEXT


def test_api_market_value_checked(tg: FakeTelegram) -> None:
    for bad in ("M3", "m1", ""):
        with pytest.raises(ValueError):
            TelegramApi(lambda: TOKEN, tg.url, market=bad)


def test_notifier_and_bot_replies_tagged_by_market(m2: M2Env, tg: FakeTelegram) -> None:
    sink = AuditAlertSink(m2.repo, m2.clock)
    n = Notifier(m2.repo, TelegramApi(lambda: TOKEN, tg.url, market="M2"), CHAT, m2.clock, None)
    sink.alert("EMERGENCY", "결과 불명", None)
    assert n.pump() == (1, 0) and tg.sent[-1]["text"] == "[M2] [긴급] 결과 불명"
    b = m2_bot(m2, tg)
    tg.push(1, "/status")
    b.poll_once(timeout=0)
    assert tg.sent[-1]["text"].startswith("[M2] 정지")
