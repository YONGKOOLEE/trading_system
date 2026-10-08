"""실제 진입점 `run_main` 리허설 2 — 실행 중 장애·장시간 (PAPER, 로컬 키움 시뮬레이터·가짜 SNTP, 실제 벽시계).

test_run_rehearsal.py의 Rig(시뮬레이터·시계 오프셋·임시 루트·가짜 .env.paper)를 그대로 쓰고, 여기서 더한 주입 지점:
- SNTP: 가짜 서버를 제어 가능한 서버(ControlSntp: 정상 / 무응답 / 오차 5초)로 바꿈 (base.FakeSntp 교체).
- 10분 주기 시계 점검: app.main이 쓰는 Supervisor를 clock_every만 짧게 준 하위 클래스로 바꿈
  (CLOCK_CHECK_SECONDS는 __init__ 기본값으로 정의 시점에 묶여 모듈 상수 패치가 먹지 않음).
- 실행 중 시계 점프: Rig.offset을 실행 중에 바꿈(감시 스레드 LoopMonitor가 벽시각·단조 시각 차이로 감지 → K8).
- 실시간 끊김: Sim.drop_ws() + ws_reject_reg(재접속 REG 거부 = 업무 오류, 토큰 무효화 없음)로 끊긴 상태를 유지.
실제 네트워크·실제 키움·실제 SNTP 서버 연결 없음. src 수정 없음.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.rehearsal   # A-084 ⑥: 빠른 확인은 -m "not rehearsal and not e2e"

import trading_system.app.main as main_mod
from tests.scenario import test_run_rehearsal as base
from tests.scenario.test_run_rehearsal import ACCOUNT, CODE, ORDER_APIS, Rig, _ntp, raise_first, started, wait_until
from trading_system.domain.clock import KST
from trading_system.domain.enums import IntentState
from trading_system.ops.cli import reconcile_resolve_main, resume_main, status_main


# --- 보조: 제어 가능한 가짜 SNTP ----------------------------------------------------------------------

class ControlSntp:
    """base.FakeSntp와 같은 응답 + mode: "ok" | "silent"(무응답) | "skew"(+5초 오차)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self.mode = "ok"
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(512)
            except (TimeoutError, OSError):
                continue
            if len(data) < 48:
                continue
            self.requests += 1
            if self.mode == "silent":
                continue
            t = datetime.now(UTC) + (timedelta(seconds=5) if self.mode == "skew" else timedelta(0))
            now = _ntp(t)
            out = bytearray(48)
            out[0] = (0 << 6) | (4 << 3) | 4
            out[1] = 2
            out[24:32] = data[40:48]
            struct.pack_into("!II", out, 32, *now)
            struct.pack_into("!II", out, 40, *now)
            try:
                self.sock.sendto(bytes(out), addr)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._t.join(2)
        self.sock.close()


def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, clock_every: float | None = None,
             auto_fill: bool = True) -> tuple[Rig, list]:
    """Rig + 제어 SNTP. clock_every를 주면 감시 스레드의 주기 시계 점검 간격을 그 초로. 감시 객체 목록도 돌려준다."""
    monkeypatch.setattr(base, "FakeSntp", ControlSntp)
    sups: list = []
    orig = main_mod.Supervisor

    class _Sup(orig):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
            if clock_every is not None:
                kw["clock_every"] = clock_every
            super().__init__(*a, **kw)
            sups.append(self)

    monkeypatch.setattr(main_mod, "Supervisor", _Sup)
    return Rig(tmp_path, monkeypatch, auto_fill=auto_fill), sups


def halts(r: Rig, kind: str) -> list[dict]:
    return r.q("SELECT halt_type, state, actor, reason, created_at, released_at, released_by, conditions "
               "FROM halt WHERE halt_type=? ORDER BY created_at", (kind,))


def ops_log(r: Rig) -> list[dict]:
    out: list[dict] = []
    for p in sorted((r.base / "logs").glob("ops-*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def emergencies(r: Rig) -> list[str]:
    return [str(e.get("message")) for e in ops_log(r) if e.get("kind") == "ALERT" and e.get("level") == "EMERGENCY"]


def command_results(r: Rig, command: str) -> list[tuple]:
    return [tuple(x.values()) for x in r.q(
        "SELECT status, result FROM ops_command WHERE command=? ORDER BY created_seq", (command,))]


def resume_k8_at_start(r: Rig) -> None:
    assert wait_until(lambda: started(r), 20), "하트비트·K8이 20초 안에 안 보임"
    rc, txt = r.ops(resume_main, "K8")
    assert rc == 0, txt
    assert wait_until(lambda: "K8" not in r.active(), 10), (r.active(), r.q("SELECT * FROM ops_command"))


@pytest.fixture
def rig_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    r, _ = make_rig(tmp_path, monkeypatch)
    yield r
    r.close()


# --- 1. 실행 중 실시간(WS) 끊김 → K5(link:realtime) → 재접속 → 대조 → K5 자동 해제 -------------------------------

def test_r1_ws_drop_k5_reconnect_reconcile_auto_release(rig_ws: Rig) -> None:
    r = rig_ws
    seen: dict = {}

    def scenario() -> None:
        resume_k8_at_start(r)
        assert r.clock.now() < datetime(2026, 10, 6, 9, 29, 58, tzinfo=KST), "09:30 판정 전에 끊지 못함"
        logins0 = r.sim.ws_logins
        r.sim.ws_reject_reg = True          # 재접속 REG 거부 → 끊긴 상태 유지(토큰 무효화 없음)
        r.sim.drop_ws()
        assert wait_until(lambda: "K5" in r.active(), 10), r.active()
        seen["status_k5"] = r.ops(status_main)[1]
        k5 = halts(r, "K5")[-1]
        seen["k5_conds_down"] = json.loads(k5["conditions"])
        # 끊긴 동안 운영 대조(조회 경로 정상·대조 일치)가 실시간 경로를 풀면 안 된다 (RV-02)
        n_manual = len(r.q("SELECT 1 FROM reconciliation WHERE trigger='MANUAL'"))
        rc, txt = r.ops(reconcile_resolve_main, "--run", "--account", ACCOUNT)
        assert rc == 0, txt
        assert wait_until(lambda: len(r.q("SELECT 1 FROM reconciliation WHERE trigger='MANUAL'")) > n_manual, 10)
        time.sleep(1.0)
        seen["k5_after_manual_recon"] = "K5" in r.active()
        # 09:30 판정이 K5 중에 온다 → 판정 건너뜀(전역 정지), 주문 없음
        assert wait_until(lambda: r.q("SELECT 1 FROM audit_event WHERE kind='EVALUATION_SKIPPED' AND ref LIKE ?",
                                      ("%09:30:00%",)), 30), r.q("SELECT kind, ref, summary FROM audit_event "
                                                                  "WHERE kind LIKE 'EVALUATION%'")
        seen["k5_at_eval"] = "K5" in r.active()
        seen["orders_during_k5"] = r.apis(*ORDER_APIS)
        seen["logins_failed"] = r.sim.ws_logins - logins0
        # 복구: REG 허용 → 다음 재접속(지수 대기 최대 16초) → RECONNECTED → 대조 → K5 자동 해제
        seq_up = r.q("SELECT COALESCE(MAX(created_seq), 0) AS m FROM reconciliation")[0]["m"]
        r.sim.ws_reject_reg = False
        assert wait_until(lambda: "K5" not in r.active(), 45), (r.active(), halts(r, "K5"))
        seen["recon_after_up"] = r.q("SELECT trigger, result, created_at FROM reconciliation WHERE created_seq > ? "
                                     "ORDER BY created_seq", (seq_up,))
        time.sleep(2.0)
        seen["status_end"] = r.ops(status_main)[1]

    rc, out, errors = r.run(scenario, max_seconds=100)
    raise_first(errors)
    assert rc == 0, out
    assert "K5" in seen["status_k5"], seen["status_k5"]
    assert seen["k5_conds_down"].get("link:realtime", {}).get("ok") is False, seen["k5_conds_down"]
    assert seen["k5_after_manual_recon"], "실시간 끊긴 동안 대조만으로 K5가 풀림"
    assert seen["k5_at_eval"]
    assert seen["orders_during_k5"] == [], seen["orders_during_k5"]
    assert seen["logins_failed"] >= 1
    k5 = halts(r, "K5")
    assert len(k5) == 1 and k5[0]["state"] == "RELEASED" and k5[0]["released_by"] == "auto", k5
    conds = json.loads(k5[0]["conditions"])
    assert conds["link:realtime"]["ok"] is True and conds["reconciled"]["ok"] is True, conds
    assert conds["reconciled"]["at"] >= conds["link:realtime"]["at"], conds   # 재접속 뒤 대조로 해제
    assert seen["recon_after_up"] and all(x["result"] == "MATCH" for x in seen["recon_after_up"]), seen["recon_after_up"]
    # 위치·주문 일관: 주문 없음, 포지션 없음, 열린 불일치·K4·결과 불명 없음
    assert r.apis(*ORDER_APIS) == []
    assert not r.q("SELECT 1 FROM position WHERE CAST(quantity AS REAL) <> 0")
    assert r.sim.positions.get(CODE, (0,))[0] == 0
    assert r.q("SELECT item_id FROM recon_item WHERE state='OPEN'") == []
    assert halts(r, "K4") == []
    assert r.q("SELECT 1 FROM intent WHERE state=?", (IntentState.UNKNOWN.value,)) == []
    assert "해제되지 않은 정지: 0건" in seen["status_end"], seen["status_end"]
    assert any("K5" in m for m in emergencies(r))


# --- 2. 주기 SNTP 점검 실패(무응답)·오차 초과 → K6 → 서버 복구 → clock_ok → K6 자동 해제 -----------------------

def test_r2_sntp_failure_periodic_check_k6_and_auto_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, sups = make_rig(tmp_path, monkeypatch, clock_every=3.0)
    sntp: ControlSntp = r.sntp  # type: ignore[assignment]
    seen: dict = {}

    def cc() -> list[dict]:
        return r.q("SELECT at, source, offset_seconds, result FROM clock_check ORDER BY rowid")

    def scenario() -> None:
        assert wait_until(lambda: started(r), 20)
        assert wait_until(lambda: len(cc()) >= 3, 15), cc()       # 시작 1회 + 주기 점검
        seen["ok_before"] = [x["result"] for x in cc()]
        # (a) 서버 무응답 → 판정 불가 → K6
        sntp.mode = "silent"
        assert wait_until(lambda: "K6" in r.active(), 15), (r.active(), cc())
        seen["status_k6"] = r.ops(status_main)[1]
        seen["unknown_row"] = cc()[-1]
        sntp.mode = "ok"
        assert wait_until(lambda: "K6" not in r.active(), 15), (r.active(), cc(), halts(r, "K6"))
        # (b) 오차 5초 > 허용 1초 → K6 → 복구
        sntp.mode = "skew"
        assert wait_until(lambda: "K6" in r.active(), 15), (r.active(), cc())
        seen["exceeded_row"] = cc()[-1]
        sntp.mode = "ok"
        assert wait_until(lambda: "K6" not in r.active(), 15), (r.active(), cc(), halts(r, "K6"))
        seen["status_end"] = r.ops(status_main)[1]

    try:
        rc, out, errors = r.run(scenario, max_seconds=80)
    finally:
        r.close()
    raise_first(errors)
    assert rc == 0, out
    assert set(seen["ok_before"]) == {"OK"}, seen["ok_before"]
    assert "K6" in seen["status_k6"], seen["status_k6"]
    assert seen["unknown_row"]["result"] == "UNKNOWN" and seen["unknown_row"]["offset_seconds"] is None, seen
    assert seen["exceeded_row"]["result"] == "EXCEEDED", seen["exceeded_row"]
    assert abs(float(seen["exceeded_row"]["offset_seconds"]) - 5.0) < 0.5, seen["exceeded_row"]
    k6 = halts(r, "K6")
    assert len(k6) == 2 and all(h["state"] == "RELEASED" and h["released_by"] == "auto" for h in k6), k6
    assert all(json.loads(h["conditions"])["clock_ok"]["ok"] is True for h in k6), k6
    halt_part = seen["status_end"].split("해제되지 않은 정지")[1].split("처리 대기 명령")[0]
    assert "K8" in halt_part and "K6" not in halt_part, seen["status_end"]
    assert sups and sups[0].errors == [], sups[0].errors
    assert r.apis(*ORDER_APIS) == []


# --- 3. 장시간(약 3.5분) 장중 실행: 거짓 정지·긴급 알림·멈춤 없음, 대조 약 1분마다 ------------------------------

SOAK_SECONDS = 210.0


def test_r3_soak_market_hours_no_false_halts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, sups = make_rig(tmp_path, monkeypatch, clock_every=20.0)
    seen: dict = {"samples": []}

    def scenario() -> None:
        t0 = time.monotonic()
        resume_k8_at_start(r)
        seen["resumed_at"] = r.clock.now()
        while time.monotonic() - t0 < SOAK_SECONDS:
            hb = datetime.fromisoformat((r.base / "heartbeat").read_text(encoding="utf-8").strip())
            pos = r.q("SELECT quantity FROM position WHERE instrument=?", (CODE,))
            seen["samples"].append({"active": sorted(r.active()), "hb_age": (r.clock.now() - hb).total_seconds(),
                                    "qty": pos[0]["quantity"] if pos else "0"})
            time.sleep(5.0)
        seen["status"] = r.ops(status_main)[1]

    try:
        rc, out, errors = r.run(scenario, max_seconds=SOAK_SECONDS + 30)
    finally:
        r.close()
    raise_first(errors)
    assert rc == 0, out
    samples = seen["samples"]
    assert len(samples) >= 30, len(samples)
    # 재개 뒤 활성 정지 없음 (거짓 K8·K5·K6·K4 없음), 하트비트 신선(< 5초)
    assert all(s["active"] == [] for s in samples), [s for s in samples if s["active"]]
    assert max(s["hb_age"] for s in samples) < 5.0, max(s["hb_age"] for s in samples)
    # 미보유 → 09:30 매수 체결 → 보유 유지
    qtys = [float(s["qty"]) for s in samples]
    assert qtys[0] == 0 and qtys[-1] == 1, qtys
    assert r.apis(*ORDER_APIS) == ["kt10000"]
    # 정지 기록: 시작 K8(사용자 재개) 하나뿐
    rows = r.q("SELECT halt_type, state, released_by FROM halt")
    assert [(x["halt_type"], x["state"]) for x in rows] == [("K8", "RELEASED")], rows
    assert str(rows[0]["released_by"]).startswith("LOCAL"), rows
    # 긴급 알림: 시작 K8 발동 하나뿐. 판정 스레드 멈춤 알림·예외 기록 없음
    em = emergencies(r)
    assert len(em) == 1 and "K8" in em[0] and "프로세스 시작" in em[0], em
    log = ops_log(r)
    assert not [e for e in log if "진행 없음" in str(e.get("message", ""))]
    assert not [e for e in log if e.get("kind") == "EXCEPTION"], [e for e in log if e.get("kind") == "EXCEPTION"]
    assert sups and sups[0].errors == [] and sups[0].engine.errors == [], (sups[0].errors, sups[0].engine.errors)
    # 대조: 장중 주기 약 1분마다, 모두 일치
    per = r.q("SELECT created_at, result FROM reconciliation WHERE trigger='PERIODIC' ORDER BY created_seq")
    assert 2 <= len(per) <= 4, per
    assert all(x["result"] == "MATCH" for x in per), per
    ats = [datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")) for x in per]
    gaps = [(b - a).total_seconds() for a, b in zip(ats, ats[1:])]
    assert all(59.0 <= g <= 63.0 for g in gaps), gaps
    assert r.q("SELECT 1 FROM reconciliation WHERE result<>'MATCH'") == []
    # 시계 점검 약 20초마다, 모두 OK
    cc = r.q("SELECT result FROM clock_check")
    assert len(cc) >= 8 and {x["result"] for x in cc} == {"OK"}, cc
    assert "보유 포지션: 1건" in seen["status"] and "해제되지 않은 정지: 0건" in seen["status"], seen["status"]
    assert "열린 대조 불일치: 0건" in seen["status"] and "결과 불명 의도: 0건" in seen["status"], seen["status"]


# --- 4. 실행 중 K8(시계 점프) → 대조 전 재개 거부 → 다음 주기 대조 → recovery_ok → 재개 성공 (DP-41) -------------

def test_r4_runtime_k8_clock_jump_resume_after_next_reconcile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, sups = make_rig(tmp_path, monkeypatch)
    seen: dict = {}

    def k8_rows() -> list[dict]:
        return [h for h in halts(r, "K8") if h["state"] != "RELEASED"]

    def scenario() -> None:
        resume_k8_at_start(r)
        assert r.clock.now() < datetime(2026, 10, 6, 9, 29, 58, tzinfo=KST)
        t_jump = datetime.now(UTC)
        r.offset -= timedelta(seconds=5)          # 실행 중 벽시계 5초 뒤로 (절전·시계 조정 모사)
        assert wait_until(lambda: k8_rows(), 10), halts(r, "K8")
        seen["k8"] = k8_rows()[0]
        # 대조 전 재개 → 거부 (recovery_ok 없음)
        assert r.ops(resume_main, "K8")[0] == 0
        assert wait_until(lambda: len(command_results(r, "RESUME")) >= 2
                          and command_results(r, "RESUME")[-1][0] != "PENDING", 10), command_results(r, "RESUME")
        seen["resume_early"] = command_results(r, "RESUME")[-1]
        seen["k8_still"] = "K8" in r.active()
        # 다음 주기 대조(1분 주기) → 실행 중 복구 판정 → recovery_ok → 재개 대기
        n_per = len(r.q("SELECT 1 FROM reconciliation WHERE trigger='PERIODIC'"))
        assert wait_until(lambda: len(r.q("SELECT 1 FROM reconciliation WHERE trigger='PERIODIC'")) > n_per, 75)
        assert wait_until(lambda: k8_rows() and k8_rows()[0]["state"] == "AWAITING_RESUME", 5), k8_rows()
        seen["conds"] = json.loads(k8_rows()[0]["conditions"])
        seen["jump_wall_s"] = (datetime.now(UTC) - t_jump).total_seconds()
        assert r.ops(resume_main, "K8")[0] == 0
        assert wait_until(lambda: "K8" not in r.active(), 10), (r.active(), command_results(r, "RESUME"))
        seen["resume_late"] = command_results(r, "RESUME")[-1]
        seen["status"] = r.ops(status_main)[1]

    try:
        rc, out, errors = r.run(scenario, max_seconds=110)
    finally:
        r.close()
    raise_first(errors)
    assert rc == 0, out
    assert seen["k8"]["actor"] == "monitor" and "시계 점프" in seen["k8"]["reason"], seen["k8"]
    assert seen["resume_early"][0] == "REJECTED" and "recovery_ok" in str(seen["resume_early"][1]), seen["resume_early"]
    assert seen["k8_still"]
    assert seen["conds"]["recovery_ok"]["ok"] is True and "실행 중 복구 판정" in seen["conds"]["recovery_ok"]["evidence"], \
        seen["conds"]
    assert seen["resume_late"][0] == "DONE", seen["resume_late"]
    assert "해제되지 않은 정지: 0건" in seen["status"], seen["status"]
    # K8 중 09:30 판정은 건너뜀 → 주문 없음 (늦은 판정 금지: 재개 뒤에도 다시 판정하지 않음)
    assert r.apis(*ORDER_APIS) == []
    assert r.q("SELECT 1 FROM audit_event WHERE kind='EVALUATION_SKIPPED' AND ref LIKE ?", ("%09:30:00%",))
    assert sups and sups[0].loop.events and "시계 점프" in sups[0].loop.events[0], sups[0].loop.events
    assert len(sups[0].loop.events) == 1, sups[0].loop.events
