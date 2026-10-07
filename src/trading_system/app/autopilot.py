"""PAPER 무인 자동 운영 (T-025, CP-029 draft, 사용자 결정 A-078). **M1 PAPER 전용 — LIVE는 자동화하지 않는다.**

평일 06:45에 작업 스케줄러가 `scripts/autopilot.py --mode PAPER`를 한 번 실행한다(등록은 사용자가 관리자 PowerShell에서
`scripts/schedule-register.ps1`, 시계 동기화 `w32tm /resync`는 06:40 별도 작업). 이 프로세스가 하루를 맡는다:

  1. 모드 확인: --mode가 PAPER가 아니거나 환경 변수 TRADE_MODE가 PAPER가 아니면 아무것도 만들지 않고 거부(코드 2).
  2. 시장 달력(config/calendar): 휴장일이면 텔레그램 1줄 후 종료. 달력 없음·08:55 이후 시작이면 알림 후 종료.
  3. 단일 실행: var/paper/autopilot.lock. run이 이미 돌고 있으면(var/paper/run.lock, 수동 실행) 알림 후 종료.
  4. 시작 점검(preflight, 같은 프로세스 안) 실패 → run을 시작하지 않고 긴급 알림, 그날 매매 없음.
  5. run 시작: 기존 `python -m trading_system.app run`을 자식 프로세스로(실행 잠금·시작 K8·감시 스레드 그대로).
  6. K8 자동 재개: 원격 K8 재개(CP-026 D-137)와 **같은 기준** — 명령 처리 시점 새 대조 + 01 §2.11 조건 1~5.
     운영 명령 표에 채널 AUTO·주체 "autopilot"로 RESUME K8을 넣는다(시한 not_after = 08:55, 감사에서 구분).
     거부되면 5분 간격 재시도(최대 30회), 08:55까지 못 풀리면 알림 후 중단(그날 매매 없음 — run은 마감까지 둔다).
     사람 확인 정지(K1·K2·K3·K4·K7)가 걸려 있으면 명령을 넣지 않고 알림만(자동 재개하지 않음).
  7. 장중: run 프로세스 생존·하트비트 감시(멈추면 긴급 알림, 자동 재시작 없음).
  8. 15:45(정규장 마감이 늦은 날은 마감 + 15분): 마감 대조(CLOSE) 완료·전송 중 의도 0건 확인(최대 30분 대기) →
     정지 요청 파일 var/paper/STOP → run이 Ctrl+C와 같은 경로로 정상 종료 → paper-review → 백업 → 일일 요약 텔레그램.

자동 운영은 주문하지 않는다. 주문·정지·재개 판정은 모두 run(엔진·게이트·정지 관리자)이 기존 경로로 한다(CLAUDE.md §8-2).
자동 운영의 알림은 텔레그램으로 직접 보낸다(감사 ALERT에 쓰지 않음 — run이 꺼진 뒤 다음 실행 때 밀린 알림으로 다시
보내지지 않게). 모든 진행은 var/paper/logs/autopilot-YYYYMMDD.log에 남는다(비밀값 없음, 가림 처리).
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time as _time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

from trading_system.config.errors import ConfigError
from trading_system.domain.clock import KST, Clock, SystemClock, from_iso, to_iso
from trading_system.domain.enums import HaltType, IntentState, Mode
from trading_system.domain.market import CostRates, SessionInfo
from trading_system.halt.manager import Channel
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.monitoring.liveness import watchdog_check
from trading_system.ops.cli import REPO_ROOT
from trading_system.reporting.day_summary import build_day_summary
from trading_system.storage.jsonlog import Masker
from trading_system.storage.repository import Repository

from .instance_lock import AUTOPILOT_LOCK, InstanceLocked, acquire_instance_lock, instance_running
from .main import STOP_REQUEST

AUTO_ACTOR = "autopilot"
# 사람이 확인·재개하는 정지 — 걸려 있으면 K8 자동 재개 명령을 넣지 않는다(A-078). K9(전략 범위)는 다른 전략을 막지 않고
# 교체로 중지된 이전 버전에 계속 남으므로 제외한다(CP-029 결정 필요 Q-3). K5·K6·K10·K11은 조건 충족 시 자동 해제 유형
HUMAN_HALTS = frozenset({HaltType.K1, HaltType.K2, HaltType.K3, HaltType.K4, HaltType.K7})
LEVEL_KO = {"EMERGENCY": "긴급", "WARNING": "경고", "INFO": "정보"}


class AutopilotRefused(ValueError):
    pass


def mode_refusal(mode_arg: str | None, env: Mapping[str, str], env_file: Path | None = None) -> str | None:
    """PAPER가 아니면 거부 사유. LIVE·BACKTEST·소문자·누락 모두 거부 (A-078, CLAUDE.md §4·§7).

    env_file(루트 .env)을 주면 run과 같은 규칙(read_trade_mode: 환경 변수 → .env 한 줄)으로 읽은 모드도 PAPER여야 한다
    (재검토 L3 — 작업 스케줄러에는 환경 변수가 없으므로 .env의 TRADE_MODE=PAPER가 필요)."""
    if mode_arg != Mode.PAPER.value:
        return f"자동 운영은 PAPER 전용 — --mode {mode_arg!r} 자동 시작 거부 (LIVE·BACKTEST는 자동화하지 않음, A-078)"
    raw = env.get("TRADE_MODE")
    if raw is not None and raw != Mode.PAPER.value:
        return f"환경 변수 TRADE_MODE={raw[:20]!r}가 PAPER가 아님 — 자동 시작 거부"
    if env_file is not None:
        from trading_system.config.modes import read_trade_mode
        try:
            mode = read_trade_mode(env, env_file)
        except ConfigError as exc:
            return (f"TRADE_MODE 확인 불가({exc}) — 자동 시작 거부 "
                    f"(환경 변수 또는 루트 .env에 TRADE_MODE=PAPER 필요)")
        if mode is not Mode.PAPER:
            return f"루트 .env의 TRADE_MODE={mode.value}가 PAPER가 아님 — 자동 시작 거부 (.env에 TRADE_MODE=PAPER 필요)"
    return None


@dataclass(frozen=True)
class Schedule:
    """시각·간격 (A-078 값. 바꾸려면 사용자 결정 — CP-029)."""
    k8_deadline: time = time(8, 55)                    # K8 자동 재개 시한 (KST)
    stop_at: time = time(15, 45)                       # 종료 절차 시작 (KST)
    stop_after_close: timedelta = timedelta(minutes=15)   # 마감이 늦은 날(특별 개장 시간): 마감 + 15분이 더 늦으면 그 시각
    close_wait: timedelta = timedelta(minutes=30)      # 마감 대조·전송 중 의도를 기다리는 최대 시간
    resume_every: timedelta = timedelta(minutes=5)     # K8 자동 재개 재시도 간격
    resume_max: int = 30                               # 최대 시도 횟수 (CLAUDE.md §8-7 종료 조건 — 시한과 함께)
    command_wait: timedelta = timedelta(minutes=2)     # 명령이 처리되지 않으면 경고
    start_wait: timedelta = timedelta(minutes=5)       # run 시작 대조가 안 보이면 경고
    exit_wait: timedelta = timedelta(minutes=3)        # 정지 요청 뒤 run 종료를 기다리는 시간
    heartbeat_stale: timedelta = timedelta(seconds=90)  # 하트비트 멈춤 판정 (감시 스레드 5초 주기)
    poll_seconds: float = 5.0                          # K8·마감 단계 확인 간격
    day_poll_seconds: float = 30.0                     # 장중 감시 간격


class RunHandle(Protocol):
    def alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...


@dataclass
class AutopilotDeps:
    clock: Clock
    sleep: Callable[[float], None]
    notify: Callable[[str, str], None]           # (등급, 문구) → 텔레그램
    preflight: Callable[[], tuple[int, str]]     # (종료 코드, 출력)
    launch: Callable[[], RunHandle]
    review: Callable[[], tuple[int, str]]
    backup: Callable[[], tuple[int, str]]
    log: Callable[[str], None] = field(default=lambda m: None)


@dataclass
class _K8State:
    started: bool
    k8: bool
    human: tuple[str, ...]
    command: dict | None


class Autopilot:
    def __init__(self, mode: Mode, base: Path, account: str, calendar: KrxCalendar, schedule: Schedule,
                 deps: AutopilotDeps, *, reference_time: time, rates: CostRates | None) -> None:
        if mode is not Mode.PAPER:
            raise AutopilotRefused(f"자동 운영은 PAPER 전용 — {mode.value} 거부 (A-078)")
        self.base = base
        self.db = base / "trading.db"
        self.account = account
        self.calendar = calendar
        self.s = schedule
        self.d = deps
        self.reference_time = reference_time
        self.rates = rates
        self.notes: list[str] = []
        self.problems: list[str] = []
        self._started_seen = False
        self._mask = Masker()

    # --- 진입 ---------------------------------------------------------------------------------
    def run(self) -> int:
        now = self.d.clock.now()
        day = now.astimezone(KST).date()
        session = self.calendar.session(day)
        if session is None:
            self._notify("WARNING", f"{day} 시장 달력 없음(config/calendar) — 자동 운영 안 함, 오늘 매매 없음")
            return 1
        if not session.is_open:
            self._notify("INFO", f"{day} 휴장일 — 자동 운영 쉼")
            return 0
        deadline = _at(day, self.s.k8_deadline)
        if now >= deadline:
            self._notify("WARNING", f"늦은 시작({_hm(now)}) — K8 재개 시한 {self.s.k8_deadline:%H:%M}이 지나 자동 운영 "
                                    f"안 함, 오늘 매매 없음")
            return 1
        try:
            lock = acquire_instance_lock(self.base, AUTOPILOT_LOCK)
        except InstanceLocked:
            self._notify("WARNING", "자동 운영이 이미 실행 중 — 두 번째 실행 거부")
            return 2
        try:
            if instance_running(self.base):
                started = self._read(self._last_start_day)
                if isinstance(started, date) and started < day:
                    # 어제(이전 거래일) 시작한 run이 아직 돈다 — 종료 절차가 빠진 것. 정지 요청 후 오늘은 시작하지 않는다 (M1)
                    self._write_stop("이전 거래일 run")
                    self._notify("EMERGENCY", f"어제 run이 아직 실행 중(시작 {started}) — 정지 요청 파일 작성. 오늘 자동 운영 "
                                              f"안 함(오늘 매매 없음). PC에서 종료 확인 후 수동 절차 또는 다음 거래일")
                    return 2
                self._notify("WARNING", "run이 이미 실행 중(수동 실행?) — 자동 운영 안 함(시작 점검·run 시작 없음)")
                return 2
            return self._day(day, session, deadline)
        finally:
            lock.release()

    def _last_start_day(self, repo: Repository) -> date | None:
        rows = repo.select("SELECT created_at FROM reconciliation WHERE account=? AND trigger='STARTUP' "
                           "ORDER BY created_seq DESC LIMIT 1", (self.account,))
        return from_iso(rows[0]["created_at"]).astimezone(KST).date() if rows else None

    def _write_stop(self, why: str) -> bool:
        try:
            (self.base / STOP_REQUEST).write_text(f"autopilot {why} {to_iso(self.d.clock.now().astimezone(UTC))}\n",
                                                  encoding="utf-8")
        except OSError as exc:
            self.d.log(f"정지 요청 파일 작성 실패: {type(exc).__name__}")
            return False
        self.d.log(f"정지 요청 파일 작성 ({why})")
        return True

    def _day(self, day: date, session: SessionInfo, deadline: datetime) -> int:
        rc, out = self.d.preflight()
        self.d.log(f"시작 점검 출력:\n{out}")
        if rc != 0:
            lines = [ln.strip() for ln in out.splitlines() if "실패" in ln or "거부" in ln][:6]
            self._notify("EMERGENCY", "시작 점검 실패(코드 %d) — run 시작 안 함, 오늘 매매 없음\n%s" % (rc, "\n".join(lines)))
            return 1
        self._note("시작 점검 통과")
        stop_file = self.base / STOP_REQUEST
        if stop_file.exists():
            stop_file.unlink()
        launched = self.d.clock.now()
        run = self.d.launch()
        try:
            return self._after_launch(run, day, session, deadline, launched)
        except Exception as exc:  # noqa: BLE001 — 자동 운영 결함으로 run을 고아로 두지 않는다 (재검토 M1)
            self.d.log(f"자동 운영 예외:\n{traceback.format_exc()}")   # pythonw에는 표준 오류가 없다 → 일 로그
            stopped = False
            try:
                stopped = run.alive() and self._write_stop("자동 운영 예외")
            except Exception:  # noqa: BLE001
                pass
            self._notify("EMERGENCY", f"자동 운영 오류 {type(exc).__name__} — "
                                      f"{'run에 정지 요청 파일 작성(정상 종료 경로)' if stopped else 'run은 실행 중 아님'}. "
                                      f"종료 절차·요약 없음, PC에서 var/paper/logs/autopilot 로그 확인")
            return 1

    def _after_launch(self, run: RunHandle, day: date, session: SessionInfo, deadline: datetime,
                      launched: datetime) -> int:
        self._started_seen = False
        self._notify("INFO", f"시작 점검 통과, run 시작({_hm(launched)}) — K8 자동 재개 진행")
        assert session.regular_close is not None
        stop_at = max(_at(day, self.s.stop_at), session.regular_close + self.s.stop_after_close)
        outcome = self._k8_phase(run, launched, deadline, stop_at)
        if outcome == "DIED" and not self._started_seen and run.exit_code() == 2 and instance_running(self.base):
            # 확인과 시작 사이에 수동 run이 켜져 자식 run이 실행 잠금에 막힘 — 사망이 아니라 경합 (재검토 L6)
            self._notify("WARNING", "수동 run이 이미 실행 중 — 자동 운영 중단(자식 run은 실행 잠금에 막혀 종료). "
                                    "paper-review·백업은 수동 절차로")
            return 2
        if outcome == "DIED" or not self._day_phase(run, stop_at):
            return self._finish(run, day, died=True)
        if outcome != "RESUMED":
            self.problems.append("K8 자동 재개 못 함")
        self._close_phase(run, session, stop_at)
        return self._finish(run, day, died=False)

    # --- K8 자동 재개 -------------------------------------------------------------------------
    def _k8_phase(self, run: RunHandle, launched: datetime, deadline: datetime, stop_at: datetime) -> str:
        s = self.s
        attempts = 0
        pending: str | None = None
        pending_at = launched
        next_try = launched
        human_seen: tuple[str, ...] = ()
        warned_start = warned_fail = warned_slow = False
        last_reason = ""
        since = to_iso(launched.astimezone(UTC))
        while True:
            if not run.alive():
                return "DIED"
            now = self.d.clock.now()
            st = self._read(lambda repo: self._k8_state(repo, since, pending))
            if st is not None:
                cmd = st.command
                if cmd is not None and cmd["status"] != "PENDING":
                    pending = None
                    if cmd["status"] != "DONE":
                        last_reason = str(cmd["result"] or cmd["status"])
                        self.d.log(f"K8 자동 재개 거부(시도 {attempts}): {last_reason}")
                        if not warned_fail:
                            warned_fail = True
                            self._notify("WARNING", f"K8 자동 재개 실패(시도 {attempts}회): {last_reason} — "
                                                    f"{_minutes(s.resume_every)}분마다 재시도, {_hm(deadline)}까지")
                self._started_seen = self._started_seen or st.started
                if st.started and not st.k8:
                    if attempts:
                        self._notify("INFO", f"K8 자동 재개 완료({_hm(now)}, 시도 {attempts}회) — 정상 운영")
                        self._note(f"K8 자동 재개(시도 {attempts}회)")
                    else:
                        self._notify("INFO", f"K8이 이미 해제됨({_hm(now)}, 다른 경로) — 자동 재개 생략")
                        self._note("K8 이미 해제(다른 경로)")
                    return "RESUMED"
                if st.started and pending is None:
                    if st.human:
                        if st.human != human_seen:
                            human_seen = st.human
                            self._notify("WARNING", f"사람 확인 정지 {', '.join(st.human)}가 걸려 있어 K8을 자동 재개하지 "
                                                    f"않음 — 확인 후 사용자가 재개(/resume 또는 PC). {_hm(deadline)}까지 그 "
                                                    f"정지가 풀리면 자동 재개를 이어 간다")
                    else:
                        human_seen = ()
                        if now >= next_try and attempts < s.resume_max and now < deadline:
                            pending = self._insert_resume(now, deadline, attempts + 1)
                            if pending is not None:
                                attempts += 1
                                pending_at = now
                            next_try = now + s.resume_every
                elif pending is not None and now - pending_at > s.command_wait and not warned_slow:
                    warned_slow = True
                    self._notify("WARNING", f"K8 자동 재개 명령이 {_minutes(s.command_wait)}분 넘게 처리되지 않음 — "
                                            f"run 멈춤 의심(/status 확인)")
                elif not st.started and not warned_start and now - launched > s.start_wait:
                    warned_start = True
                    self._notify("WARNING", f"run 시작(시작 대조)이 {_minutes(s.start_wait)}분 넘게 확인되지 않음 — "
                                            f"/status 확인")
            if now >= deadline and (pending is None or now >= deadline + s.command_wait):
                why = f" 마지막 사유: {last_reason}" if last_reason else ""
                if human_seen:
                    why = f" 사람 확인 정지: {', '.join(human_seen)}"
                self._notify("WARNING", f"{_hm(deadline)}까지 K8 자동 재개 못 함(시도 {attempts}회) — 오늘 매매 없음. "
                                        f"run은 {_hm(stop_at)}까지 유지(마감 대조·종료 절차 그대로), 원격 /resume K8 가능."
                                        f"{why}")
                self._note(f"K8 자동 재개 못 함(시도 {attempts}회)")
                return "DEADLINE"
            self.d.sleep(s.poll_seconds)

    def _k8_state(self, repo: Repository, since: str, pending: str | None) -> _K8State:
        started = bool(repo.select("SELECT 1 FROM reconciliation WHERE account=? AND trigger='STARTUP' AND created_at>=? "
                                   "LIMIT 1", (self.account, since)))
        rows = [r for r in repo.halt_rows(None) if r["account"] in (None, self.account)]
        k8 = any(r["halt_type"] == HaltType.K8.value for r in rows)
        human = tuple(sorted({r["halt_type"] for r in rows if HaltType(r["halt_type"]) in HUMAN_HALTS}))
        return _K8State(started, k8, human, repo.command(pending) if pending else None)

    def _insert_resume(self, now: datetime, deadline: datetime, n: int) -> str | None:
        cid = f"auto-{now.astimezone(KST):%Y%m%d}-{n}-{uuid.uuid4().hex[:8]}"
        args = {"account": self.account, "halt_type": HaltType.K8.value, "not_after": to_iso(deadline.astimezone(UTC))}

        def put(repo: Repository) -> str:
            with repo.tx():
                repo.insert_command(cid, Channel.AUTO, AUTO_ACTOR, "RESUME", args, now)
                repo.audit(now, "OPS_COMMAND_RECEIVED", AUTO_ACTOR, cid, {"command": "RESUME", "channel": Channel.AUTO})
            return cid

        done = self._read(put)
        if done is not None:
            self.d.log(f"K8 자동 재개 명령 {n}: {cid}")
        return done

    # --- 장중 감시 ----------------------------------------------------------------------------
    def _day_phase(self, run: RunHandle, stop_at: datetime) -> bool:
        stale = False
        while True:
            now = self.d.clock.now()
            if now >= stop_at:
                return True
            if not run.alive():
                return False
            hb = watchdog_check(self.base / "heartbeat", now, self.s.heartbeat_stale)
            if not hb.alive and not stale:
                stale = True
                self.problems.append("하트비트 멈춤")
                self._notify("EMERGENCY", f"run 하트비트 멈춤: {hb.detail} — 자동 재시작 없음, PC·/status 확인")
            elif hb.alive and stale:
                stale = False
                self._notify("INFO", "run 하트비트 회복")
            self.d.sleep(min(self.s.day_poll_seconds, max(1.0, (stop_at - now).total_seconds())))

    # --- 마감·종료 ----------------------------------------------------------------------------
    def _close_phase(self, run: RunHandle, session: SessionInfo, stop_at: datetime) -> None:
        assert session.regular_close is not None
        since = to_iso(session.regular_close.astimezone(UTC))
        limit = stop_at + self.s.close_wait
        while True:
            if not run.alive():
                break
            now = self.d.clock.now()
            st = self._read(lambda repo: self._close_state(repo, since))
            if st is not None and st[0] and st[1] == 0:
                result, open_items = st[2], st[3]
                if result == "MATCH" and open_items == 0:
                    self._note("마감 대조 완료(일치)")
                else:   # 불일치·열린 항목은 "완료"로 보고하지 않는다 (재검토 L5)
                    what = f"마감 대조 불일치({result}, 열린 불일치 {open_items}건)"
                    self.problems.append(what)
                    self._note(what)
                break
            if now >= limit:
                done, inflight = (st[0], st[1]) if st is not None else (False, -1)
                self.problems.append("마감 대조 미완료" if not done else f"전송 중 의도 {inflight}건")
                self._notify("WARNING", f"마감 대조 {'완료' if done else '미완료'}, 전송 중 의도 "
                                        f"{'확인 불가' if inflight < 0 else str(inflight) + '건'} — "
                                        f"{_minutes(self.s.close_wait)}분 기다린 뒤 그대로 종료(다음 시작 대조에서 확인)")
                break
            self.d.sleep(self.s.poll_seconds)
        if run.alive():
            self._write_stop("15:45 종료 절차")
            end = self.d.clock.now() + self.s.exit_wait
            while run.alive() and self.d.clock.now() < end:
                self.d.sleep(min(self.s.poll_seconds, 5.0))
        if run.alive():
            self.problems.append("run 정상 종료 안 됨")
            self._notify("EMERGENCY", f"정지 요청 뒤 {_minutes(self.s.exit_wait)}분 안에 run이 끝나지 않음 — PC에서 확인. "
                                      f"백업·요약은 그대로 진행")
        else:
            self._note(f"run 종료(코드 {run.exit_code()})")

    def _close_state(self, repo: Repository, since: str) -> tuple[bool, int, str | None, int]:
        """(마감 대조 완료, 전송 중 의도 수, 마지막 마감 대조 결과, 열린 불일치 수)."""
        rows = repo.select("SELECT result FROM reconciliation WHERE account=? AND trigger='CLOSE' AND result<>'QUERY_FAILED' "
                           "AND created_at>=? ORDER BY created_seq DESC LIMIT 1", (self.account, since))
        inflight = [i for s in (IntentState.PENDING_SEND, IntentState.SENT) for i in repo.intents_in_state(s)
                    if i.account == self.account]
        return bool(rows), len(inflight), (rows[0]["result"] if rows else None), len(repo.open_recon_items(self.account))

    def _finish(self, run: RunHandle, day: date, *, died: bool) -> int:
        if died:
            code = run.exit_code()
            self.problems.append(f"run 조기 종료(코드 {code})")
            hint = ("정지 요청 파일·수동 조작으로 끝났을 수 있음" if code == 0
                    else "PC에서 var/paper/logs(fatal.log·run-console) 확인")
            self._notify("EMERGENCY", f"run 프로세스가 종료됨(코드 {code}) — 자동 재시작 없음, 오늘 남은 매매 없음. {hint}")
        for name, fn in (("paper-review", self.d.review), ("백업", self.d.backup)):
            try:
                rc, out = fn()
            except Exception as exc:  # noqa: BLE001 — 한 단계 실패로 요약을 막지 않는다
                rc, out = 99, f"{type(exc).__name__}"
            self.d.log(f"{name} 출력:\n{out}")
            if rc == 0:
                self._note(f"{name} 완료")
            else:
                self.problems.append(f"{name} 실패")
                self._note(f"{name} 실패(코드 {rc})")
        try:
            text = build_day_summary(self.db, Mode.PAPER, self.account, day, self.reference_time, self.rates, self.notes)
        except Exception as exc:  # noqa: BLE001
            text = f"[{day} 일일 요약] 집계 실패({type(exc).__name__}) — PC에서 paper-review 확인\n" + "\n".join(self.notes)
        if self.problems:
            text += "\n확인 필요: " + ", ".join(self.problems)
        self._notify("INFO", text)
        return 1 if self.problems else 0

    # --- 보조 ---------------------------------------------------------------------------------
    def _read(self, fn: Callable[[Repository], object]):  # type: ignore[no-untyped-def]
        """저장소 한 번 열어 읽기·쓰기. 실패(잠금 등)는 기록만 하고 None — 다음 확인 때 다시 본다."""
        if not self.db.exists():
            return None
        try:
            repo = Repository(self.db, Mode.PAPER)
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
        text = self._mask.text(text)   # 계좌번호 형태·토큰 등 가림 (재검토 L4) — 시작 점검 출력 줄 등 외부 문구 포함
        self.d.log(f"알림 {level}: {text}")
        try:
            self.d.notify(level, text)
        except Exception as exc:  # noqa: BLE001 — 알림 실패로 운영 절차를 멈추지 않는다
            self.d.log(f"알림 전송 실패: {type(exc).__name__}")


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=KST).astimezone(UTC)


def _hm(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H:%M")


def _minutes(d: timedelta) -> int:
    return int(d.total_seconds() // 60)


# --- 실행 환경 조립 (작업 스케줄러가 부르는 진입점) ------------------------------------------------------

class DayLog:
    """var/paper/logs/autopilot-YYYYMMDD.log에 한 줄씩 (가림 처리). 콘솔이 있으면 함께 출력."""

    def __init__(self, logs: Path, clock: Clock) -> None:
        self._logs = logs
        self._clock = clock
        self._mask = Masker()

    def __call__(self, message: str) -> None:
        now = self._clock.now().astimezone(KST)
        text = self._mask.text(message)
        try:
            self._logs.mkdir(parents=True, exist_ok=True)
            with (self._logs / f"autopilot-{now:%Y%m%d}.log").open("a", encoding="utf-8") as f:
                f.write(f"{now:%H:%M:%S}\t{text}\n")
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


class TelegramNotify:
    """자동 운영 알림을 텔레그램으로 직접 보낸다(최대 3회 시도). 실패는 로그에만 — 토큰은 오류 문구에 없다."""

    def __init__(self, api, chat: str, log: Callable[[str], None],  # type: ignore[no-untyped-def]
                 sleep: Callable[[float], None] = _time.sleep) -> None:
        self._api, self._chat, self._log, self._sleep = api, chat, log, sleep

    def __call__(self, level: str, text: str) -> None:
        from trading_system.ops.telegram import TelegramError
        msg = Masker().text(f"[{LEVEL_KO.get(level, level)}] [자동 운영] {text}")
        for i in range(3):
            try:
                self._api.send_message(self._chat, msg)
                return
            except TelegramError as exc:
                self._log(f"텔레그램 전송 실패 {i + 1}/3: {exc}")
                if exc.permanent:
                    return
            self._sleep(2.0)


class SubprocessRun:
    """run 자식 프로세스. 출력은 var/paper/logs/run-console-YYYYMMDD.log (창 없음 — 무인 실행)."""

    def __init__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._out = log_path.open("ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._p = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                   stdout=self._out, stderr=subprocess.STDOUT, creationflags=flags)

    def alive(self) -> bool:
        return self._p.poll() is None

    def exit_code(self) -> int | None:
        code = self._p.poll()
        if code is not None and not self._out.closed:
            self._out.close()
        return code


def console_python() -> str:
    """작업 스케줄러는 pythonw.exe(창 없음)로 이 도구를 부른다. run 자식은 같은 폴더의 python.exe로(출력을 파일로 받음)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def captured(fn: Callable[[], int]) -> tuple[int, str]:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = fn()
    except Exception as exc:  # noqa: BLE001 — 점검·집계 도구의 예외는 실패 코드로
        return 99, buf.getvalue() + f"\n예외: {type(exc).__name__}"
    return rc, buf.getvalue()


def autopilot_main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/autopilot.py",
                                description="PAPER 무인 자동 운영 (CP-029). 작업 스케줄러가 평일 06:45에 실행한다")
    p.add_argument("--mode", required=True, help="PAPER만 허용 (LIVE·BACKTEST는 거부)")
    p.add_argument("--root", default=str(REPO_ROOT), help="저장소 루트")
    ns = p.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    root = Path(ns.root).resolve()
    why = mode_refusal(ns.mode, env, root / ".env")   # .env는 TRADE_MODE 한 줄만 읽는다 (config/modes)
    if why:
        _say(f"자동 운영 거부: {why}")
        return 2
    base = root / "var" / "paper"
    clock = SystemClock()
    log = DayLog(base / "logs", clock)
    try:
        from trading_system.config.app_file import load_app_settings
        from trading_system.config.market_file import load_market_settings
        from trading_system.marketdata.errors import MarketDataError

        from .startup import check_startup
        from .telegram_run import CHAT_KEY, TOKEN_KEY, check_telegram_secrets
        try:
            startup = check_startup(Mode.PAPER, root, base / "trading.db", root / ".env.paper")
            app = load_app_settings(root / "config" / "app.paper.toml", Mode.PAPER, root)
            if app.account != startup.account:
                raise ConfigError("배포 설정의 계좌 별칭과 한도 파일의 계좌가 다르다")
            if not app.telegram_enabled:
                raise ConfigError("무인 자동 운영에는 텔레그램 알림이 켜져 있어야 한다 (config/app.paper.toml [telegram])")
            check_telegram_secrets(startup.secrets)
            calendar = KrxCalendar.load_dir(root / "config" / "calendar")
            rates = load_market_settings(root / "config" / "market.paper.toml").cost_rates
        except (ConfigError, MarketDataError, OSError) as exc:
            log(f"자동 운영 거부(설정): {exc}")
            return 2
    except ImportError as exc:   # pragma: no cover — 설치 문제
        log(f"자동 운영 거부(모듈): {type(exc).__name__}")
        return 2

    from trading_system.ops.cli import backup_main
    from trading_system.ops.telegram import TelegramApi

    from .preflight import preflight_main
    from .review_tool import review_main
    assert startup.secrets is not None
    api = TelegramApi(startup.secrets.get(TOKEN_KEY).reveal)
    notify = TelegramNotify(api, startup.secrets.get(CHAT_KEY).reveal().strip(), log)
    child_env = {**env, "TRADE_MODE": Mode.PAPER.value, "PYTHONIOENCODING": "utf-8",
                 "PYTHONPATH": os.pathsep.join(x for x in (str(root / "src"), env.get("PYTHONPATH", "")) if x)}
    common = ["--mode", Mode.PAPER.value, "--root", str(root)]

    def launch() -> RunHandle:
        stamp = clock.now().astimezone(KST).strftime("%Y%m%d")
        return SubprocessRun([console_python(), "-m", "trading_system.app", "run", "--root", str(root)], root,
                             child_env, base / "logs" / f"run-console-{stamp}.log")

    deps = AutopilotDeps(clock=clock, sleep=_time.sleep, notify=notify,
                         preflight=lambda: captured(lambda: preflight_main(root, child_env)),
                         launch=launch, review=lambda: captured(lambda: review_main(common)),
                         backup=lambda: captured(lambda: backup_main(common)), log=log)
    log(f"자동 운영 시작 (PAPER, 계좌 {app.account})")
    try:
        rc = Autopilot(Mode.PAPER, base, app.account, calendar, Schedule(), deps,
                       reference_time=startup.limits.daily_reference_time, rates=rates).run()
    except Exception as exc:  # noqa: BLE001 — run 시작 전 단계의 결함 (시작 뒤는 Autopilot._day가 처리)
        log(f"자동 운영 예외:\n{traceback.format_exc()}")
        notify("EMERGENCY", f"자동 운영 오류 {type(exc).__name__} — PC에서 var/paper/logs/autopilot 로그 확인")
        return 1
    log(f"자동 운영 끝 (코드 {rc})")
    return rc


def ops_stop_main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """PAPER run 정상 종료 요청 (A-079, CP-029 D-159): 정지 요청 파일 var/paper/STOP을 쓴다. PAPER 전용.

    run이 실행 중(run.lock 보유)일 때만 쓴다 — 꺼져 있으면 다음 시작이 지울 파일을 남기지 않는다. --wait초 동안
    실행 잠금이 풀리는지(=정상 종료) 확인한다. 주문·정지 판정을 하지 않는다(정지는 청산이 아님)."""
    p = argparse.ArgumentParser(prog="scripts/ops-stop.py", description="PAPER run 정상 종료 요청 (정지 요청 파일)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--root", default=str(REPO_ROOT), help="저장소 루트")
    p.add_argument("--wait", type=int, default=60, help="종료 확인 대기(초), 0이면 쓰기만")
    ns = p.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    why = mode_refusal(ns.mode, env)
    if why:
        print(f"거부: {why}")
        return 2
    base = Path(ns.root).resolve() / "var" / "paper"
    if not instance_running(base):
        print("거부: PAPER run이 실행 중이 아님 — 정지 요청 파일을 만들지 않음")
        return 1
    (base / STOP_REQUEST).write_text(f"ops-stop {to_iso(datetime.now(UTC))}\n", encoding="utf-8")
    print(f"정지 요청 파일 작성: {base / STOP_REQUEST} (run이 tick 사이에 확인해 Ctrl+C와 같은 순서로 종료)")
    end = _time.monotonic() + max(0, ns.wait)
    while ns.wait > 0 and _time.monotonic() < end:
        if not instance_running(base):
            print("run 종료 확인(실행 잠금 해제)")
            return 0
        _time.sleep(1.0)
    if ns.wait > 0:
        print(f"{ns.wait}초 안에 종료 확인 안 됨 — scripts/status.py --mode PAPER로 확인")
        return 1
    return 0


__all__ = ["AUTO_ACTOR", "Autopilot", "AutopilotDeps", "AutopilotRefused", "Schedule", "autopilot_main",
           "mode_refusal", "HUMAN_HALTS", "ops_stop_main"]
