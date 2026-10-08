"""T-009 백테스트 결과 파일 (trades.csv·equity.csv·summary.json, 04 §2.6, D-078, 사용자 형식 승인 2026-10-08).

모든 가격·달력·전략은 가상이다 (형식·동작 시험용, 실제 시장 데이터 아님).
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import replace
from datetime import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from tests.scenario.test_t009_backtest import config
from trading_system.app.backtest import BacktestResult, run_backtest
from trading_system.domain.market import estimate_cost

TRADE_HEADER = ["종목", "진입일시", "청산일시", "진입가", "청산가", "수량", "손익", "비용", "보유일", "청산사유"]
EQUITY_HEADER = ["날짜", "총자산", "현금", "보유평가액", "낙폭"]
OPEN_REASON = "미청산(기간 종료)"
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def _csv(p: Path) -> list[list[str]]:
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "엑셀용 UTF-8 BOM"
    return list(csv.reader(io.StringIO(raw[3:].decode("utf-8"), newline="")))


def _run(tmp_path: Path, run_id: str = "x1", **over: object) -> tuple[BacktestResult, Path]:
    r = run_backtest(config(tmp_path, run_id, **over))
    return r, r.db_path.parent


def test_three_files_written_next_to_db(tmp_path: Path) -> None:
    _, d = _run(tmp_path)
    for name in ("trading.db", "trades.csv", "equity.csv", "summary.json"):
        assert (d / name).is_file(), name


def test_trades_csv_matches_round_trips(tmp_path: Path) -> None:
    r, d = _run(tmp_path)
    rows = _csv(d / "trades.csv")
    assert rows[0] == TRADE_HEADER
    closed = [x for x in rows[1:] if x[9] != OPEN_REASON]
    assert len(closed) == len(r.trades) > 0
    rule_ids = {rule.rule_id for rule in config(tmp_path).strategy.spec.rules}
    # 체결 원장에서 독립 계산한 가중 평균가·수량·비용
    rates = config(tmp_path).gate_settings.cost_rates
    for t, row in zip(r.trades, closed, strict=True):
        inst, ent, ext, ep, xp, qty, pnl, cost, hold, reason = row
        assert inst == t.instrument
        assert STAMP.match(ent) and STAMP.match(ext)
        assert ent[:10] == t.entry_day.isoformat() and ext[:10] == t.exit_day.isoformat()
        assert ent[11:] == "09:05" and ext[11:] == "09:05"  # 단일 판정: 체결 시각 = 판정 시각
        assert Decimal(pnl) == t.pnl and int(hold) == t.holding_days
        assert reason in rule_ids
        fills = [f for f in r.fills if f[2] == inst and t.entry_day <= f[0] <= t.exit_day]
        buys = [(Decimal(f[4]), Decimal(f[5])) for f in fills if f[3] == "BUY"]
        sells = [(Decimal(f[4]), Decimal(f[5])) for f in fills if f[3] == "SELL"]
        bq, sq = sum(q for q, _ in buys), sum(q for q, _ in sells)
        assert Decimal(qty) == bq == sq
        assert Decimal(ep) == (sum(q * p for q, p in buys) / bq).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert Decimal(xp) == (sum(q * p for q, p in sells) / sq).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert Decimal(cost) == sum((estimate_cost(q * p, False, rates) for q, p in buys), Decimal("0")) \
            + sum((estimate_cost(q * p, True, rates) for q, p in sells), Decimal("0"))
        assert Decimal(pnl) == sum(q * p for q, p in sells) - sum(q * p for q, p in buys) - Decimal(cost)
    for x in rows[1:]:
        for v in (x[3], x[4], x[5], x[6], x[7]):
            assert v == "" or "e" not in v.lower()  # 지수 표기 없는 Decimal 문자열


def test_open_position_row_and_summary_count(tmp_path: Path) -> None:
    full, _ = _run(tmp_path, "full")
    first = full.trades[0]
    r, d = _run(tmp_path, "cut", end=first.entry_day)   # 진입한 날 끝냄 → 미청산 1건
    rows = _csv(d / "trades.csv")[1:]
    opens = [x for x in rows if x[9] == OPEN_REASON]
    assert len(opens) >= 1
    o = next(x for x in opens if x[0] == first.instrument)
    assert o[1][:10] == first.entry_day.isoformat() and o[2] == "" and o[4] == "" and o[6] == ""
    assert Decimal(o[5]) > 0 and Decimal(o[7]) > 0
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert s["open_positions"] == len(opens)
    assert s["metrics"]["trades"] == r.metrics["trades"]  # 미청산은 지표 거래 수에 넣지 않음 (D-078)


def test_equity_csv_daily_split_and_drawdown(tmp_path: Path) -> None:
    r, d = _run(tmp_path)
    rows = _csv(d / "equity.csv")
    assert rows[0] == EQUITY_HEADER
    body = rows[1:]
    assert [(x[0], Decimal(x[1])) for x in body] == [(day.isoformat(), e) for day, e in r.equity]
    peak = Decimal(config(tmp_path).initial_cash)
    for day_s, total, cash, held, dd in body:
        assert Decimal(total) == Decimal(cash) + Decimal(held)
        peak = max(peak, Decimal(total))
        assert Decimal(dd) == ((peak - Decimal(total)) / peak).quantize(Decimal("0.000001"))
    assert max(Decimal(x[4]) for x in body) == Decimal(r.metrics["max_drawdown"])
    assert all(Decimal(x[3]) > 0 for x in body)  # 가상 패턴에서 TST-B를 첫날부터 끝까지 보유


def test_summary_json_contents(tmp_path: Path) -> None:
    cfg = config(tmp_path, "s1")
    r = run_backtest(cfg)
    text = (r.db_path.parent / "summary.json").read_text(encoding="utf-8")
    s = json.loads(text)
    assert "\\u" not in text and text.startswith("{\n  ")  # ensure_ascii=False, indent 2
    assert s["metrics"] == r.metrics
    assert s["run_id"] == "s1" and s["segment"] == "ALL"
    assert s["start"] == r.days[0].isoformat() and s["end"] == r.days[-1].isoformat()
    assert s["strategy_id"] == cfg.strategy.spec.strategy_id and s["strategy_version"] == cfg.strategy.spec.version
    assert s["params"] == {k: str(v) for k, v in sorted(cfg.params.items())}
    assert s["cost_rates"] == {"commission_rate": str(cfg.gate_settings.cost_rates.commission_rate),
                               "sell_tax_rate": str(cfg.gate_settings.cost_rates.sell_tax_rate)}
    assert s["slippage_ticks"] == 1 and s["slippage_tick"] == "10"
    assert s["settings_hash"] == cfg.account_limits.version_hash()
    assert s["digest"] == r.digest
    assert s["reproducible"] is None  # 재현성은 두 번 실행해야 알 수 있음 — run_backtest 단독에서는 미산출
    assert s["quality_failures"] == len(r.quality_failures)
    assert s["rejects"] == {k: v for k, v in sorted(r.rejects.items())}
    assert s["initial_cash"] == "10000000" and s["costs"] == str(r.costs)
    assert s["eval_times"] == ["09:05"]


def test_rejects_counted_in_summary(tmp_path: Path) -> None:
    from tests.support.gate_fakes import scenario_limits
    tiny = scenario_limits(l1_max_loss_ratio=Decimal("0.001"), l2_max_symbol_ratio=Decimal("0.001"),
                           l7_max_order_amount=Decimal("10000000"))
    r, d = _run(tmp_path, "tight", account_limits=tiny)
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert s["rejects"].get("G7", 0) == r.rejects["G7"] > 0
    assert _csv(d / "trades.csv")[1:] == []
    eq = _csv(d / "equity.csv")[1:]
    assert eq and all(Decimal(x[3]) == 0 and x[1] == x[2] for x in eq)  # 보유 없음 → 총자산 = 현금


def test_outputs_deterministic_across_runs(tmp_path: Path) -> None:
    _, a = _run(tmp_path, "a")
    _, b = _run(tmp_path, "b")
    for name in ("trades.csv", "equity.csv"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    sa = (a / "summary.json").read_text(encoding="utf-8").replace('"run_id": "a"', '"run_id": "b"')
    assert sa == (b / "summary.json").read_text(encoding="utf-8")


def test_export_does_not_change_results(tmp_path: Path) -> None:
    on = run_backtest(config(tmp_path, "on"))
    off = run_backtest(config(tmp_path, "off", write_exports=False))
    assert not (off.db_path.parent / "trades.csv").exists()
    assert (on.digest, on.metrics, on.fills, on.equity) == (off.digest, off.metrics, off.fills, off.equity)


def test_multi_eval_fill_times(tmp_path: Path) -> None:
    r, d = _run(tmp_path, "m", eval_times=(time(9, 5), time(14, 30)))
    rows = _csv(d / "trades.csv")[1:]
    stamps = {x[i][11:] for x in rows for i in (1, 2) if x[i]}
    assert stamps and stamps <= {"09:05", "14:30"}
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert s["eval_times"] == ["09:05", "14:30"]
    assert s["digest"] == r.digest


def test_trade_rows_partial_increase_decrease_same_day_reentry() -> None:
    """재검토(2026-10-08) 중간: 왕복 집계를 부분 체결·증액·감액·같은 날 재진입·미청산으로 손계산과 대조 (가상 체결)."""
    from datetime import date

    from trading_system.app.backtest_export import FillDetail, trade_rows

    d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    fills = [
        (d1, "1", "A", "BUY", "10", "100"), (d1, "1", "A", "BUY", "5", "101"),   # 같은 주문 부분 체결 2건
        (d2, "2", "A", "SELL", "5", "102"),                                      # 감액
        (d2, "3", "A", "BUY", "5", "104"),                                       # 증액
        (d2, "4", "B", "BUY", "3", "50"),                                        # 끝까지 미청산
        (d3, "5", "A", "SELL", "15", "103"),                                     # 전량 청산 → 1번째 왕복
        (d3, "6", "A", "BUY", "10", "103"), (d3, "7", "A", "SELL", "10", "105"),  # 같은 날 재진입·청산
    ]
    details = [FillDetail(f"{d.isoformat()} 09:05", r) for (d, *_), r in zip(fills, [
        "ENTRY", "ENTRY", "TRIM", "ADD", "ENTRY", "EXIT-2", "ENTRY", "STOP"], strict=True)]
    cost = lambda amount, sell: Decimal("2") if sell else Decimal("1")  # noqa: E731 — 손계산용 고정 비용
    rows, n_open = trade_rows(fills, details, cost, lambda a, b: (b - a).days, d3)
    assert n_open == 1
    assert rows == [
        # 매수 1000+505+520=2025/20주, 매도 510+1545=2055/20주, 비용 매수 3건×1 + 매도 2건×2 = 7, 손익 2055−2025−7=23
        ("A", "2026-01-05 09:05", "2026-01-07 09:05", "101.25", "102.75", "20", "23", "7", "2", "EXIT-2"),
        ("A", "2026-01-07 09:05", "2026-01-07 09:05", "103.00", "105.00", "10", "17", "3", "0", "STOP"),
        ("B", "2026-01-06 09:05", "", "50.00", "", "3", "", "1", "1", OPEN_REASON),
    ]
