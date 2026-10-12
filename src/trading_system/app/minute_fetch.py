"""분봉 축적 도구 — 키움 모의 ka10080 조회만 (T-028, CP-032 D-177, 승인 A-095 ②). 사람이 직접 실행한다.

근거: A-095 (2026-10-12) "② 분봉 매일 축적: 저녁(run 꺼진 뒤) 관심 종목 분봉 저장, 대상은 전략실이 정한 목록".
제공 범위(K-U30 2026-10-12 모의 실측): 약 1년(2025-10-01 이후). 매일 저녁 실행해 보관 기간을 늘리는 용도다.
분봉을 쓰는 전략의 PAPER 사용은 분봉 전용 검증 기준(A-095 ③, 보류) 전 불가 — 이 도구는 백테스트·연구용 CSV만 만든다.

연결 전 검사 (하나라도 어긋나면 네트워크 객체를 만들지 않는다 — 종료 코드 2)
- `--mode PAPER`만. TRADE_MODE=PAPER·.env.paper·connection_approved = true·모의 도메인(connect_paper).
- 장외 전용: KRX 거래일 06:30~17:00 KST(늦게 끝나는 날은 마감 + 90분)이면 거부 (probe_tool.market_hours_refusal).
- PAPER run(run.lock)·자동 운영(autopilot.lock)·하트비트(30초 이내)가 있으면 거부.
- 허용 API(HTTP 경계): au10001·au10002·ka10080만. 밖의 api-id는 전송 전에 막는다. TR당 1.2초 간격.

종목마다
1. 기존 `<out>/<코드>-<길이>m.csv`가 있으면 먼저 읽는다(이 도구 형식이 아니면 그 종목 실패, 파일은 그대로).
   조회 하한 = --since가 기존 첫 날보다 앞이면 --since(앞쪽 채우기), 아니면 max(--since, 기존 마지막 날).
2. ka10080(tic_scope=--minutes, upd_stkpc_tp=1 수정주가, base_dt=마지막으로 장이 끝난 거래일) 연속 조회 —
   하한보다 오래된 봉이 나온 쪽에서 멈춘다. 같은 쪽 3회 연속 실패면 그 종목 실패(누락 — 아무것도 쓰지 않음).
   3종목 연속 실패면 나머지는 조회하지 않는다. 종목당 안전 상한 400쪽.
3. 정규장 봉만·확정 봉만·날짜별 품질(marketdata.minute.prepare_minutes). 품질 무효인 날은 쓰지 않고 meta에 적는다.
   연속 조회가 하한에 닿지 못하고 끝났으면(제공 범위 끝·상한) 가장 오래된 날은 부분일일 수 있다 → 첫 정규장 봉이
   그날 개장 시각이 아니면 그날을 버리고 적는다.
4. CSV 이어 쓰기: 이미 있는 시각은 건너뛰고(기존 값 유지 — 값이 다르면 건수만 적음) 새 봉만 더한다.
   열 datetime(봉 시작 시각 KST, D-177 가정),open,high,low,close,volume.
- 기록: `<out>/meta/minute-fetch-<YYYYMMDD-HHMMSS>.json` — 종목별 쪽 수·추가·건너뜀·제외 건수·무효인 날·실패·미조회·
  API 호출 수. 토큰·앱키·계좌번호는 기록·출력하지 않는다.

사용 (PowerShell, 저녁·주말·휴일에 — 거래일 06:30~17:00 KST에는 거부됨):
    cd C:\\AI\\trading_system\\trading_system
    $env:TRADE_MODE='PAPER'
    @("005930", "000660", "069500") | Set-Content -Encoding utf8 var\\backtest\\minute-codes.txt
    .venv\\Scripts\\python scripts\\fetch-minute.py --mode PAPER --codes-file var\\backtest\\minute-codes.txt --minutes 1 --since 2025-10-01 --out var\\backtest\\data\\kiwoom-ka10080-adj

기대 출력 (첫 실행, 종목당 1분봉 1년 ≈ 175쪽 × 1.2초 ≈ 3.5분):
    분봉 수집 시작: 3종목 ka10080 1분 수정주가 2025xxxx~2026xxxx(마지막 거래일) (TR당 1.2초 간격) → ...\\kiwoom-ka10080-adj
      1/3 005930: 2025-10-0x~2026-xx-xx 추가 ...봉 건너뜀 0 (정규장 밖 ...·부분일 제외 2025-10-01)
    토큰 폐기 au10002: 완료
    요약: 종목 3 성공 3 실패 0 미조회 0, 호출 ...회, 저장 ...\\kiwoom-ka10080-adj, 메타 minute-fetch-<시각>.json
다음 날 저녁 같은 명령 → 기존 마지막 날부터만 조회, "추가 380봉 건너뜀 380"처럼 새 거래일 봉만 더해진다(1분봉, 정규장
09:00~15:19 380봉 — 15:20~15:29 마감 동시호가는 체결 없음 = 거래 없음, 15:30 동시호가 체결 봉은 제외).
종료 코드: 0 = 모두 정상, 1 = 일부 실패·미조회(메타의 failed·not_queried), 2 = 실행 전 거부(연결 없음).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
from trading_system.broker.kiwoom.token import TokenError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.minute import (
    CONFIRM_MARGIN, MIN_LIST_KEY, MINUTE_RULES_VERSION, MINUTE_SCOPES, MergeResult, MinuteDataMissing, kst_day,
    merge_minute_csv, minute_csv_name, prepare_minutes, read_minute_csv)
from trading_system.storage.repository import Repository

from .investor_probe import last_closed_day, read_code_list
from .preflight import connect_paper
from .probe_tool import (MAX_CONSECUTIVE_FAILURES, PROBE_INTERVAL, UNSUPPORTED_MARK, ProbeError, _api_summary,
                         _load_calendar, _mask, _running_refusal, fatal_critical, market_hours_refusal)

TOOL = "scripts/fetch-minute.py"
BASIS = "A-095 ② (2026-10-12) CP-032 단계 2 T-028 — 조회만, 주문 없음"
MINUTE_REST_APIS = frozenset({"au10001", "au10002", "ka10080"})   # HTTP 경계 허용 목록
CHART_PATH = "/api/dostk/chart"
UPD_STKPC_TP = "1"                 # 수정주가 (D-177: 백테스트·PAPER 판정 같은 값)
SINCE_DEFAULT = "2025-10-01"       # K-U30 실측 제공 시작
OUT_DEFAULT = "var/backtest/data/kiwoom-ka10080-adj"
PAGES_MAX = 400                    # 종목당 안전 상한 (1분봉 1년 ≈ 175쪽)
SOURCE = "kiwoom ka10080 upd_stkpc_tp=1"
REGULAR_RULE = ("봉 [cntr_tm, cntr_tm + 길이)가 KRX 달력의 그날 정규장 [개장, 마감) 안에 완전히 들어가는 봉만 "
                "(A-095·K-U43, cntr_tm = 봉 시작 가정 D-177). 마감 동시호가 체결 봉·장 마감 뒤 봉·휴장일·달력 없는 날 제외")


# --- 조회 ----------------------------------------------------------------------------------------------------

def _row_day(row: object) -> str | None:
    """행의 날짜(YYYYMMDD). 시각 형식이 아니면 None — 걸러내지 않고 파싱이 누락으로 잡게 둔다."""
    t = row.get("cntr_tm") if isinstance(row, dict) else None
    return t[:8] if isinstance(t, str) and len(t) == 14 and t.isascii() and t.isdigit() else None


def fetch_minute_rows(adapter: Any, guard: Any, code: str, minutes: int, base_day: date,
                      floor: date) -> tuple[dict, list[object]]:
    """한 종목 ka10080 연속 조회(최신→과거). floor보다 오래된 날의 봉이 나온 쪽에서 멈추고 floor 이후 행만 돌려준다.
    같은 쪽 3회 연속 실패면 error(누락)."""
    body = {"stk_cd": code, "tic_scope": str(minutes), "upd_stkpc_tp": UPD_STKPC_TP, "base_dt": f"{base_day:%Y%m%d}"}
    floor_s = f"{floor:%Y%m%d}"
    res: dict[str, Any] = {"pages": 0, "error": None, "end": None}
    rows: list[object] = []
    cont: tuple[str | None, str | None] = (None, None)
    streak = 0
    while res["pages"] < PAGES_MAX:
        if guard.blocked or fatal_critical(adapter):
            res["error"] = "허용 밖 API 시도 또는 브로커 치명 오류 — 누락"
            return res, rows
        try:
            with call_priority(Priority.MARKET):
                page = adapter._query_page("ka10080", body, cont, CHART_PATH)  # noqa: SLF001 — 쪽 단위 재시도
            items = (page.body or {}).get(MIN_LIST_KEY)
            if not isinstance(items, list):
                raise BrokerQueryError(f"ka10080: 목록 키 {MIN_LIST_KEY} 없음 또는 목록 아님")
        except BrokerQueryError as exc:
            streak += 1
            if UNSUPPORTED_MARK in str(exc) or streak >= MAX_CONSECUTIVE_FAILURES:
                res["error"] = f"{res['pages'] + 1}쪽 조회 {streak}회 실패 — 누락: {_mask(exc)}"
                return res, rows
            continue
        streak = 0
        res["pages"] += 1
        rows.extend(items)
        if any((d := _row_day(i)) is not None and d < floor_s for i in items):
            res["end"] = "since 도달"
            break
        if not ((page.cont_yn or "").upper() == "Y" and page.next_key):
            res["end"] = "더 없음"
            break
        cont = ("Y", page.next_key)
    else:
        res["end"] = f"상한 {PAGES_MAX}쪽 도달"
    return res, [r for r in rows if (d := _row_day(r)) is None or d >= floor_s]


# --- 종목 처리 ---------------------------------------------------------------------------------------------------

def _floor(existing: Sequence[Any], since: date) -> date:
    if not existing:
        return since
    first, last = kst_day(existing[0]), kst_day(existing[-1])
    return since if since < first else max(since, last)


def process_code(rows: Sequence[object], res: Mapping[str, Any], code: str, minutes: int, calendar: KrxCalendar,
                 now: datetime, path: Path) -> tuple[dict, str | None]:
    """조회 결과 → CSV 이어 쓰기. (종목 기록, 실패 사유 또는 None)."""
    sup = prepare_minutes(rows, code, minutes, calendar, now)
    if sup.missing:
        return {"pages": res["pages"], "end": res["end"]}, f"누락: {sup.reason}"
    bars = list(sup.bars)
    dropped: list[str] = []
    if res["end"] != "since 도달" and bars:
        oldest = kst_day(bars[0])
        s = calendar.session(oldest)
        if s is None or s.regular_open is None or bars[0].start != s.regular_open:
            dropped.append(oldest.isoformat())
            bars = [b for b in bars if kst_day(b) != oldest]
    merged = merge_minute_csv(path, bars)
    rec = {"pages": res["pages"], "end": res["end"], "rows_received": len(rows),
           "first": bars[0].start.astimezone(KST).date().isoformat() if bars else None,
           "last": bars[-1].start.astimezone(KST).date().isoformat() if bars else None,
           "added": merged.added, "skipped_existing": merged.skipped, "differing_existing": merged.differing,
           "excluded": dict(sup.excluded), "partial_days_dropped": dropped,
           "invalid_days": [{"day": d.isoformat(), "issues": list(v[:10])} for d, v in sup.invalid_days],
           "no_trade_slots": sum(n for d, n in sup.no_trade_slots.items() if d.isoformat() not in dropped)}
    return rec, None


def export_minute_csv(repo: Repository, instrument: str, minutes: int, out_dir: Path,
                      adjusted: str = "1") -> tuple[Path, MergeResult]:
    """DB 분봉 표 → `<out_dir>/<코드>-<길이>m.csv` (백테스트·연구용, 이어 쓰기)."""
    path = out_dir / minute_csv_name(instrument, minutes)
    return path, merge_minute_csv(path, repo.minute_bars(instrument, minutes, adjusted))


# --- 진입점 ----------------------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=PROBE_INTERVAL, allowed_apis=MINUTE_REST_APIS)


def _refuse(msg: str) -> int:
    print(f"분봉 수집 거부(연결 없음): {msg}")
    return 2


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog=TOOL, description="키움 모의 ka10080 분봉 축적 (조회만, T-028, A-095 ②)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--codes-file", default=None, help="종목 목록 (code,... CSV 또는 한 줄에 하나, # 주석)")
    p.add_argument("--minutes", type=int, default=1, help=f"봉 길이(분): {', '.join(map(str, MINUTE_SCOPES))}")
    p.add_argument("--since", default=SINCE_DEFAULT, help="첫 날짜 YYYY-MM-DD (제공 범위 약 1년)")
    p.add_argument("--out", default=None, help=f"저장 폴더 (기본 {OUT_DEFAULT})")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def minute_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
                clock: Clock | None = None, connect: Connect | None = None) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회)에서만 실행한다. LIVE·BACKTEST는 연결하지 않는다")
    if ns.minutes not in MINUTE_SCOPES:
        return _refuse(f"--minutes {ns.minutes} — {', '.join(map(str, MINUTE_SCOPES))}만 허용(ka10080 tic_scope)")
    try:
        since = date.fromisoformat(ns.since)
    except ValueError:
        return _refuse(f"--since {ns.since} — YYYY-MM-DD 형식 아님")
    if not ns.codes_file:
        return _refuse("--codes-file이 필요")
    codes_file = _resolve(root, ns.codes_file)
    try:
        codes = read_code_list(codes_file)
    except (OSError, ProbeError) as exc:
        return _refuse(f"--codes-file: {exc}")
    clock = clock or SystemClock()
    now = clock.now()
    calendar = _load_calendar(root)
    if calendar is None:
        return _refuse("KRX 달력(config/calendar) 없음 또는 형식 오류 — 정규장 봉을 가릴 수 없다")
    hours = market_hours_refusal(now, calendar)
    if hours:
        return _refuse(hours)
    running = _running_refusal(root / "var" / "paper", now)
    if running:
        return _refuse(running)
    last_day = last_closed_day(now, calendar)
    if since > last_day:
        return _refuse(f"--since {since}가 마지막 거래일 {last_day}보다 뒤")
    out = _resolve(root, ns.out or OUT_DEFAULT)

    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return _refuse("접속 대상이 키움 모의 도메인이 아님 — 수집하지 않는다")
    print(f"분봉 수집 시작: {len(codes)}종목 ka10080 {ns.minutes}분 수정주가 {since:%Y%m%d}~{last_day:%Y%m%d}"
          f"(마지막 거래일) (TR당 {PROBE_INTERVAL}초 간격) → {out}")
    meta: dict[str, Any] = {
        "tool": TOOL, "basis": BASIS, "started_at": now.astimezone(KST).isoformat(timespec="seconds"),
        "account_alias": adapter.cfg.account_alias, "codes_file": str(codes_file), "codes_requested": len(codes),
        "minutes": ns.minutes, "since": f"{since:%Y%m%d}", "until": f"{last_day:%Y%m%d}",
        "request": {"tic_scope": str(ns.minutes), "upd_stkpc_tp": UPD_STKPC_TP, "base_dt": f"{last_day:%Y%m%d}"},
        "allowed_rest_apis": sorted(MINUTE_REST_APIS), "interval_seconds": PROBE_INTERVAL, "source": SOURCE,
        "rules": {"version": MINUTE_RULES_VERSION, "regular_session": REGULAR_RULE,
                  "confirm_margin_seconds": int(CONFIRM_MARGIN.total_seconds()),
                  "quality": "중복 시각·역순·음수 거래량·0 이하 가격·고저 모순 → 그 날 무효(저장 안 함), 빈 분 = 거래 없음"},
        "columns": ["datetime(봉 시작 KST)", "open", "high", "low", "close", "volume"],
        "token": {"issued": False, "revoked": None}, "ok": 0, "per_code": {}, "failed": [], "not_queried": [],
    }
    out.mkdir(parents=True, exist_ok=True)
    try:
        try:
            adapter.tokens.token()
            meta["token"]["issued"] = True
        except TokenError as exc:
            meta["failed"].append({"code": "-", "reason": f"토큰 발급 실패: {_mask(exc)}"})
            meta["not_queried"] = list(codes)
        streak = 0
        for idx, code in enumerate(codes if meta["token"]["issued"] else []):
            if guard.blocked or fatal_critical(adapter):
                meta["not_queried"] = codes[idx:]
                print("중단: 허용 밖 API 시도 또는 브로커 치명 오류 — 나머지 조회 안 함")
                break
            path = out / minute_csv_name(code, ns.minutes)
            head = f"  {idx + 1:>3}/{len(codes)} {code}"
            try:
                existing = read_minute_csv(path, code, ns.minutes) if path.exists() else []
            except (MinuteDataMissing, OSError) as exc:
                meta["failed"].append({"code": code, "reason": f"기존 CSV를 읽을 수 없음(그대로 둠): {exc}"})
                print(f"{head}: 실패 — 기존 CSV를 읽을 수 없음(그대로 둠)", flush=True)
                continue
            floor = _floor(existing, since)
            res, rows = fetch_minute_rows(adapter, guard, code, ns.minutes, last_day, floor)
            reason = res["error"]
            if reason is None:
                rec, reason = process_code(rows, res, code, ns.minutes, calendar, now, path)
            if reason is None:
                rec["floor"] = floor.isoformat()
                meta["per_code"][code] = rec
                meta["ok"] += 1
                streak = 0
                notes = [f"{k} {v}" for k, v in rec["excluded"].items()]
                notes += [f"부분일 제외 {d}" for d in rec["partial_days_dropped"]]
                notes += [f"무효인 날 {d['day']}" for d in rec["invalid_days"]]
                print(f"{head}: {rec['first']}~{rec['last']} 추가 {rec['added']}봉 건너뜀 {rec['skipped_existing']}"
                      f"{' (' + '·'.join(notes) + ')' if notes else ''}", flush=True)
                continue
            meta["failed"].append({"code": code, "reason": reason, "pages": res["pages"]})
            print(f"{head}: 실패 — {reason}", flush=True)
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES or UNSUPPORTED_MARK in reason:
                meta["not_queried"] = codes[idx + 1:]
                print(f"중단: 연속 {streak}종목 실패 — 나머지 {len(meta['not_queried'])}종목 조회 안 함")
                break
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        if meta["token"]["issued"]:
            meta["token"]["revoked"] = revoked
            print(f"토큰 폐기 au10002: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    meta["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
    meta["api_calls"] = _api_summary(guard)
    meta["calls"] = sum(s["calls"] for s in meta["api_calls"].values())
    meta["blocked_before_send"] = list(guard.blocked)
    meta["critical"] = [f"{k}: {_mask(m)}" for k, m in adapter.critical]
    (out / "meta").mkdir(parents=True, exist_ok=True)
    mpath = out / "meta" / f"minute-fetch-{now.astimezone(KST):%Y%m%d-%H%M%S}.json"
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"요약: 종목 {len(codes)} 성공 {meta['ok']} 실패 {len(meta['failed'])} 미조회 {len(meta['not_queried'])}, "
          f"호출 {meta['calls']}회, 저장 {out}, 메타 {mpath.name}")
    bad = meta["failed"] or meta["not_queried"] or meta["blocked_before_send"] or meta["critical"]
    return 1 if bad else 0
