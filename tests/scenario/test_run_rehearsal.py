"""실제 진입점 `run_main` 리허설 (PAPER, 로컬 키움 시뮬레이터 127.0.0.1) — 기존 e2e는 부품을 손으로 조립했다.

여기서는 app/main.run_main을 pytest 주 스레드에서 그대로 돌린다(실행 잠금·SNTP·감시 스레드·실시간·시세 스레드·
SIGINT 처리기 포함). 운영 조작(resume/kill/status/preflight)은 보조 스레드에서 실제 운영 함수(ops.cli·app.preflight)로 한다.

src를 고치지 않고 쓴 주입 지점 (보고서 "src seam" 참조):
- 키움 접속 대상: `trading_system.broker.kiwoom.adapter.KiwoomConfig`를 시뮬레이터 URL을 넣는 팩토리로 바꿈
  (_paper_broker가 함수 안에서 import하므로 모듈 속성 교체가 먹는다. check_endpoint는 루프백 허용).
- 시계: `SystemClock.now`를 "실제 시각 + 고정 오프셋"으로 바꿔 거래일(2026-10-06 화) 09:29:4x KST에서 시작.
  실제 시간 흐름은 그대로(감시 스레드의 monotonic 루프 감시와 맞음).
- SNTP: 임시 루트의 app.paper.toml 사본에서 sntp_servers만 로컬 가짜 UDP 서버(127.0.0.1)로 바꿈. 가짜 서버는
  실제 UTC를 답한다(조회 함수 query_offset은 패치하지 않은 실제 시각을 쓰므로 오차 ≈ 0).
- 자격정보: 임시 루트에 시뮬레이터 가상 값으로 .env.paper를 시험이 직접 씀(실제 .env*는 읽지 않음).
- 정지: Ctrl+C 대응 경로 = `_thread.interrupt_main()` → run_main이 설치한 SIGINT 처리기 → stop 이벤트.
실제 네트워크·실제 키움·실제 SNTP 서버 연결 없음.
"""

from __future__ import annotations

import _thread
import io
import shutil
import signal
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.rehearsal   # A-084 ⑥: 빠른 확인은 -m "not rehearsal and not e2e"

from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET, Sim, _md_init
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.main import run_main
from trading_system.app.preflight import preflight_main
from trading_system.app.strategy_tool import strategy_main
from trading_system.domain.clock import KST, SystemClock
from trading_system.domain.enums import IntentState, Mode
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.ops.cli import kill_main, resume_main, status_main
from trading_system.storage.repository import Repository

REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "KR-PAPER-01"
CODE = "005930"
DAY = date(2026, 10, 6)                       # 화요일 거래일 (10-05 대체공휴일)
START_KST = datetime(2026, 10, 6, 9, 29, 40, tzinfo=KST)
ENV = {"TRADE_MODE": "PAPER"}
ORDER_APIS = ("kt10000", "kt10001", "kt10002", "kt10003")


# --- 보조: 가짜 SNTP(UDP) ----------------------------------------------------------------------------

def _ntp(t: datetime) -> tuple[int, int]:
    d = t - datetime(1900, 1, 1, tzinfo=UTC)
    return d.days * 86400 + d.seconds, int(d.microseconds * (2**32) / 1_000_000)


class FakeSntp:
    """127.0.0.1 UDP. 요청의 transmit을 originate로 돌려주고 실제 UTC를 receive/transmit으로 답한다."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
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
            now = _ntp(datetime.now(UTC))
            out = bytearray(48)
            out[0] = (0 << 6) | (4 << 3) | 4     # 서버 응답
            out[1] = 2                           # stratum 2
            out[24:32] = data[40:48]             # originate = 요청 transmit
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


# --- 보조: 임시 루트 -----------------------------------------------------------------------------------

def make_root(tmp: Path, sntp_port: int) -> Path:
    root = tmp / "repo"
    for rel in ("config/limits.paper.toml", "config/market.paper.toml", "config/ticks/krx.toml",
                "config/strategies/paper0-verify-v1.toml", "docs/capabilities.md"):
        if not (REPO / rel).exists():   # 없는 파일은 건너뜀 (krx.toml은 CP-032 D-173부터 있음 → run이 호가 표를 읽음)
            continue
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    shutil.copytree(REPO / "config" / "calendar", root / "config" / "calendar")
    app = (REPO / "config" / "app.paper.toml").read_text(encoding="utf-8")
    # 배포 설정 v2 교체(2026-10-07, A-067 B-5) — 예행연습은 v1 기준이라 복사본의 전략 항목만 v1로 고정(시험 변경 사유 기록)
    for v2, v1 in (("paper0_verify:Paper0VerifyV2", "paper0_verify:Paper0Verify"),
                   ("paper0-verify-v2.toml", "paper0-verify-v1.toml"),
                   ('verification_approval = "A-067"', 'verification_approval = "A-037"')):
        app = app.replace(v2, v1)
    lines = [f'sntp_servers = ["127.0.0.1:{sntp_port}"]' if ln.startswith("sntp_servers") else ln
             for ln in app.splitlines()
             if ln.strip() not in ("[telegram]", "enabled = true", "enabled = false")]  # 텔레그램은 test_telegram_run에서
    assert any(ln.startswith("sntp_servers = [\"127.0.0.1") for ln in lines)
    assert "connection_approved = true" in app   # 실제 설정 그대로 (사용자 승인 A-035로 true)
    (root / "config" / "app.paper.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 가상 자격정보(시뮬레이터 값). 실제 .env*는 읽지도 복사하지도 않는다.
    (root / ".env.paper").write_text(
        "TRADE_MODE=PAPER\n"
        f"KIWOOM_PAPER_APP_KEY={APP_KEY}\nKIWOOM_PAPER_SECRET_KEY={SECRET}\n"
        f"KIWOOM_PAPER_ACCOUNT_NO={ACCOUNT_NO}\nKIWOOM_PAPER_ACCOUNT_ALIAS={ACCOUNT}\n", encoding="utf-8")
    with redirect_stdout(io.StringIO()):
        assert strategy_main(["register", "--mode", "PAPER", "--root", str(root)]) == 0
        ev = root / "var" / "backtest" / "paper0-verify-v1" / "report.md"
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text("가상 증거 (시험용)\n", encoding="utf-8")
        assert strategy_main(["transition", "--mode", "PAPER", "--root", str(root), "paper0-verify", "1",
                              "BACKTEST_PASSED", "--approval", "A-901", "--evidence",
                              "var/backtest/paper0-verify-v1/report.md"]) == 0
        assert strategy_main(["transition", "--mode", "PAPER", "--root", str(root), "paper0-verify", "1",
                              "PAPER", "--approval", "A-902"]) == 0
    return root


class _Router(io.TextIOBase):
    """sys.stdout 대역: 스레드별로 출력 버퍼를 나눈다(주 스레드 run 출력과 보조 스레드 운영 명령 출력이 섞이지 않게)."""

    def __init__(self, default: io.StringIO) -> None:
        self.default = default
        self.per: dict[int, io.StringIO] = {}

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        return self.per.get(threading.get_ident(), self.default).write(s)


def capture(fn: Callable[[], int]) -> tuple[int, str]:
    """현재 스레드의 출력만 잡아 fn을 부른다."""
    buf = io.StringIO()
    out = sys.stdout
    if isinstance(out, _Router):
        out.per[threading.get_ident()] = buf
        try:
            rc = fn()
        finally:
            out.per.pop(threading.get_ident(), None)
    else:
        with redirect_stdout(buf):
            rc = fn()
    return rc, buf.getvalue()


def daily_rows(cal: KrxCalendar, last: date, days: int = 400) -> list[dict]:
    rows, d = [], last
    while d > last - timedelta(days=days):
        if cal.is_trading_day(d):
            rows.append({"dt": d.strftime("%Y%m%d"), "open_pric": "70000", "high_pric": "70500",
                         "low_pric": "69500", "cur_prc": "+70000", "trde_qty": "1000", "pred_pre": "+0"})
        d -= timedelta(days=1)
    return rows   # 최신 먼저


class Rig:
    """시험 한 건의 환경: 시뮬레이터·가짜 SNTP·임시 루트·시계 오프셋."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, *, auto_fill: bool) -> None:
        self.offset = START_KST - datetime.now(UTC)
        rig = self
        monkeypatch.setattr(SystemClock, "now", lambda self: datetime.now(UTC) + rig.offset)
        self.clock = SystemClock()
        self.sntp = FakeSntp()
        self.root = make_root(tmp, self.sntp.port)
        self.base = self.root / "var" / "paper"
        cal = KrxCalendar.load_dir(self.root / "config" / "calendar")
        assert cal.is_trading_day(DAY) and not cal.is_trading_day(date(2026, 10, 5))
        self.sim = Sim(now=lambda: self.clock.now().astimezone(KST).replace(tzinfo=None), auto_fill=auto_fill)
        self.sim.entr = Decimal("100000000")
        _md_init(self.sim)
        self.sim.daily[CODE] = daily_rows(cal, date(2026, 10, 2))
        self.sim.info[CODE] = {"cur_prc": "+70000", "upl_pric": "+91000", "lst_pric": "-49000", "base_pric": "70000"}
        self.sim.master["0"] = [{"code": CODE, "state": "", "orderWarning": "0"}]

        import trading_system.broker.kiwoom.adapter as adapter_mod
        orig = adapter_mod.KiwoomConfig
        sim = self.sim

        def cfg(*a, **kw):  # type: ignore[no-untyped-def]
            kw.update(rest_url=sim.url, ws_url=sim.ws_url, per_tr_interval=0.02)
            return orig(*a, **kw)

        monkeypatch.setattr(adapter_mod, "KiwoomConfig", cfg)
        self.orig_sigint = signal.getsignal(signal.SIGINT)

    def jump_to(self, at: datetime) -> None:
        """프로세스가 꺼진 사이에만 쓴다(실행 중 점프는 시계 점프 K8 대상)."""
        self.offset = at - datetime.now(UTC)

    def close(self) -> None:
        signal.signal(signal.SIGINT, self.orig_sigint)   # run_main이 설치한 처리기를 원래대로
        self.sim.close()
        self.sntp.close()

    # --- 조회 (별도 연결, 읽기만) -------------------------------------------------------------------
    def q(self, sql: str, params: tuple = ()) -> list[dict]:
        repo = Repository(self.base / "trading.db", Mode.PAPER)
        try:
            return repo.select(sql, params)
        finally:
            repo.close()

    def active(self) -> set[str]:
        repo = Repository(self.base / "trading.db", Mode.PAPER)
        try:
            return set(repo.active_halts(ACCOUNT))
        finally:
            repo.close()

    def apis(self, *names: str) -> list[str]:
        return [a for a, _, _ in list(self.sim.log) if a in names]

    def ops(self, fn: Callable[[list[str]], int], *args: str) -> tuple[int, str]:
        return capture(lambda: fn(["--mode", "PAPER", "--root", str(self.root), *args]))

    # --- 실행 -------------------------------------------------------------------------------------
    def run(self, scenario: Callable[[], None], max_seconds: float = 55.0) -> tuple[int, str, list[BaseException]]:
        """주 스레드에서 run_main, 보조 스레드에서 scenario. scenario가 끝나면 Ctrl+C 대응(interrupt_main)."""
        errors: list[BaseException] = []
        handler_before = signal.getsignal(signal.SIGINT)

        def worker() -> None:
            try:
                scenario()
            except BaseException as exc:  # noqa: BLE001 — 주 스레드에서 다시 낸다
                errors.append(exc)
            finally:
                # 처리기가 설치된 뒤에만 보낸다(그 전이면 KeyboardInterrupt가 pytest로 감)
                if wait_until(lambda: signal.getsignal(signal.SIGINT) is not handler_before, 20):
                    _thread.interrupt_main()

        out = io.StringIO()
        t = threading.Thread(target=worker, name="rehearsal-ops", daemon=True)
        t.start()
        with redirect_stdout(_Router(out)):
            rc = run_main(self.root, dict(ENV), max_seconds=max_seconds)
        t.join(30)
        fatal = self.base / "logs" / "fatal.log"
        if fatal.exists():
            errors.append(AssertionError(f"fatal.log 생김: {fatal.read_text(encoding='utf-8')[:2000]}"))
        return rc, out.getvalue(), errors


def wait_until(cond: Callable[[], object], timeout: float, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001 — DB 잠금 등 일시 오류는 다시 본다
            pass
        time.sleep(step)
    return False


def started(rig: Rig) -> bool:
    return (rig.base / "heartbeat").exists() and "K8" in rig.active()


@pytest.fixture
def rig_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    r = Rig(tmp_path, monkeypatch, auto_fill=True)
    yield r
    r.close()


@pytest.fixture
def rig_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    r = Rig(tmp_path, monkeypatch, auto_fill=False)
    yield r
    r.close()


def raise_first(errors: list[BaseException]) -> None:
    if errors:
        raise errors[0]


# --- 1. 시작 → 요약·하트비트·실행 잠금·preflight 거부 ----------------------------------------------------

def test_1_start_summary_heartbeat_lock_and_preflight_refused(rig_fill: Rig) -> None:
    r = rig_fill
    seen: dict = {}

    def scenario() -> None:
        assert wait_until(lambda: started(r), 20), "하트비트·K8이 20초 안에 안 보임"
        assert wait_until(lambda: r.apis("ka10001"), 10), "시세 조회 없음"
        n_tok = len(r.apis("au10001"))
        seen["second"], seen["second_out"] = capture(
            lambda: run_main(r.root, dict(ENV), max_seconds=1))                # 두 번째 실행
        seen["pre"], seen["pre_out"] = capture(
            lambda: preflight_main(r.root, dict(ENV), rest_url=r.sim.url, ws_url=r.sim.ws_url, clock=r.clock,
                                   per_tr_interval=0.0, sntp_query=lambda h, p: Decimal("0")))
        seen["tok_after"] = len(r.apis("au10001")) - n_tok
        seen["sntp"] = r.sntp.requests
        seen["cc"] = r.q("SELECT result, source FROM clock_check ORDER BY seq")

    rc, out, errors = r.run(scenario, max_seconds=40)
    raise_first(errors)
    assert rc == 0, out
    assert "시작: 모드 PAPER, 계좌 KR-PAPER-01, 복구 판정 통과" in out, out
    assert "활성 정지:" in out and "K8" in out.split("활성 정지:")[1].splitlines()[0], out
    assert "전략 paper0-verify v1" in out and ": PAPER" in out, out
    assert "결과 불명 의도: 0건" in out
    # 두 번째 run은 잠금으로 거부(연결 없음)
    assert seen["second"] == 2 and "시작 거부" in seen["second_out"], seen["second_out"]
    # 실행 중 preflight 거부, 토큰 발급(연결) 없음
    assert seen["pre"] == 2 and "실행 중" in seen["pre_out"], seen["pre_out"]
    assert seen["tok_after"] == 0
    # SNTP: 로컬 가짜 서버로 시작 점검 OK (K6 없음)
    assert seen["sntp"] >= 1 and seen["cc"] and seen["cc"][0]["result"] == "OK", seen["cc"]
    assert "K6" not in r.active()
    # 정상 종료 후 잠금 풀림
    acquire_instance_lock(r.base).release()


# --- 2+3. RESUME K8 → 09:30 매수 체결 → status 포지션 → Ctrl+C 종료 → 재시작 K8·중복 없음·대조 일치 -------------

def test_2_3_resume_buy_fill_status_graceful_stop_restart(rig_fill: Rig) -> None:
    r = rig_fill
    seen: dict = {}

    def first() -> None:
        assert wait_until(lambda: started(r), 20)
        rc, txt = r.ops(resume_main, "K8")
        assert rc == 0, txt
        assert wait_until(lambda: "K8" not in r.active(), 10), (r.active(), r.q("SELECT * FROM ops_command"))
        assert r.clock.now() < datetime(2026, 10, 6, 9, 30, tzinfo=KST), "09:30 전에 재개 못 함"
        assert wait_until(lambda: r.q("SELECT 1 FROM intent WHERE state='FILLED'"), 40), (
            r.q("SELECT request_type, result, failed_check, reason FROM gate_decision"),
            r.q("SELECT kind, summary FROM audit_event WHERE kind LIKE 'EVALUATION%'"))
        assert wait_until(lambda: r.q("SELECT 1 FROM position WHERE CAST(quantity AS REAL) = 1"), 5)
        rc, txt = r.ops(status_main)
        seen["status"] = txt

    rc, out, errors = r.run(first, max_seconds=55)
    raise_first(errors)
    assert rc == 0, out
    assert "보유 포지션: 1건" in seen["status"] and f"{ACCOUNT} {CODE} 수량 1" in seen["status"], seen["status"]
    assert "해제되지 않은 정지: 0건" in seen["status"], seen["status"]
    assert r.apis(*ORDER_APIS) == ["kt10000"]
    # 3. 정상 종료: 잠금 풀림, 토큰 폐기(au10002), 결과 불명 없음
    acquire_instance_lock(r.base).release()
    assert r.apis("au10002"), "종료 시 토큰 폐기 없음"
    assert r.q("SELECT 1 FROM intent WHERE state=?", (IntentState.UNKNOWN.value,)) == []
    assert r.q("SELECT 1 FROM intent WHERE state IN ('PENDING_SEND','SENT','ACCEPTED','PARTIAL')") == []

    # 재시작 (09:30 판정 유예 5분 안): K8 다시, 재개해도 중복 매수 없음, 대조 일치
    def second() -> None:
        assert wait_until(lambda: started(r) and r.q(
            "SELECT 1 FROM halt WHERE halt_type='K8' AND state<>'RELEASED'"), 20), r.active()
        seen["k8_again"] = "K8" in r.active()
        rc, txt = r.ops(resume_main, "K8")
        assert rc == 0, txt
        assert wait_until(lambda: "K8" not in r.active(), 10), r.active()
        time.sleep(5)   # 판정 주기 여러 번 (유예 안에서 다시 매수하려 하는지)
        seen["status2"] = r.ops(status_main)[1]

    t_restart = r.clock.now()
    assert t_restart < datetime(2026, 10, 6, 9, 35, tzinfo=KST)
    rc, out2, errors = r.run(second, max_seconds=40)
    raise_first(errors)
    assert rc == 0, out2
    assert seen["k8_again"]
    assert "복구 판정 통과" in out2 and "K8" in out2.split("활성 정지:")[1].splitlines()[0], out2
    assert r.apis(*ORDER_APIS) == ["kt10000"], "재시작 후 중복 주문"
    assert [i["request_type"] for i in r.q("SELECT request_type FROM intent")] == ["ENTRY"]
    assert r.q("SELECT item_id FROM recon_item WHERE state='OPEN'") == []
    assert r.q("SELECT 1 FROM halt WHERE halt_type='K4'") == [], "재시작 대조에서 불일치(K4)"
    assert "열린 대조 불일치: 0건" in seen["status2"] and "보유 포지션: 1건" in seen["status2"], seen["status2"]
    assert "해제되지 않은 정지: 0건" in seen["status2"], seen["status2"]
    assert len(r.apis("au10002")) == 2


# --- 5. 보유 상태로 종료 → 14:29 재시작 → K8 재개 → 14:30 매도 체결 → 포지션 0 --------------------------------

def test_5_restart_before_exit_time_sells_at_1430(rig_fill: Rig) -> None:
    r = rig_fill
    seen: dict = {}

    def morning() -> None:
        assert wait_until(lambda: started(r), 20)
        assert r.ops(resume_main, "K8")[0] == 0
        assert wait_until(lambda: r.q("SELECT 1 FROM position WHERE CAST(quantity AS REAL) = 1"), 40)

    rc, out, errors = r.run(morning, max_seconds=55)
    raise_first(errors)
    assert rc == 0, out
    r.jump_to(datetime(2026, 10, 6, 14, 29, 40, tzinfo=KST))

    def afternoon() -> None:
        assert wait_until(lambda: started(r) and r.q(
            "SELECT 1 FROM halt WHERE halt_type='K8' AND state<>'RELEASED'"), 20), (
            [tuple(x.values()) for x in r.q("SELECT halt_type, state, created_at, reason FROM halt")],
            [tuple(x.values()) for x in r.q("SELECT command, status, result, created_at FROM ops_command")])
        assert r.ops(resume_main, "K8")[0] == 0
        assert wait_until(lambda: "K8" not in r.active(), 10), r.active()
        assert wait_until(lambda: r.q("SELECT 1 FROM intent WHERE request_type='EXIT' AND state='FILLED'"), 40), (
            r.q("SELECT request_type, result, failed_check, reason FROM gate_decision"),
            r.q("SELECT kind, summary FROM audit_event WHERE kind LIKE 'EVALUATION%'"))
        assert wait_until(lambda: not r.q("SELECT 1 FROM position WHERE CAST(quantity AS REAL) > 0"), 5)
        seen["status"] = r.ops(status_main)[1]

    rc, out2, errors = r.run(afternoon, max_seconds=55)
    raise_first(errors)
    assert rc == 0, out2
    assert r.apis(*ORDER_APIS) == ["kt10000", "kt10001"]
    assert r.sim.positions[CODE][0] == 0
    assert "보유 포지션: 0건" in seen["status"] and "미체결 주문: 0건" in seen["status"], seen["status"]
    assert "해제되지 않은 정지: 0건" in seen["status"] and "열린 대조 불일치: 0건" in seen["status"], seen["status"]
    assert [tuple(x.values()) for x in r.q("SELECT request_type, state FROM intent ORDER BY created_seq")] == [
        ("ENTRY", "FILLED"), ("EXIT", "FILLED")]


# --- 4. 실행 중 KILL → 2초 안 K1 → 매수 미체결 취소 ---------------------------------------------------------

OPEN_NEW_BUY = ("SELECT 1 FROM broker_order WHERE side='BUY' AND state IN ('ACCEPTED','PARTIAL') "
                "AND origin IN ('INTERNAL_NEW','INTERNAL_MODIFY')")   # repository.open_orders와 같은 기준


def kill_flow(r: Rig) -> dict:
    seen: dict = {}

    def scenario() -> None:
        assert wait_until(lambda: started(r), 20)
        assert r.ops(resume_main, "K8")[0] == 0
        assert wait_until(lambda: "K8" not in r.active(), 10), r.active()
        assert wait_until(lambda: r.q(OPEN_NEW_BUY), 40), (
            r.q("SELECT request_type, result, failed_check, reason FROM gate_decision"),
            r.q("SELECT state FROM intent"))
        t0 = time.monotonic()
        rc, txt = r.ops(kill_main, "--reason", "리허설")
        assert rc == 0, txt
        assert wait_until(lambda: "K1" in r.active(), 2.0), "K1이 2초 안에 안 보임"
        seen["k1_s"] = time.monotonic() - t0
        assert wait_until(lambda: r.apis("kt10003"), 10), r.apis(*ORDER_APIS)
        assert wait_until(lambda: not r.q(OPEN_NEW_BUY), 10), (
            [tuple(x.values()) for x in r.q("SELECT order_no, side, state, origin FROM broker_order")],
            r.q("SELECT request_type, state FROM intent"))
        seen["orders"] = [tuple(x.values()) for x in r.q(
            "SELECT order_no, side, state, origin FROM broker_order ORDER BY order_no")]
        seen["intents"] = [tuple(x.values()) for x in r.q(
            "SELECT request_type, state FROM intent ORDER BY created_seq")]
        seen["status"] = r.ops(status_main)[1]

    rc, out, errors = r.run(scenario, max_seconds=55)
    raise_first(errors)
    assert rc == 0, out
    acquire_instance_lock(r.base).release()
    return seen


def test_4_kill_while_running_k1_fast_and_open_buy_cancelled(rig_open: Rig) -> None:
    r = rig_open
    seen = kill_flow(r)
    assert seen["k1_s"] <= 2.0
    assert r.apis(*ORDER_APIS) == ["kt10000", "kt10003"]
    assert all(not o.open for o in r.sim.orders.values())
    assert seen["orders"][0][:3] == ("0000001", "BUY", "CANCELLED"), seen["orders"]
    assert [i[0] for i in seen["intents"]] == ["ENTRY", "CANCEL"], seen["intents"]
    assert "K1" in seen["status"] and "KILL 파일: 있음" in seen["status"], seen["status"]
    assert r.q("SELECT 1 FROM intent WHERE state=?", (IntentState.UNKNOWN.value,)) == []
    assert "K1" in r.active()                                   # 정지는 청산이 아님 — 해제는 사람이


# (수정됨: ops/cli.py status 미체결 조회에서 INTERNAL_CANCEL 행 제외 — 이전 xfail)
def test_4b_status_shows_no_open_orders_after_kill_cancel(rig_open: Rig) -> None:
    seen = kill_flow(rig_open)
    assert "미체결 주문: 0건" in seen["status"], seen["status"]
