"""종목명 → 종목코드 변환 도구 — 키움 모의 ka10099 조회만 (A-107). 사람이 직접 실행한다.

입력: 종목명 파일(한 줄에 하나, # 주석). 출력(`--out` 폴더): `codes.txt`(fetch-daily `--codes`용), `name-map.csv`
(name,code,market,match), `unmatched.csv`(name,reason). 기존 파일은 덮어쓰지 않는다.

매칭 규칙 (추측 없음)
1. 앞뒤 공백만 제거한 이름이 ka10099 `name`과 정확히 같으면 match=exact.
2. 정확 일치가 하나도 없을 때만 정규화(NFKC 전각→반각, 모든 공백 제거, 라틴 대소문자 무시) 비교 → match=normalized.
3. 같은 이름에 항목이 여럿이면 보통주(ETF·ETN·ELW·우선주가 아닌 것)가 정확히 하나일 때만 그것을 고른다.
   보통주가 없거나 둘 이상이면 고르지 않고 unmatched에 후보 코드와 함께 적는다. 퍼지(유사) 매칭은 하지 않는다.
대상 시장: 코스피(mrkt_tp 0)·코스닥(10). 상품 구분용으로 ETF(8)·ETN(60) 목록도 조회한다(후보 표시에만 사용).

연결 조건·거부는 daily_fetch와 같다: `--mode PAPER`, 실행 잠금(run.lock)·하트비트(30초 이내) 거부, connect_paper 조건,
허용 API는 au10001·au10002·ka10099뿐(나머지는 GuardedHttp가 전송 전에 막음), 끝나면 토큰 폐기(au10002).
"""

from __future__ import annotations

import argparse
import csv
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom import parse
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import Clock, SystemClock
from trading_system.marketdata.kiwoom_feed import INFO_PATH

from .daily_fetch import _CODE, _PRODUCT_WORDS, is_preferred
from .instance_lock import instance_running
from .preflight import ORDER_API_IDS, connect_paper, engine_alive

RESOLVE_APIS = frozenset({"au10001", "au10002", "ka10099"})
STOCK_MARKETS = (("0", "KOSPI"), ("10", "KOSDAQ"))
PRODUCT_MARKETS = (("8", "ETF"), ("60", "ETN"))
OUT_FILES = ("codes.txt", "name-map.csv", "unmatched.csv")
NAME_MAP_HEADER = ("name", "code", "market", "match")
UNMATCHED_HEADER = ("name", "reason")


class ResolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    code: str
    name: str
    market: str          # KOSPI / KOSDAQ / ETF / ETN
    common: bool         # 보통주로 볼 수 있는가 (상품·우선주·형식 이상이 아님)


@dataclass(frozen=True)
class Resolution:
    name: str
    code: str | None
    market: str
    match: str           # exact / normalized
    reason: str          # 미매칭 사유 (매칭이면 "")


# --- 순수 함수 ---------------------------------------------------------------------------------------

def normalize(name: str) -> str:
    """NFKC(전각→반각 등) → 모든 공백 제거 → casefold(라틴 대소문자 무시)."""
    return "".join(unicodedata.normalize("NFKC", name).split()).casefold()


def read_names(path: Path) -> list[str]:
    """한 줄에 이름 하나(# 주석, 빈 줄 무시). 앞뒤 공백 제거, 같은 이름은 한 번만(입력 순서 유지)."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.split("#", 1)[0].strip()
        if text and text not in names:
            names.append(text)
    if not names:
        raise ResolveError(f"{path.name}: 종목명 없음")
    return names


def make_entry(row: Mapping[str, object], market: str, product: bool) -> Entry | None:
    code, name = str(row.get("code") or "").strip(), str(row.get("name") or "").strip()
    if not code or not name:
        return None
    labels = " ".join(str(row.get(k) or "") for k in ("marketName", "kind", "upName")).upper()
    common = (not product and _CODE.fullmatch(code) is not None and not is_preferred(code, name)
              and not any(w in labels for w in _PRODUCT_WORDS))
    return Entry(code, name, market, common)


def _decide(name: str, hits: list[Entry], match: str) -> Resolution:
    commons = {e.code: e for e in hits if e.common}
    if len(commons) == 1:
        e = next(iter(commons.values()))
        return Resolution(name, e.code, e.market, match, "")
    cands = ", ".join(f"{e.code}({e.market}{'' if e.common else ' 비보통주'})" for e in hits)
    if len(commons) > 1:
        return Resolution(name, None, "", "", f"동명 여러 개 — 후보 {cands}")
    return Resolution(name, None, "", "", f"보통주 아님(우선주·ETF·ETN 등) — 후보 {cands}")


def resolve_one(name: str, entries: Sequence[Entry]) -> Resolution:
    exact = [e for e in entries if e.name == name]
    if exact:
        return _decide(name, exact, "exact")
    key = normalize(name)
    norm = [e for e in entries if normalize(e.name) == key] if key else []
    if norm:
        return _decide(name, norm, "normalized")
    return Resolution(name, None, "", "", "없음 — 상장폐지·이름 변경 가능")


def resolve_all(names: Sequence[str], entries: Sequence[Entry]) -> list[Resolution]:
    return [resolve_one(n, entries) for n in names]


# --- 조회·출력 ---------------------------------------------------------------------------------------

def load_entries(adapter) -> list[Entry]:  # type: ignore[no-untyped-def]
    """ka10099 4개 시장 목록 → 항목. 하나라도 실패하거나 코스피·코스닥이 비면 오류(부분 목록으로 판정하지 않음)."""
    entries: list[Entry] = []
    for mk, label in (*STOCK_MARKETS, *PRODUCT_MARKETS):
        with call_priority(Priority.MARKET):
            _, rows = adapter.query_raw(INFO_PATH, "ka10099", {"mrkt_tp": mk}, "list")
        got = [e for r in rows if (e := make_entry(r, label, (mk, label) in PRODUCT_MARKETS)) is not None]
        if not got and (mk, label) in STOCK_MARKETS:
            raise ResolveError(f"ka10099 {label} 목록이 비어 있음 — 판정하지 않고 중단")
        entries.extend(got)
    products = {e.code for e in entries if e.market in ("ETF", "ETN")}   # 주식 목록에 섞여 있어도 상품이면 비보통주
    return [e if e.common is False or e.code not in products else replace(e, common=False) for e in entries]


def _write(out: Path, results: Sequence[Resolution]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    codes: list[str] = []
    for r in results:
        if r.code is not None and r.code not in codes:
            codes.append(r.code)
    (out / "codes.txt").write_text("".join(c + "\n" for c in codes), encoding="utf-8", newline="")
    with (out / "name-map.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(NAME_MAP_HEADER)
        w.writerows((r.name, r.code, r.market, r.match) for r in results if r.code is not None)
    with (out / "unmatched.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(UNMATCHED_HEADER)
        w.writerows((r.name, r.reason) for r in results if r.code is None)


# --- 진입점 ------------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=1.1, allowed_apis=RESOLVE_APIS)


def _refuse(msg: str) -> int:
    print(f"종목명 변환 거부(연결 없음): {msg}")
    return 2


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog="scripts/resolve-names.py",
                                description="종목명 → 종목코드 변환 (키움 모의 ka10099 조회만, A-107)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--names", required=True, help="종목명 파일(한 줄에 하나, # 주석)")
    p.add_argument("--out", required=True, help="결과 폴더 (codes.txt·name-map.csv·unmatched.csv)")
    p.add_argument("--dry-run", action="store_true", help="이름 파일만 읽어 검증 (연결하지 않음)")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def _resolve_path(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def resolve_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
                 clock: Clock | None = None, connect: Connect | None = None) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회)에서만 실행한다")
    try:
        names = read_names(_resolve_path(root, ns.names))
    except (OSError, UnicodeDecodeError, ResolveError) as exc:
        return _refuse(f"--names 파일: {exc}")
    out = _resolve_path(root, ns.out)
    clash = [f for f in OUT_FILES if (out / f).exists()]
    if clash:
        return _refuse(f"{out}에 이미 {', '.join(clash)}가 있음 — 덮어쓰지 않는다. 다른 --out을 지정")
    if ns.dry_run:
        print(f"--dry-run: 종목명 {len(names)}개 확인 (연결하지 않음, 출력 없음)")
        return 0
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
    results: list[Resolution] = []
    try:
        try:
            entries = load_entries(adapter)
        except (BrokerQueryError, parse.KiwoomParseError, ResolveError) as exc:
            print(f"ka10099 조회 실패(결과를 쓰지 않음): {exc}")
            return 1
        results = resolve_all(names, entries)
        if guard.blocked or getattr(adapter, "critical", None):
            print("중단: 주문 API 시도 또는 브로커 치명 오류 — 결과를 쓰지 않음")
            rc = 1
        else:
            _write(out, results)
    finally:
        revoked = adapter.close()
        print(f"토큰 폐기: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    blocked = list(guard.blocked) + [rec.api_id for rec in guard.records if rec.api_id in ORDER_API_IDS]
    if blocked:
        print(f"주문 API 호출 시도 {len(blocked)}회(전송 전 차단) — 결과를 쓰지 말 것")
        rc = 1
    matched = sum(1 for r in results if r.code is not None)
    print(f"요약: 이름 {len(names)}개 중 매칭 {matched}, 미매칭 {len(names) - matched}, 저장 {out}")
    return rc
