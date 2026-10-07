"""백테스트용 일봉 수집 도구 — 키움 모의 조회만 (T-018, 승인 A-073 E·F).

사람이 직접 실행한다. 오늘 PAPER run을 끈 뒤에만 실행한다(토큰 발급·폐기가 실행 중인 엔진의 토큰을 무효화할 수 있음).

순서
1. 연결 전 검사: `--mode PAPER`, 실행 잠금·하트비트(30초 이내면 거부, preflight와 같은 검사), 연결 조건(connect_paper:
   TRADE_MODE=PAPER, .env.paper, connection_approved = true). 하나라도 어긋나면 네트워크 객체를 만들지 않는다.
2. 종목 선정(--codes가 없을 때): ka10099 코스피(mrkt_tp 0) 목록에서 ETF·ETN·ELW·우선주·투자유의·거래정지 제외
   → 남은 종목마다 ka10001 `mac`(시가총액)으로 순위 → 상위 N.
3. 종목별 ka10081 수정주가(upd_stkpc_tp=1) 일봉을 since부터 → `<out>/<코드>.csv` (date,open,high,low,close,volume).

필드 확인 상태 (docs/broker-specs/kiwoom-kr.md)
- ka10099 code·name·state·auditInfo·orderWarning·marketName: 실측 확인(§7b, 2026-10-03).
- ka10099의 상장주식수·가격 필드: 확인 기록 없음 → 쓰지 않는다.
- ka10001 `mac`(시가총액)·`flo_stk`(상장주식)·`cur_prc`(현재가): `cur_prc`만 사용 중(시세). `mac`·`flo_stk`는
  **미확인 — 첫 실행 결과로 확인**. 단위도 미확인이지만 같은 필드끼리 비교하는 순위에는 영향이 없다.
  `mac`이 없거나 해석 불가면 추측하지 않고 중단한다. `flo_stk × cur_prc`는 교차 확인용으로만 출력한다.
- 우선주 판정의 "코드 끝자리 0이 아니면 우선주"는 KRX 관례에 기댄 가정이다(보수적: 잘못 빼면 후보에서 빠질 뿐).

부수 파일은 `<out>/meta/`에 둔다. 백테스트(--data)가 `<out>/*.csv`를 모두 일봉으로 읽기 때문이다.
주문 API는 GuardedHttp가 전송 전에 막고, 이 모듈은 조회(query_raw)와 토큰 폐기(close)만 부른다.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom import parse
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.domain.market import TradingStatus
from trading_system.marketdata.kiwoom_feed import CHART_PATH, INFO_PATH, master_statuses
from trading_system.marketdata.store import latest_complete_day

from .instance_lock import instance_running
from .preflight import ORDER_API_IDS, connect_paper, engine_alive

KOSPI, ETF_MARKET, ETN_MARKET = "0", "8", "60"     # ka10099 mrkt_tp (kiwoom_feed.MASTER_MARKETS와 같은 값)
TOP_MIN, TOP_MAX, TOP_DEFAULT = 20, 50, 30
SINCE_DEFAULT = "2016-01-01"
CAP_FIELD = "mac"                                   # ka10001 시가총액 — 미확인, 첫 실행 결과로 확인
CAP_BASIS = "ka10001 mac (미확인 필드 — 첫 실행 결과로 확인)"
CROSS_FIELDS = ("flo_stk", "cur_prc")               # 교차 확인용 상장주식 × 현재가 — 미확인
DAILY_LIST_KEY = "stk_dt_pole_chart_qry"
MAX_CONSECUTIVE_FAILURES = 3                        # 연속 실패가 이만큼이면 나머지는 조회하지 않는다
LATE_START_DAYS = 14                                # 첫 봉이 since보다 이만큼 넘게 늦으면 표시
CSV_HEADER = ("date", "open", "high", "low", "close", "volume")

_CODE = re.compile(r"[0-9A-Z]{6}", re.ASCII)
_PREFERRED_NAME = re.compile(r"(우[A-Z]?|\(전환\))$")
_PRODUCT_WORDS = ("ETF", "ETN", "ELW")


class FetchError(RuntimeError):
    pass


# --- 종목 선정 (순수 함수) ---------------------------------------------------------------------------

def is_preferred(code: str, name: str) -> bool:
    """우선주: 이름 끝 '우'·'우B'·'(전환)' 또는 코드 끝자리가 0이 아님(KRX 관례, 가정)."""
    return bool(_PREFERRED_NAME.search(name.strip())) or not code.endswith("0")


def exclusion_reason(row: Mapping[str, object], product_codes: set[str]) -> str | None:
    """ka10099 행 → 제외 사유. None이면 후보."""
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip()
    if not _CODE.fullmatch(code):
        return "코드 형식 아님"
    labels = " ".join(str(row.get(k) or "") for k in ("marketName", "kind", "upName")).upper()
    if code in product_codes:
        return "ETF·ETN 목록(ka10099 mrkt_tp 8·60)에 있음"
    for w in _PRODUCT_WORDS:
        if w in labels:
            return f"{w} 상품 표시"
    if is_preferred(code, name):
        return "우선주(이름 또는 코드 끝자리)"
    statuses = master_statuses(dict(row))
    if TradingStatus.HALTED in statuses:
        return "거래정지(state·auditInfo)"
    if statuses:
        return f"투자유의·관리({', '.join(sorted(s.value for s in statuses))})"
    warn = str(row.get("orderWarning") or "0").strip()
    if warn not in ("", "0"):
        return f"투자유의(orderWarning={warn})"
    return None


@dataclass(frozen=True)
class Ranked:
    rank: int
    code: str
    name: str
    market_cap: Decimal
    cross: Decimal | None          # flo_stk × cur_prc (교차 확인용, 없으면 None)


def _list(adapter, market: str) -> list[dict]:  # type: ignore[no-untyped-def]
    with call_priority(Priority.MARKET):
        _, rows = adapter.query_raw(INFO_PATH, "ka10099", {"mrkt_tp": market}, "list")
    return rows


def _market_cap(adapter, code: str) -> tuple[Decimal, Decimal | None]:  # type: ignore[no-untyped-def]
    with call_priority(Priority.MARKET):
        body, _ = adapter.query_raw(INFO_PATH, "ka10001", {"stk_cd": code}, None)
    if CAP_FIELD not in body:
        raise FetchError(f"{code}: ka10001 응답에 시가총액 필드 '{CAP_FIELD}' 없음 — 추측하지 않고 중단 "
                         f"(kiwoom-kr.md 미확인 필드)")
    try:
        cap = abs(parse.dec(body.get(CAP_FIELD), CAP_FIELD))
    except parse.KiwoomParseError as exc:
        raise FetchError(f"{code}: ka10001 '{CAP_FIELD}' 해석 불가 ({exc}) — 중단") from None
    if cap <= 0:
        raise FetchError(f"{code}: ka10001 '{CAP_FIELD}'가 0 이하 — 중단")
    cross: Decimal | None = None
    try:
        cross = abs(parse.dec(body.get(CROSS_FIELDS[0]), CROSS_FIELDS[0])) * \
            parse.price(body.get(CROSS_FIELDS[1]), CROSS_FIELDS[1])
    except parse.KiwoomParseError:
        cross = None
    return cap, cross


def select_universe(adapter, top: int, log: Callable[[str], None]) -> tuple[list[Ranked], list[tuple[str, str, str]]]:  # type: ignore[no-untyped-def]
    """(상위 N, 제외 목록[(코드, 이름, 사유)]). ka10001 한 종목이라도 실패하면 순위가 불완전하므로 FetchError."""
    kospi = _list(adapter, KOSPI)
    products = {str(r.get("code") or "").strip() for m in (ETF_MARKET, ETN_MARKET) for r in _list(adapter, m)}
    excluded: list[tuple[str, str, str]] = []
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in kospi:
        code, name = str(r.get("code") or "").strip(), str(r.get("name") or "").strip()
        reason = exclusion_reason(r, products)
        if reason is None and code in seen:
            reason = "목록 중복"
        if reason is not None:
            excluded.append((code, name, reason))
            continue
        seen.add(code)
        candidates.append((code, name))
    log(f"ka10099 코스피 {len(kospi)}종목 중 제외 {len(excluded)}, 시가총액 조회 대상 {len(candidates)} "
        f"(예상 소요 약 {len(candidates) * 1.1 / 60:.0f}분 이상, 추정)")
    caps: list[tuple[str, str, Decimal, Decimal | None]] = []
    failed: list[str] = []
    streak = 0
    for i, (code, name) in enumerate(candidates, start=1):
        try:
            cap, cross = _market_cap(adapter, code)
        except BrokerQueryError as exc:
            failed.append(f"{code}({exc})")
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                break   # 연속 실패면 더 부르지 않는다 (어차피 순위 불완전 → 중단)
            continue
        streak = 0
        caps.append((code, name, cap, cross))
        if i % 100 == 0:
            log(f"  시가총액 조회 {i}/{len(candidates)}")
    if failed:
        raise FetchError(f"ka10001 조회 실패 {len(failed)}건 — 순위가 불완전해 중단: {', '.join(failed[:5])}")
    caps.sort(key=lambda t: (-t[2], t[0]))
    ranked = [Ranked(i, c, n, cap, cross) for i, (c, n, cap, cross) in enumerate(caps[:top], start=1)]
    return ranked, excluded


def cross_check(ranked: list[Ranked]) -> str:
    """교차 확인: 상위 N을 flo_stk × cur_prc 순위로 다시 세웠을 때 순서가 같은지(같은 N 안에서만)."""
    if not ranked or any(r.cross is None for r in ranked):
        return f"교차 확인 불가 — ka10001 {'·'.join(CROSS_FIELDS)} 없음 또는 해석 불가"
    by_cross = [r.code for r in sorted(ranked, key=lambda r: (-(r.cross or 0), r.code))]
    same = sum(1 for a, b in zip(by_cross, [r.code for r in ranked]) if a == b)
    return f"교차 확인({'×'.join(CROSS_FIELDS)}): 상위 {len(ranked)} 중 순위 일치 {same}"


# --- 일봉 ----------------------------------------------------------------------------------------

Bar = tuple[date, Decimal, Decimal, Decimal, Decimal, Decimal]


def _dt(row: Mapping[str, object]) -> date:
    s = str(row.get("dt") or "")
    if len(s) != 8 or not (s.isascii() and s.isdigit()):
        raise parse.KiwoomParseError("dt 형식 아님")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def fetch_daily(adapter, code: str, since: date, until: date) -> list[Bar]:  # type: ignore[no-untyped-def]
    """since~until 수정주가 일봉(오래된 순). 상장이 since 뒤면 상장일부터. 중복·해석 불가·빈 결과는 실패."""
    floor = since.strftime("%Y%m%d")

    def enough(rows: list[dict]) -> bool:
        return any(str(r.get("dt") or "") < floor for r in rows)

    with call_priority(Priority.MARKET):
        _, rows = adapter.query_raw(CHART_PATH, "ka10081", {
            "stk_cd": code, "base_dt": until.strftime("%Y%m%d"), "upd_stkpc_tp": "1"}, DAILY_LIST_KEY, enough)
    out: dict[date, Bar] = {}
    for r in rows:
        d = _dt(r)
        if d < since or d > until:
            continue
        if d in out:
            raise parse.KiwoomParseError(f"일봉 날짜 중복 {d}")
        out[d] = (d, parse.price(r.get("open_pric"), "open_pric"), parse.price(r.get("high_pric"), "high_pric"),
                  parse.price(r.get("low_pric"), "low_pric"), parse.price(r.get("cur_prc"), "cur_prc"),
                  parse.qty(r.get("trde_qty"), "trde_qty"))
    if not out:
        raise FetchError(f"{code}: {since}~{until} 일봉 없음")
    return [out[d] for d in sorted(out)]


def write_daily_csv(path: Path, bars: Sequence[Bar]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for d, o, h, lo, c, v in bars:
            w.writerow([d.isoformat(), o, h, lo, c, v])
    os.replace(tmp, path)


# --- 진입점 ----------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=1.1)


def _refuse(msg: str) -> int:
    print(f"일봉 수집 거부(연결 없음): {msg}")
    return 2


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog="scripts/fetch-daily.py",
                                description="키움 모의 ka10081 수정주가 일봉 수집 (조회만, T-018)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--top", type=int, default=TOP_DEFAULT, help=f"시가총액 상위 N ({TOP_MIN}~{TOP_MAX})")
    p.add_argument("--since", default=SINCE_DEFAULT, help="첫 날짜 YYYY-MM-DD")
    p.add_argument("--out", default=None, help="저장 폴더 (기본 var/backtest/data/kiwoom-ka10081-adj-<마지막 확정일>)")
    p.add_argument("--codes", default=None, help="종목코드 목록 파일(한 줄에 하나, # 주석). 주면 순위 계산 생략")
    p.add_argument("--dry-run", action="store_true", help="종목 선정까지만 (일봉 조회 안 함)")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def read_codes(path: Path) -> list[str]:
    """한 줄에 종목코드 하나(# 주석, 쉼표 뒤는 무시). 이 도구가 쓴 universe.csv(rank,code,...)도 그대로 받는다."""
    codes: list[str] = []
    lines = path.read_text(encoding="utf-8-sig").splitlines()   # PowerShell 5.1 Set-Content -Encoding utf8은 BOM을 붙임
    column = 1 if lines and lines[0].strip().startswith("rank,code") else 0
    if column:
        lines = lines[1:]
    for i, line in enumerate(lines, start=1 + column):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        parts = text.split(",")
        code = parts[column].strip() if len(parts) > column else ""
        if not _CODE.fullmatch(code):
            raise FetchError(f"{path.name}:{i}: 종목코드 형식 아님")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise FetchError(f"{path.name}: 종목코드 없음")
    return codes


def _write_universe(meta: Path, ranked: list[Ranked], excluded: list[tuple[str, str, str]]) -> Path:
    """순위 선정 결과. --codes 실행은 이 파일을 덮어쓰지 않는다(순위 근거 보존)."""
    meta.mkdir(parents=True, exist_ok=True)
    path = meta / "universe.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["rank", "code", "name", "market_cap", "basis", "shares_x_price"])
        for r in ranked:
            w.writerow([r.rank, r.code, r.name, r.market_cap, CAP_BASIS, "" if r.cross is None else r.cross])
    with (meta / "excluded.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["code", "name", "reason"])
        w.writerows(excluded)
    return path


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def _fetch_all(adapter, guard, ranked: list[Ranked], since: date, until: date, out: Path,  # type: ignore[no-untyped-def]
               summary: dict) -> None:
    """종목별 일봉 조회 → CSV. 실패 종목은 건너뛰고 기록. 연속 실패·주문 API 시도·브로커 치명 오류면 멈춘다."""
    out.mkdir(parents=True, exist_ok=True)
    streak = 0
    per_code: dict[str, dict] = {}
    for idx, r in enumerate(ranked):
        if guard.blocked or getattr(adapter, "critical", None):
            summary["not_queried"] = [x.code for x in ranked[idx:]]
            print("중단: 주문 API 시도 또는 브로커 치명 오류 — 나머지 종목 조회 안 함")
            break
        try:
            bars = fetch_daily(adapter, r.code, since, until)
        except (BrokerQueryError, parse.KiwoomParseError, FetchError) as exc:
            summary["failed"].append({"code": r.code, "reason": str(exc)})
            print(f"  실패 {r.code}: {exc}")
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                summary["not_queried"] = [x.code for x in ranked[idx + 1:]]
                print(f"중단: 연속 {streak}종목 실패 — 나머지 {len(summary['not_queried'])}종목 조회 안 함")
                break
            continue
        streak = 0
        write_daily_csv(out / f"{r.code}.csv", bars)
        per_code[r.code] = {"first": bars[0][0].isoformat(), "last": bars[-1][0].isoformat(), "rows": len(bars)}
        note = "" if bars[0][0] <= since + timedelta(days=LATE_START_DAYS) else \
            " (첫 봉이 since보다 늦음 — 상장일 또는 제공 범위 한계, 미확인)"
        print(f"  {r.code}: {bars[0][0]} ~ {bars[-1][0]} {len(bars)}행{note}")
    summary["ok"] = len(per_code)
    summary["per_code"] = per_code


def fetch_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
               clock: Clock | None = None, connect: Connect | None = None) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회)에서만 실행한다")
    if not TOP_MIN <= ns.top <= TOP_MAX:
        return _refuse(f"--top {ns.top} — {TOP_MIN}~{TOP_MAX}만 허용")
    try:
        since = date.fromisoformat(ns.since)
    except ValueError:
        return _refuse(f"--since {ns.since} — YYYY-MM-DD 형식 아님")
    clock = clock or SystemClock()
    base = root / "var" / "paper"
    if instance_running(base):   # 시작 직후(하트비트 전)도 막는다 (preflight와 같음, RV-15)
        return _refuse("PAPER 엔진이 실행 중(실행 잠금 보유) — run을 끈 뒤 다시 실행")
    alive = engine_alive(base / "heartbeat", clock.now())
    if alive:
        return _refuse(alive)
    now = clock.now()
    until = latest_complete_day(now, None)   # 일봉 확정 20:00 전이면 전 거래일까지 (DP-5)
    if since > until:
        return _refuse(f"--since {since}가 마지막 확정 거래일 {until}보다 뒤")
    out = _resolve(root, ns.out) if ns.out else \
        root / "var" / "backtest" / "data" / f"kiwoom-ka10081-adj-{until.strftime('%Y%m%d')}"
    codes: list[str] | None = None
    if ns.codes:
        try:
            codes = read_codes(_resolve(root, ns.codes))
        except (OSError, FetchError) as exc:
            return _refuse(f"--codes 파일: {exc}")
    # 기존 일봉은 덮어쓰지 않는다. --codes면 그 종목 파일만 없으면 된다(실패 종목 재수집용)
    existing = sorted(p.stem for p in out.glob("*.csv")) if out.exists() else []
    clash = existing if codes is None else [c for c in codes if c in existing]
    if clash:
        return _refuse(f"{out}에 이미 일봉 CSV가 있음({', '.join(clash[:5])}) — 덮어쓰지 않는다. "
                       f"다른 --out을 지정하거나, 실패 종목만 --codes로 다시 받는다")
    if codes is not None:
        if ns.dry_run:
            print(f"--codes {len(codes)}종목 (순위 계산 생략, --dry-run이라 연결하지 않음): {', '.join(codes)}")
            return 0

    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    meta = out / "meta"
    summary: dict = {"started_at": now.astimezone(KST).isoformat(), "since": since.isoformat(),
                     "until": until.isoformat(), "api": "ka10081 upd_stkpc_tp=1 (수정주가)", "codes": [],
                     "ok": 0, "failed": [], "not_queried": [], "order_api_calls": 0, "dry_run": ns.dry_run}
    rc = 0
    try:
        if codes is None:
            print(f"종목 선정: ka10099 코스피 + ka10001 시가총액 필드 '{CAP_FIELD}' 사용 ({CAP_BASIS})")
            try:
                ranked, excluded = select_universe(adapter, ns.top, print)
            except (BrokerQueryError, parse.KiwoomParseError, FetchError) as exc:
                print(f"종목 선정 실패(일봉 조회 안 함): {exc}")
                return 1
            if len(ranked) < ns.top:
                print(f"경고: 후보가 {len(ranked)}종목뿐 — 요청 {ns.top}")
            print(cross_check(ranked))
            where = _write_universe(meta, ranked, excluded)
            print(f"선정 {len(ranked)}종목 (근거: {CAP_BASIS}) → {where}, 제외 목록 {meta / 'excluded.csv'}")
        else:
            ranked = [Ranked(i, c, "", Decimal("0"), None) for i, c in enumerate(codes, start=1)]
            print(f"--codes 지정 {len(ranked)}종목 (순위 계산 생략, meta/universe.csv는 그대로 둠)")
        for r in ranked:
            cap = "" if codes is not None else f"  시가총액 {r.market_cap}"
            print(f"  {r.rank:>2}. {r.code} {r.name}{cap}")
        summary["codes"] = [r.code for r in ranked]
        if ns.dry_run:
            print("--dry-run: 일봉은 조회하지 않음")
        else:
            _fetch_all(adapter, guard, ranked, since, until, out, summary)
            if summary["failed"] or summary["not_queried"]:
                rc = 1
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002 (calendar_tool과 같음)
        print(f"토큰 폐기: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    blocked = list(guard.blocked) + [rec.api_id for rec in guard.records if rec.api_id in ORDER_API_IDS]
    summary["order_api_calls"] = len(blocked)
    if blocked:
        print(f"수집 중 주문 API 호출 시도 {len(blocked)}회(전송 전 차단) — 결과를 쓰지 말 것")
        rc = 1
    meta.mkdir(parents=True, exist_ok=True)
    summary_path = meta / f"fetch-summary-{now.astimezone(KST).strftime('%Y%m%d-%H%M%S')}.json"  # 실행마다 따로
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"요약: 성공 {summary['ok']}, 실패 {len(summary['failed'])}, 미조회 {len(summary['not_queried'])}, "
          f"기간 {since} ~ {until}, 저장 {out}, 요약 파일 {summary_path.name}")
    return rc
