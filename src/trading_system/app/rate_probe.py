"""PV-12 키움 모의 호출 제한(1700) 유도 실측 도구 — 조회 API 하나만 (04 §4.1 PV-12, kiwoom-kr §2·K-U10, 승인 A-097 ③).

목적: 모의 서버에서 같은 조회 TR을 점점 짧은 간격으로 보내 호출 제한 오류(1700, 또는 1701·1702)가 처음 나오는 간격과,
1700 뒤 정상 응답이 돌아오기까지 필요한 대기(K-U10)를 기록한다. 04는 "PV-12는 조회 API로만 유도한다"고 정했다 —
이 도구는 주문 API를 부르지 않는다(HTTP 경계 허용 목록 {au10001, au10002, ka10001}, 밖의 api-id는 전송 전에 막는다).

사람이 직접 실행한다. 승인 A-097 ③: "저녁(run 꺼진 시간) 실행". 같은 앱키로 PAPER run·자동 운영이 돌고 있으면 그쪽
조회까지 호출 제한에 걸리고, 끝의 토큰 폐기가 엔진 토큰을 무효화할 수 있으므로 실행 중이면 거부한다.

연결 전 검사 (하나라도 어긋나면 네트워크 객체를 만들지 않는다 — 종료 코드 2)
- `--mode PAPER`만. TRADE_MODE=PAPER·.env.paper·connection_approved = true·모의 도메인(preflight.connect_paper).
- 장외 전용: probe_tool.market_hours_refusal 그대로(KRX 거래일 06:30~17:00 KST, 늦게 끝나는 날은 마감 + 90분 거부).
- PAPER run(run.lock)·자동 운영(autopilot.lock)·하트비트(30초 이내)가 있으면 거부 (probe_tool._running_refusal).

절차
1. 토큰 발급 au10001 (어댑터 TokenManager, 호출 제한기 기본 간격 그대로).
2. 간격 단계 --intervals(기본 1.0 → 0.5 → 0.25 → 0초)마다 ka10001 주식기본정보(본문 {"stk_cd": 종목}, 종목 1개 —
   기본 005930)를 최대 --per-step회(기본 10). 간격은 직전 요청을 보낸 시각부터 잰다. **이 도구 안에서만** 간격을
   줄인다 — 어댑터·호출 제한기 기본 간격(1.1초)과 M1 PAPER run 동작은 바꾸지 않는다. 요청은 어댑터의 조회 재시도
   (_query_page: 1700이면 지수 대기 후 최대 3회)를 거치지 않고 GuardedHttp로 한 번씩 보낸다 — 첫 1700을 그대로 보기 위해서다.
3. 1700·1701·1702가 오면 **그 단계를 즉시 중단** → 그 응답을 받은 뒤 1·2·4·8·16초(상한 16초)를 차례로 기다렸다 1회씩
   보내 정상 응답이 처음 돌아온 대기를 "회복에 필요한 대기"로 기록 → 종료(다음 단계로 가지 않는다).
   대기는 직전 응답(앞선 회복 시도의 1700 포함)을 받은 뒤부터 잰다. 앞선 시도도 호출이므로 측정값은 보수적(큰 쪽)이다.
4. 즉시 중단(더 보내지 않음): HTTP 418·403(차단), 1700~1702가 아닌 429, 인증 오류(401·8001~8031 등),
   1687, 그 밖의 0이 아닌 코드·return_code 없음·본문 해석 불가·5xx, 전송 오류(연결 실패·타임아웃).
5. 상한: ka10001 전체 호출 --max-calls(기본·최대 80), 전체 시간 --max-seconds(기본·최대 180초, 토큰 발급 뒤부터).
   다음 호출이 상한을 넘기면 보내지 않고 끝낸다(회복 측정 중에도 같다).
6. 끝나면 토큰 폐기 au10002 (어느 경로로 끝나도).

기록: `var/paper/probe/<YYYYMMDD-HHMMSS>-rate-probe.json`·`.md` — 단계별 간격·호출 수·응답 코드 분포·실제 간격 최소/최대,
첫 1700 시각과 그 직전 간격, 회복 시도(대기·코드), 실패 응답의 return_msg(Masker 가림·160자)만. 가격 값·응답 원문·
토큰·앱키·계좌번호는 기록·출력하지 않는다. 판정(K-U10 해소 여부·D-048 보수값 조정)은 사람이 결과를 보고
kiwoom-kr에 "② 모의 실측, 일자"로 적는다(필요하면 CP).

사용 (PowerShell, 저녁·주말·휴일, PAPER run·자동 운영이 꺼진 뒤):
    cd C:\\AI\\trading_system\\trading_system
    $env:TRADE_MODE='PAPER'
    .venv\\Scripts\\python scripts\\rate-probe.py --mode PAPER

    선택: --code 005930 (종목 1개), --intervals "1.0,0.5,0.25,0" (초, 내림차순, 0~1.2, 최대 6개),
          --per-step 10 (1~20), --max-calls 80 (1~80), --max-seconds 180 (1~180)

기대 출력 (예: 0.25초 단계 첫 호출에서 1700, 2초 대기로 회복한 경우 — 실제 값은 서버가 정한다):
    호출 제한 실측 시작: ka10001 005930 / 간격 1.0 → 0.5 → 0.25 → 0.0초, 단계마다 최대 10회 / 상한 80회·180초
    토큰 발급 au10001: 정상
      간격 1.0초: 10회 {'0': 10} — 단계 완료
      간격 0.5초: 10회 {'0': 10} — 단계 완료
      간격 0.25초: 1회 {'1700': 1} — 1700 수신 — 단계 중단
    첫 1700: 2026-..T..+09:00, 단계 0.25초 1번째, 직전 간격 0.25초, [1700:...]
      회복 대기 1초: 코드 1700
      회복 대기 2초: 코드 0
    결과: 1700 유도·회복 측정 완료 — 회복에 필요한 대기 2초
    토큰 폐기 au10002: 완료
    기록: ...\\var\\paper\\probe\\<시각>-rate-probe.json
    요약: ...\\var\\paper\\probe\\<시각>-rate-probe.md
    (1700이 끝까지 안 나오면 "결과: 1700 미발생" — 단계마다 10회, 모두 약 20초)
종료 코드: 0 = 측정 완료(1700 유도·회복 또는 미발생), 1 = 중단·회복 안 됨·상한 도달·토큰 실패, 2 = 실행 전 거부(연결 없음).
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
from trading_system.broker.kiwoom.errors import ErrorClass, classify, normalize_code, specific_code
from trading_system.broker.kiwoom.http import AfterSendError, HttpReply, PreSendError
from trading_system.broker.kiwoom.token import TokenError
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.marketdata.kiwoom_feed import INFO_PATH

from .preflight import PROBE_DEFAULT, connect_paper
from .probe_tool import (
    ProbeError, _api_summary, _load_calendar, _mask, _running_refusal, market_hours_refusal, parse_codes)

# --- 허용 목록·설정값 -----------------------------------------------------------------------------------

PROBE_API = "ka10001"                                            # 주식기본정보 (조회 TR 하나, A-097 ③)
RATE_REST_APIS = frozenset({"au10001", "au10002", PROBE_API})    # HTTP 경계 허용 목록
TOKEN_INTERVAL = 1.2            # 어댑터 호출 제한기 간격(토큰 발급 au10001에만 쓰임) — 보수값 그대로
DEFAULT_INTERVALS = (1.0, 0.5, 0.25, 0.0)
INTERVAL_MAX, INTERVALS_MAX_ITEMS = 1.2, 6
PER_STEP_DEFAULT, PER_STEP_MAX = 10, 20
MAX_CALLS_DEFAULT = MAX_CALLS_LIMIT = 80
MAX_SECONDS_DEFAULT = MAX_SECONDS_LIMIT = 180
RECOVERY_WAITS = (1, 2, 4, 8, 16)   # 1700 뒤 회복 확인 대기(초), 상한 16초
LIMIT_CODES = frozenset({1700, 1701, 1702})   # kiwoom-kr §2: API별·전체·그룹 요청 초과
BLOCK_STATUS = frozenset({403, 418})          # 차단 응답 — 즉시 중단


class RateProbeError(RuntimeError):
    pass


# --- 응답 분류 (순수 함수) -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    kind: str                 # "OK" | "LIMIT" | "ABORT"
    status: int | None
    code: int | None
    reason: str = ""          # ABORT 사유
    message: str = ""         # 실패 응답 return_msg (가림 처리)

    @property
    def key(self) -> str:
        if self.code is not None:
            return str(self.code)
        return "전송 오류" if self.status is None else f"HTTP {self.status}"


def classify_reply(r: HttpReply) -> Outcome:
    """한 응답 → 정상 / 호출 제한(1700~1702) / 즉시 중단. GuardedHttp·어댑터와 같은 코드 해석(specific_code·classify)."""
    msg = "" if r.body is None else _mask(r.body.get("return_msg"))
    code = None if r.body is None else specific_code(normalize_code(r.body.get("return_code")), r.body.get("return_msg"))
    if r.status in BLOCK_STATUS:
        return Outcome("ABORT", r.status, code, f"차단 응답 HTTP {r.status}", msg)
    if r.body is None:
        if r.status == 401:
            return Outcome("ABORT", r.status, None, "인증 오류 HTTP 401 (본문 해석 불가)")
        return Outcome("ABORT", r.status, None, f"HTTP {r.status} 본문 해석 불가")
    if r.status == 401 and code in (None, 0):
        code = 8005
    if code in LIMIT_CODES:
        return Outcome("LIMIT", r.status, code, "", msg)
    if r.status == 429:
        return Outcome("ABORT", r.status, code, f"HTTP 429 (코드 {code}, 1700~1702 아님)", msg)
    if code is None:
        return Outcome("ABORT", r.status, None, f"return_code 없음 (HTTP {r.status})", msg)
    cls = classify(code)
    if cls is ErrorClass.OK and r.status < 400:
        return Outcome("OK", r.status, 0)
    word = "인증 오류 " if cls is ErrorClass.AUTH else ""
    return Outcome("ABORT", r.status, code, f"{word}HTTP {r.status} 코드 {code} ({cls.value})", msg)


# --- 시간 ------------------------------------------------------------------------------------------------

class Pacer:
    """보낸 시각 기준 간격 유지와 상한 계산. monotonic·sleep 주입(시험은 가짜 시간)."""

    def __init__(self, monotonic: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._mono = monotonic
        self._sleep = sleep
        self.start = monotonic()
        self.last_sent: float | None = None

    def elapsed(self) -> float:
        return self._mono() - self.start

    def until_gap(self, interval: float) -> float:
        """직전 요청을 보낸 뒤 interval초가 되려면 더 기다려야 할 시간."""
        if self.last_sent is None:
            return 0.0
        return max(self.last_sent + interval - self._mono(), 0.0)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    def mark_sent(self) -> float | None:
        """지금 보낸다 — 직전 요청과의 실제 간격(초, 밀리초 반올림)을 돌려준다."""
        now = self._mono()
        gap = None if self.last_sent is None else round(now - self.last_sent, 3)
        self.last_sent = now
        return gap


# --- 실측 ------------------------------------------------------------------------------------------------

class Run:
    """ka10001 한 종목을 보내며 단계·회복을 기록한다. adapter는 토큰, guard는 HTTP(허용 목록) 경계."""

    def __init__(self, adapter: Any, guard: Any, code: str, clock: Clock, pacer: Pacer, max_calls: int,
                 max_seconds: float, log: Callable[[str], None]) -> None:
        self.adapter, self.guard, self.code, self.clock, self.pacer = adapter, guard, code, clock, pacer
        self.max_calls, self.max_seconds, self.log = max_calls, max_seconds, log
        self.calls = 0
        self.messages: dict[str, str] = {}     # 코드 → 마지막 실패 return_msg (가림)

    def cap_reason(self, wait: float) -> str | None:
        if self.calls >= self.max_calls:
            return f"호출 상한 {self.max_calls}회 도달"
        if self.pacer.elapsed() + wait > self.max_seconds:
            return f"시간 상한 {self.max_seconds:.0f}초 도달"
        return None

    def send(self) -> tuple[Outcome, float | None, str]:
        """한 번 보낸다(재시도 없음). (결과, 직전 요청과의 간격, 보낸 시각 KST)."""
        try:
            token = self.adapter.tokens.token()
        except TokenError as exc:
            return Outcome("ABORT", None, None, f"토큰 오류: {_mask(exc)}"), None, ""
        gap = self.pacer.mark_sent()
        at = self.clock.now().astimezone(KST).isoformat(timespec="milliseconds")
        self.calls += 1
        try:
            r = self.guard.post(INFO_PATH, PROBE_API, {"stk_cd": self.code}, token)
        except (PreSendError, AfterSendError) as exc:
            return Outcome("ABORT", None, None, f"전송 오류: {_mask(exc)}"), gap, at
        out = classify_reply(r)
        if out.kind != "OK" and out.message:
            self.messages[out.key] = out.message
        return out, gap, at   # 인증 오류여도 토큰을 무효화하지 않는다 — 끝의 폐기(au10002)가 그 토큰을 보내야 한다

    def steps(self, intervals: Sequence[float], per_step: int) -> tuple[list[dict], dict | None, str | None]:
        """(단계 기록, 첫 호출 제한, 중단 사유)."""
        out: list[dict] = []
        for interval in intervals:
            step: dict[str, Any] = {"interval": interval, "calls": 0, "codes": {}, "gap_min": None, "gap_max": None,
                                    "ended": "단계 완료"}
            out.append(step)
            for n in range(1, per_step + 1):
                wait = self.pacer.until_gap(interval)
                cap = self.cap_reason(wait)
                if cap:
                    step["ended"] = cap
                    self._log_step(step)
                    return out, None, cap
                self.pacer.sleep(wait)
                res, gap, at = self.send()
                self._count(step, res, gap)
                if res.kind == "LIMIT":
                    step["ended"] = f"{res.code} 수신 — 단계 중단"
                    self._log_step(step)
                    first = {"code": res.code, "at": at, "step_interval": interval, "call_in_step": n,
                             "total_call": self.calls, "gap_before": gap, "http_status": res.status,
                             "return_msg": res.message}
                    return out, first, None
                if res.kind == "ABORT":
                    step["ended"] = f"중단 — {res.reason}"
                    self._log_step(step)
                    return out, None, res.reason
            self._log_step(step)
        return out, None, None

    def recover(self) -> tuple[dict, str | None]:
        """1700 응답을 받은 뒤 1·2·4·8·16초 대기 후 1회씩. (회복 기록, 중단 사유)."""
        rec: dict[str, Any] = {"waits": list(RECOVERY_WAITS), "attempts": [], "recovered_after": None}
        for wait in RECOVERY_WAITS:
            cap = self.cap_reason(wait)
            if cap:
                return rec, cap
            self.pacer.sleep(wait)
            res, gap, at = self.send()
            rec["attempts"].append({"wait": wait, "code": res.code, "http_status": res.status, "at": at, "gap": gap,
                                    "kind": res.kind})
            self.log(f"  회복 대기 {wait}초: {'코드 ' + res.key if res.kind != 'ABORT' else '중단 — ' + res.reason}")
            if res.kind == "OK":
                rec["recovered_after"] = wait
                return rec, None
            if res.kind == "ABORT":
                return rec, res.reason
        return rec, None

    @staticmethod
    def _count(step: dict, res: Outcome, gap: float | None) -> None:
        step["calls"] += 1
        step["codes"][res.key] = step["codes"].get(res.key, 0) + 1
        if gap is not None:
            step["gap_min"] = gap if step["gap_min"] is None else min(step["gap_min"], gap)
            step["gap_max"] = gap if step["gap_max"] is None else max(step["gap_max"], gap)

    def _log_step(self, step: dict) -> None:
        self.log(f"  간격 {step['interval']}초: {step['calls']}회 {step['codes']} — {step['ended']}")


# --- 인자 ------------------------------------------------------------------------------------------------

def parse_intervals(text: str) -> list[float]:
    out: list[float] = []
    for part in text.split(","):
        s = part.strip()
        try:
            v = float(s)
        except ValueError:
            raise RateProbeError(f"--intervals 값 {s[:8]!r} — 숫자(초)") from None
        if not 0.0 <= v <= INTERVAL_MAX:
            raise RateProbeError(f"--intervals 값 {s[:8]!r} — 0~{INTERVAL_MAX}초")
        out.append(v)
    if len(out) > INTERVALS_MAX_ITEMS:
        raise RateProbeError(f"--intervals는 최대 {INTERVALS_MAX_ITEMS}개")
    if any(b >= a for a, b in zip(out, out[1:])):
        raise RateProbeError("--intervals는 줄어드는 순서(예: 1.0,0.5,0.25,0)")
    return out


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog="scripts/rate-probe.py",
                                description="키움 모의 호출 제한(1700) 유도 실측 — ka10001 조회만 (PV-12, A-097 ③)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--code", default=PROBE_DEFAULT, help=f"종목코드 1개 (기본 {PROBE_DEFAULT})")
    p.add_argument("--intervals", default=",".join(str(v) for v in DEFAULT_INTERVALS),
                   help="간격 단계(초) 쉼표 목록, 줄어드는 순서")
    p.add_argument("--per-step", type=int, default=PER_STEP_DEFAULT, help=f"단계마다 최대 호출 (1~{PER_STEP_MAX})")
    p.add_argument("--max-calls", type=int, default=MAX_CALLS_DEFAULT, help=f"ka10001 전체 호출 상한 (1~{MAX_CALLS_LIMIT})")
    p.add_argument("--max-seconds", type=int, default=MAX_SECONDS_DEFAULT,
                   help=f"전체 시간 상한 초 (1~{MAX_SECONDS_LIMIT})")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def _check_args(ns: argparse.Namespace) -> tuple[str, list[float]]:
    if not 1 <= ns.per_step <= PER_STEP_MAX:
        raise RateProbeError(f"--per-step {ns.per_step} — 1~{PER_STEP_MAX}")
    if not 1 <= ns.max_calls <= MAX_CALLS_LIMIT:
        raise RateProbeError(f"--max-calls {ns.max_calls} — 1~{MAX_CALLS_LIMIT}")
    if not 1 <= ns.max_seconds <= MAX_SECONDS_LIMIT:
        raise RateProbeError(f"--max-seconds {ns.max_seconds} — 1~{MAX_SECONDS_LIMIT}")
    try:
        codes = parse_codes(ns.code)
    except ProbeError as exc:
        raise RateProbeError(str(exc)) from None
    if len(codes) != 1:
        raise RateProbeError("--code는 종목 1개만")
    return codes[0], parse_intervals(ns.intervals)


# --- 진입점 ----------------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=TOKEN_INTERVAL, allowed_apis=RATE_REST_APIS)


def _refuse(msg: str) -> int:
    print(f"호출 제한 실측 거부(연결 없음): {msg}")
    return 2


def rate_probe_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
                    clock: Clock | None = None, connect: Connect | None = None,
                    monotonic: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회)에서만 실행한다. LIVE·BACKTEST는 연결하지 않는다")
    try:
        code, intervals = _check_args(ns)
    except RateProbeError as exc:
        return _refuse(str(exc))
    clock = clock or SystemClock()
    now = clock.now()
    hours = market_hours_refusal(now, _load_calendar(root))
    if hours:
        return _refuse(hours)
    base = root / "var" / "paper"
    running = _running_refusal(base, now)
    if running:
        return _refuse(running)

    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return _refuse("접속 대상이 키움 모의 도메인이 아님 — 실측하지 않는다")

    shown = " → ".join(str(v) for v in intervals)
    print(f"호출 제한 실측 시작: {PROBE_API} {code} / 간격 {shown}초, 단계마다 최대 {ns.per_step}회 / "
          f"상한 {ns.max_calls}회·{ns.max_seconds}초")
    report: dict[str, Any] = {
        "tool": "scripts/rate-probe.py", "basis": "04 PV-12, kiwoom-kr K-U10, 승인 A-097 ③ (조회 API만, 주문 없음)",
        "started_at": now.astimezone(KST).isoformat(timespec="seconds"), "account_alias": adapter.cfg.account_alias,
        "api": PROBE_API, "code": code,
        "settings": {"intervals": intervals, "per_step": ns.per_step, "max_calls": ns.max_calls,
                     "max_seconds": ns.max_seconds, "recovery_waits": list(RECOVERY_WAITS),
                     "allowed_rest_apis": sorted(RATE_REST_APIS)},
        "token": {"issued": False, "expires_at": None, "revoked": None},
        "steps": [], "first_limit": None, "recovery": None, "calls_total": 0, "elapsed_seconds": None,
        "result": "", "failures": [],
    }
    try:
        try:
            adapter.tokens.token()
            exp = adapter.tokens.expires_at
            report["token"].update(issued=True, expires_at=exp.astimezone(KST).isoformat() if exp else None)
            print("토큰 발급 au10001: 정상")
        except TokenError as exc:
            report["failures"].append(f"토큰 발급 실패: {_mask(exc)}")
            report["result"] = "중단 — 토큰 발급 실패"
            print(f"토큰 발급 실패 — 조회 안 함: {_mask(exc)}")
        if report["token"]["issued"]:
            _measure(report, Run(adapter, guard, code, clock, Pacer(monotonic, sleep), ns.max_calls,
                                 float(ns.max_seconds), print), intervals, ns.per_step)
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        if report["token"]["issued"]:
            report["token"]["revoked"] = revoked
            print(f"토큰 폐기 au10002: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    report["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
    report["api_calls"] = _api_summary(guard)
    report["blocked_before_send"] = list(guard.blocked)
    if report["blocked_before_send"]:
        report["failures"].append(f"허용 밖 API 시도(전송 전 차단): {', '.join(report['blocked_before_send'])}")
    paths = write_report(base / "probe", report, now)
    print(f"기록: {paths[0]}")
    print(f"요약: {paths[1]}")
    return 1 if report["failures"] else 0


def _measure(report: dict, run: Run, intervals: Sequence[float], per_step: int) -> None:
    steps, first, stop = run.steps(intervals, per_step)
    report["steps"], report["first_limit"] = steps, first
    if first is not None:
        print(f"첫 {first['code']}: {first['at']}, 단계 {first['step_interval']}초 {first['call_in_step']}번째, "
              f"직전 간격 {first['gap_before']}초, {first['return_msg'] or '-'}")
        rec, stop = run.recover()
        report["recovery"] = rec
        if rec["recovered_after"] is not None:
            report["result"] = "1700 유도·회복 측정 완료"
        elif stop:
            report["result"] = f"중단 — 회복 측정 중 {stop}"
            report["failures"].append(report["result"])
        else:
            report["result"] = f"1700 유도 — {RECOVERY_WAITS[-1]}초 대기로도 회복 안 됨"
            report["failures"].append(report["result"])
    elif stop:
        report["result"] = f"중단 — {stop}"
        report["failures"].append(report["result"])
    else:
        report["result"] = "1700 미발생"
    report["calls_total"] = run.calls
    report["elapsed_seconds"] = round(run.pacer.elapsed(), 3)
    report["return_msgs"] = dict(run.messages)
    rec = report["recovery"]
    tail = f" — 회복에 필요한 대기 {rec['recovered_after']}초" if rec and rec["recovered_after"] is not None else ""
    print(f"결과: {report['result']}{tail}")


# --- 기록 ------------------------------------------------------------------------------------------------

def write_report(directory: Path, report: dict, now: datetime) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(KST):%Y%m%d-%H%M%S}-rate-probe"
    jpath, mpath = directory / f"{stem}.json", directory / f"{stem}.md"
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    mpath.write_text(render_md(report), encoding="utf-8")
    return jpath, mpath


def render_md(report: dict) -> str:
    rec = report["recovery"]
    first = report["first_limit"]
    L: list[str] = [
        f"# 키움 모의 호출 제한 실측 (PV-12) {report['started_at'][:10]}", "",
        f"- 근거: {report['basis']}",
        f"- 실행: {report['started_at']} ~ {report.get('finished_at', '-')} KST, 계좌 별칭 {report['account_alias']}, "
        f"{report['api']} {report['code']}",
        f"- 설정: {json.dumps(report['settings'], ensure_ascii=False)}",
        f"- 토큰: 발급 {report['token']['issued']}, 폐기 {report['token']['revoked']}",
        f"- 결과: **{report['result'] or '-'}** (ka10001 {report['calls_total']}회, {report['elapsed_seconds']}초)",
        "- 이 파일은 간격·호출 수·반환 코드·서버 문구(가림)만 담는다. 가격 값과 응답 원문은 저장하지 않았다.",
        "- 판정(K-U10 해소·D-048 보수값 조정)은 사람이 이 결과로 하고 kiwoom-kr.md에 \"② 모의 실측\"으로 적는다.", "",
        "## 단계별", "",
        "| 간격(초) | 호출 | 코드별 | 실제 간격 최소(초) | 실제 간격 최대(초) | 끝 |", "|---|---|---|---|---|---|",
    ]
    L += [f"| {s['interval']} | {s['calls']} | {s['codes']} | {s['gap_min']} | {s['gap_max']} | {s['ended']} |"
          for s in report["steps"]]
    L += ["", "## 첫 호출 제한", ""]
    if first is None:
        L.append("- 1700 미발생" if report["result"] == "1700 미발생" else "- 없음")
    else:
        L += [f"- 코드 {first['code']} (HTTP {first['http_status']}), 시각 {first['at']}",
              f"- 단계 {first['step_interval']}초의 {first['call_in_step']}번째 (전체 {first['total_call']}번째), "
              f"직전 요청과의 실제 간격 {first['gap_before']}초",
              f"- return_msg: {first['return_msg'] or '-'}"]
    L += ["", "## 회복 (K-U10)", ""]
    if rec is None:
        L.append("- 측정 안 함")
    else:
        L += ["- 대기는 직전 응답을 받은 뒤부터 잰다. 앞선 회복 시도도 호출이므로 값은 보수적(큰 쪽)이다.", "",
              "| 대기(초) | 코드 | HTTP | 시각 |", "|---|---|---|---|"]
        L += [f"| {a['wait']} | {a['code'] if a['code'] is not None else a['kind']} | {a['http_status']} | {a['at']} |"
              for a in rec["attempts"]]
        got = rec["recovered_after"]
        L += ["", f"- 회복에 필요한 대기: {got}초" if got is not None else "- 회복에 필요한 대기: 확인 못 함"]
    L += ["", "## 실패 응답 문구 (가림)", ""]
    L += [f"- {k}: {v}" for k, v in sorted(report.get("return_msgs", {}).items())] or ["- 없음"]
    L += ["", "## API 호출 (HTTP 경계 기록)", "", "| api-id | 호출 | 정상 | 코드별 | 마지막 실패 |", "|---|---|---|---|---|"]
    L += [f"| {a} | {s['calls']} | {s['ok']} | {s['codes']} | {s['last_error'] or '-'} |"
          for a, s in sorted(report["api_calls"].items())]
    L += ["", f"- 전송 전 차단(허용 밖 API): {', '.join(report['blocked_before_send']) or '없음'}", "", "## 실패", ""]
    L += [f"- {f}" for f in report["failures"]] or ["- 없음"]
    return "\n".join(L) + "\n"
