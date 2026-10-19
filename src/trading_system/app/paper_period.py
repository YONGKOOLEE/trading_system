"""CP-041 D-278 PAPER 기간 분류 (T-072, 승인 A-122 Q-4 (가), 착수 A-122 ②·A-127) — BACKTEST 판정 전용, 순수 계산.

- 지표: 28 UTC일 창 중 **완결 왕복 0건 비율** p₀(28). 창 = 기간 안의 매 UTC일을 시작일로 하는 28일(시작일 포함 28개 날짜),
  기간 안에 완전히 들어가는 창만. 완결 왕복 = 진입 체결일과 전량 청산 체결일이 모두 그 창 안(04 D-078 — PAPER가 무보유에서
  시작해 진입~청산 경로를 보는지). 참고로 진입 0건 비율과 60·90일 창 값도 낸다.
- 판정: p₀(28) ≥ 25%(A-122 Q-4 (가))면 저빈도 → PAPER-1 최소 90 UTC일(D-279), 아니면 최소 28 UTC일(CP-037 D-232).
  비율 비교는 분수로 정확히 한다(경계 25.0% = 저빈도).
- 거부: 기간 끝 또는 왕복 날짜가 최종 확인 구간(final_from, 2026-01-01) 이후(D-163 보호), 28일 창 수 0.
- 실행 조건(파라미터 파일·한도 파일)은 이름·SHA-256으로 기록한다 — PAPER에 쓸 파일과 같아야 한다(축소판 수량 내림으로
  진입이 줄 수 있음, D-278 (2)). 분류의 승인은 PAPER 전환 승인 때 함께(D-278 (4)) — 이 모듈은 계산·표시만 한다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

THRESHOLD = Decimal("0.25")               # A-122 Q-4 (가)
WINDOW_DAYS = (28, 60, 90)
MIN_DAYS_GENERAL = 28                     # CP-037 D-232
MIN_DAYS_LOW = 90                         # CP-041 D-279 (A-119 ④)
_Q6 = Decimal("0.000001")


class PaperPeriodError(ValueError):
    """분류 입력 거부 (2026 행·창 수 0)."""


@dataclass(frozen=True)
class Lot:
    """왕복 하나: 진입 체결 UTC일, 전량 청산 체결 UTC일(끝까지 열려 있으면 None)."""
    entry: date
    exit: date | None


def _file_ref(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _ratio(k: int, n: int) -> str | None:
    return None if n == 0 else format((Decimal(k) / Decimal(n)).quantize(_Q6), "f")


def classify_paper_period(lots: Sequence[Lot], *, start: date, end: date, final_from: date,
                          threshold: Decimal = THRESHOLD, params_file: Path | None = None,
                          limits_file: Path | None = None) -> dict:
    """기간 start~end(포함)의 왕복 기록으로 D-278 분류. 결과는 validation.json에 그대로 넣는 dict."""
    if end >= final_from:
        raise PaperPeriodError(f"기간 끝 {end}가 최종 확인 구간({final_from} 이후)과 겹침 — D-163 보호")
    late = [x for x in lots if x.entry >= final_from or (x.exit is not None and x.exit >= final_from)]
    if late:
        raise PaperPeriodError(f"최종 확인 구간({final_from}, 2026 이후) 행이 들어 있음 — {len(late)}건 (D-278 (2))")
    n_days = (end - start).days + 1
    if n_days - WINDOW_DAYS[0] + 1 <= 0:
        raise PaperPeriodError(f"창 수 0 — 기간 {max(0, n_days)}일 < {WINDOW_DAYS[0]}일")
    inside = [x for x in lots if start <= x.entry <= end]
    rounds = [x for x in inside if x.exit is not None and x.exit <= end]
    windows: dict[str, dict] = {}
    for w in WINDOW_DAYS:
        count = max(0, n_days - w + 1)
        no_rt = no_entry = 0
        for i in range(count):
            a = start + timedelta(days=i)
            b = a + timedelta(days=w - 1)
            if not any(a <= x.entry and x.exit <= b for x in rounds):  # type: ignore[operator]
                no_rt += 1
            if not any(a <= x.entry <= b for x in inside):
                no_entry += 1
        windows[str(w)] = {"count": count, "no_round_trip": no_rt, "p0": _ratio(no_rt, count),
                           "no_entry": no_entry, "p_entry0": _ratio(no_entry, count)}
    w28 = windows[str(WINDOW_DAYS[0])]
    low = Fraction(w28["no_round_trip"], w28["count"]) >= Fraction(threshold)
    return {
        "cp": "CP-041 D-278", "approved_by": "A-122", "period": [start.isoformat(), end.isoformat()],
        "threshold": str(threshold), "windows": windows, "rounds_total": len(rounds), "entries_total": len(inside),
        "low_frequency": low, "class": "저빈도" if low else "일반",
        "min_paper_days": MIN_DAYS_LOW if low else MIN_DAYS_GENERAL,
        "rule": "CP-041 D-279" if low else "CP-037 D-232",
        "params_file": _file_ref(params_file), "limits_file": _file_ref(limits_file),
    }


def _ref_text(ref: dict | None) -> str:
    return "출처 미기록" if ref is None else f"`{ref['name']}` SHA-256 `{ref['sha256']}`"


def render_paper_period(c: dict) -> list[str]:
    """validation.md의 "PAPER 기간 분류" 절. 분류의 승인은 PAPER 전환 승인 때(사용자) — 여기서는 계산값만."""
    head = ["", "## PAPER 기간 분류 (CP-041 D-278·D-279 — 계산·표시, 분류 승인은 PAPER 전환 승인 때)", ""]
    if "error" in c:
        return head + [f"- 분류 불가: {c['error']}"]
    w = c["windows"]
    lines = head + [
        f"- 기간 {c['period'][0]}~{c['period'][1]} 연속 1회 실행(기본 슬리피지), 완결 왕복 {c['rounds_total']}건·진입 "
        f"{c['entries_total']}건",
        f"- 실행 조건: 파라미터 {_ref_text(c['params_file'])} / 한도 {_ref_text(c['limits_file'])} — PAPER에 쓸 파일과 "
        "같아야 한다(다르면 다시 계산)",
        "", "| 창 | 창 수 | 완결 왕복 0건 | p₀ | 진입 0건 | 진입 0건 비율 |", "|---|---|---|---|---|---|"]
    for k in (str(x) for x in WINDOW_DAYS):
        x = w[k]
        lines.append(f"| {k}일 | {x['count']} | {x['no_round_trip']} | {x['p0'] or '—'} | {x['no_entry']} | "
                     f"{x['p_entry0'] or '—'} |")
    lines += ["", f"- **p₀(28) {w['28']['p0']}** (기준 ≥ {c['threshold']}) → **{c['class']}** — PAPER-1 최소 "
                  f"{c['min_paper_days']} UTC일 ({c['rule']})"
              + (". 90일 뒤 완결 왕복 0건이면 합격 판정 보류 → 사용자 결정(D-279)" if c["low_frequency"] else "")]
    return lines


__all__ = ["Lot", "PaperPeriodError", "THRESHOLD", "classify_paper_period", "render_paper_period"]
