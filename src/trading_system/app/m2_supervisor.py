"""M2 감독 프로세스 — 무인 운영, **M2 PAPER(바이낸스 Spot 테스트넷) 전용** (CP-037 D-223·D-225~D-228·D-230, T-054,
승인 A-113 Q-1 (가)·Q-2 (나)). LIVE·BACKTEST·M1·M3는 자동화하지 않는다(A-078과 같은 원칙, CLAUDE.md §4·§7).

M2 체크아웃(C:\\AI\\trading_system\\m2-binance)에서 `scripts/m2-supervisor.py --mode PAPER --market M2`로 돈다(작업 스케줄러
로그온 트리거·매일 보조 트리거 — 등록은 사용자가 T-055 스크립트로, 자동 실행 등록 승인 D-231 뒤). 하는 일:

  1. 모드·시장 확인: --mode PAPER, --market M2, TRADE_MODE(환경 변수 → 루트 .env 한 줄) = PAPER, M2 시작 검사
     (config/m2·자격정보·DB 메타), 연결 전 조건(접속 스위치·키), 시작 일치 표(PAPER·M2·테스트넷 호스트). 하나라도 아니면
     **아무 파일도 만들지 않고** 코드 2(V-296 (a)(b)) — 사유는 표준 출력에만.
  2. 텔레그램: config/m2/app.paper.toml [telegram] enabled = true + M2 전용 봇 키(D-213) — 아니면 같은 방식으로 코드 2(V-296 (c)).
  3. 감독 잠금 var/paper/m2-supervisor.lock — 이미 돌고 있으면 즉시 코드 0(매일 보조 트리거의 정상 경우, V-296 (d)).
  4. run이 이미 실행 중(var/paper/run.lock — 수동 run) → 경고 후 코드 2, 새 run 없음(V-296 (e)).
  5. 시작 점검(M2 preflight, 같은 프로세스) 실패 → 긴급 알림, run 시작 안 함, 코드 2(V-296 (f)).
  6. run 시작: `scripts/run-m2.py`를 자식 프로세스로(실행 잠금·시작 K8·감시 스레드·하트비트 그대로).
  7. K8 자동 재개(D-226): 운영 명령 표에 채널 AUTO·주체 m2-supervisor로 RESUME K8(not_after = 넣은 시각 + 10분).
     run이 명령 시점에 새 대조 + 01 §2.11 조건 1~5 + M2 추가 조건(서버 시각·규칙·사용자 스트림·시세 — app/m2_resume_conditions)
     을 본다. 거부되면 5분 간격, 최대 12회(1시간) — 12회 거부면 경고 1건 후 중단(사용자 /resume K8 가능).
     사람 확인 정지(K1·K2·K3·K4·K7)가 걸려 있으면 명령을 넣지 않는다(알림은 정지 감시가 보냄).
  8. 감시: run 생존·하트비트(90초). 사람 확인 정지 종류가 바뀌면 알림(K4 = 테스트넷 리셋 의심 긴급 알림, D-228).
     하트비트가 멈춰도 재시작·강제 종료하지 않는다(알림만).
  9. 재시작(D-225): run이 끝나면 — 코드 0(정지 요청 파일·ops-stop = 의도된 종료) → 재시작 안 함, 긴급 알림, **감독도 종료**
     (다시 시작: 사용자 또는 다음 로그온). 코드 2·사람 확인 정지 중 종료·직전 24시간 자동 재시작 2회 도달 → 재시작 안 함,
     긴급 알림 "M2 정지 — 사람 확인", run 없이 대기(감독 잠금 유지 — 매일 보조 트리거가 새 run을 띄우지 않게). 그 밖(비정상
     종료) → 5분(2회째 15분) 뒤 사람 확인 정지 재확인·시작 점검 통과 시 재시작. 재시작 run은 항상 K8 → 시작 대조 → 7.
     재시작 횟수는 M2 DB 감사(kind M2_SUPERVISOR, event RESTART)로 센다 — 감독이 다시 떠도 24시간 상한이 이어진다.
 10. UTC 일과(D-227): 감독이 도는 동안 UTC 날짜가 바뀌면 00:05 이후 일 경계 대조(DAY_BOUNDARY) 기록을 확인하고(최대 00:30까지
     기다림) paper-review → 실행 중 백업(SQLite 온라인 백업 API, storage/maintenance.backup — 실행 잠금을 보지 않음) →
     텔레그램 일일 요약 1건(reporting/m2_day_summary, 계좌 식별·금액 원문 없음). 실패는 요약의 "확인 필요". run은 건드리지
     않는다(재시작 없음). KST 자정은 아무 영향 없음.
 11. 정지: 감독 정지 요청 파일 var/paper/M2-SUPERVISOR-STOP(`scripts/m2-supervisor.py --stop`)을 보면 run에 정지 요청 파일
     (var/paper/STOP)을 쓰고 최대 3분 기다린 뒤 감독 종료 — 강제 종료하지 않는다(D-230). KILL 파일·/kill은 run의 K1(run 유지,
     재시작 대상 아님). 처리되지 않은 예외(Ctrl+C 포함)는 이 감독이 띄운 run에 정지 요청 파일을 쓰고 코드 3.

감독은 주문하지 않는다. 주문·정지·재개 판정은 모두 run(엔진·게이트·정지 관리자)이 기존 경로로 한다(CLAUDE.md §8-2). 감독은
K8 재개 요청만 운영 명령 표에 넣는다. 정지는 청산이 아니다(§8-8) — 어디서도 자동 청산 없음.
알림은 텔레그램으로 직접(감사 ALERT에 쓰지 않음, D-154·D-230). 진행은 var/paper/logs/m2-supervisor-YYYYMMDD.log(UTC 날짜,
가림 처리)에 남는다.

종료 코드: 0 정상(감독 정지 요청, 의도된 run 종료, 이미 실행 중) | 2 run 시작 전 거부(설정·모드·텔레그램·수동 run·시작 점검)
| 3 처리되지 않은 예외(Ctrl+C 포함).
"""

from __future__ import annotations

import argparse
import os
import sys
import time as _time
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from trading_system.config.errors import ConfigError
from trading_system.domain.clock import Clock, SystemClock, from_iso, to_iso
from trading_system.domain.enums import HaltType, Mode
from trading_system.halt.manager import Channel
from trading_system.monitoring.liveness import watchdog_check
from trading_system.ops.cli import REPO_ROOT
from trading_system.storage.jsonlog import SENSITIVE_KEYS, Masker
from trading_system.storage.repository import OrderIdentity, Repository

from .autopilot import HUMAN_HALTS, RunHandle, SubprocessRun, TelegramNotify, captured, console_python, strip_paths
from .instance_lock import InstanceLocked, acquire_instance_lock, instance_running
from .main import STOP_REQUEST
from .supervisor import write_fallback

SUPERVISOR_ACTOR = "m2-supervisor"
M2_SUPERVISOR_LOCK = "m2-supervisor.lock"      # var/paper — 감독 단일 실행
SUPERVISOR_STOP = "M2-SUPERVISOR-STOP"         # var/paper — 감독 정지 요청 파일 (--stop이 쓴다)
AUDIT_KIND = "M2_SUPERVISOR"                   # M2 DB 감사: summary.event = RUN_START·RUN_EXIT·RESTART·…
SUPERVISOR_FALLBACK = "alerts-fallback-m2-supervisor.log"
EXIT_OK, EXIT_REFUSED, EXIT_ABORTED = 0, 2, 3
EXIT_HELP = ("종료 코드: 0 정상(감독 정지 요청·의도된 run 종료·이미 실행 중) | 2 run 시작 전 거부 | 3 처리하지 못한 예외. "
             "사유는 var/paper/logs/m2-supervisor-YYYYMMDD.log (설정 거부는 표준 출력에만 — 파일을 만들지 않음)")
LEVEL_KO = {"EMERGENCY": "긴급", "WARNING": "경고", "INFO": "정보"}


class M2SupervisorRefused(ValueError):
    pass


@dataclass(frozen=True)
class M2Schedule:
    """시각·간격 (CP-037 D-225·D-226·D-227 값 — 바꾸려면 CP)."""
    resume_every: timedelta = timedelta(minutes=5)       # K8 자동 재개 간격 (D-226)
    resume_max: int = 12                                 # 최대 시도 (D-226 — 1시간)
    resume_valid: timedelta = timedelta(minutes=10)      # 명령 not_after = 넣은 시각 + 10분 (D-226)
    command_wait: timedelta = timedelta(minutes=2)       # 명령이 처리되지 않으면 경고
    start_wait: timedelta = timedelta(minutes=5)         # 시작 대조가 안 보이면 경고
    heartbeat_stale: timedelta = timedelta(seconds=90)   # 하트비트 멈춤 (D-223)
    restart_delays: tuple[timedelta, ...] = (timedelta(minutes=5), timedelta(minutes=15))   # D-225
    restart_window: timedelta = timedelta(hours=24)      # D-225
    restart_max: int = 2                                 # 직전 24시간 자동 재시작 상한 (D-225)
    exit_wait: timedelta = timedelta(minutes=3)          # 정지 요청 뒤 run 종료 대기
    routine_start: time = time(0, 5)                     # UTC 일과 시작 (D-227)
    routine_deadline: time = time(0, 30)                 # 일 경계 대조를 기다리는 한계 [DP]
    poll_seconds: float = 5.0                            # K8 단계 확인 간격
    idle_poll_seconds: float = 30.0                      # 그 밖 감시 간격


@dataclass
class M2SupervisorDeps:
    clock: Clock
    sleep: Callable[[float], None]
    notify: Callable[[str, str], None]             # (등급, 문구) → 텔레그램
    preflight: Callable[[], tuple[int, str]]       # (종료 코드, 출력)
    launch: Callable[[], RunHandle]
    review: Callable[[], tuple[int, str]]
    backup: Callable[[], tuple[int, str]]
    log: Callable[[str], None] = field(default=lambda m: None)
    summary: Callable[[date, Sequence[str]], str] | None = None   # None = reporting.m2_day_summary


@dataclass
class _Snapshot:
    started: bool
    k8: bool
    human: tuple[str, ...]
    command: dict | None


@dataclass
class _K8Phase:
    launched: datetime
    since: str
    next_try: datetime
    active: bool = True
    attempts: int = 0
    pending: str | None = None
    pending_at: datetime | None = None
    warned_fail: bool = False
    warned_slow: bool = False
    warned_start: bool = False
    last_reason: str = ""


def request_supervisor_stop(base: Path, now: datetime) -> Path:
    """감독 정지 요청 파일을 쓴다(`--stop`). 감독은 다음 확인 때 run에 정지 요청 파일을 쓰고 끝난다(강제 종료 없음)."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / SUPERVISOR_STOP
    path.write_text(f"m2-supervisor --stop {to_iso(now.astimezone(UTC))}\n", encoding="utf-8")
    return path


class M2Supervisor:
    def __init__(self, mode: Mode, base: Path, account: str, schedule: M2Schedule, deps: M2SupervisorDeps) -> None:
        if mode is not Mode.PAPER:
            raise M2SupervisorRefused(f"M2 감독은 PAPER 전용 — {mode.value} 거부 (CP-037 D-223·D-231)")
        self.base = base
        self.db = base / "trading.db"
        self.account = account
        self.s = schedule
        self.d = deps
        self.run_handle: RunHandle | None = None
        self.notes: list[str] = []
        self.problems: list[str] = []
        self._mask = Masker()
        self._k8: _K8Phase | None = None
        self._started = False
        self._human_seen: tuple[str, ...] = ()
        self._hb_stale = False
        self._waiting: str | None = None
        self._restart_at: datetime | None = None
        self._restart_reason = ""
        self._restart_memory: list[datetime] = []
        self._routine_day: date | None = None

    # --- 진입 ---------------------------------------------------------------------------------
    def run(self) -> int:
        try:
            lock = acquire_instance_lock(self.base, M2_SUPERVISOR_LOCK)
        except InstanceLocked:
            self.d.log("감독이 이미 실행 중 — 즉시 종료 (매일 보조 트리거의 정상 경우)")
            return EXIT_OK
        try:
            if instance_running(self.base):
                self._notify("WARNING", "run이 이미 실행 중(수동 run?) — 감독 시작 안 함(시작 점검·새 run 없음). "
                                        "수동 run을 끝낸 뒤 감독을 다시 시작")
                return EXIT_REFUSED
            stale = self.base / SUPERVISOR_STOP
            if stale.exists():   # 지난 감독의 정지 요청이 남아 있으면 새 감독을 바로 멈추지 않게 지운다(잠금을 잡은 뒤)
                stale.unlink()
                self.d.log("이전 감독 정지 요청 파일을 지움")
            try:
                return self._main()
            except BaseException as exc:  # noqa: BLE001 — KeyboardInterrupt 포함: 띄운 run에 정지 요청 후 코드 3
                return self._aborted(exc)
        finally:
            lock.release()

    def _main(self) -> int:
        now = self.d.clock.now()
        self._routine_day = now.astimezone(UTC).date()   # 일과는 이 감독이 도는 동안 넘는 UTC 경계부터
        rc, out = self.d.preflight()
        self.d.log(f"시작 점검 출력:\n{out}")
        if rc != 0:
            self._notify("EMERGENCY", "시작 점검 실패(코드 %d) — run 시작 안 함\n%s" % (rc, _fail_lines(out)))
            return EXIT_REFUSED
        self._launch()
        self._notify("INFO", f"감독 시작 — 시작 점검 통과, run 시작({_hm(now)} UTC). K8 자동 재개 진행")
        while True:
            now = self.d.clock.now()
            if (self.base / SUPERVISOR_STOP).exists():
                return self._stop_supervisor()
            run = self.run_handle
            if run is not None:
                if run.alive():
                    self._watch(now)
                elif self._on_exit(run, now) == "EXIT":
                    return EXIT_OK
            elif self._restart_at is not None and now >= self._restart_at:
                self._restart(now)
            self._routine(now)
            k8_busy = self._k8 is not None and self._k8.active and self.run_handle is not None
            self.d.sleep(self.s.poll_seconds if k8_busy else self.s.idle_poll_seconds)

    # --- run 시작·감시 ------------------------------------------------------------------------
    def _launch(self) -> None:
        stop_file = self.base / STOP_REQUEST
        if stop_file.exists():
            stop_file.unlink()
        now = self.d.clock.now()
        self.run_handle = self.d.launch()
        self._k8 = _K8Phase(launched=now, since=to_iso(now.astimezone(UTC)), next_try=now)
        self._started = False
        self._hb_stale = False

    def _watch(self, now: datetime) -> None:
        k = self._k8
        pending = k.pending if k is not None else None
        st = self._read(lambda repo: self._snapshot(repo, k.since if k is not None else None, pending))
        if isinstance(st, _Snapshot):
            if st.started and not self._started:
                self._started = True
                self._audit(now, "RUN_START", launched=k.since if k is not None else None)
            self._check_human(st.human)
            if k is not None and k.active:
                self._k8_step(k, st, now)
        self._check_heartbeat(now)

    def _snapshot(self, repo: Repository, since: str | None, pending: str | None) -> _Snapshot:
        started = since is not None and bool(repo.select(
            "SELECT 1 FROM reconciliation WHERE account=? AND trigger='STARTUP' AND created_at>=? LIMIT 1",
            (self.account, since)))
        rows = [r for r in repo.halt_rows(None) if r["account"] in (None, self.account)]
        k8 = any(r["halt_type"] == HaltType.K8.value for r in rows)
        return _Snapshot(started, k8, _human(rows), repo.command(pending) if pending else None)

    def _check_human(self, human: tuple[str, ...]) -> None:
        """사람 확인 정지 종류가 바뀌면 알림 (D-230). K4 = 대조 불일치 → 테스트넷 리셋 의심 긴급 알림 (D-228)."""
        if human == self._human_seen:
            return
        old, self._human_seen = self._human_seen, human
        if not human:
            self._notify("INFO", f"사람 확인 정지 해제됨(이전 {', '.join(old)})")
            self._note(f"사람 확인 정지 해제({', '.join(old)})")
            return
        new_k4 = HaltType.K4.value in human and HaltType.K4.value not in old
        text = (f"사람 확인 정지 {', '.join(human)} — 자동 해제·K8 자동 재개 없음(정지는 청산이 아님, 사람 확인)")
        if new_k4:
            text += (". K4 = 대조 불일치: 테스트넷 리셋 의심 — 귀가 후 PC에서 확인, 리셋이면 엔진 정지 중에 "
                     "reset-confirm(PAPER 기간 다시 계산)")
        self._notify("EMERGENCY" if new_k4 or HaltType.K3.value in human else "WARNING", text)
        self._note(f"사람 확인 정지 {', '.join(human)}")

    def _check_heartbeat(self, now: datetime) -> None:
        k = self._k8
        if not self._started and (k is None or now - k.launched < self.s.start_wait):
            return
        hb = watchdog_check(self.base / "heartbeat", now, self.s.heartbeat_stale)
        if not hb.alive and not self._hb_stale:
            self._hb_stale = True
            self.problems.append("하트비트 멈춤")
            self._notify("EMERGENCY", f"run 하트비트 멈춤: {hb.detail} — 자동 재시작·강제 종료 없음, PC·/status 확인")
        elif hb.alive and self._hb_stale:
            self._hb_stale = False
            if "하트비트 멈춤" in self.problems:
                self.problems.remove("하트비트 멈춤")
            self._note("하트비트 멈춤 → 회복됨")
            self._notify("INFO", "run 하트비트 회복")

    # --- K8 자동 재개 (D-226) ------------------------------------------------------------------
    def _k8_step(self, k: _K8Phase, st: _Snapshot, now: datetime) -> None:
        s = self.s
        cmd = st.command
        if cmd is not None and cmd["status"] != "PENDING":
            k.pending = None
            if cmd["status"] != "DONE":
                k.last_reason = str(cmd["result"] or cmd["status"])
                self.d.log(f"K8 자동 재개 거부(시도 {k.attempts}): {k.last_reason}")
                if not k.warned_fail:
                    k.warned_fail = True
                    self._notify("WARNING", f"K8 자동 재개 실패(시도 {k.attempts}회): {k.last_reason} — "
                                            f"{_minutes(s.resume_every)}분마다 재시도(최대 {s.resume_max}회)")
        if st.started and not st.k8:
            if k.attempts:
                self._notify("INFO", f"K8 자동 재개 완료({_hm(now)} UTC, 시도 {k.attempts}회) — 정상 운영")
                self._note(f"K8 자동 재개(시도 {k.attempts}회)")
                self._audit(now, "K8_AUTO_RESUMED", attempts=k.attempts)
            else:
                self._notify("INFO", f"K8이 이미 해제됨({_hm(now)} UTC, 다른 경로) — 자동 재개 생략")
                self._note("K8 이미 해제(다른 경로)")
            k.active = False
            return
        if st.started and k.pending is None:
            if st.human:
                return   # 사람 확인 정지 — 명령을 넣지 않는다(알림은 _check_human)
            if k.attempts >= s.resume_max:
                self._notify("WARNING", f"K8 자동 재개 {s.resume_max}회 실패 — 자동 재개 중단. 마지막 사유: "
                                        f"{k.last_reason}. 확인 후 /resume K8 가능(원격 조건), run은 유지")
                self._note(f"K8 자동 재개 못 함(시도 {k.attempts}회)")
                self.problems.append("K8 자동 재개 못 함")
                self._audit(now, "K8_AUTO_RESUME_GAVE_UP", attempts=k.attempts, reason=k.last_reason[:200])
                k.active = False
                return
            if now >= k.next_try:
                cid = self._insert_resume(now, k.attempts + 1)
                if cid is not None:
                    k.attempts += 1
                    k.pending, k.pending_at = cid, now
                k.next_try = now + s.resume_every
        elif k.pending is not None and k.pending_at is not None and now - k.pending_at > s.command_wait \
                and not k.warned_slow:
            k.warned_slow = True
            self._notify("WARNING", f"K8 자동 재개 명령이 {_minutes(s.command_wait)}분 넘게 처리되지 않음 — "
                                    f"run 멈춤 의심(/status 확인)")
        elif not st.started and not k.warned_start and now - k.launched > s.start_wait:
            k.warned_start = True
            self._notify("WARNING", f"run 시작(시작 대조)이 {_minutes(s.start_wait)}분 넘게 확인되지 않음 — /status 확인")

    def _insert_resume(self, now: datetime, n: int) -> str | None:
        cid = f"m2sup-{now.astimezone(UTC):%Y%m%dT%H%M%S}-{n}-{uuid.uuid4().hex[:8]}"
        args = {"account": self.account, "halt_type": HaltType.K8.value,
                "not_after": to_iso((now + self.s.resume_valid).astimezone(UTC))}

        def put(repo: Repository) -> str:
            with repo.tx():
                repo.insert_command(cid, Channel.AUTO, SUPERVISOR_ACTOR, "RESUME", args, now)
                repo.audit(now, "OPS_COMMAND_RECEIVED", SUPERVISOR_ACTOR, cid,
                           {"command": "RESUME", "channel": Channel.AUTO})
            return cid

        done = self._read(put)
        if done is not None:
            self.d.log(f"K8 자동 재개 명령 {n}: {cid}")
        return done if isinstance(done, str) else None

    # --- 종료·재시작 (D-225) -------------------------------------------------------------------
    def _on_exit(self, run: RunHandle, now: datetime) -> str:
        code = run.exit_code()
        self.run_handle = None
        self._k8 = None
        self._hb_stale = False
        if "하트비트 멈춤" in self.problems:
            self.problems.remove("하트비트 멈춤")
        human = self._read(lambda repo: _human([r for r in repo.halt_rows(None)
                                                if r["account"] in (None, self.account)]))
        human = human if isinstance(human, tuple) else ()
        self._audit(now, "RUN_EXIT", code=code, human=list(human))
        self._note(f"run 종료(코드 {code})")
        if code == 0:
            self._audit(now, "PLANNED_STOP", code=0)
            self._notify("EMERGENCY", "run 종료(코드 0) — 정지 요청 파일·ops-stop 등 의도된 종료로 봄. 자동 재시작 안 함, "
                                      "감독 종료. 다시 시작: 사용자 직접 또는 다음 로그온")
            return "EXIT"
        why = None
        recent = self._recent_restarts(now)
        if code == 2:
            why = "run 시작 거부(코드 2) — 설정·실행 잠금·시작 조건 확인"
        elif human:
            why = f"사람 확인 정지 {', '.join(human)} 중 종료"
        elif len(recent) >= self.s.restart_max:
            why = f"직전 24시간 자동 재시작 {len(recent)}회 — 재시작 상한 {self.s.restart_max}회 도달"
            self._audit(now, "RESTART_CAP", restarts=len(recent))
        if why is not None:
            self._wait(now, f"run 종료(코드 {code}), {why}")
            return "WAIT"
        delay = self.s.restart_delays[min(len(recent), len(self.s.restart_delays) - 1)]
        self._restart_at = now + delay
        self._restart_reason = f"코드 {code}"
        self._notify("EMERGENCY", f"run 비정상 종료(코드 {code}) — {_minutes(delay)}분 뒤 자동 재시작(직전 24시간 "
                                  f"{len(recent) + 1}/{self.s.restart_max}회째). 재시작 run은 K8 → 시작 대조 → 자동 재개 "
                                  f"조건(대조 전 주문 없음)")
        return "RESTART"

    def _restart(self, now: datetime) -> None:
        self._restart_at = None
        human = self._read(lambda repo: _human([r for r in repo.halt_rows(None)
                                                if r["account"] in (None, self.account)]))
        if isinstance(human, tuple) and human:
            self._wait(now, f"재시작 전 사람 확인 정지 {', '.join(human)} 확인")
            return
        rc, out = self.d.preflight()
        self.d.log(f"재시작 전 시작 점검 출력:\n{out}")
        if rc != 0:
            self._wait(now, f"재시작 전 시작 점검 실패(코드 {rc})\n{_fail_lines(out)}")
            return
        n = len(self._recent_restarts(now)) + 1
        self._restart_memory.append(now)
        self._audit(now, "RESTART", n=n, reason=self._restart_reason)
        self._launch()
        self._notify("INFO", f"자동 재시작 run 시작({_hm(now)} UTC, 직전 24시간 {n}/{self.s.restart_max}회) — "
                             f"K8 → 시작 대조 → 자동 재개 조건")
        self._note(f"자동 재시작 {n}회째({self._restart_reason})")

    def _wait(self, now: datetime, detail: str) -> None:
        self._waiting = detail
        if "M2 정지 — 사람 확인 대기" not in self.problems:
            self.problems.append("M2 정지 — 사람 확인 대기")
        self._audit(now, "WAIT", detail=detail[:300])
        self._note(f"M2 정지 — 사람 확인 대기({detail.splitlines()[0]})")
        self._notify("EMERGENCY", f"M2 정지 — 사람 확인: {detail}. 자동 재시작 안 함, run 없이 대기(텔레그램 명령은 "
                                  f"처리되지 않음 — run이 처리). 귀가 후 PC에서 확인, 감독 정지: m2-supervisor.py --stop")

    def _recent_restarts(self, now: datetime) -> list[datetime]:
        """직전 24시간 자동 재시작 시각 — M2 DB 감사(M2_SUPERVISOR RESTART)와 메모리 중 많은 쪽(보수)."""
        since = now - self.s.restart_window
        mem = [t for t in self._restart_memory if t > since]

        def rows(repo: Repository) -> list[datetime]:
            out = []
            for r in repo.select("SELECT at, summary FROM audit_event WHERE kind=? AND at>=? ORDER BY seq",
                                 (AUDIT_KIND, to_iso(since.astimezone(UTC)))):
                if '"event": "RESTART"' in r["summary"]:
                    out.append(from_iso(r["at"]))
            return out

        db = self._read(rows)
        db = db if isinstance(db, list) else []
        return db if len(db) >= len(mem) else mem

    # --- UTC 일과 (D-227) ----------------------------------------------------------------------
    def _routine(self, now: datetime) -> None:
        utc_now = now.astimezone(UTC)
        day = utc_now.date()
        if self._routine_day is None or day <= self._routine_day or utc_now.time() < self.s.routine_start:
            return
        since = to_iso(datetime.combine(day, time(0), tzinfo=UTC))
        seen = self._read(lambda repo: _day_boundary(repo, self.account, since))
        alive = self.run_handle is not None and self.run_handle.alive()
        if not isinstance(seen, str) and alive and utc_now.time() < self.s.routine_deadline:
            return
        self._routine_day = day
        closing = day - timedelta(days=1)
        notes, problems = list(self.notes), list(self.problems)
        if not isinstance(seen, str):
            problems.append("일 경계 대조 미확인" + ("" if alive else "(run 없음)"))
        elif seen != "MATCH":
            problems.append(f"일 경계 대조 {seen}")
        else:
            notes.append("일 경계 대조 완료(일치)")
        for name, fn in (("paper-review", self.d.review), ("백업", self.d.backup)):
            try:
                rc, out = fn()
            except Exception as exc:  # noqa: BLE001 — 한 단계 실패로 요약을 막지 않는다
                rc, out = 99, type(exc).__name__
            self.d.log(f"{name} 출력:\n{out}")
            if rc == 0:
                notes.append(f"{name} 완료")
            else:
                problems.append(f"{name} 실패")
        try:
            if self.d.summary is not None:
                text = self.d.summary(closing, notes)
            else:
                from trading_system.reporting.m2_day_summary import build_m2_day_summary
                text = build_m2_day_summary(self.db, self.account, closing, notes)
        except Exception as exc:  # noqa: BLE001
            text = f"[M2 {closing} 일일 요약] 집계 실패({type(exc).__name__}) — PC에서 paper-review 확인\n" + \
                "\n".join(notes)
        if problems:
            text += "\n확인 필요: " + ", ".join(problems)
        self._notify("INFO", text)
        self.notes = []
        # 계속되는 상태는 새 날의 "확인 필요"에 남긴다(대기·하트비트 멈춤·K8 못 함)
        self.problems = [p for p in self.problems if p in ("M2 정지 — 사람 확인 대기", "하트비트 멈춤",
                                                           "K8 자동 재개 못 함")]

    # --- 정지 (D-230) -------------------------------------------------------------------------
    def _stop_supervisor(self) -> int:
        try:
            (self.base / SUPERVISOR_STOP).unlink()
        except OSError:
            pass
        now = self.d.clock.now()
        run = self.run_handle
        state = "run 없음"
        if run is not None and run.alive():
            self._write_stop("감독 정지 요청")
            end = now + self.s.exit_wait
            while run.alive() and self.d.clock.now() < end:
                self.d.sleep(min(self.s.poll_seconds, 5.0))
            if run.alive():
                state = "run 종료 확인 안 됨"
                self._notify("EMERGENCY", f"감독 정지 요청 뒤 {_minutes(self.s.exit_wait)}분 안에 run이 끝나지 않음 — "
                                          f"강제 종료 안 함, PC에서 확인(scripts/status.py --mode PAPER --market M2)")
            else:
                state = f"run 정상 종료(코드 {run.exit_code()})"
                self._audit(self.d.clock.now(), "RUN_EXIT", code=run.exit_code(), human=[])
        self._audit(self.d.clock.now(), "SUPERVISOR_STOP", state=state)
        self._notify("INFO", f"감독 정지 요청 처리 — {state}, 감독 종료(다시 시작: 사용자 직접 또는 다음 로그온)")
        return EXIT_OK

    def _write_stop(self, why: str) -> bool:
        try:
            (self.base / STOP_REQUEST).write_text(f"m2-supervisor {why} "
                                                  f"{to_iso(self.d.clock.now().astimezone(UTC))}\n", encoding="utf-8")
        except OSError as exc:
            self.d.log(f"정지 요청 파일 작성 실패: {type(exc).__name__}")
            return False
        self.d.log(f"정지 요청 파일 작성 ({why})")
        return True

    def _aborted(self, exc: BaseException) -> int:
        name = type(exc).__name__
        try:
            self.d.log(f"감독 예외 종료 (코드 {EXIT_ABORTED}) {name}:\n{traceback.format_exc()}")
        except Exception:  # noqa: BLE001
            pass
        stopped = False
        try:
            run = self.run_handle
            if run is not None and run.alive() and instance_running(self.base):
                stopped = self._write_stop("감독 예외 종료")
        except Exception:  # noqa: BLE001
            stopped = False
        self._notify("EMERGENCY", f"감독 예외 종료 {name} — 종료 코드 {EXIT_ABORTED}. "
                                  f"{'감독이 띄운 run에 정지 요청 파일 작성(정상 종료 경로)' if stopped else 'run 실행 여부는 /status·PC로 확인'}"
                                  f", var/paper/logs/m2-supervisor 로그 확인")
        return EXIT_ABORTED

    # --- 보조 ---------------------------------------------------------------------------------
    def _audit(self, now: datetime, event: str, **summary: object) -> None:
        def put(repo: Repository) -> bool:
            with repo.tx():
                repo.audit(now, AUDIT_KIND, SUPERVISOR_ACTOR, None, {"event": event, **summary})
            return True
        if self._read(put) is None:
            self.d.log(f"감사 기록 못 함({event}) — 저장소 없음 또는 잠김")

    def _read(self, fn: Callable[[Repository], object]) -> object:
        """저장소 한 번 열어 읽기·쓰기. 저장소가 없으면 만들지 않는다. 실패는 기록만 하고 None."""
        if not self.db.exists():
            return None
        try:
            repo = Repository(self.db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
        except Exception as exc:  # noqa: BLE001
            self.d.log(f"저장소 열기 실패: {type(exc).__name__}")
            return None
        try:
            return fn(repo)
        except Exception as exc:  # noqa: BLE001
            self.d.log(f"저장소 조회 실패: {type(exc).__name__}: {exc}")
            return None
        finally:
            repo.close()

    def _note(self, text: str) -> None:
        line = f"{_hm(self.d.clock.now())} {text}"
        self.notes.append(line)
        self.d.log(line)

    def _notify(self, level: str, text: str) -> None:
        text = self._mask.text(text)
        self.d.log(f"알림 {level}: {text}")
        try:
            self.d.notify(level, text)
        except Exception as exc:  # noqa: BLE001 — 알림 실패로 감독 절차를 멈추지 않는다
            self.d.log(f"알림 전송 실패: {type(exc).__name__}")


def _human(rows: Iterable[dict]) -> tuple[str, ...]:
    return tuple(sorted({r["halt_type"] for r in rows if HaltType(r["halt_type"]) in HUMAN_HALTS}))


def _day_boundary(repo: Repository, account: str, since: str) -> str | None:
    rows = repo.select("SELECT result FROM reconciliation WHERE account=? AND trigger='DAY_BOUNDARY' AND created_at>=? "
                       "ORDER BY created_seq DESC LIMIT 1", (account, since))
    return str(rows[0]["result"]) if rows else None


def _fail_lines(out: str) -> str:
    return "\n".join([ln.strip() for ln in out.splitlines() if "실패" in ln or "거부" in ln][:6])


def _hm(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%H:%M")


def _minutes(d: timedelta) -> int:
    return int(d.total_seconds() // 60)


# --- 실행 환경 조립 (작업 스케줄러가 부르는 진입점) ------------------------------------------------------

class SupervisorLog:
    """var/paper/logs/m2-supervisor-YYYYMMDD.log(UTC 날짜)에 한 줄씩(가림 처리). 콘솔이 있으면 함께 출력."""

    def __init__(self, logs: Path, clock: Clock) -> None:
        self._logs = logs
        self._clock = clock
        self._secrets: set[str] = set()
        self._mask = Masker()

    def add_secrets(self, values: Iterable[str]) -> None:
        self._secrets |= {v for v in values if v}
        self._mask = Masker(self._secrets)

    def __call__(self, message: str) -> None:
        now = self._clock.now().astimezone(UTC)
        text = self._mask.text(message)
        try:
            self._logs.mkdir(parents=True, exist_ok=True)
            with (self._logs / f"m2-supervisor-{now:%Y%m%d}.log").open("a", encoding="utf-8") as f:
                f.write(f"{now:%H:%M:%S}Z\t{text}\n")
        except OSError:
            pass
        _say(text)


def _say(text: str) -> None:
    if sys.stdout is None:   # pythonw(작업 스케줄러)에는 콘솔이 없다
        return
    try:
        print(text, flush=True)
    except (OSError, UnicodeError):
        pass


class M2TelegramNotify(TelegramNotify):
    """M2 전용 봇(D-213)으로 감독 알림. 재시도·429·fallback 규칙은 M1 자동 운영과 같다(TelegramNotify) — 머리말만 [M2 감독]."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        log = self._log   # 상속한 재전송 기록 문구의 M1 파일 이름을 M2 이름으로 (기록 문구만 — 동작 같음)
        self._log = lambda m: log(m.replace("alerts-fallback-autopilot.log", SUPERVISOR_FALLBACK))

    def __call__(self, level: str, text: str) -> None:
        msg = Masker().text(f"[{LEVEL_KO.get(level, level)}] [M2 감독] {strip_paths(text)}")
        sent, permanent = self._send(msg, self._attempts)
        if sent:
            return
        write_fallback(self._fallback, msg)
        self._log(f"텔레그램 전송 최종 실패 — {SUPERVISOR_FALLBACK}에 기록"
                  + ("(메시지 거부, 재전송 안 함)" if permanent else ", 종료 전 1회 재전송"))
        if not permanent:
            self.failed.append(msg)


def mode_market_refusal(mode_arg: str | None, market_arg: str | None, env: Mapping[str, str],
                        env_file: Path) -> str | None:
    """PAPER·M2가 아니면 거부 사유 (D-223·D-231). TRADE_MODE는 run과 같은 규칙(환경 변수 → 루트 .env 한 줄)."""
    if mode_arg != Mode.PAPER.value:
        return f"M2 감독은 PAPER 전용 — --mode {mode_arg!r} 거부 (LIVE·BACKTEST는 자동화하지 않음, D-231)"
    if market_arg != "M2":
        return f"M2 감독은 M2 전용 — --market {market_arg!r} 거부 (M1 무인 운영은 scripts/autopilot.py, D-223)"
    raw = env.get("TRADE_MODE")
    if raw is not None and raw != Mode.PAPER.value:
        return f"환경 변수 TRADE_MODE={raw[:20]!r}가 PAPER가 아님 — 감독 시작 거부"
    from trading_system.config.modes import read_trade_mode
    try:
        mode = read_trade_mode(env, env_file)
    except ConfigError as exc:
        return f"TRADE_MODE 확인 불가({exc}) — 감독 시작 거부 (환경 변수 또는 루트 .env에 TRADE_MODE=PAPER 필요)"
    if mode is not Mode.PAPER:
        return f"루트 .env의 TRADE_MODE={mode.value}가 PAPER가 아님 — 감독 시작 거부 (.env에 TRADE_MODE=PAPER 필요)"
    return None


MakeDeps = Callable[[Path, Path, Any, Callable[[str], None]], M2SupervisorDeps]


def m2_supervisor_main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None, *,
                       hosts: Iterable[str] | None = None, open_session: Any = None,
                       telegram_base_url: str = "https://api.telegram.org", make_deps: MakeDeps | None = None) -> int:
    """작업 스케줄러 진입점 (scripts/m2-supervisor.py가 접속 도메인 표·바이낸스 세션 함수를 주입 — app은 바이낸스 패키지를
    import하지 않는다, D-119). 설정 거부(코드 2)는 파일을 만들지 않고 표준 출력에만 남긴다(V-296 (a)(b)(c))."""
    p = argparse.ArgumentParser(prog="scripts/m2-supervisor.py",
                                description="M2 PAPER 감독 프로세스 (CP-037 D-223). 작업 스케줄러가 로그온 시 실행",
                                epilog=EXIT_HELP)
    p.add_argument("--mode", required=True, help="PAPER만 허용 (LIVE·BACKTEST는 거부)")
    p.add_argument("--market", required=True, help="M2만 허용")
    p.add_argument("--root", default=str(REPO_ROOT), help="저장소 루트 (M2 체크아웃)")
    p.add_argument("--stop", action="store_true", help="감독 정지 요청 파일을 쓰고 감독 종료를 기다린다(run은 정지 요청 "
                                                       "파일로 정상 종료 — 강제 종료 없음)")
    p.add_argument("--wait", type=int, default=240, help="--stop 뒤 감독 종료 확인 대기(초)")
    ns = p.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    root = Path(ns.root).resolve()
    why = mode_market_refusal(ns.mode, ns.market, env, root / ".env")
    if why:
        print(f"거부: {why}")
        return EXIT_REFUSED
    base = root / "var" / "paper"
    if ns.stop:
        return _stop_main(base, ns.wait)
    try:
        cfg = _check_settings(root, base, hosts)
    except ConfigError as exc:
        print(f"거부(설정): {exc}")
        return EXIT_REFUSED
    clock = SystemClock()
    log = SupervisorLog(base / "logs", clock)
    _register_secrets(log, cfg.secrets)
    if make_deps is None:
        deps, notify = _production_deps(root, base, cfg, log, clock, open_session, telegram_base_url, env)
    else:
        deps, notify = make_deps(root, base, cfg, log), None
    log(f"감독 시작 (M2 PAPER, 계좌 별칭 {cfg.account})")
    try:
        rc = M2Supervisor(Mode.PAPER, base, cfg.account, M2Schedule(), deps).run()
    finally:
        if notify is not None:
            try:
                notify.flush()
            except Exception as exc:  # noqa: BLE001
                log(f"밀린 알림 재전송 실패: {type(exc).__name__}")
    log(f"감독 끝 (코드 {rc})")
    return rc


def _stop_main(base: Path, wait: int) -> int:
    if not instance_running(base, M2_SUPERVISOR_LOCK):
        print("거부: M2 감독이 실행 중이 아님 — 정지 요청 파일을 만들지 않음 (run만 끝내려면 ops-stop --market M2)")
        return 1
    path = request_supervisor_stop(base, datetime.now(UTC))
    print(f"감독 정지 요청 파일 작성: {path} (감독이 run에 정지 요청 파일을 쓰고 끝남 — 강제 종료 없음)")
    end = _time.monotonic() + max(0, wait)
    while wait > 0 and _time.monotonic() < end:
        if not instance_running(base, M2_SUPERVISOR_LOCK):
            print("감독 종료 확인(감독 잠금 해제)")
            return 0
        _time.sleep(1.0)
    if wait > 0:
        print(f"{wait}초 안에 감독 종료 확인 안 됨 — scripts/status.py --mode PAPER --market M2로 확인")
        return 1
    return 0


def _check_settings(root: Path, base: Path, hosts: Iterable[str] | None):  # type: ignore[no-untyped-def]
    """M2 시작 검사(파일을 만들지 않음): config/m2·자격정보·DB 메타·연결 전 조건·시작 일치 표·텔레그램. ConfigError."""
    from trading_system.config.market_scope import check_start_scope
    from trading_system.domain.market_time import Market

    from .startup import check_m2_startup, m2_paper_problems
    from .telegram_run import M2_CHAT_KEY, M2_TOKEN_KEY, check_telegram_secrets
    try:
        cfg = check_m2_startup(Mode.PAPER, root, base / "trading.db", root / ".env.paper")
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — LiveNotAvailable·ModeConsistencyError 등도 설정 거부로
        raise ConfigError(f"{type(exc).__name__}: {exc}") from None
    problems, _switch = m2_paper_problems(cfg)
    if problems:
        raise ConfigError(problems)
    if hosts is None:
        raise ConfigError("접속 도메인 표가 없다 — 감독은 scripts/m2-supervisor.py로 실행(테스트넷 호스트 표 주입)")
    check_start_scope(Mode.PAPER, Market.M2, cfg.broker, cfg.account, frozenset(hosts))   # PAPER·M2·테스트넷만
    if not cfg.app.telegram_enabled:
        raise ConfigError("무인 운영에는 텔레그램 알림이 켜져 있어야 한다 (config/m2/app.paper.toml [telegram] "
                          "enabled = true, M2 전용 봇 D-213)")
    check_telegram_secrets(cfg.secrets, (M2_TOKEN_KEY, M2_CHAT_KEY))
    return cfg


def _register_secrets(log: SupervisorLog, store: Any) -> None:
    keys = getattr(store, "keys", None)
    if keys is None:
        return
    log.add_secrets([store.get(k).reveal() for k in keys() if SENSITIVE_KEYS.search(k)])


def _production_deps(root: Path, base: Path, cfg: Any, log: SupervisorLog, clock: Clock, open_session: Any,
                     telegram_base_url: str, env: Mapping[str, str]) -> tuple[M2SupervisorDeps, M2TelegramNotify]:
    from trading_system.ops.cli import backup_main
    from trading_system.ops.telegram import TelegramApi

    from .m2_preflight import m2_preflight_main
    from .review_tool import review_main
    from .telegram_run import M2_CHAT_KEY, M2_TOKEN_KEY
    api = TelegramApi(cfg.secrets.get(M2_TOKEN_KEY).reveal, telegram_base_url)
    notify = M2TelegramNotify(api, cfg.secrets.get(M2_CHAT_KEY).reveal().strip(), log,
                              fallback=base / "logs" / SUPERVISOR_FALLBACK)
    child_env = {**env, "TRADE_MODE": Mode.PAPER.value, "PYTHONIOENCODING": "utf-8",
                 "PYTHONPATH": os.pathsep.join(x for x in (str(root / "src"), env.get("PYTHONPATH", "")) if x)}
    common = ["--mode", Mode.PAPER.value, "--market", "M2", "--root", str(root)]

    def preflight() -> tuple[int, str]:
        if open_session is None:
            return 2, "거부: 바이낸스 세션 함수가 없다 — scripts/m2-supervisor.py로 실행"
        return captured(lambda: m2_preflight_main(root, open_session, child_env, market="M2"))

    def launch() -> RunHandle:
        stamp = clock.now().astimezone(UTC).strftime("%Y%m%d")
        return SubprocessRun([console_python(), str(root / "scripts" / "run-m2.py")], root, child_env,
                             base / "logs" / f"run-console-m2-{stamp}.log")

    deps = M2SupervisorDeps(clock=clock, sleep=_time.sleep, notify=notify, preflight=preflight, launch=launch,
                            review=lambda: captured(lambda: review_main(common)),
                            backup=lambda: captured(lambda: backup_main(common)), log=log)
    return deps, notify


__all__ = ["AUDIT_KIND", "EXIT_ABORTED", "EXIT_OK", "EXIT_REFUSED", "M2Schedule", "M2Supervisor", "M2SupervisorDeps",
           "M2SupervisorRefused", "M2TelegramNotify", "M2_SUPERVISOR_LOCK", "SUPERVISOR_ACTOR", "SUPERVISOR_STOP",
           "m2_supervisor_main", "mode_market_refusal", "request_supervisor_stop"]
