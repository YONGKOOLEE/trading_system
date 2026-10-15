"""CP-035 보유 중 전략 교체 거부 (A-104 "(가) 보유 중 교체 금지", D-200, V-278·V-279, DP-49).

- V-278: 교체 중지(STOPPED, 사유 REPLACE)는 그 전략 소유 보유·예약(소유권 RESERVED·CONFIRMED)이 있으면 거부.
  무보유면 허용. 거부는 상태·전이 기록·K9를 바꾸지 않는다. 사용자 판단 중지(USER)는 보유 중에도 허용(변경 없음).
- V-279: 배포 설정에서 빠진 전략이 보유·예약을 소유하면 시작 거부(DP-49). 사용자 판단 중지(USER)·폐기는 예외
  (그 보유는 운영 요청만 처리 — 01 K9, 배포 여부와 무관). 다른 계좌 보유는 보지 않는다.

가상 계좌·가상 승인 A-9xx만 쓴다. 네트워크 없음.
"""

from __future__ import annotations

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_e2e_paper0_days import (
    ACCOUNT as PF_ACCOUNT, CODE as PF_CODE, DAYS, ROOT as PF_ROOT, PaperWorld, kst, run_trading_day, start_and_resume,
)
from tests.scenario.test_t017_gaps import _prepare_v2, _switch_to_v2
from tests.scenario.test_t007_strategy import register, run, sig, to_paper, wire
from tests.support.sample_strategies import cross_spec
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import GateResult, RequestType
from trading_system.domain.clock import KST
from trading_system.domain.strategy import StopReason, StrategyState


def _state(env: Env, version: int = 1) -> dict:
    row = env.repo.strategy_state("t-cross", version, "M1")
    assert row is not None
    return row


def _k9(env: Env) -> list[dict]:
    return [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K9"]


# --- V-278 교체 중지 전이 ------------------------------------------------------------------------

def test_v278_replace_stop_refused_while_strategy_holds(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    assert env.repo.position(ACCOUNT, "TST-A").quantity > 0
    before = env.repo.strategy_transitions("t-cross", 1)
    with pytest.raises(ValueError) as exc:
        reg.transition("t-cross", 1, StrategyState.STOPPED, "A-931", stop_reason=StopReason.REPLACE)
    msg = str(exc.value)
    assert "보유 중 교체 금지" in msg and "A-104" in msg and "1건" in msg and "TST-A" in msg
    assert _state(env)["state"] == "PAPER"                               # 상태 불변
    assert env.repo.strategy_transitions("t-cross", 1) == before        # 전이 기록 없음
    assert _k9(env) == []                                                # K9 발동 없음
    assert env.repo.active_ownership(ACCOUNT, "TST-A").owner == "t-cross:1"


def test_v278_replace_stop_refused_while_entry_is_only_reserved(env: Env) -> None:
    """미체결 매수(소유권 RESERVED, 예약)만 있어도 거부 — '보유·예약'."""
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(spec, RequestType.ENTRY, "TST-B", ev="r")).result is GateResult.APPROVED
    [own] = env.repo.active_ownerships_of("t-cross:1")
    assert own["state"] == "RESERVED" and env.repo.position(ACCOUNT, "TST-B").quantity == 0
    with pytest.raises(ValueError, match="보유 중 교체 금지"):
        reg.transition("t-cross", 1, StrategyState.STOPPED, "A-932", stop_reason=StopReason.REPLACE)
    assert _state(env)["state"] == "PAPER" and _k9(env) == []


def test_v278_replace_stop_allowed_when_flat(env: Env) -> None:
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    assert run(env, sig(spec, RequestType.EXIT, "TST-A", ev="x")).result is GateResult.APPROVED
    assert env.repo.active_ownerships_of("t-cross:1") == []
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-933", stop_reason=StopReason.REPLACE)
    row = _state(env)
    assert row["state"] == "STOPPED" and row["stop_reason"] == "REPLACE"
    [k9] = _k9(env)
    assert k9["variant"] == "REPLACE" and k9["scope_owner"] == "t-cross:1"


def test_v278_user_stop_still_allowed_while_holding(env: Env) -> None:
    """사용자 판단 중지는 이 결정 범위 밖 — 보유 중에도 허용(보유는 운영 요청으로 정리, 01 K9)."""
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-934", stop_reason=StopReason.USER)
    assert _state(env)["state"] == "STOPPED" and _state(env)["stop_reason"] == "USER"


# --- V-279 시작 시 배포 설정 검사 (DP-49) ------------------------------------------------------------

def _holding_v1(env: Env):  # type: ignore[no-untyped-def]
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    assert run(env, sig(spec, RequestType.ENTRY, "TST-A", ev="a")).result is GateResult.APPROVED
    return reg


def test_v279_undeployed_owner_with_holding_is_a_start_problem(env: Env) -> None:
    reg = _holding_v1(env)
    problems = reg.undeployed_holding_problems(ACCOUNT, {"t-cross:2"})
    assert len(problems) == 1
    assert "t-cross:1" in problems[0] and "PAPER" in problems[0] and "보유 중 교체 금지" in problems[0]
    assert reg.undeployed_holding_problems(ACCOUNT, {"t-cross:1"}) == []          # 배포 중이면 문제 없음
    assert reg.undeployed_holding_problems("KR-OTHER-01", set()) == []            # 다른 계좌 보유는 보지 않는다


def test_v279_user_stopped_owner_may_stay_undeployed(env: Env) -> None:
    reg = _holding_v1(env)
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-935", stop_reason=StopReason.USER)
    assert reg.undeployed_holding_problems(ACCOUNT, set()) == []                  # 운영 요청만 — 배포와 무관


def test_v279_flat_replaced_owner_may_stay_undeployed(env: Env) -> None:
    """오늘의 PAPER 배포와 같은 형태: v1 교체 중지(무보유)·배포 설정에서 빠짐 → 문제 없음."""
    reg = wire(env)
    spec = register(reg, env)
    to_paper(reg, spec)
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-936", stop_reason=StopReason.REPLACE)
    v2 = register(reg, env, cross_spec(version=2))
    to_paper(reg, v2)
    assert reg.undeployed_holding_problems(ACCOUNT, {"t-cross:2"}) == []


# --- V-279 시작 점검(preflight)에도 같은 검사 (A-105: 시작 거부 사유가 텔레그램에 실리게) -------------------------
# 독립 검토: run의 build_engine 거부 사유는 run 콘솔에만 남고, 시작 점검은 통과("시작 점검 통과, run 시작")한 뒤 자동 운영이
# "run 프로세스가 종료됨(코드 2)…"만 알렸다. 시작 점검이 같은 검사로 먼저 거부하면 자동 운영이 그 줄을 텔레그램으로 보낸다.

def _pf_sim(clock_now):  # type: ignore[no-untyped-def]
    """시작 점검용 키움 모의 서버(루프백, 가상값). 일봉·시세 조회에 쓸 005930 데이터만 넣는다."""
    from datetime import date as _date, timedelta as _td

    from tests.support.kiwoom_sim import Sim, _md_init
    s = Sim(now=lambda: clock_now().astimezone(KST).replace(tzinfo=None))
    _md_init(s)
    d, rows = _date(2026, 10, 7), []
    while len(rows) < 30:
        if d.weekday() < 5:
            p = 70000 + len(rows)
            rows.append({"dt": d.strftime("%Y%m%d"), "open_pric": str(p), "high_pric": str(p + 500),
                         "low_pric": str(p - 500), "cur_prc": f"+{p}", "trde_qty": "1000", "pred_pre": "+0"})
        d -= _td(days=1)
    s.daily[PF_CODE] = rows
    s.info[PF_CODE] = {"cur_prc": "+70000", "upl_pric": "+91000", "lst_pric": "-49000"}
    return s


def _pf_files(root) -> None:  # type: ignore[no-untyped-def]
    """PaperWorld 루트에 시작 점검이 읽는 파일만 더한다: 가상 자격정보(.env.paper)·달력."""
    import shutil

    from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET
    (root / ".env.paper").write_text("\n".join([
        "TRADE_MODE=PAPER", f"KIWOOM_PAPER_APP_KEY={APP_KEY}", f"KIWOOM_PAPER_SECRET_KEY={SECRET}",
        f"KIWOOM_PAPER_ACCOUNT_NO={ACCOUNT_NO}", f"KIWOOM_PAPER_ACCOUNT_ALIAS={PF_ACCOUNT}"]) + "\n", encoding="utf-8")
    shutil.copytree(PF_ROOT / "config" / "calendar", root / "config" / "calendar", dirs_exist_ok=True)


def _pf_run(w, sim, at):  # type: ignore[no-untyped-def]
    from decimal import Decimal as _D

    from trading_system.app.preflight import preflight_main
    from trading_system.domain.clock import FixedClock
    return preflight_main(w.root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url,
                          clock=FixedClock(at), probe_code=PF_CODE, per_tr_interval=0.0,
                          sntp_query=lambda host, port: _D("0.01"))


def _reopen(w) -> None:  # type: ignore[no-untyped-def]
    """PaperWorld.close(불변 조건 검사)용으로, 닫힌 저장소를 다시 연다."""
    import sqlite3

    from trading_system.domain.enums import Mode
    from trading_system.storage.repository import Repository
    try:
        w.repo.connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        w.repo = Repository(w.root / "var" / "paper" / "trading.db", Mode.PAPER)


def _audit_count(root) -> int:  # type: ignore[no-untyped-def]
    import sqlite3
    conn = sqlite3.connect(f"file:{(root / 'var' / 'paper' / 'trading.db').as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0])
    finally:
        conn.close()


def _orphan_preflight(tmp_path, capsys):  # type: ignore[no-untyped-def]
    """v1이 005930 보유 중 배포 설정만 v2로 바꾼 상태(교체 전이 없음)에서 시작 점검. (종료 코드, 출력, sim 호출 기록)."""
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        w.run_until(kst(DAYS[0], 9, 31))
        assert w.position() == 1 and w.repo.active_ownership(PF_ACCOUNT, PF_CODE).owner == "paper0-verify:1"
        _prepare_v2(w)                                   # 저장소를 닫고 배포 설정 v2로, v2 등록·PAPER (v1 전이 없음)
        _pf_files(w.root)
        before = _audit_count(w.root)
        sim = _pf_sim(w.clock.now)
        try:
            capsys.readouterr()
            rc = _pf_run(w, sim, kst(DAYS[1], 8, 0))
            out = capsys.readouterr().out
            calls = list(sim.log)
        finally:
            sim.close()
        assert _audit_count(w.root) == before           # 검사는 읽기만 (기록·생성 없음)
        return rc, out, calls
    finally:
        _reopen(w)
        w.close()


def test_v279_preflight_refuses_undeployed_owner_with_holding(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    rc, out, calls = _orphan_preflight(tmp_path, capsys)
    assert rc == 2, out
    assert calls == []                                   # 연결 전에 거부 — 토큰 발급·조회 0건, 주문 0건
    line = next(ln for ln in out.splitlines() if "paper0-verify:1" in ln)
    assert "거부" in line and "보유 중 교체 금지" in line and PF_CODE in line and "1건" in line, line
    # 고치는 방법(전략 청산·사용자 판단 중지·되돌려 배포)이 같은 줄에 — 자동 운영은 "거부"·"실패" 줄만 보낸다
    assert "config/app.paper.toml" in line and "청산" in line, line
    assert r"scripts\strategy.py transition --mode PAPER paper0-verify 1 STOPPED --stop-reason USER" in line, line
    assert "통과" not in out


def test_v279_autopilot_forwards_preflight_orphan_refusal(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    """자동 운영 경로: 실제 시작 점검 출력 그대로 → EMERGENCY 알림에 사유·조치가 실리고 run은 시작하지 않는다."""
    from tests.scenario.test_t025_autopilot import Rig
    rc, out, _ = _orphan_preflight(tmp_path / "w", capsys)
    r = Rig(tmp_path / "ap", preflight_rc=rc)
    r.preflight_text = out
    assert r.ap.run() == 2
    assert r.events == ["preflight"]                     # run 시작 안 함
    [(level, text)] = [(lv, t) for lv, t in r.notes if "시작 점검 실패" in t]
    assert level == "EMERGENCY"
    assert "paper0-verify:1" in text and "보유 중 교체 금지" in text and PF_CODE in text, text
    assert "--stop-reason USER" in text and "config/app.paper.toml" in text, text
    assert not any("run 시작" in t and "통과" in t for _, t in r.notes)


def test_v279_preflight_passes_today_shape(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    """오늘 PAPER 배포 형태: v1 교체 중지(REPLACE)·무보유·배포에 없음, v2 배포·005930 보유 → 시작 점검 통과."""
    w = PaperWorld(tmp_path)
    try:
        start_and_resume(w)
        run_trading_day(w, DAYS[0])                      # v1 왕복 → 무보유
        w.run_until(kst(DAYS[0], 16, 1))
        w.run_until(kst(DAYS[1], 8, 30))
        _switch_to_v2(w)                                 # v1 STOPPED(REPLACE), v2 PAPER, 배포 설정 v2
        w.run_until(kst(DAYS[1], 9, 31))
        assert w.repo.active_ownership(PF_ACCOUNT, PF_CODE).owner == "paper0-verify:2"
        qty = w.repo.position(PF_ACCOUNT, PF_CODE)
        rows = {(r["strategy_id"], r["version"]): r for r in w.repo.strategy_states()}
        assert rows[("paper0-verify", 1)]["state"] == "STOPPED" and rows[("paper0-verify", 1)]["stop_reason"] == "REPLACE"
        w.repo.close()
        _pf_files(w.root)
        sim = _pf_sim(w.clock.now)
        sim.positions[PF_CODE] = (qty.quantity, qty.avg_price)   # 브로커 잔고 = 내부 보유 (시작 대조 일치)
        try:
            capsys.readouterr()
            rc = _pf_run(w, sim, kst(DAYS[2], 8, 0))
            out = capsys.readouterr().out
        finally:
            sim.close()
        assert rc == 0, out
        assert "결과: 통과" in out and "보유 중 교체 금지" not in out
    finally:
        _reopen(w)
        w.close()
