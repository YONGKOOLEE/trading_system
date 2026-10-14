"""저녁 수집 — CAP-DAT-05a 투자자별 일별 순매수 (T-035, CP-033 D-188·D-190·D-191·D-192, 승인 A-100 Q-6 (a)·Q-8 (b)).

자동 운영(app/autopilot.py)의 15:45 종료 절차(마감 대조 → run 정상 종료 → paper-review → 백업 → 일일 요약) **뒤** 한 단계로
불린다. 설정 `config/collect.paper.toml`의 `enabled = true`일 때만 — 파일이 없거나 꺼져 있으면 아무것도 하지 않는다(기록도
없음). 수집 결과·실패는 알림·기록만 하고 자동 운영 종료 코드·요약·다음 날 운영에 영향을 주지 않는다.

조회만 한다. 주문 API는 부르지 않는다(HTTP 경계 허용 목록 au10001·au10002·ka10060 — 밖의 api-id는 전송 전 차단).

연결 전 검사 (하나라도 어긋나면 네트워크 객체를 만들지 않는다 — 코드 2)
- PAPER run 실행 잠금(run.lock)·하트비트(30초 이내)가 있으면 거부. 자동 운영 잠금(autopilot.lock)은 호출자 자신이라 보지 않는다.
- 거래일에는 max(15:45, 정규장 마감 + 15분) KST 전이면 거부(자동 운영 Schedule.stop_at·stop_after_close와 같은 값).
- 마감 시한(설정 deadline, 기본 17:30 KST — 작업 스케줄러 실행 시간 상한 11시간(06:45~17:45) 안) 뒤면 거부.
- 종목 목록 파일(설정 codes_file — 코스피200 구성 목록, A-100 Q-8 (b))을 못 읽으면 거부.
- TRADE_MODE=PAPER·.env.paper·connection_approved = true·모의 도메인(connect_paper) — 하나라도 아니면 거부.

수집 (종목마다, TR당 1.2초 간격)
- ka10060(dt = 마지막으로 장이 끝난 거래일, 금액·순매수)을 확인 구간(최근 window_days 거래일) 시작보다 오래된 행이 나올
  때까지 연속 조회(T-034 collect_fetch 재사용 — 같은 쪽 3회 연속 실패면 그 종목 실패).
- 품질(D-191, marketdata.investor_flow): 무효 일자는 저장하지 않고 감사 INVESTOR_FLOW_QUALITY에 사유를 남긴다. 합계 ≠ 0은
  경고만(저장).
- 저장(D-190, storage investor_flow·investor_flow_seen): 새 값 = 회차 1, 같은 값 = 관측만 추가, 다른 값 = 새 회차 + 감사
  INVESTOR_FLOW_CORRECTED. 확정(D-189)은 관측 시각으로 판정한다 — 다음 날 저녁 수집이 전날 값을 다시 받아 확정 근거가 된다.
- 종목 사이마다 run.lock·하트비트·마감 시한을 다시 보고, 걸리면 나머지는 조회하지 않는다. 3종목 연속 실패도 중단.
- 기록: var/paper/collect/evening-<YYYYMMDD-HHMMSS>.json (종목별 결과·건수·호출 수 — 값·토큰·앱키·계좌번호 없음).
종료 코드: 0 = 모두 정상, 1 = 일부 실패·미조회·중단, 2 = 연결 전 거부(연결 없음).
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
from trading_system.broker.kiwoom.token import TokenError
from trading_system.config.errors import ConfigError
from trading_system.domain.clock import KST, Clock
from trading_system.domain.enums import Mode
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.investor_flow import SOURCE as FLOW_SOURCE, is_trading_day, parse_ka10060_rows
from trading_system.storage.repository import Repository

from .instance_lock import instance_running
from .investor_probe import collect_fetch, last_closed_day, read_code_list
from .preflight import connect_paper, engine_alive
from .probe_tool import (MAX_CONSECUTIVE_FAILURES, PROBE_INTERVAL, UNSUPPORTED_MARK, ProbeError, _api_summary,
                         _load_calendar, _mask, fatal_critical)

CONFIG_REL = Path("config") / "collect.paper.toml"
EVENING_REST_APIS = frozenset({"au10001", "au10002", "ka10060"})   # HTTP 경계 허용 목록 (D-192·V-262)
EVENING_EARLIEST = time(15, 45)            # 자동 운영 Schedule.stop_at과 같은 값 (시험이 같음을 확인)
AFTER_CLOSE = timedelta(minutes=15)        # 자동 운영 Schedule.stop_after_close와 같은 값
DEADLINE_DEFAULT = time(17, 30)
DEADLINE_MAX = time(17, 30)                # 작업 스케줄러 실행 시간 상한(06:45 + 11시간 = 17:45) 전 15분 여유
WINDOW_DEFAULT, WINDOW_MAX = 5, 120        # 확인 구간(거래일). ka10060 1쪽 = 100행(K-U42 실측)
SETTING_KEYS = frozenset({"enabled", "codes_file", "window_days", "deadline", "financials"})
COLLECTOR = "collector"


@dataclass(frozen=True)
class CollectSettings:
    enabled: bool
    codes_file: Path | None
    window_days: int = WINDOW_DEFAULT
    deadline: time = DEADLINE_DEFAULT
    financials: bool = False


OFF = CollectSettings(False, None)


# --- 설정 -------------------------------------------------------------------------------------------------------

def _hhmm(raw: object, name: str) -> time:
    if not isinstance(raw, str):
        raise ConfigError(f"{name}: \"HH:MM\" 문자열")
    try:
        t = time.fromisoformat(raw)
    except ValueError:
        raise ConfigError(f"{name}: \"HH:MM\" 형식 아님") from None
    if t.second or t.microsecond:
        raise ConfigError(f"{name}: 분 단위까지만")
    return t


def load_collect_settings(path: Path, root: Path) -> CollectSettings:
    """config/collect.paper.toml. 파일이 없으면 꺼짐. 형식 오류·알 수 없는 키·켜졌는데 필수 키 누락이면 ConfigError."""
    if not path.exists():
        return OFF
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name}: 읽기·형식 오류 ({type(exc).__name__})") from None
    unknown = sorted(set(raw) - SETTING_KEYS)
    if unknown:
        raise ConfigError(f"{path.name}: 알 수 없는 키 {', '.join(unknown)} (허용: {', '.join(sorted(SETTING_KEYS))})")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled: true|false")
    if enabled:
        missing = sorted(SETTING_KEYS - set(raw))
        if missing:
            raise ConfigError(f"켜려면 필요한 키 없음: {', '.join(missing)}")
    codes = raw.get("codes_file")
    if codes is not None and (not isinstance(codes, str) or not codes.strip()):
        raise ConfigError("codes_file: 종목 목록 파일 경로 문자열 (저장소 기준 상대 경로 가능)")
    window = raw.get("window_days", WINDOW_DEFAULT)
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= WINDOW_MAX:
        raise ConfigError(f"window_days: 1~{WINDOW_MAX} 정수 (확인 구간 거래일 수)")
    deadline = _hhmm(raw.get("deadline", DEADLINE_DEFAULT.strftime("%H:%M")), "deadline")
    if not EVENING_EARLIEST < deadline <= DEADLINE_MAX:
        raise ConfigError(f"deadline: {EVENING_EARLIEST:%H:%M} 초과 ~ {DEADLINE_MAX:%H:%M} 이하 "
                          "(작업 스케줄러 실행 시간 상한 17:45 전)")
    financials = raw.get("financials", False)
    if not isinstance(financials, bool):
        raise ConfigError("financials: true|false")
    if financials:
        raise ConfigError("financials: 재무 스냅샷 축적(T-037) 미구현 — false만")
    path_ = None if codes is None else (Path(codes) if Path(codes).is_absolute() else root / codes)
    return CollectSettings(enabled, path_, window, deadline, financials)


# --- 시간 판정 (순수 함수) -----------------------------------------------------------------------------------------

def window_start(last_day: date, n: int, calendar: KrxCalendar | None) -> date:
    """last_day부터 거꾸로 n번째 거래일 (last_day가 거래일이면 그날이 1번째)."""
    d, seen = last_day, 0
    for _ in range(n * 3 + 31):
        if is_trading_day(calendar, d):
            seen += 1
            if seen == n:
                return d
        d -= timedelta(days=1)
    return d


def _earliest(day: date, calendar: KrxCalendar | None) -> datetime | None:
    """거래일이면 저녁 수집 허용 시작 시각(KST), 아니면 None."""
    session = calendar.session(day) if calendar is not None else None
    trading = session.is_open if session is not None else day.weekday() < 5
    if not trading:
        return None
    at = datetime.combine(day, EVENING_EARLIEST, tzinfo=KST)
    if session is not None and session.regular_close is not None:
        at = max(at, (session.regular_close + AFTER_CLOSE).astimezone(KST))
    return at


def _running(base: Path, now: datetime) -> str | None:
    if instance_running(base):
        return "PAPER run이 실행 중(run.lock 보유) — 수집하지 않음"
    return engine_alive(base / "heartbeat", now)


def evening_refusal(base: Path, now: datetime, calendar: KrxCalendar | None, deadline: time) -> str | None:
    """연결 전 거부 사유 (V-262). 자동 운영 잠금(autopilot.lock)은 호출자 자신이라 보지 않는다."""
    why = _running(base, now)
    if why:
        return why
    local = now.astimezone(KST)
    earliest = _earliest(local.date(), calendar)
    if earliest is not None and local < earliest:
        return (f"거래일 {local:%H:%M} KST — 저녁 수집은 {earliest:%H:%M} KST(자동 운영 15:45 종료 절차, 늦은 마감은 "
                f"마감 + 15분) 뒤에만")
    if local >= datetime.combine(local.date(), deadline, tzinfo=KST):
        return f"마감 시한 {deadline:%H:%M} KST 지남 — 오늘은 수집하지 않음(다음 거래일 저녁 수집이 확인 구간을 다시 받음)"
    return None


# --- 수집 ---------------------------------------------------------------------------------------------------------

def collect_investor_flows(adapter: Any, guard: Any, repo: Repository, codes: Sequence[str], last_day: date,
                           calendar: KrxCalendar | None, clock: Clock, *, window_days: int,
                           should_stop: Callable[[], str | None], log: Callable[[str], None],
                           run_id: str) -> dict:
    """종목마다 ka10060 확인 구간 조회 → 품질 → 저장. 토큰은 호출자가 발급·폐기한다."""
    first = window_start(last_day, window_days, calendar)
    out: dict[str, Any] = {"codes": len(codes), "ok": 0, "failed": [], "not_queried": [], "new": 0, "same": 0,
                           "corrected": 0, "invalid_days": 0, "sum_warnings": 0, "stopped": None}
    streak = 0
    for idx, code in enumerate(codes):
        why = should_stop()
        if why is None and (guard.blocked or fatal_critical(adapter)):
            why = "허용 밖 API 시도 또는 브로커 치명 오류"
        if why is not None:
            out["not_queried"], out["stopped"] = list(codes[idx:]), why
            log(f"  중단: {why} — 나머지 {len(codes) - idx}종목 조회 안 함")
            break
        res, rows = collect_fetch(adapter, guard, code, last_day, first)
        if res["error"] is not None or not rows:
            reason = str(res["error"] or f"{first:%Y%m%d} 이후 행 없음")
            out["failed"].append({"code": code, "reason": reason, "pages": res["pages"]})
            log(f"  {idx + 1:>3}/{len(codes)} {code}: 실패 — {reason}")
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES or UNSUPPORTED_MARK in reason:
                out["not_queried"], out["stopped"] = list(codes[idx + 1:]), f"연속 {streak}종목 실패"
                log(f"  중단: 연속 {streak}종목 실패 — 나머지 {len(codes) - idx - 1}종목 조회 안 함")
                break
            continue
        streak = 0
        p = parse_ka10060_rows(rows, code, calendar, first=first, last=last_day)
        now = clock.now()
        with repo.tx():
            w = repo.record_investor_flows(p.flows, FLOW_SOURCE, now, run_id)
            if p.invalid or p.warnings or p.bad_rows:
                repo.audit(now, "INVESTOR_FLOW_QUALITY", COLLECTOR, code,
                           {"invalid": p.invalid, "sum_warnings": p.warnings, "bad_rows": p.bad_rows, "run": run_id})
        out["ok"] += 1
        out["new"] += w.new
        out["same"] += w.same
        out["corrected"] += w.corrected
        out["invalid_days"] += len(p.invalid)
        out["sum_warnings"] += len(p.warnings)
        log(f"  {idx + 1:>3}/{len(codes)} {code}: 새 {w.new} 같음 {w.same} 정정 {w.corrected} 무효 {len(p.invalid)} "
            f"합계 경고 {len(p.warnings)}")
    return out


# --- 진입 ---------------------------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=PROBE_INTERVAL, allowed_apis=EVENING_REST_APIS)


def _stop_reason(base: Path, clock: Clock, deadline: time) -> str | None:
    now = clock.now()
    why = _running(base, now)
    if why:
        return why
    local = now.astimezone(KST)
    if local >= datetime.combine(local.date(), deadline, tzinfo=KST):
        return f"마감 시한 {deadline:%H:%M} KST 도달"
    return None


def evening_collect(root: Path, environ: Mapping[str, str] | None, clock: Clock, settings: CollectSettings, *,
                    connect: Connect | None = None, log: Callable[[str], None] = print) -> int:
    """저녁 수집 한 번. 마지막 기록 줄이 요약("저녁 수집(05a): …")이다."""
    base = root / "var" / "paper"
    calendar = _load_calendar(root)
    now = clock.now()

    def refuse(why: str) -> int:
        log(f"저녁 수집 거부(연결 없음): {why}")
        return 2

    why = evening_refusal(base, now, calendar, settings.deadline)
    if why:
        return refuse(why)
    if settings.codes_file is None:
        return refuse("종목 목록 파일(codes_file) 설정 없음")
    try:
        codes = read_code_list(settings.codes_file)
    except (OSError, ProbeError) as exc:
        return refuse(f"종목 목록 {settings.codes_file.name}: {type(exc).__name__ if isinstance(exc, OSError) else exc}")
    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return refuse("접속 대상이 키움 모의 도메인이 아님")
    last_day = last_closed_day(now, calendar)
    stamp = now.astimezone(KST)
    run_id = f"evening-{stamp:%Y%m%d-%H%M%S}"
    first = window_start(last_day, settings.window_days, calendar)
    log(f"저녁 수집 시작: {len(codes)}종목 ka10060 {first:%Y%m%d}~{last_day:%Y%m%d} (TR당 {PROBE_INTERVAL}초 간격)")
    meta: dict[str, Any] = {"tool": "app/investor_collect.py", "run": run_id, "basis": "CP-033 A-100 — 조회만, 주문 없음",
                            "started_at": stamp.isoformat(timespec="seconds"), "account_alias": adapter.cfg.account_alias,
                            "codes_file": settings.codes_file.name, "allowed_rest_apis": sorted(EVENING_REST_APIS),
                            "window": {"first": first.isoformat(), "last": last_day.isoformat(),
                                       "trading_days": settings.window_days},
                            "token": {"issued": False, "revoked": None}, "failures": [], "investor": None,
                            "stopped": None}
    repo = Repository(base / "trading.db", Mode.PAPER)
    try:
        try:
            adapter.tokens.token()
            meta["token"]["issued"] = True
        except TokenError as exc:
            meta["failures"].append(f"토큰 발급 실패: {_mask(exc)}")
        if meta["token"]["issued"]:
            inv = collect_investor_flows(adapter, guard, repo, codes, last_day, calendar, clock,
                                         window_days=settings.window_days,
                                         should_stop=lambda: _stop_reason(base, clock, settings.deadline),
                                         log=log, run_id=run_id)
            meta["investor"], meta["stopped"] = inv, inv["stopped"]
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        repo.close()
        if meta["token"]["issued"]:
            meta["token"]["revoked"] = revoked
    meta["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
    meta["api_calls"] = _api_summary(guard)
    meta["calls"] = sum(s["calls"] for s in meta["api_calls"].values())
    meta["blocked_before_send"] = list(guard.blocked)
    meta["critical"] = [f"{k}: {_mask(m)}" for k, m in adapter.critical]
    (base / "collect").mkdir(parents=True, exist_ok=True)
    mpath = base / "collect" / f"{run_id}.json"
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    inv = meta["investor"] or {"codes": len(codes), "ok": 0, "failed": [], "not_queried": list(codes), "new": 0,
                               "same": 0, "corrected": 0, "invalid_days": 0, "sum_warnings": 0}
    bad = bool(meta["failures"] or inv["failed"] or inv["not_queried"] or meta["blocked_before_send"]
               or meta["critical"])
    log(f"저녁 수집(05a): 종목 {inv['codes']} 성공 {inv['ok']} 실패 {len(inv['failed'])} 미조회 "
        f"{len(inv['not_queried'])}, 새 {inv['new']} 같음 {inv['same']} 정정 {inv['corrected']}, 무효 일자 "
        f"{inv['invalid_days']}, 합계 경고 {inv['sum_warnings']}, 호출 {meta['calls']}회"
        + (f", 중단: {meta['stopped']}" if meta["stopped"] else "") + (", 토큰 발급 실패" if meta["failures"] else ""))
    return 1 if bad else 0


def evening_from_autopilot(root: Path, environ: Mapping[str, str] | None, clock: Clock, *,
                           connect: Connect | None = None, settings_path: Path | None = None) -> tuple[int, str] | None:
    """자동 운영 종료 절차 뒤 단계 (A-100 Q-6 (a)). 꺼짐·설정 없음 → None(아무것도 하지 않음).

    (종료 코드, 기록 — 첫 줄이 요약)을 돌려준다. 예외는 호출자(자동 운영)가 잡는다."""
    try:
        settings = load_collect_settings(settings_path or root / CONFIG_REL, root)
    except ConfigError as exc:
        return 2, f"저녁 수집 설정 오류(config/collect.paper.toml): {exc} — 수집 안 함"
    if not settings.enabled:
        return None
    lines: list[str] = []
    rc = evening_collect(root, environ, clock, settings, connect=connect, log=lines.append)
    return rc, "\n".join([lines[-1], *lines[:-1]]) if lines else ""


__all__ = ["AFTER_CLOSE", "CONFIG_REL", "CollectSettings", "EVENING_EARLIEST", "EVENING_REST_APIS",
           "collect_investor_flows", "evening_collect", "evening_from_autopilot", "evening_refusal",
           "load_collect_settings", "window_start"]
