"""CP-041 T-072 (A-122 Q-4 (가), 착수 A-122 ②·A-127) — PAPER 기간 분류(D-278) V-349, 검증 보고서 "PAPER 기간 분류" 절,
`paper-review --market M2` 전략별 기간 표시(D-279 — 집계 상세는 test_t059_paper1_low_frequency).

가상 왕복 기록·합성 1분봉(임시 폴더·메모리)만 쓴다. 실제 데이터 파일(var/)은 읽지 않고, 어떤 실제 전략에도 분류를 실행하지
않는다. 모든 값은 가상이다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.app.paper_period import (
    THRESHOLD, Lot, PaperPeriodError, classify_paper_period, render_paper_period,
)

D = Decimal
S = date(2020, 1, 1)
FINAL = date(2026, 1, 1)


def lots_for(n_windows: int, empty_first: int) -> tuple[list[Lot], date]:
    """28일 창 n_windows개(기간 = n_windows + 27일) 중 앞 empty_first개만 완결 왕복이 없도록 하루짜리 왕복을 28일 간격으로 둔다."""
    end = S + timedelta(days=n_windows + 26)
    lots, d = [], empty_first + 27
    while True:
        lots.append(Lot(S + timedelta(days=d), S + timedelta(days=d)))
        if d >= n_windows - 1:
            break
        d += 28
    return lots, end


# --- V-349 분류 (순수 계산) ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("empty,p0,low,min_days", [
    (249, "0.249000", False, 28), (250, "0.250000", True, 90), (730, "0.730000", True, 90),
])
def test_v349_threshold_25_percent_boundary(empty: int, p0: str, low: bool, min_days: int) -> None:
    lots, end = lots_for(1000, empty)
    c = classify_paper_period(lots, start=S, end=end, final_from=FINAL)
    w = c["windows"]["28"]
    assert (w["count"], w["no_round_trip"], w["p0"]) == (1000, empty, p0)
    assert c["low_frequency"] is low and c["min_paper_days"] == min_days
    assert c["threshold"] == str(THRESHOLD) == "0.25"
    assert c["class"] == ("저빈도" if low else "일반")


def test_v349_entry_and_60_90_values_reported() -> None:
    end = date(2020, 4, 30)                                       # 121일 → 28일 창 94·60일 창 62·90일 창 32
    lots = [Lot(date(2020, 1, 10), date(2020, 2, 20)),            # 42일 왕복 — 28일 창에는 완결이 안 들어감
            Lot(date(2020, 4, 25), None)]                         # 끝까지 열린 진입
    c = classify_paper_period(lots, start=S, end=end, final_from=FINAL)
    w28, w60, w90 = (c["windows"][k] for k in ("28", "60", "90"))
    assert (w28["count"], w60["count"], w90["count"]) == (94, 62, 32)
    assert w28["no_round_trip"] == 94 and w28["p0"] == "1.000000"
    assert w28["no_entry"] == 94 - 10 - 6                          # 1/10 포함 창 10개(1/1~1/10 시작), 4/25 포함 6개(3/29~4/3)
    assert w60["no_round_trip"] == 62 - 10                         # 시작일 1/1~1/10만 왕복 전체를 담는다
    assert w90["no_round_trip"] == 32 - 10
    assert c["low_frequency"] is True and c["rounds_total"] == 1 and c["entries_total"] == 2


def test_v349_rows_from_2026_refused() -> None:
    with pytest.raises(PaperPeriodError, match="2026"):
        classify_paper_period([Lot(date(2025, 12, 20), date(2026, 1, 2))], start=date(2025, 1, 1),
                              end=date(2025, 12, 31), final_from=FINAL)
    with pytest.raises(PaperPeriodError, match="최종 확인"):
        classify_paper_period([], start=date(2025, 1, 1), end=date(2026, 1, 1), final_from=FINAL)


def test_v349_zero_windows_refused() -> None:
    with pytest.raises(PaperPeriodError, match="창 수 0"):
        classify_paper_period([], start=S, end=S + timedelta(days=26), final_from=FINAL)


def test_v349_render_shows_sources_names_and_hashes(tmp_path: Path) -> None:
    params = tmp_path / "crypto-x-paper.toml"
    params.write_text('strategy = "x"\nversion = 1\n[params]\nperiod_minutes = 60\n', encoding="utf-8")
    limits = tmp_path / "limits.paper.toml"
    limits.write_text("# 가상 한도\n", encoding="utf-8")
    lots, end = lots_for(100, 30)
    c = classify_paper_period(lots, start=S, end=end, final_from=FINAL, params_file=params, limits_file=limits)
    assert c["params_file"] == {"name": params.name, "sha256": hashlib.sha256(params.read_bytes()).hexdigest()}
    assert c["limits_file"]["sha256"] == hashlib.sha256(limits.read_bytes()).hexdigest()
    md = "\n".join(render_paper_period(c))
    for part in ("PAPER 기간 분류", "D-278", "p₀(28)", "0.300000", "저빈도", "90 UTC일", params.name, limits.name,
                 c["params_file"]["sha256"], "진입 0건", "60일", "90일", "PAPER 전환 승인"):
        assert part in md, part
    unknown = classify_paper_period(lots, start=S, end=end, final_from=FINAL)
    assert "출처 미기록" in "\n".join(render_paper_period(unknown))


def test_v349_render_error_section() -> None:
    md = "\n".join(render_paper_period({"error": "창 수 0 — 기간 3일 < 28일"}))
    assert "PAPER 기간 분류" in md and "분류 불가" in md and "창 수 0" in md


# --- 검증 실행기 연결 (validation.json·validation.md) ---------------------------------------------------------------

def test_validation_tiny_windows_report_classification_unavailable(tmp_path: Path) -> None:
    from tests.scenario.test_t053_m2_minute_backtest import DAY, Script, cfg_of, day_bars
    from trading_system.app.backtest_m2_minute import MinuteValidationInputs, run_minute_validation
    from trading_system.app.validation import Window
    tpl = cfg_of(tmp_path, Script(), day_bars(DAY, days=2), params={"period_minutes": 60, "qty": D("1")})
    ws = (Window(1, date(2024, 1, 1), date(2024, 12, 31), DAY, DAY),
          Window(2, date(2024, 1, 2), date(2025, 1, 1), DAY + timedelta(days=1), DAY + timedelta(days=1)))
    res = run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "val", data_start=DAY,
                                                       windows=ws, perturbation_days=1, skip_variants=True))
    assert "창 수 0" in res["paper_period"]["error"]
    assert res["verdict"]["verdict"] == "판단 불가"                     # 일반 판정 경로는 분류와 무관
    md = (tmp_path / "val" / "validation.md").read_text(encoding="utf-8")
    assert "PAPER 기간 분류" in md and "분류 불가" in md


def test_validation_continuous_run_classifies_daily_round_trips(tmp_path: Path) -> None:
    """28일을 넘는 연속 기간: 매일 01:00 진입·02:00 청산 → 모든 28일 창에 왕복 → p₀(28) = 0, 일반(최소 28일)."""
    from tests.scenario.test_t053_m2_minute_backtest import DAY, Script, cfg_of, day_bars
    from trading_system.app.backtest_m2_minute import MinuteValidationInputs, run_minute_validation
    from trading_system.app.validation import Window
    n = 29
    tpl = cfg_of(tmp_path, Script(), day_bars(DAY, days=n), params={"period_minutes": 60, "qty": D("1")})
    last = DAY + timedelta(days=n - 1)
    ws = (Window(1, date(2024, 1, 1), date(2024, 12, 31), DAY, DAY + timedelta(days=13)),
          Window(2, date(2024, 1, 2), date(2025, 1, 1), DAY + timedelta(days=14), last))
    params = tmp_path / "p.toml"
    params.write_text("x = 1\n", encoding="utf-8")
    res = run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "val", data_start=DAY,
                                                       windows=ws, perturbation_days=1, skip_variants=True,
                                                       params_file=params, limits_file=None))
    c = res["paper_period"]
    assert c["period"] == [DAY.isoformat(), last.isoformat()]
    assert c["windows"]["28"]["count"] == 2 and c["windows"]["28"]["p0"] == "0.000000"
    assert c["windows"]["60"]["count"] == 0 and c["windows"]["60"]["p0"] is None
    assert c["low_frequency"] is False and c["min_paper_days"] == 28 and c["rounds_total"] == n
    assert c["params_file"]["name"] == "p.toml" and c["limits_file"] is None
    saved = json.loads((tmp_path / "val" / "validation.json").read_text(encoding="utf-8"))
    assert saved["paper_period"] == c
    md = (tmp_path / "val" / "validation.md").read_text(encoding="utf-8")
    assert "p₀(28) 0.000000" in md and "일반" in md


# --- paper-review --market M2 연결 ---------------------------------------------------------------------------------

def _m2_root_with_strategy(tmp: Path) -> Path:
    from tests.scenario.test_t054_m2_supervisor import _root
    root = _root(tmp)
    params = root / "config" / "strategies" / "t-lf-v1.toml"
    params.parent.mkdir(parents=True, exist_ok=True)
    params.write_text('strategy = "t-lf"\nversion = 1\n[params]\nperiod_minutes = 60\n', encoding="utf-8")
    app = root / "config" / "m2" / "app.paper.toml"
    app.write_text(app.read_text(encoding="utf-8") + '\n[[strategies]]\nmodule = "x.y:Z"\n'
                   'params = "config/strategies/t-lf-v1.toml"\neval_times = []\n', encoding="utf-8")
    return root


def _seed(root: Path) -> None:
    from trading_system.domain.enums import Mode
    from trading_system.storage.repository import OrderIdentity, Repository
    db = root / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        with repo.tx():
            at = datetime.now(UTC) - timedelta(days=100)
            repo.audit(at, "PAPER_PERIOD_START", "user", "BN-PAPER-01", {"at": "가상"})
            repo.insert_reconciliation("BN-PAPER-01", "STARTUP", "MATCH", {}, at)
    finally:
        repo.close()


def test_review_cli_m2_without_class_file_shows_unclassified(tmp_path: Path) -> None:
    from trading_system.app.review_tool import review_main
    root = _m2_root_with_strategy(tmp_path)
    _seed(root)
    out = tmp_path / "r.md"
    assert review_main(["--mode", "PAPER", "--market", "M2", "--root", str(root), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "PAPER-1 기간 조건" in text and "t-lf:1" in text and "미분류" in text


def test_review_cli_m2_class_file_exception_strategy(tmp_path: Path) -> None:
    from trading_system.app.review_tool import review_main
    root = _m2_root_with_strategy(tmp_path)
    _seed(root)
    (root / "config" / "m2" / "paper-period.toml").write_text(
        '[[strategies]]\nstrategy = "t-lf"\nversion = 1\nlow_frequency = true\nlow_frequency_exception = true\n'
        'approved_by = "A-900"\n', encoding="utf-8")
    out = tmp_path / "r.md"
    assert review_main(["--mode", "PAPER", "--market", "M2", "--root", str(root), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "저빈도 예외(CP-038)" in text and "CP-038 D-238" in text and "연장 중" in text and "A-900" in text


def test_cli_validate_passes_params_and_limits_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """`backtest --market M2 --interval 1m --validate`: 분류 실행 조건 = --params·--limits 파일(기본 config/m2/limits.paper.toml).
    6창 실제 실행은 test_t053 test_cli_minute_validate_six_windows가 한다(분류 연속 실행 포함) — 여기서는 연결만."""
    from tests.scenario.test_t053_m2_minute_backtest import ROOT, example_params, minute_csv
    from trading_system.app import backtest_m2_minute as bmm
    from trading_system.app.backtest_m2_cli import backtest_m2_main
    seen: dict = {}

    def fake(inp):  # type: ignore[no-untyped-def]
        seen["inp"] = inp
        return {"verdict": {"verdict": "판단 불가"}}
    monkeypatch.setattr(bmm, "run_minute_validation", fake)
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", date(2021, 6, 1), 1)
    params = example_params(tmp_path / "p.toml", n=60)
    code = backtest_m2_main(["--interval", "1m", "--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
                             "--params", str(params), "--data", str(data), "--run-id", "v1", "--lookback", "31",
                             "--slippage", "0.001", "--validate", "--skip-variants", "--out", str(tmp_path / "out")], ROOT)
    assert code == 0, capsys.readouterr().out
    inp = seen["inp"]
    assert inp.params_file == params.resolve()
    assert inp.limits_file == ROOT / "config" / "m2" / "limits.paper.toml" and inp.limits_file.is_file()


@pytest.mark.parametrize("body,needle", [
    ('[[strategies]]\nstrategy = "t-lf"\nversion = 1\nlow_frequency = "yes"\napproved_by = "A-900"\n', "low_frequency"),
    ('[[strategies]]\nstrategy = "t-lf"\nversion = 1\nlow_frequency = true\n', "approved_by"),
    ('[[strategies]]\nstrategy = "t-lf"\nversion = 1\nlow_frequency = true\napproved_by = "A-900"\nx = 1\n', "모르는"),
])
def test_review_cli_m2_bad_class_file_falls_back_to_unclassified(tmp_path: Path, body: str, needle: str) -> None:
    from trading_system.app.review_tool import review_main
    root = _m2_root_with_strategy(tmp_path)
    _seed(root)
    (root / "config" / "m2" / "paper-period.toml").write_text(body, encoding="utf-8")
    out = tmp_path / "r.md"
    assert review_main(["--mode", "PAPER", "--market", "M2", "--root", str(root), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "미분류" in text and "분류 파일 오류" in text and needle in text
