"""CP-038 저빈도 예외 경로 — 판정 로직·적격 검사·설정·원장 (T-058, 승인 A-121 Q-1~Q-5, 착수 A-122 ②). BACKTEST 판정 전용.

주문 경로·게이트·한도·PAPER 코드와 무관하다(CP-038 §9). 일반 판정(04 §2.9, CP-030)은 그대로 먼저 하고, 결과가 C-2 하나만
"판단 불가"이며 전략이 적격(D-235 E-1~E-5)일 때만 이 경로(D-236 LF-0~LF-8)를 추가로 판정한다(D-234). 실행(창·전체 이력·
표본 심볼)은 app/backtest_m2_minute가 하고, 여기는 계산과 문구만 둔다.

- 수치는 config/low-frequency.toml에서만 읽는다(D-237 (1) — 명령줄 옵션 없음). 승인 A-ID(approved_by)가 없거나
  approvals.md에 그 A-ID·CP-038 행이 없으면 거부. 판정표에 CP 버전·설정 SHA-256을 적는다(D-237 (3)).
- 거래별 수익률 r = 거래 손익(비용 반영) ÷ 그 거래 총 진입 체결 금액(증액 포함), 로그 수익 = ln(1 + r) — 크기 무관(§3).
- "거래당 기대값 > 0"(LF-3 창·LF-6·LF-7·LF-8.3 심볼)은 [DP-85] 로그 수익 평균 > 0 **그리고** 손익(USDT) 평균 > 0 둘 다.
- LF-3 거래 0건 창이 한도를 넘으면 [DP-85] "판단 불가"(V-305 기대 결과 — §3 표의 "미충족"과 다름, 둘 다 통과 불가).
- 원장(low-frequency-ledger.jsonl): 저빈도 판정을 한 번이라도 계산한 전략 버전은 파라미터·설정(수치)이 바뀌면 거부
  → 새 버전 필요(D-237 (2)(4), D-245 ②).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.app.backtest_m2 import sellable
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import RequestType
from trading_system.domain.models import Signal
from trading_system.domain.strategy import EvaluationSnapshot
from trading_system.domain.trading_rules import SymbolRules

CP_ID = "CP-038"
LABEL = "저빈도 예외(CP-038)"
PASS = "통과(저빈도 예외, CP-038)"
PASS_C7 = "통과(저빈도 예외, CP-038 — 과대 추정 가능)"
FAIL = "미충족(저빈도 예외 기준)"
UNDET = "판단 불가"
MET = "충족"
UNMET = "미충족"
STRUCTURAL_REASONS = ("보유 기간", "판정 주기", "유니버스 크기")          # D-235 E-1
VERIFY_FAMILIES = ("paper0-verify", "m2-verify", "m2-paper0-verify")    # D-161 검증용 전략 (E-4)
UNKNOWN_PAST = datetime(1970, 1, 1, tzinfo=UTC)   # 커밋 시각을 모르는 공식 판정표 → 선언보다 먼저 있었다고 본다(보수)
NO_MINUTE_SAMPLE = "분봉 판정 전략인데 표본 심볼 1분봉 실행 결과 없음 (D-244 (7) — 일봉 근사로 대신하지 않음)"
NO_SAMPLE_WIRING = ("LF-8 표본 실행 연결 전 — 1분봉 여러 심볼 실행기·표본 파일 배치는 T-068 뒤 (D-244 (7), "
                    "표본 데이터 없음)")
BOOTSTRAP_LIMITS = ("참고 표시(판정 제외, D-242): 거래 간 독립 가정(국면 의존 무시), 표본 n이 작음, "
                    "선택 편향(C-7·LF-0)을 반영하지 못함")
_A_ID = re.compile(r"A-\d+")
_RISK_UP = (RequestType.ENTRY, RequestType.INCREASE)
_Q6 = Decimal("0.000001")
_Q4 = Decimal("0.0001")
_ZERO = Decimal("0")


class LowFrequencyError(RuntimeError):
    pass


# --- 설정 (D-237) ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LowFreqSettings:
    cp: str
    cp_version: str
    approved_by: str
    markets: tuple[str, ...]
    c2_min_trades: int
    lf1_min_window_trades: int
    lf2_min_history_trades: int
    lf3_max_zero_windows: int
    lf3_positive_numerator: int
    lf3_positive_denominator: int
    lf4_k_min: int
    lf4_k_ratio: Decimal
    lf6_slippage_multiplier: int
    lf8_role: str
    lf8_min_trades: int
    lf8_min_positive_symbol_ratio: Decimal
    lf8_slippage_multiplier: int
    lf8_delist_haircut: Decimal
    lf8_list_path: str
    lf8_list_sha256: str
    bootstrap_draws: int
    bootstrap_quantile: Decimal
    sha256: str


_SCHEMA: dict[str, dict[str, str]] = {
    "eligibility": {"c2_min_trades": "int"},
    "lf": {"lf1_min_window_trades": "int", "lf2_min_history_trades": "int", "lf3_max_zero_windows": "int0",
           "lf3_positive_numerator": "int", "lf3_positive_denominator": "int", "lf4_k_min": "int",
           "lf4_k_ratio": "ratio", "lf6_slippage_multiplier": "int"},
    "lf8": {"role": "role", "min_trades": "int", "min_positive_symbol_ratio": "ratio", "slippage_multiplier": "int",
            "delist_haircut": "ratio", "list_path": "text", "list_sha256": "sha"},
    "bootstrap": {"draws": "int", "quantile": "ratio"},
}
_TOP = {"cp", "cp_version", "approved_by", "markets", *_SCHEMA}


def _value(kind: str, raw: object, key: str, problems: list[str]) -> object:
    if kind in ("int", "int0"):
        lo = 0 if kind == "int0" else 1
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < lo:
            problems.append(f"{key}: {lo} 이상의 정수")
            return None
        return raw
    if kind == "ratio":
        try:
            v = Decimal(raw) if isinstance(raw, str) else None
        except InvalidOperation:
            v = None
        if v is None or not v.is_finite() or not _ZERO < v <= 1:
            problems.append(f"{key}: 0 초과 1 이하 소수 문자열")
            return None
        return v
    if kind == "role":
        if raw not in ("required", "reference"):
            problems.append(f"{key}: required 또는 reference (Q-4)")
            return None
        return raw
    if kind == "sha":
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{64}", raw):
            problems.append(f"{key}: SHA-256 16진 64자")
            return None
        return raw
    if not isinstance(raw, str) or not raw:
        problems.append(f"{key}: 문자열")
        return None
    return raw


def load_low_frequency_settings(path: Path, *, approvals_text: str | None) -> LowFreqSettings:
    """config/low-frequency.toml → 설정. 모든 키 필수, 모르는 키 거부, approved_by(A-ID)가 approvals.md의 CP-038 행에 있어야 한다."""
    data = path.read_bytes()
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path.name}: TOML 형식 오류 ({type(exc).__name__})") from None
    problems: list[str] = [f"모르는 키: {k}" for k in sorted(set(raw) - _TOP)]
    if raw.get("cp") != CP_ID:
        problems.append("cp: CP-038")
    if not isinstance(raw.get("cp_version"), str) or not raw["cp_version"]:
        problems.append("cp_version: 문자열")
    approved = raw.get("approved_by")
    if not isinstance(approved, str) or not _A_ID.fullmatch(approved):
        problems.append("approved_by: 승인 A-ID 필요 — A-ID 없는 수치로는 실행하지 않는다 (D-237)")
        approved = None
    markets = raw.get("markets")
    if not isinstance(markets, list) or not markets or any(m not in ("M1", "M2", "M3") for m in markets):
        problems.append("markets: M1·M2·M3 중 하나 이상의 목록")
    vals: dict[str, object] = {}
    for table, keys in _SCHEMA.items():
        sub = raw.get(table)
        if not isinstance(sub, dict):
            problems.append(f"[{table}] 표 없음")
            continue
        problems += [f"모르는 키: {table}.{k}" for k in sorted(set(sub) - set(keys))]
        for k, kind in keys.items():
            if k not in sub:
                problems.append(f"{k}: 없음")
                continue
            vals[f"{table}.{k}"] = _value(kind, sub[k], k, problems)
    if approved is not None and approvals_text is not None and not any(
            f"| {approved} |" in line and CP_ID in line for line in approvals_text.splitlines()):
        problems.append(f"approved_by {approved}: docs/approvals.md에 그 A-ID의 CP-038 승인 행이 없음")
    if problems:
        raise ConfigError([f"{path.name}: {p}" for p in problems])
    return LowFreqSettings(
        cp=CP_ID, cp_version=raw["cp_version"], approved_by=approved, markets=tuple(markets),  # type: ignore[arg-type]
        c2_min_trades=vals["eligibility.c2_min_trades"],  # type: ignore[arg-type]
        lf1_min_window_trades=vals["lf.lf1_min_window_trades"],  # type: ignore[arg-type]
        lf2_min_history_trades=vals["lf.lf2_min_history_trades"],  # type: ignore[arg-type]
        lf3_max_zero_windows=vals["lf.lf3_max_zero_windows"],  # type: ignore[arg-type]
        lf3_positive_numerator=vals["lf.lf3_positive_numerator"],  # type: ignore[arg-type]
        lf3_positive_denominator=vals["lf.lf3_positive_denominator"],  # type: ignore[arg-type]
        lf4_k_min=vals["lf.lf4_k_min"], lf4_k_ratio=vals["lf.lf4_k_ratio"],  # type: ignore[arg-type]
        lf6_slippage_multiplier=vals["lf.lf6_slippage_multiplier"],  # type: ignore[arg-type]
        lf8_role=vals["lf8.role"], lf8_min_trades=vals["lf8.min_trades"],  # type: ignore[arg-type]
        lf8_min_positive_symbol_ratio=vals["lf8.min_positive_symbol_ratio"],  # type: ignore[arg-type]
        lf8_slippage_multiplier=vals["lf8.slippage_multiplier"],  # type: ignore[arg-type]
        lf8_delist_haircut=vals["lf8.delist_haircut"], lf8_list_path=vals["lf8.list_path"],  # type: ignore[arg-type]
        lf8_list_sha256=vals["lf8.list_sha256"], bootstrap_draws=vals["bootstrap.draws"],  # type: ignore[arg-type]
        bootstrap_quantile=vals["bootstrap.quantile"], sha256=hashlib.sha256(data).hexdigest())  # type: ignore[arg-type]


# --- E-1 선언 (전략 명세의 "저빈도 선언 (CP-038 E-1)" 표) -----------------------------------------------------------

@dataclass(frozen=True)
class Declaration:
    version: int
    annual_min: Decimal
    annual_max: Decimal
    reasons: tuple[str, ...]
    tried_combinations: int | None      # LF-0 (없으면 LF-0 판단 불가)
    search_period: str
    choice_reason: str
    commit: str | None = None           # 선언이 들어 있는 명세 파일의 마지막 커밋 (작업 트리 변경 있으면 None)
    committed_at: datetime | None = None


_HEAD = re.compile(r"^##\s*저빈도 선언")


def parse_declaration(text: str) -> tuple[Declaration | None, list[str]]:
    """명세 마크다운 → 선언. 절이 없거나 '선언 | 아니오'면 (None, []). 형식 오류면 (None, 문제 목록)."""
    rows: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if _HEAD.match(line):
            inside = True
            continue
        if inside and line.startswith("#"):
            break
        if inside and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] not in ("항목", "") and not set(cells[0]) <= {"-", ":"}:
                rows[cells[0]] = cells[1]
    if not inside or rows.get("선언") != "예":
        return None, []
    problems: list[str] = []
    try:
        version = int(rows.get("버전", ""))
    except ValueError:
        problems.append("저빈도 선언: 버전(정수) 없음")
        version = 0
    lo = hi = _ZERO
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*~\s*(\d+(?:\.\d+)?)\s*", rows.get("예상 연간 거래 수", ""))
    if m is None or Decimal(m.group(1)) > Decimal(m.group(2)):
        problems.append("저빈도 선언: 예상 연간 거래 수 '하한~상한' 형식 아님")
    else:
        lo, hi = Decimal(m.group(1)), Decimal(m.group(2))
    reasons = tuple(x.strip() for x in rows.get("구조적 사유", "").split(",") if x.strip())
    if not reasons or any(x not in STRUCTURAL_REASONS for x in reasons):
        problems.append(f"저빈도 선언: 구조적 사유는 {'·'.join(STRUCTURAL_REASONS)} 중에서")
    tried: int | None = None
    if rows.get("시도 조합 수", "").isdigit() and int(rows["시도 조합 수"]) >= 1:
        tried = int(rows["시도 조합 수"])
    if problems:
        return None, problems
    return Declaration(version, lo, hi, reasons, tried, rows.get("탐색 데이터 기간", ""), rows.get("고른 이유", "")), []


# --- 거래 기록 ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeRec:
    instrument: str
    entry_day: date
    exit_day: date
    pnl: Decimal                 # 비용 반영 (호가 자산)
    entry_amount: Decimal        # 그 거래 총 진입 체결 금액(증액 포함)

    @property
    def log_return(self) -> Decimal:
        if self.entry_amount <= 0:
            raise LowFrequencyError("진입 체결 금액 0 이하 거래 — 엔진 오류")
        g = 1 + self.pnl / self.entry_amount
        if g <= 0:
            raise LowFrequencyError("거래 손실이 진입 금액을 넘음 — 현물에서 불가, 엔진 오류")
        return g.ln()


@dataclass(frozen=True)
class OpenLot:
    instrument: str
    quantity: Decimal
    cash: Decimal                # 이 왕복의 호가 자산 흐름 합 (매수 −)
    buy_amount: Decimal
    entry_day: date


def round_trip_records(fills: Sequence[object], pairs: Mapping[str, tuple[str, str]],
                       rules: Mapping[str, SymbolRules]) -> tuple[list[TradeRec], dict[str, OpenLot]]:
    """backtest_m2._round_trips와 같은 왕복 규칙(D-102) + 진입 체결 금액. 끝까지 닫히지 않은 왕복은 OpenLot."""
    out: list[TradeRec] = []
    st: dict[str, dict] = {}  # type: ignore[type-arg]
    for f in fills:
        inst = f.instrument  # type: ignore[attr-defined]
        base = pairs[inst][0]
        s = st.setdefault(inst, {"qty": _ZERO, "cash": _ZERO, "buy": _ZERO, "entry": None})
        day = f.at.astimezone(UTC).date()  # type: ignore[attr-defined]
        if s["entry"] is None:
            s["entry"], s["cash"], s["buy"] = day, _ZERO, _ZERO
        if f.side == "BUY":  # type: ignore[attr-defined]
            s["qty"] += f.quantity - (f.commission if f.commission_asset == base else _ZERO)  # type: ignore[attr-defined]
            s["cash"] -= f.quote_qty  # type: ignore[attr-defined]
            s["buy"] += f.quote_qty  # type: ignore[attr-defined]
        else:
            s["qty"] -= f.quantity  # type: ignore[attr-defined]
            s["cash"] += f.quote_qty - f.commission  # type: ignore[attr-defined]
        if sellable(s["qty"], rules[inst]) == 0:
            out.append(TradeRec(inst, s["entry"], day, s["cash"], s["buy"]))
            s["entry"] = None
            s["qty"] = _ZERO if s["qty"] < 0 else s["qty"]
    lots = {i: OpenLot(i, s["qty"], s["cash"], s["buy"], s["entry"]) for i, s in st.items()
            if s["entry"] is not None and sellable(s["qty"], rules[i]) > 0}
    return out, lots


def close_out(lot: OpenLot, last_close: Decimal, fee: Decimal, haircut: Decimal, day: date) -> TradeRec:
    """D-244 (4): 보유 중 데이터가 끝나면 마지막 종가 × haircut(0.9)로 전량 청산했다고 보고 거래로 집계(수수료 차감)."""
    return TradeRec(lot.instrument, lot.entry_day, day, lot.cash + lot.quantity * last_close * haircut * (1 - fee),
                    lot.buy_amount)


def _mean(xs: Sequence[Decimal]) -> Decimal | None:
    return sum(xs, _ZERO) / len(xs) if xs else None


def _positive(trades: Sequence[TradeRec]) -> bool:
    """[DP-85] 거래당 기대값 > 0 = 로그 수익 평균 > 0 그리고 손익 평균 > 0."""
    if not trades:
        return False
    return _mean([t.log_return for t in trades]) > 0 and _mean([t.pnl for t in trades]) > 0  # type: ignore[operator]


def _f(x: Decimal | None, q: Decimal = _Q6) -> str | None:
    return None if x is None else format(x.quantize(q), "f")


# --- 적격 (D-235) ------------------------------------------------------------------------------------------------

def e2_threshold(s: LowFreqSettings, windows: int) -> Decimal:
    return Decimal(s.c2_min_trades) / Decimal(windows)


def annual_trades(n: int, days: int) -> Decimal:
    return Decimal(n) * Decimal(365) / Decimal(days)


def is_verification(strategy_id: str) -> bool:
    return any(strategy_id == f or strategy_id.startswith(f + "-") for f in VERIFY_FAMILIES)


def check_eligibility(s: LowFreqSettings, *, market: str, strategy_id: str, version: int,
                      declaration: Declaration | None, declaration_problems: Sequence[str],
                      first_official_at: datetime | None, general: Mapping[str, object], reproducible: bool,
                      lookahead_ok: bool, limits_profile: str | None, limits_approved_by: str | None, windows: int,
                      history_trades: int | None, history_days: int | None) -> dict[str, object]:
    """E-1~E-5. history_*가 None이면 E-2를 미루고 pre_ok(나머지 충족 여부)만 준다. 결과 문구는 V-304."""
    items: dict[str, str] = {}
    reasons: list[str] = []
    first: str | None = None

    def fail(result: str) -> None:
        nonlocal first
        first = first or result
    general_only = "부적격 — 일반 판정만"
    if market not in s.markets:
        reasons.append(f"Q-3: 시장 {market}은 저빈도 예외 미적용 (A-121 — {','.join(s.markets)}만)")
        fail(general_only)
    if is_verification(strategy_id):
        items["E-4"] = "제외"
        reasons.append("E-4 검증용 전략(D-161) — 성과 기준 적용 제외")
        fail("제외(검증용 전략, D-161)")
    else:
        items["E-4"] = MET
    e1: list[str] = []
    if declaration is None:
        e1.append("E-1 선언 없음" + (f" (형식 오류: {'; '.join(declaration_problems)})" if declaration_problems else ""))
    else:
        if declaration.version != version:
            e1.append(f"E-1 선언 버전 {declaration.version} ≠ 전략 버전 {version}")
        if declaration.commit is None or declaration.committed_at is None:
            e1.append("E-1 선언 커밋 확인 불가(작업 트리 변경·git 없음) — 공식 검증 전에 커밋해야 함")
        elif first_official_at is not None and first_official_at < declaration.committed_at:
            e1.append("E-1 사후 선언: 이 버전의 공식 판정표가 선언 커밋보다 먼저 있음 → 새 버전 필요(D-011)")
    c2 = general.get("C-2")
    if c2 == MET:
        items["E-3"] = "해당 없음(C-2 충족 — 일반 경로)"
        reasons.append("E-3 C-2 충족 — 일반 판정 그대로")
        fail("해당 없음(일반 경로 판정)")
    elif (general.get("C-3") != MET or general.get("C-4") != MET or not reproducible or not lookahead_ok):
        items["E-3"] = UNMET
        reasons.append(f"E-3 C-2 외 미충족(C-3 {general.get('C-3')}, C-4 {general.get('C-4')}, 재현성 "
                       f"{'통과' if reproducible else '실패'}, 교란 {'통과' if lookahead_ok else '실패'}) — 일반 판정 그대로")
        fail("미충족(예외 경로 열지 않음)")
    else:
        items["E-3"] = MET
    if not limits_profile or not _A_ID.fullmatch(limits_approved_by or ""):
        items["E-5"] = UNMET
        reasons.append("E-5 미승인 한도 프로필(A-ID 없음) 결과로는 적격·판정 불가 (CP-034 D-196)")
        fail("적격 불가(미승인 한도 프로필, E-5)")
    else:
        items["E-5"] = MET
    annual: Decimal | None = None
    if history_trades is None or history_days is None or history_days <= 0:
        items["E-2"] = "미계산"
    else:
        annual = annual_trades(history_trades, history_days)
        thr = e2_threshold(s, windows)
        if annual < thr:
            items["E-2"] = MET
        else:
            items["E-2"] = UNMET
            reasons.append(f"E-2 전체 이력 연평균 거래 {_f(annual, _Q4)} ≥ {s.c2_min_trades} ÷ {windows}창 "
                           f"= {_f(thr, _Q4)} — 저빈도 아님")
        if declaration is not None and not declaration.annual_min <= annual <= declaration.annual_max:
            e1.append(f"E-1 실측 연평균 {_f(annual, _Q4)}가 선언 범위 {declaration.annual_min}~{declaration.annual_max} 밖 "
                      "→ 선언 무효(새 버전에서 다시 선언)")
    items["E-1"] = UNMET if e1 else MET
    reasons[0:0] = e1
    if e1:
        fail(general_only)
    if items["E-2"] == UNMET:
        fail(general_only)
    pre_ok = first is None
    eligible = pre_ok and items["E-2"] == MET
    return {"eligible": eligible, "pre_ok": pre_ok, "result": "적격" if eligible else (first or "E-2 미계산"),
            "items": dict(sorted(items.items())), "reasons": reasons,
            "annual_trades": _f(annual, _Q4), "threshold": _f(e2_threshold(s, windows), _Q4) if windows else None}


# --- LF-0~LF-7 (D-236) -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowLF:
    k: int
    trades: list[TradeRec]
    hold_return: Decimal | None           # 단순 보유 창 수익률(C-5 방식, 비용 반영)
    open_pnl: Decimal | None              # 창 끝 미청산 평가 손익 (표시만)


def top_k(s: LowFreqSettings, n: int) -> int:
    return max(s.lf4_k_min, math.ceil(Decimal(n) * s.lf4_k_ratio))


def lf4(s: LowFreqSettings, logs: Sequence[tuple[Decimal, date]]) -> dict[str, object]:
    """상위 k건(로그 수익 큰 순, 동률은 진입일 이른 것 먼저) 제외 뒤 나머지 합 ≥ 0."""
    k = top_k(s, len(logs))
    ranked = sorted(logs, key=lambda x: (-x[0], x[1]))
    top, rest = ranked[:k], ranked[k:]
    total = sum((x[0] for x in logs), _ZERO)
    rest_sum = sum((x[0] for x in rest), _ZERO)
    share = sum((x[0] for x in top), _ZERO) / total if total > 0 else None
    return {"status": MET if rest_sum >= 0 else UNMET, "k": k, "n": len(logs), "rest_sum": _f(rest_sum),
            "top_share": _f(share, _Q4), "removed_entry_days": [d.isoformat() for _, d in top]}


def lf5(s: LowFreqSettings, windows: Sequence[WindowLF]) -> dict[str, object]:
    """하락 창(단순 보유 < 0) 각각에서 청산 거래 로그 수익 합 > ln(1 + 보유 수익률). 하락 창 0개 → 판단 불가."""
    if any(w.hold_return is None for w in windows):
        return {"status": UNDET, "reason": "단순 보유 수익률을 계산하지 못한 창 있음", "windows": []}
    rows = []
    ok = True
    for w in windows:
        if w.hold_return >= 0:  # type: ignore[operator]
            continue
        got = sum((t.log_return for t in w.trades), _ZERO)
        thr = (1 + w.hold_return).ln()  # type: ignore[operator]
        ok &= got > thr
        rows.append({"k": w.k, "hold_return": str(w.hold_return), "trades": len(w.trades), "log_sum": _f(got),
                     "threshold": _f(thr), "met": got > thr,
                     "open_pnl": None if w.open_pnl is None else format(w.open_pnl, "f")})
    if not rows:
        return {"status": UNDET, "reason": "하락 창 0개", "windows": []}
    return {"status": MET if ok else UNMET, "windows": rows}


def _lf_positive(trades: Sequence[TradeRec]) -> dict[str, object]:
    return {"status": MET if _positive(trades) else UNMET, "trades": len(trades),
            "mean_log": _f(_mean([t.log_return for t in trades])), "mean_pnl": _f(_mean([t.pnl for t in trades]))}


def compose(items: Mapping[str, Mapping[str, object]], *, c7: bool) -> str:
    """D-234 문구. 판단 불가 검사 → 충족 검사. LF-R·C-5+·참고 역할 LF-8은 판정에 넣지 않는다."""
    keys = [k for k in items if k.startswith("LF-") and k != "LF-R"
            and not (k == "LF-8" and items[k].get("role") == "reference")]
    statuses = [items[k].get("status") for k in keys]
    if UNDET in statuses:
        return UNDET
    if UNMET in statuses:
        return FAIL
    return PASS_C7 if c7 else PASS


def judge_lf(s: LowFreqSettings, *, declaration: Declaration, windows: Sequence[WindowLF],
             history: Sequence[TradeRec], history_slip2: Sequence[TradeRec],
             variants: Mapping[str, Sequence[TradeRec]], variants_run: bool, lf8: Mapping[str, object] | None,
             c7: bool) -> dict[str, object]:
    items: dict[str, dict[str, object]] = {}
    lf0 = declaration.tried_combinations is not None and bool(declaration.search_period) and \
        bool(declaration.choice_reason)
    items["LF-0"] = {"status": MET if lf0 else UNDET, "tried_combinations": declaration.tried_combinations,
                     "search_period": declaration.search_period, "choice_reason": declaration.choice_reason}
    pooled = sum(len(w.trades) for w in windows)
    items["LF-1"] = {"status": MET if pooled >= s.lf1_min_window_trades else UNDET, "trades": pooled,
                     "required": s.lf1_min_window_trades}
    items["LF-2"] = {"status": MET if len(history) >= s.lf2_min_history_trades else UNDET, "trades": len(history),
                     "required": s.lf2_min_history_trades}
    zero = sum(1 for w in windows if not w.trades)
    m = len(windows) - zero
    req = math.ceil(Decimal(m * s.lf3_positive_numerator) / Decimal(s.lf3_positive_denominator))
    pos = sum(1 for w in windows if _positive(w.trades))
    if zero > s.lf3_max_zero_windows:
        st3 = UNDET          # [DP-85] V-305
    else:
        st3 = MET if pos >= req else UNMET
    items["LF-3"] = {"status": st3, "zero_windows": zero, "max_zero_windows": s.lf3_max_zero_windows,
                     "windows_with_trades": m, "positive_windows": pos, "required": req}
    items["LF-4"] = lf4(s, [(t.log_return, t.entry_day) for t in history])
    items["LF-5"] = lf5(s, windows)
    items["LF-6"] = {**_lf_positive(history_slip2), "slippage_multiplier": s.lf6_slippage_multiplier}
    if not variants_run or not variants:
        items["LF-7"] = {"status": UNDET, "reason": "변형 미실행" if not variants_run else "변형 가능한 수치 파라미터 없음",
                         "variants": []}
    else:
        rows = [{"label": k, **_lf_positive(v)} for k, v in variants.items()]
        items["LF-7"] = {"status": MET if all(r["status"] == MET for r in rows) else UNMET, "variants": rows}
    if lf8 is None:
        items["LF-8"] = {"status": UNDET, "role": s.lf8_role, "reason": "판단 불가(표본 데이터 없음)"}
    else:
        items["LF-8"] = {**lf8}
    verdict = compose(items, c7=c7)
    reasons = [f"{k} {v['status']}" + (f" — {v['reason']}" if v.get("reason") else "")
               for k, v in items.items() if v.get("status") != MET]
    return {"items": items, "verdict": verdict, "reasons": reasons}


# --- LF-8 (D-244·D-245) ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Lf8SymbolResult:
    trades: list[TradeRec]             # 기본 슬리피지
    trades_slip2: list[TradeRec]       # 슬리피지 × lf8_slippage_multiplier
    hold_return: Decimal | None        # 참고: 단순 보유


def _lf8_one(s: LowFreqSettings, syms: Mapping[str, Lf8SymbolResult]) -> dict[str, str]:
    allt = [t for r in syms.values() for t in r.trades]
    slip2 = [t for r in syms.values() for t in r.trades_slip2]
    items: dict[str, str] = {}
    items["LF-8.1"] = MET if len(allt) >= s.lf8_min_trades else UNMET
    m1, m2 = _mean([t.log_return for t in allt]), _mean([t.log_return for t in slip2])
    items["LF-8.2"] = MET if m1 is not None and m1 > 0 and m2 is not None and m2 > 0 else UNMET
    traded = [k for k, r in syms.items() if r.trades]
    pos = [k for k in traded if _positive(syms[k].trades)]
    ratio = Decimal(len(pos)) / Decimal(len(traded)) if traded else _ZERO
    items["LF-8.3"] = MET if traded and ratio >= s.lf8_min_positive_symbol_ratio else UNMET
    contrib = {k: sum((t.log_return for t in syms[k].trades), _ZERO) for k in traded}
    rest_mean = None
    if contrib:
        top = sorted(contrib, key=lambda k: (-contrib[k], k))[0]
        rest_mean = _mean([t.log_return for k in traded if k != top for t in syms[k].trades])
    items["LF-8.4"] = MET if rest_mean is not None and rest_mean > 0 else UNMET
    return items


def judge_lf8(s: LowFreqSettings, runs: Mapping[str, Mapping[str, Lf8SymbolResult]] | None, *,
              deployed: Sequence[TradeRec], list_sha256: str | None, no_data_reason: str = "") -> dict[str, object]:
    """runs = 별칭(배포 심볼) → 표본 심볼 → 결과. 별칭이 여럿이면 모두 충족해야 충족(보수, DP-86)."""
    base: dict[str, object] = {"role": s.lf8_role, "list_sha256": list_sha256}
    if not runs or not any(runs.values()):
        return {**base, "status": UNDET, "items": {},
                "reason": f"판단 불가(표본 데이터 없음) — {no_data_reason or NO_MINUTE_SAMPLE}"}
    by_alias = {a: _lf8_one(s, syms) for a, syms in sorted(runs.items())}
    keys = ("LF-8.1", "LF-8.2", "LF-8.3", "LF-8.4")
    items = {k: (UNMET if any(v[k] == UNMET for v in by_alias.values()) else MET) for k in keys}
    first = runs[sorted(runs)[0]]
    sample_mean = _mean([t.log_return for r in first.values() for t in r.trades])
    dep_mean = _mean([t.log_return for t in deployed])
    ratio = dep_mean / sample_mean if dep_mean is not None and sample_mean not in (None, _ZERO) else None
    ref = {"symbols": {k: {"trades": len(r.trades), "mean_log": _f(_mean([t.log_return for t in r.trades])),
                           "hold_return": None if r.hold_return is None else str(r.hold_return)}
                       for k, r in sorted(first.items())},
           "deployed_over_sample": _f(ratio, _Q4), "sample_symbols": len(first),
           "sample_trades": sum(len(r.trades) for r in first.values())}
    return {**base, "status": UNMET if UNMET in items.values() else MET, "items": items,
            "by_alias": by_alias, "reference": ref}


# --- LF-R 부트스트랩 (D-242, 참고) --------------------------------------------------------------------------------

def seed_of(strategy_id: str, version: int) -> str:
    return f"cp038:{strategy_id}-v{version}"


def bootstrap_reference(s: LowFreqSettings, logs: Sequence[Decimal], seed: str) -> dict[str, object]:
    out: dict[str, object] = {"seed": seed, "draws": s.bootstrap_draws, "n": len(logs),
                              "quantile": str(s.bootstrap_quantile), "limits": BOOTSTRAP_LIMITS}
    if not logs:
        return {**out, "p_plus": None, "q_mean": None}
    rng = random.Random(seed)
    xs = [float(x) for x in logs]
    n = len(xs)
    pos = 0
    means: list[float] = []
    for _ in range(s.bootstrap_draws):
        tot = math.fsum(rng.choices(xs, k=n))
        pos += tot > 0
        means.append(tot / n)
    means.sort()
    q = means[min(len(means) - 1, int(Decimal(s.bootstrap_draws) * s.bootstrap_quantile))]
    return {**out, "p_plus": format(Decimal(pos) / Decimal(s.bootstrap_draws), ".4f"), "q_mean": format(q, ".6f")}


# --- 원장 (D-237 (2)(4), D-245 ②) ---------------------------------------------------------------------------------

def params_hash(params: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(params), sort_keys=True, default=str).encode()).hexdigest()


def check_ledger(path: Path | None, strategy: str, version: int, params_sha: str, settings_sha: str) -> None:
    if path is None or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("strategy") == strategy and rec.get("version") == version and \
                (rec.get("params_sha256") != params_sha or rec.get("settings_sha256") != settings_sha):
            raise LowFrequencyError(
                f"{strategy} v{version}은 저빈도 예외 판정 기록이 있다 — 파라미터·CP-038 설정이 그때와 다르면 "
                "새 버전 필요 (D-237 (2)(4), D-245 ②, D-011)")


def record_ledger(path: Path | None, strategy: str, version: int, params_sha: str, settings_sha: str,
                  cp_version: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"strategy": strategy, "version": version, "params_sha256": params_sha,
                            "settings_sha256": settings_sha, "cp_version": cp_version,
                            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")


# --- 표시 (D-240) ------------------------------------------------------------------------------------------------

def table_valid(md: str, o: Mapping[str, object]) -> bool:
    """저빈도 예외를 적용한 판정표는 맨 위(5줄 안) 표시 줄과 종합 판정 줄 모두에 LABEL이 있어야 유효(C-7과 같은 처리)."""
    low = o.get("low_frequency")
    if not isinstance(low, Mapping) or not low.get("applied"):
        return True
    verdict = str(o["verdict"]["verdict"])  # type: ignore[index]
    lines = md.splitlines()
    head = any(x.startswith(f"- **{LABEL}**") for x in lines[:5])
    final = any(x.startswith("- **판정:") and LABEL in x and verdict in x for x in lines)
    return head and final


# --- LF-8 실행 보조 ----------------------------------------------------------------------------------------------

class EntryYears:
    """LF-8 표본 실행용 전략 감싸개(D-244 (3)): 목록 해 밖 판정의 신규 진입·증액 신호만 버린다. 청산·감액은 그대로.
    전략 코드·파라미터는 바꾸지 않는다."""

    def __init__(self, inner: object, years: frozenset[int]) -> None:
        self._inner = inner
        self.spec = inner.spec  # type: ignore[attr-defined]
        self._years = years
        self.blocked = 0

    def evaluate(self, snapshot: EvaluationSnapshot, params: Mapping[str, object]) -> list[Signal]:
        out = list(self._inner.evaluate(snapshot, params))  # type: ignore[attr-defined]
        if snapshot.as_of.astimezone(UTC).year in self._years:
            return out
        keep = [x for x in out if getattr(x, "kind", None) not in _RISK_UP]
        self.blocked += len(out) - len(keep)
        return keep


def continuous_rules(alias: str) -> SymbolRules:
    """D-244 (5): 표본 심볼 거래 규칙 사본이 없어 수량·가격 단위를 1e-8로 둔다(연속값 근사). 최소 주문 금액 5 USDT는
    BTCUSDT와 같게 둔다(작은 주문이 거부되는 쪽 — 보수, DP-86)."""
    tiny = Decimal("0.00000001")
    return SymbolRules(instrument=alias, tick_size=tiny, min_price=tiny, max_price=Decimal("100000000"),
                       lot_min_qty=tiny, lot_max_qty=Decimal("100000000000"), lot_step=tiny,
                       market_min_qty=Decimal("0"), market_max_qty=Decimal("0"), market_step=Decimal("0"),
                       min_notional=Decimal("5"), max_notional=None, apply_min_to_market=True,
                       apply_max_to_market=False)


# --- 입력 묶음 ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Lf8Sample:
    source: object                     # MinuteSource (시작, 끝) → 1분봉
    years: frozenset[int]              # 목록 해 (D-244 (2)(3))
    data_start: date
    data_end: date                     # 마지막 봉 날짜 — 전체 이력 끝 전이면 보유분 × 0.9 청산(D-244 (4))


@dataclass(frozen=True)
class Lf8Inputs:
    list_sha256: str
    aliases: tuple[str, ...]           # 표본 봉을 넣을 배포 심볼 이름(전략 유니버스) — DP-86
    samples: Mapping[str, Lf8Sample]


@dataclass(frozen=True)
class LowFrequencyInputs:
    settings: LowFreqSettings
    declaration: Declaration | None
    declaration_problems: list[str]
    first_official_at: datetime | None
    history_start: date
    history_end: date
    ledger: Path | None
    lf8: Lf8Inputs | None
    lf8_no_data_reason: str
    params_sha256: str
    market: str = "M2"
    extras: dict = field(default_factory=dict)


__all__ = ["CP_ID", "FAIL", "LABEL", "PASS", "PASS_C7", "UNDET", "Declaration", "EntryYears", "Lf8Inputs", "Lf8Sample",
           "Lf8SymbolResult", "LowFreqSettings", "LowFrequencyError", "LowFrequencyInputs", "OpenLot", "TradeRec",
           "WindowLF", "bootstrap_reference", "check_eligibility", "check_ledger", "close_out", "compose",
           "continuous_rules", "judge_lf", "judge_lf8", "load_low_frequency_settings", "parse_declaration",
           "record_ledger", "round_trip_records", "seed_of", "table_valid", "top_k"]
