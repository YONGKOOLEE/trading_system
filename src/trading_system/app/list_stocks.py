"""전 종목 목록 도구 — 키움 모의 ka10099 조회만 (A-108). 사람이 직접 실행한다.

출력: `--out <file.csv>`(열 code,name,market,listed_date,kind,kind_basis,managed,state,audit_info,order_warning)와
`<file.csv>.codes-stocks.txt`(코스피·코스닥 중 ETF·ETN이 아닌 종목 코드, fetch-daily `--codes`용). 기존 파일은 덮어쓰지 않는다.

- market: KOSPI(mrkt_tp 0)·KOSDAQ(10)·ETF(8)·ETN(60). ETF·ETN 목록에 있는 코드는 주식 목록에 섞여 있어도 ETF·ETN으로 둔다.
- listed_date: ka10099 `regDay`(등록일, 공식 명세의 이름 — 이 저장소의 실측 기록에는 없음, 미확인). 없거나 해석 불가면 빈칸.
- kind: 시장코드(ETF·ETN) 외에는 모두 추정이며 kind_basis에 근거를 적는다. 판정 순서: 스팩 → 리츠 → 우선주 → 보통주.
- managed: state·auditInfo에 '관리'가 있으면 '관리종목', 아니면 빈칸.
연결 조건·거부는 name_resolve와 같다. 허용 API는 au10001·au10002·ka10099뿐, 끝나면 토큰 폐기.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom import parse
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import Clock, SystemClock
from trading_system.marketdata.kiwoom_feed import INFO_PATH

from .daily_fetch import _CODE, _PREFERRED_NAME
from .instance_lock import instance_running
from .preflight import ORDER_API_IDS, connect_paper, engine_alive

LIST_APIS = frozenset({"au10001", "au10002", "ka10099"})
MARKETS = (("0", "KOSPI"), ("10", "KOSDAQ"), ("8", "ETF"), ("60", "ETN"))
PRODUCT_LABELS = ("ETF", "ETN")
HEADER = ("code", "name", "market", "listed_date", "kind", "kind_basis", "managed", "state", "audit_info", "order_warning")
LISTED_FIELD = "regDay"
_DATE8 = re.compile(r"[0-9]{8}", re.ASCII)


class ListError(RuntimeError):
    pass


def listed_date(row: Mapping[str, object]) -> str:
    """regDay(YYYYMMDD) → YYYY-MM-DD. 없거나 형식이 다르면 빈칸(추측하지 않음)."""
    s = str(row.get(LISTED_FIELD) or "").strip()
    if not _DATE8.fullmatch(s) or s == "00000000":
        return ""
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def classify(code: str, name: str, market: str, row: Mapping[str, object]) -> tuple[str, str]:
    """(kind, kind_basis). ETF·ETN만 시장코드로 확정, 나머지는 추정."""
    if market in PRODUCT_LABELS:
        return market, "시장코드"
    name = name.strip()
    labels = " ".join(str(row.get(k) or "") for k in ("marketName", "kind", "upName")).upper()
    if "스팩" in name or "SPAC" in name.upper():
        return "스팩", "이름 '스팩'(추정)"
    if "리츠" in name:
        return "리츠", "이름 '리츠'(추정)"
    if "리츠" in labels or "부동산투자" in labels:
        return "리츠", "업종 '리츠'(추정)"
    if _PREFERRED_NAME.search(name):
        return "우선주", "이름 끝 '우'(우선주 추정)"
    if _CODE.fullmatch(code) and not code.endswith("0"):
        return "우선주", "코드 끝자리(우선주 추정)"
    if _CODE.fullmatch(code):
        return "보통주", "코드 끝자리 0·이름 우선주 아님(추정)"
    return "기타", "코드 형식 아님"


def managed_flag(row: Mapping[str, object]) -> str:
    text = f"{row.get('state') or ''}|{row.get('auditInfo') or ''}"
    return "관리종목" if "관리" in text else ""


def build_rows(by_market: Mapping[str, Sequence[Mapping[str, object]]]) -> list[dict[str, str]]:
    """시장별 ka10099 행 → CSV 행. 같은 코드는 ETF·ETN 우선으로 한 번만."""
    out: dict[str, dict[str, str]] = {}
    for label in ("ETF", "ETN", "KOSPI", "KOSDAQ"):
        for r in by_market.get(label, []):
            code, name = str(r.get("code") or "").strip(), str(r.get("name") or "").strip()
            if not code or code in out:
                continue
            kind, basis = classify(code, name, label, r)
            out[code] = {"code": code, "name": name, "market": label, "listed_date": listed_date(r), "kind": kind,
                         "kind_basis": basis, "managed": managed_flag(r), "state": str(r.get("state") or "").strip(),
                         "audit_info": str(r.get("auditInfo") or "").strip(),
                         "order_warning": str(r.get("orderWarning") or "").strip()}
    return list(out.values())


def stock_codes(rows: Sequence[Mapping[str, str]]) -> list[str]:
    return [r["code"] for r in rows if r["market"] in ("KOSPI", "KOSDAQ") and _CODE.fullmatch(r["code"])]


def load_markets(adapter) -> dict[str, list[dict]]:  # type: ignore[no-untyped-def]
    """ka10099 4개 시장. 하나라도 실패하거나 코스피·코스닥이 비면 오류(부분 목록을 쓰지 않음)."""
    got: dict[str, list[dict]] = {}
    for mk, label in MARKETS:
        with call_priority(Priority.MARKET):
            _, rows = adapter.query_raw(INFO_PATH, "ka10099", {"mrkt_tp": mk}, "list")
        if not rows and label in ("KOSPI", "KOSDAQ"):
            raise ListError(f"ka10099 {label} 목록이 비어 있음 — 중단")
        got[label] = rows
    return got


def _write(out: Path, codes_path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    codes_path.write_text("".join(c + "\n" for c in stock_codes(rows)), encoding="utf-8", newline="")


Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=1.1, allowed_apis=LIST_APIS)


def _refuse(msg: str) -> int:
    print(f"종목 목록 거부(연결 없음): {msg}")
    return 2


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog="scripts/list-stocks.py",
                                description="전 종목 목록 CSV (키움 모의 ka10099 조회만, A-108)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--out", required=True, help="결과 CSV 파일 (옆에 <파일>.codes-stocks.txt도 씀)")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def list_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
              clock: Clock | None = None, connect: Connect | None = None) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회)에서만 실행한다")
    p = Path(ns.out)
    out = p if p.is_absolute() else root / p
    codes_path = out.with_name(out.name + ".codes-stocks.txt")
    clash = [x.name for x in (out, codes_path) if x.exists()]
    if clash:
        return _refuse(f"{out.parent}에 이미 {', '.join(clash)}가 있음 — 덮어쓰지 않는다. 다른 --out을 지정")
    clock = clock or SystemClock()
    base = root / "var" / "paper"
    if instance_running(base):
        return _refuse("PAPER 엔진이 실행 중(실행 잠금 보유) — run을 끈 뒤 다시 실행")
    alive = engine_alive(base / "heartbeat", clock.now())
    if alive:
        return _refuse(alive)
    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    rc = 0
    rows: list[dict[str, str]] = []
    try:
        try:
            rows = build_rows(load_markets(adapter))
        except (BrokerQueryError, parse.KiwoomParseError, ListError) as exc:
            print(f"ka10099 조회 실패(결과를 쓰지 않음): {exc}")
            return 1
        if guard.blocked or getattr(adapter, "critical", None):
            print("중단: 주문 API 시도 또는 브로커 치명 오류 — 결과를 쓰지 않음")
            rc = 1
        else:
            _write(out, codes_path, rows)
    finally:
        revoked = adapter.close()
        print(f"토큰 폐기: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    blocked = list(guard.blocked) + [rec.api_id for rec in guard.records if rec.api_id in ORDER_API_IDS]
    if blocked:
        print(f"주문 API 호출 시도 {len(blocked)}회(전송 전 차단) — 결과를 쓰지 말 것")
        rc = 1
    counts = {m: sum(1 for r in rows if r["market"] == m) for _, m in MARKETS}
    no_date = sum(1 for r in rows if not r["listed_date"])
    note = f" ({LISTED_FIELD} 필드 없음 또는 해석 불가)" if rows and no_date == len(rows) else ""
    print(f"요약: {counts}, 주식 코드 {len(stock_codes(rows))}개, listed_date 빈칸 {no_date}/{len(rows)}{note}, 저장 {out}")
    return rc
