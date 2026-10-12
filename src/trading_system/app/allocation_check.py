"""배분 규칙 대조 판정 (CP-034 D-199, A-092 Q-9, T-042). BACKTEST 전용(네트워크·브로커 없음).

같은 유니버스·같은 창(기본 D-162 7개)·같은 코드 커밋에서 **필터 유 vs 무**를 기본 슬리피지·2배로 실행한다
(창 × 2 × 2 실행, 실행마다 기존 run_backtest — 게이트·실행기·체결 모델 그대로). 배분 규칙 입력은 지수 CSV(index/001.csv).

| 기준 | 조건 | 역할 |
|---|---|---|
| ① 손실 창 수 | 유 < 무 (기본 슬리피지, 총수익률 < 0인 창) | 필수 |
| ② 거래당 기대값 | 유 ≥ 무 (기본·2배 모두, 창 합산) | 필수 |
| ③ 최악 창 총수익률 | 유 > 무 (기본 슬리피지) | 필수 |
| ④ 창별 일관성 | 양수 창 수(거래 있는 창 기준) 비감소 | 기록만 |
| ⑤ 놓친 이익 | 필터 실행에 거래가 없는 창에서 무필터가 번 금액 | 참고만 |
| ⑥ 최종 확인 | 최종 확인 구간 1회(D-163) + PAPER 20거래일 이상 | 통과 전 LIVE 금지 |

①②③ 충족 → "배분 규칙 채택 후보". 값이 없어(거래 0 등) 비교할 수 없으면 "판단 불가". 필터 실행 중 배분 규칙 판정
실패(지수 결측·지연)가 한 번이라도 있으면 대조가 무효라 "판단 불가". C-2는 배분 규칙에 적용하지 않는다(기초 전략은
필터 없이 C-1~C-8 전부 — 검증 실행기 기본 실행). 판정표는 판정 제안이며 채택·상태 전환 승인은 사용자다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from trading_system.app.backtest import BacktestConfig, BacktestResult
from trading_system.app.validation import (
    FINAL_FROM, SLIPPAGE_BASE, ValidationError, Window, check_final_guard, run_window, walkforward_windows,
)

PAPER_MIN_DAYS = 20     # Q-9 ⑥
_Q6 = Decimal("0.000001")


def _cmp(ok: bool | None) -> str:
    return "판단 불가" if ok is None else ("충족" if ok else "미충족")


def decide_allocation(on: dict, off: dict) -> dict[str, str]:
    """Q-9 ①~⑥. on = 필터 유, off = 필터 무 (각 loss_windows·worst_return·exp_base·exp_double·positive_windows·
    traded_windows·missed_profit·failures)."""
    c1 = on["loss_windows"] < off["loss_windows"]
    exps = (on["exp_base"], off["exp_base"], on["exp_double"], off["exp_double"])
    c2 = None if any(x is None for x in exps) else (on["exp_base"] >= off["exp_base"]
                                                     and on["exp_double"] >= off["exp_double"])
    c3 = None if on["worst_return"] is None or off["worst_return"] is None else on["worst_return"] > off["worst_return"]
    c4 = on["positive_windows"] >= off["positive_windows"]
    marks = {"①": _cmp(c1), "②": _cmp(c2), "③": _cmp(c3)}
    if on.get("failures", 0):
        verdict = "판단 불가"
        reason = f"필터 실행 중 배분 규칙 판정 실패 {on['failures']}회(지수 결측·지연) — 대조 무효"
    elif "판단 불가" in marks.values():
        verdict, reason = "판단 불가", "비교할 값 없음(거래 0 등)"
    elif all(v == "충족" for v in marks.values()):
        verdict, reason = "배분 규칙 채택 후보", "①②③ 충족"
    else:
        verdict = "채택 후보 아님"
        reason = "미충족: " + ", ".join(k for k, v in marks.items() if v != "충족")
    return {
        "verdict": verdict, "reason": reason, **marks,
        "④": ("충족" if c4 else "미충족") + " (기록만 — 국면 필터는 거래 창이 줄어 동률 허용)",
        "⑤": f"참고만: 필터 실행에 거래가 없는 창에서 무필터 손익 {on.get('missed_profit')}원",
        "⑥": f"최종 확인(최종 확인 구간 1회, D-163) + PAPER {PAPER_MIN_DAYS}거래일 이상 — 통과 전 LIVE 금지",
        "note": "C-2는 배분 규칙에 적용하지 않음 — 기초 전략은 필터 없이 C-1~C-8 전부(D-199). 판정 제안이며 채택은 사용자 승인",
    }


def _ret(r: BacktestResult, initial: Decimal) -> Decimal:
    final = r.equity[-1][1] if r.equity else initial
    return (final - initial) / initial


def _stats(base: list[BacktestResult], double: list[BacktestResult], initial: Decimal,
           missed: Decimal | None) -> dict:
    rets = [_ret(r, initial) for r in base]
    t1 = [t.pnl for r in base for t in r.trades]
    t2 = [t.pnl for r in double for t in r.trades]
    traded = [r for r in base if r.trades]
    return {
        "loss_windows": sum(1 for x in rets if x < 0),
        "worst_return": min(rets) if rets else None,
        "exp_base": sum(t1, Decimal("0")) / len(t1) if t1 else None,
        "exp_double": sum(t2, Decimal("0")) / len(t2) if t2 else None,
        "trades_base": len(t1), "trades_double": len(t2),
        "positive_windows": sum(1 for r in traded if _ret(r, initial) > 0), "traded_windows": len(traded),
        "missed_profit": missed,
        "failures": sum(int((r.allocation or {}).get("failed_evaluations", 0)) for r in base + double),
        "blocked": sum(int((r.allocation or {}).get("blocked_evaluations", 0)) for r in base + double),
        "window_returns": [format(x.quantize(_Q6), "f") for x in rets],
    }


def compare_allocation(template: BacktestConfig, *, out_dir: Path, report_version: int,
                       windows: Sequence[Window] | None = None, final_from: date = FINAL_FROM,
                       slippage_base: Decimal = SLIPPAGE_BASE) -> dict:
    if template.allocation is None:
        raise ValidationError("배분 규칙(--allocation)이 없음")
    if template.index_bars is None:
        raise ValidationError("지수 일봉(--index, index/001.csv)이 없음 — 배분 규칙 입력 CAP-DAT-05d")
    wins = tuple(windows) if windows is not None else walkforward_windows()
    for w in wins:
        check_final_guard(w.val_start, w.val_end, final_from, final_check=False)
    out_dir.mkdir(parents=True)
    tpl = replace(template, final_from=final_from, final_check=False)
    s1, s2 = slippage_base, slippage_base * 2
    runs: dict[tuple[bool, int], list[BacktestResult]] = {}
    for filt in (True, False):
        cfg = tpl if filt else replace(tpl, allocation=None)
        for n, slip in ((1, s1), (2, s2)):
            runs[(filt, n)] = [run_window(cfg, w, label=f"{'on' if filt else 'off'}-s{n}", out_dir=out_dir / "runs",
                                          slippage_pct=slip) for w in wins]
    missed = sum((sum((t.pnl for t in off.trades), Decimal("0"))
                  for on, off in zip(runs[(True, 1)], runs[(False, 1)], strict=True) if not on.trades), Decimal("0"))
    on = _stats(runs[(True, 1)], runs[(True, 2)], tpl.initial_cash, missed)
    off = _stats(runs[(False, 1)], runs[(False, 2)], tpl.initial_cash, None)
    spec, rule = tpl.strategy.spec, tpl.allocation.spec  # type: ignore[attr-defined]
    out = {
        "strategy": spec.owner, "rule": rule.owner, "report_version": report_version,
        "params": {k: str(v) for k, v in sorted(tpl.params.items())},
        "rule_params": {k: str(v) for k, v in sorted(tpl.allocation_params.items())},
        "windows": [{"k": w.k, "validation": [w.val_start.isoformat(), w.val_end.isoformat()]} for w in wins],
        "slippage": [format(s1, "f"), format(s2, "f")], "universe": sorted(tpl.data),
        "data_version": tpl.data_version, "code_commit": tpl.code_commit,
        "settings_hash": tpl.account_limits.version_hash(), "index_bars": len(tpl.index_bars),
        "runs": len(wins) * 4, "with_filter": on, "without_filter": off,
        "verdict": decide_allocation(on, off),
    }
    (out_dir / "allocation.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                             encoding="utf-8", newline="\n")
    (out_dir / f"allocation-v{report_version}.md").write_text(render_allocation(out), encoding="utf-8", newline="\n")
    return out


def _d(x: object) -> str:
    if x is None:
        return "—"
    if isinstance(x, Decimal):
        return format(x.quantize(Decimal("0.01")), "f")
    return str(x)


def _r(x: Decimal | None) -> str:
    return "—" if x is None else format(x.quantize(_Q6), "f")


def render_allocation(o: dict) -> str:
    v, on, off = o["verdict"], o["with_filter"], o["without_filter"]
    lines = [
        f"# 배분 규칙 대조 판정 — {o['rule']} × {o['strategy']} (v{o['report_version']})",
        "",
        f"- 판정: **{v['verdict']}** — {v['reason']}",
        f"- 조건: 같은 유니버스 {len(o['universe'])}종목, 창 {len(o['windows'])}개, 슬리피지 {' / '.join(o['slippage'])}, "
        f"코드 `{o['code_commit'] or '미기재'}`, 데이터 `{o['data_version'] or '미기재'}`, 설정 해시 `{o['settings_hash']}`",
        f"- 실행 수 {o['runs']} (창 × 필터 유·무 × 슬리피지 기본·2배), 지수 일봉 {o['index_bars']}개",
        f"- 필터 실행 배분 판정: 진입 불허 {on['blocked']}회, 판정 실패 {on['failures']}회",
        "",
        "| 기준 | 조건 | 필터 유 | 필터 무 | 결과 |",
        "|---|---|---|---|---|",
        f"| ① 손실 창 수 | 유 < 무 | {on['loss_windows']} | {off['loss_windows']} | {v['①']} |",
        f"| ② 거래당 기대값(기본) | 유 ≥ 무 | {_d(on['exp_base'])} | {_d(off['exp_base'])} | {v['②']} |",
        f"| ② 거래당 기대값(2배) | 유 ≥ 무 | {_d(on['exp_double'])} | {_d(off['exp_double'])} | (②에 포함) |",
        f"| ③ 최악 창 총수익률 | 유 > 무 | {_r(on['worst_return'])} | {_r(off['worst_return'])} | {v['③']} |",
        f"| ④ 양수 창 수(거래 창 기준) | 비감소 | {on['positive_windows']}/{on['traded_windows']} | "
        f"{off['positive_windows']}/{off['traded_windows']} | {v['④']} |",
        f"| ⑤ 놓친 이익 | 참고 | {_d(on['missed_profit'])} | — | 참고만 |",
        f"| ⑥ 최종 확인 | — | — | — | {v['⑥']} |",
        "",
        f"- 창별 총수익률(기본): 유 {', '.join(on['window_returns'])} / 무 {', '.join(off['window_returns'])}",
        f"- {v['note']}",
    ]
    return "\n".join(lines) + "\n"
