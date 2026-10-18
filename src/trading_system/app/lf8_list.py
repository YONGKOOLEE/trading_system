"""LF-8 표본 목록 생성 도구 (CP-038 v0.2 D-244 (1)(2)(6), T-073). 사람이 실행하는 조회 전용 도구.

A-118 공개 일봉 사본(읽기 전용)만 읽는다. 네트워크·브로커·저장소·주문 없음(표준 라이브러리만).
규칙(D-244, 연도·N은 CP 규칙상 고정 — 명령줄로 바꿀 수 없다):
  연도 Y = 2019~2025 각각, Y-1년 UTC 일봉 quote_volume 합 상위 10 심볼(USDT 쌍).
  제외 ① BTC·ETH ② 사전 고정 제외 표 ③ 레버리지 토큰 ④ Y-1년 안 연속 7일 이상 결측(그해만) ⑤ Y-1년 12-31 전에 데이터가 끝남(그해만).
  순위는 제외 뒤 순위. 2026-01-01 이후 행은 읽지도 합산하지도 않는다(C-8).
[해석 — 규칙 원문 밖] 아래는 원문에 없어 에이전트가 정한 해석이며, 함수·표 단위로 분리해 두었다.
  H-1 ③의 판별: 기초 자산명이 UP·DOWN·BULL·BEAR로 끝나고 접미사를 뗀 기초 자산이 USDT 쌍 목록에 실제로 있을 때만(JUP 오판 방지) -> is_leverage_token
  H-2 ⑤ 데이터가 Y-1년 12-31 전에 끝난 심볼(1월 1일에 고를 수 없음)을 그해 제외 -> ended_before_year_end
  H-3 금 연동·BTC/ETH 포장 토큰을 제외 표에 포함 -> config/lf8/fixed-exclude.toml
  H-4 결측일: 날짜 행이 없거나 quote_volume이 빈 칸인 날. 이전 데이터가 있는 심볼은 Y-1년 1월 1일부터의 선두 공백도 센다(신규 상장은 결측 아님) -> missing_streak
  H-5 스테이블 의심 경고(표에 없고 종가가 지속적으로 0.98~1.02): 경고만, 자동 제외 없음 -> suspect_stable
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---- CP 규칙 고정값 (D-244). 바꾸려면 CP -----------------------------------------------------
YEARS: tuple[int, ...] = tuple(range(2019, 2026))
TOP_N = 10
CUTOFF_YEAR = 2026  # 이 연도 이후 행은 읽지 않는다 (C-8)
GAP_DAYS = 7  # 연속 결측 7일 이상 -> 그해 제외
DEPLOY_BASES = frozenset({"BTC", "ETH"})  # ① 배포 전략 매매 대상
LEVERAGE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
QUOTE = "USDT"
# ---- 스테이블 의심 경고 기준 (해석 H-5, 경고 전용) ---------------------------------------------
STABLE_LOW = Decimal("0.98")
STABLE_HIGH = Decimal("1.02")
STABLE_SHARE = Decimal("0.95")  # Y-1년 종가의 95% 이상이 범위 안
STABLE_MIN_DAYS = 30  # 비교할 종가 일수가 이보다 적으면 판단하지 않는다

VOLUME_QUANT = Decimal("0.00000001")
HEADER = ("year", "symbol", "prev_year_quote_volume", "rank", "excluded_reason")
DEFAULT_EXCLUDE = Path("config") / "lf8" / "fixed-exclude.toml"

REASON_DEPLOY = "배포 대상(BTC·ETH)"
REASON_LEVERAGE = "레버리지 토큰"
REASON_GAP = "연속 7일 이상 결측"
REASON_ENDED = "직전 해 말 전 데이터 종료"


class Lf8Error(RuntimeError):
    pass


@dataclass
class SymbolStats:
    """심볼 하나의 일봉에서 뽑은 연도별 집계. 2026-01-01 이후 행은 담지 않는다."""

    volume: dict[int, Decimal] = field(default_factory=dict)  # 연도 -> quote_volume 합
    present: dict[int, set[date]] = field(default_factory=dict)  # 연도 -> 값이 있는 날
    close_total: dict[int, int] = field(default_factory=dict)
    close_in_range: dict[int, int] = field(default_factory=dict)
    blank_volume_days: int = 0
    first_day: date | None = None
    last_day: date | None = None


@dataclass(frozen=True)
class Row:
    year: int
    symbol: str
    volume: Decimal
    rank: int | None
    reason: str


# ---- 입력 읽기 -------------------------------------------------------------------------------
def load_fixed_exclude(path: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Lf8Error(f"제외 표를 읽지 못함: {path} ({exc.__class__.__name__})") from exc
    table = data.get("exclude")
    if not isinstance(table, dict) or not table:
        raise Lf8Error(f"제외 표에 [exclude]가 없거나 비어 있음: {path}")
    out: dict[str, str] = {}
    for base, reason in table.items():
        if not base.isascii() or not base.isalnum() or base != base.upper():
            raise Lf8Error(f"제외 표 키는 대문자 영숫자 기초 자산이어야 함: {base!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise Lf8Error(f"제외 표 사유가 비어 있음: {base}")
        out[base] = reason
    return out


def load_symbols(data_dir: Path) -> list[str]:
    path = data_dir / "meta" / "symbols.csv"
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or "symbol" not in reader.fieldnames:
                raise Lf8Error(f"symbols.csv에 symbol 열이 없음: {path}")
            symbols = sorted({r["symbol"] for r in reader if r["symbol"].endswith(QUOTE) and len(r["symbol"]) > len(QUOTE)})
    except OSError as exc:
        raise Lf8Error(f"symbols.csv를 읽지 못함: {path} ({exc.__class__.__name__})") from exc
    if not symbols:
        raise Lf8Error("USDT 쌍이 하나도 없음")
    return symbols


def _dec(text: str, what: str, path: Path) -> Decimal:
    try:
        v = Decimal(text)
    except InvalidOperation as exc:
        raise Lf8Error(f"숫자가 아님({what}): {path.name}") from exc
    if not v.is_finite() or v < 0:
        raise Lf8Error(f"음수·비유한 값({what}): {path.name}")
    return v


def read_symbol(path: Path) -> SymbolStats:
    """일봉 파일 하나를 읽는다. 날짜는 엄격 증가여야 하며, 2026-01-01 이후 첫 행에서 읽기를 멈춘다."""
    st = SymbolStats()
    prev: date | None = None
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        need = {"open_time_utc", "close", "quote_volume"}
        if reader.fieldnames is None or not need <= set(reader.fieldnames):
            raise Lf8Error(f"필요한 열이 없음: {path.name}")
        for r in reader:
            try:
                day = date.fromisoformat(r["open_time_utc"][:10])
            except ValueError as exc:
                raise Lf8Error(f"날짜를 해석하지 못함: {path.name}") from exc
            if prev is not None and day <= prev:
                raise Lf8Error(f"날짜가 증가하지 않음(중복·역순): {path.name} {day}")
            prev = day
            if day.year >= CUTOFF_YEAR:
                break  # 이후 행은 읽지도 합산하지도 않는다
            if st.first_day is None:
                st.first_day = day
            st.last_day = day
            qv = (r["quote_volume"] or "").strip()
            if not qv:
                st.blank_volume_days += 1  # H-4: 결측일로 센다 (합계에는 0)
                continue
            y = day.year
            st.volume[y] = st.volume.get(y, Decimal(0)) + _dec(qv, "quote_volume", path)
            st.present.setdefault(y, set()).add(day)
            close = _dec(r["close"], "close", path)
            st.close_total[y] = st.close_total.get(y, 0) + 1
            if STABLE_LOW <= close <= STABLE_HIGH:
                st.close_in_range[y] = st.close_in_range.get(y, 0) + 1
    return st


def read_all(data_dir: Path, symbols: Sequence[str]) -> tuple[dict[str, SymbolStats], list[str]]:
    stats: dict[str, SymbolStats] = {}
    missing: list[str] = []
    for s in symbols:
        p = data_dir / f"{s}-1d.csv"
        if not p.is_file():
            missing.append(s)
            continue
        stats[s] = read_symbol(p)
    return stats, missing


# ---- 제외 판정 (해석 부분은 함수·표 단위) ------------------------------------------------------
def base_of(symbol: str) -> str:
    return symbol[: -len(QUOTE)]


def is_leverage_token(base: str, bases: frozenset[str]) -> bool:
    """H-1: 접미사를 뗀 기초 자산이 목록에 실제로 있을 때만 레버리지 토큰."""
    for suf in LEVERAGE_SUFFIXES:
        if base.endswith(suf):
            under = base[: -len(suf)]
            if under and under in bases:
                return True
    return False


def ended_before_year_end(st: SymbolStats, prev_year: int) -> bool:
    """H-2: 수집된 데이터(2025-12-31까지)가 Y-1년 12-31 전에 끝났는가."""
    return st.last_day is None or st.last_day < date(prev_year, 12, 31)


def missing_streak(st: SymbolStats, prev_year: int) -> int:
    """H-4: Y-1년 안 가장 긴 연속 결측일 수. 이전 데이터가 있는 심볼은 1월 1일부터의 선두 공백도 센다."""
    days = sorted(st.present.get(prev_year, ()))
    if not days:
        return (date(prev_year, 12, 31) - date(prev_year, 1, 1)).days + 1
    gaps = [(b - a).days - 1 for a, b in zip(days, days[1:])]
    if st.first_day is not None and st.first_day < date(prev_year, 1, 1):
        gaps.append((days[0] - date(prev_year, 1, 1)).days)
    # 빈 칸 행이 낀 경우도 위 present 차이로 이미 결측으로 잡힌다. 12-31 이후 끝은 ⑤가 맡는다.
    return max(gaps, default=0)


def suspect_stable(st: SymbolStats, prev_year: int) -> bool:
    """H-5: Y-1년 종가의 STABLE_SHARE 이상이 0.98~1.02 안 (경고 전용)."""
    total = st.close_total.get(prev_year, 0)
    if total < STABLE_MIN_DAYS:
        return False
    return Decimal(st.close_in_range.get(prev_year, 0)) / Decimal(total) >= STABLE_SHARE


def exclusion_reason(
    symbol: str, st: SymbolStats, prev_year: int, fixed: Mapping[str, str], bases: frozenset[str]
) -> str:
    base = base_of(symbol)
    if base in DEPLOY_BASES:
        return REASON_DEPLOY
    if base in fixed:
        return fixed[base]
    if is_leverage_token(base, bases):
        return REASON_LEVERAGE
    if missing_streak(st, prev_year) >= GAP_DAYS:
        return REASON_GAP
    if ended_before_year_end(st, prev_year):
        return REASON_ENDED
    return ""


# ---- 목록 생성 -------------------------------------------------------------------------------
def build_rows(
    stats: Mapping[str, SymbolStats], symbols: Sequence[str], fixed: Mapping[str, str]
) -> tuple[list[Row], list[str]]:
    bases = frozenset(base_of(s) for s in symbols)
    rows: list[Row] = []
    warnings: list[str] = []
    for year in YEARS:
        py = year - 1
        ranked = sorted(((s, st.volume[py]) for s, st in stats.items() if py in st.volume), key=lambda kv: (-kv[1], kv[0]))
        rank = 0
        for s, vol in ranked:
            if rank >= TOP_N:
                break
            st = stats[s]
            reason = exclusion_reason(s, st, py, fixed, bases)
            if reason:
                rows.append(Row(year, s, vol, None, reason))
            else:
                rank += 1
                rows.append(Row(year, s, vol, rank, ""))
            if base_of(s) not in fixed and suspect_stable(st, py):
                warnings.append(
                    f"스테이블 의심(제외 표에 없음, 자동 제외 안 함): {year}년 후보 {s} — {py}년 종가 "
                    f"{st.close_in_range.get(py, 0)}/{st.close_total.get(py, 0)}일이 {STABLE_LOW}~{STABLE_HIGH}"
                )
        if rank < TOP_N:
            warnings.append(f"{year}년 선정 {rank}개(<{TOP_N}): 후보 부족")
    return rows, warnings


def render_csv(rows: Sequence[Row]) -> bytes:
    """결정적 직렬화: UTF-8(BOM 없음), 줄바꿈 LF, 합계는 소수 8자리 고정."""
    import io

    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r.year, r.symbol, format(r.volume.quantize(VOLUME_QUANT), "f"), "" if r.rank is None else r.rank, r.reason])
    return buf.getvalue().encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 명령줄 ----------------------------------------------------------------------------------
_EPILOG = (
    "연도(2019~2025)·N(10)·결측 7일·제외 규칙은 CP-038 D-244 고정값이라 명령줄로 바꿀 수 없다(바꾸려면 CP).\n"
    "[해석 — 규칙 원문 밖] ③ 레버리지 판별(접미사를 뗀 기초 자산이 목록에 있을 때만), ⑤ 직전 해 말 전 데이터 종료 제외,\n"
    "금 연동·BTC/ETH 포장 토큰을 제외 표에 포함, 선두 결측 계산, 스테이블 의심 경고 기준(종가 95% 이상이 0.98~1.02, 30일 이상)은\n"
    "에이전트의 해석이다. 바꾸려면 lf8_list.py의 함수·상수 또는 제외 표 파일(CP로만)."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lf8-list", description="LF-8 표본 목록 생성 (CP-038 D-244, T-073). A-118 일봉 사본 읽기 전용, 네트워크 없음.",
        epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data-dir", required=True, type=Path, help="A-118 일봉 사본 디렉터리 (meta/symbols.csv와 <SYMBOL>-1d.csv)")
    p.add_argument("--out", required=True, type=Path, help="출력 CSV 경로. 옆에 <경로>.sha256 파일도 쓴다. 이미 있으면 거부")
    p.add_argument("--exclude-table", type=Path, default=None, help=f"사전 고정 제외 표(TOML). 기본: <저장소 루트>/{DEFAULT_EXCLUDE.as_posix()}")
    return p


def lf8_main(root: Path, argv: Sequence[str], out: Callable[[str], None] | None = None) -> int:
    say = out or print
    args = build_parser().parse_args(list(argv))
    exclude_path = args.exclude_table or (root / DEFAULT_EXCLUDE)
    sha_path = args.out.with_name(args.out.name + ".sha256")
    try:
        for p in (args.out, sha_path):
            if p.exists():
                raise Lf8Error(f"이미 있는 파일은 덮어쓰지 않음: {p}")
        fixed = load_fixed_exclude(exclude_path)
        symbols = load_symbols(args.data_dir)
        stats, missing = read_all(args.data_dir, symbols)
        rows, warnings = build_rows(stats, symbols, fixed)
        data = render_csv(rows)
        digest = sha256_hex(data)
        table_digest = sha256_hex(exclude_path.read_bytes())
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(data)
        sha_path.write_text(f"{digest}  {args.out.name}\n", encoding="utf-8", newline="\n")
    except Lf8Error as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    chosen = sorted({r.symbol for r in rows if r.rank is not None})
    say("[해석 — 규칙 원문 밖] ③ 레버리지 판별·⑤ 직전 해 말 전 종료 제외·금/포장 토큰 제외 표 포함·선두 결측·스테이블 의심 기준 (도움말 참조)")
    say(f"연도 {YEARS[0]}~{YEARS[-1]} / N={TOP_N} / 2026-01-01 이후 행 미사용 / 심볼 {len(symbols)}개(파일 없음 {len(missing)}개)")
    say(f"선정 심볼(합집합) {len(chosen)}개: {','.join(chosen)}")
    say(f"제외된 후보 행 {sum(1 for r in rows if r.rank is None)}개, 빈 quote_volume 일수 {sum(s.blank_volume_days for s in stats.values())}")
    say(f"스테이블 의심 기준: 종가 {STABLE_SHARE * 100}% 이상이 {STABLE_LOW}~{STABLE_HIGH}, 비교 {STABLE_MIN_DAYS}일 이상 (경고만)")
    for w in warnings:
        say(f"경고: {w}")
    say(f"제외 표 SHA-256: {table_digest}")
    say(f"출력: {args.out}")
    say(f"SHA-256: {digest}")
    return 0
