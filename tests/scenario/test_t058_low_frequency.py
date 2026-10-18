"""CP-038 T-058 (A-121, 착수 A-122 ②) — 검증 실행기 저빈도 예외 경로. V-304~V-311, V-313, V-354~V-356.

네트워크 없음. 가상 거래 기록·합성 1분봉(임시 폴더·메모리)만 쓴다. 실제 데이터 파일(var/)은 읽지 않고, 어떤 실제 전략에도
판정을 실행하지 않는다(T-058 완료 기준). 시험 전용 대본 전략은 test_t053의 Script(후보 전략 아님).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t053_m2_minute_backtest import DAY, Script, cfg_of, day_bars, source_of, utc
from tests.support.m2_fakes import m2_limits
from trading_system.app import low_frequency as lf
from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2_minute import (
    MinuteValidationInputs, run_lf8_sample, run_minute_backtest, run_minute_validation,
)
from trading_system.app.low_frequency import (
    FAIL, LABEL, PASS, PASS_C7, UNDET, Declaration, Lf8Inputs, Lf8Sample, Lf8SymbolResult, LowFrequencyError,
    LowFrequencyInputs, TradeRec, WindowLF,
)
from trading_system.app.validation import Window
from trading_system.config.errors import ConfigError
from trading_system.domain.strategy import Bar

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "low-frequency.toml"
APPROVALS_OK = "| A-121 | 2026-10-18 | 변경 제안 승인 | [CP-038](changes/CP-038-low-frequency-evidence.md) v0.2 |\n"
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def settings() -> lf.LowFreqSettings:
    return lf.load_low_frequency_settings(CONFIG, approvals_text=APPROVALS_OK)


def decl(lo: str = "1", hi: str = "16", **kw: object) -> Declaration:
    base = dict(version=1, annual_min=D(lo), annual_max=D(hi), reasons=("보유 기간",), tried_combinations=12,
                search_period="2017-08-17~2024-12-31", choice_reason="시험", commit="abc1234", committed_at=T0)
    base.update(kw)
    return Declaration(**base)  # type: ignore[arg-type]


GENERAL_C2_ONLY = {"verdict": "판단 불가", "C-2": "판단 불가", "C-3": "충족", "C-4": "충족"}


def elig(**over: object) -> dict:
    kw: dict[str, object] = dict(market="M2", strategy_id="t-lf", version=1, declaration=decl(),
                                 declaration_problems=[], first_official_at=None, general=GENERAL_C2_ONLY,
                                 reproducible=True, lookahead_ok=True, limits_profile="research",
                                 limits_approved_by="A-093", windows=6, history_trades=60, history_days=3650)
    kw.update(over)
    return lf.check_eligibility(settings(), **kw)  # type: ignore[arg-type]


def tr(pnl: str, amt: str = "100", day: date = date(2021, 1, 1), inst: str = "BTCUSDT") -> TradeRec:
    return TradeRec(inst, day, day, D(pnl), D(amt))


def wins(*counts_and_pnl: tuple[int, str], hold: str | None = "0.10") -> list[WindowLF]:
    out = []
    for k, (n, pnl) in enumerate(counts_and_pnl, start=1):
        out.append(WindowLF(k, [tr(pnl, day=date(2019 + k, 1, 1 + i % 28)) for i in range(n)],
                            None if hold is None else D(hold), D("0")))
    return out


def lf8_ok(n_sym: int = 4, per: int = 30) -> dict:
    runs = {"BTCUSDT": {f"S{i}USDT": Lf8SymbolResult([tr("2", inst=f"S{i}USDT") for _ in range(per)],
                                                     [tr("1", inst=f"S{i}USDT") for _ in range(per)], D("0.1"))
                        for i in range(n_sym)}}
    return lf.judge_lf8(settings(), runs, deployed=[tr("3")], list_sha256="x" * 64)


def judge(**over: object) -> dict:
    """모든 LF 충족 기본 입력(창 6개 × 6건 = 36, 전체 이력 60건, 하락 창 1개)에서 한 항목씩 바꾼다."""
    w = wins(*[(6, "2")] * 6)
    w[2] = replace(w[2], hold_return=D("-0.30"))
    kw: dict[str, object] = dict(declaration=decl(), windows=w, history=[tr("2", day=date(2018, 1, 1 + i % 28))
                                                                         for i in range(60)],
                                 history_slip2=[tr("1") for _ in range(60)],
                                 variants={"p-qtyplus": [tr("1")], "p-qtyminus": [tr("1")]}, variants_run=True,
                                 lf8=lf8_ok(), c7=True)
    kw.update(over)
    return lf.judge_lf(settings(), **kw)  # type: ignore[arg-type]


# --- V-310 설정 파일·사전 고정 -----------------------------------------------------------------------------------

def test_v310_settings_load_values_and_hash() -> None:
    s = settings()
    assert (s.cp, s.cp_version, s.approved_by, s.markets) == ("CP-038", "v0.2", "A-121", ("M2",))
    assert (s.lf1_min_window_trades, s.lf2_min_history_trades, s.lf3_max_zero_windows) == (30, 50, 1)
    assert (s.lf3_positive_numerator, s.lf3_positive_denominator, s.lf4_k_min, s.lf4_k_ratio) == (2, 3, 3, D("0.05"))
    assert (s.lf8_role, s.lf8_min_trades, s.lf8_min_positive_symbol_ratio, s.lf8_delist_haircut) == \
        ("required", 100, D("0.50"), D("0.9"))
    assert (s.bootstrap_draws, s.bootstrap_quantile, s.c2_min_trades) == (10000, D("0.05"), 100)
    assert s.sha256 == hashlib.sha256(CONFIG.read_bytes()).hexdigest()


@pytest.mark.parametrize("edit,needle", [
    (lambda t: t.replace('approved_by = "A-121"\n', ""), "approved_by"),
    (lambda t: t.replace('approved_by = "A-121"', 'approved_by = "미정"'), "approved_by"),
    (lambda t: t + "\n[extra]\nx = 1\n", "모르는"),
    (lambda t: t.replace("lf1_min_window_trades = 30", "lf1_min_window_trades = \"30\""), "lf1_min_window_trades"),
    (lambda t: t.replace('role = "required"', 'role = "maybe"'), "role"),
    (lambda t: t.replace("lf2_min_history_trades = 50", ""), "lf2_min_history_trades"),
])
def test_v310_settings_without_approval_or_malformed_refused(tmp_path: Path, edit, needle: str) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "lf.toml"
    p.write_text(edit(CONFIG.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        lf.load_low_frequency_settings(p, approvals_text=APPROVALS_OK)
    assert needle in str(exc.value)


def test_v310_approval_must_be_recorded_for_cp038() -> None:
    with pytest.raises(ConfigError, match="approvals"):
        lf.load_low_frequency_settings(CONFIG, approvals_text="| A-121 | 다른 CP |\n")


def test_v310_command_line_has_no_lf_options(capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.backtest_m2_cli import backtest_m2_main
    with pytest.raises(SystemExit) as exc:
        backtest_m2_main(["--interval", "1m", "--strategy", "x:y", "--params", "p", "--data", "d", "--run-id", "r",
                          "--lookback", "5", "--slippage", "0.001", "--validate", "--lf1-min-window-trades", "20"],
                         ROOT)
    assert exc.value.code == 2 and "unrecognized" in capsys.readouterr().err


# --- E-1 선언 읽기 ---------------------------------------------------------------------------------------------

DOC = """# 전략 t-lf v1

## 저빈도 선언 (CP-038 E-1)
| 항목 | 값 |
|---|---|
| 선언 | 예 |
| 버전 | 1 |
| 예상 연간 거래 수 | 3~12 |
| 구조적 사유 | 보유 기간, 판정 주기 |
| 시도 조합 수 | 24 |
| 탐색 데이터 기간 | 2017-08-17~2024-12-31 |
| 고른 이유 | 기본값 |

## 다음
"""


def test_e1_parse_declaration_section() -> None:
    d, problems = lf.parse_declaration(DOC)
    assert problems == [] and d is not None
    assert (d.version, d.annual_min, d.annual_max, d.reasons) == (1, D("3"), D("12"), ("보유 기간", "판정 주기"))
    assert (d.tried_combinations, d.search_period, d.choice_reason) == (24, "2017-08-17~2024-12-31", "기본값")
    assert d.commit is None and d.committed_at is None          # 커밋 정보는 저장소에서 따로 읽는다


def test_e1_parse_absent_or_declined_or_malformed() -> None:
    assert lf.parse_declaration("# 전략 x v1\n") == (None, [])
    assert lf.parse_declaration(DOC.replace("| 선언 | 예 |", "| 선언 | 아니오 |")) == (None, [])
    d, problems = lf.parse_declaration(DOC.replace("| 구조적 사유 | 보유 기간, 판정 주기 |", "| 구조적 사유 | 운 |")
                                       .replace("3~12", "12~3"))
    assert d is None and any("구조적 사유" in p for p in problems) and any("연간 거래 수" in p for p in problems)
    d2, p2 = lf.parse_declaration(DOC.replace("| 시도 조합 수 | 24 |\n", ""))
    assert p2 == [] and d2 is not None and d2.tried_combinations is None      # LF-0 기록 없음 → LF-0 판단 불가(선언은 유효)


# --- V-304 적격 검사 -------------------------------------------------------------------------------------------

def test_v304_eligible_baseline() -> None:
    r = elig()
    assert r["eligible"] is True and r["result"] == "적격" and r["annual_trades"] == "6.0000"


def test_v304a_no_declaration_general_only_with_reason() -> None:
    r = elig(declaration=None)
    assert r["eligible"] is False and r["result"].startswith("부적격") and r["items"]["E-1"] == "미충족"
    assert any("선언 없음" in x for x in r["reasons"])
    r2 = elig(declaration=None, declaration_problems=["구조적 사유 형식"])
    assert r2["eligible"] is False and any("구조적 사유" in x for x in r2["reasons"])


def test_v304b_declaration_after_official_run_or_uncommitted() -> None:
    r = elig(first_official_at=T0 - timedelta(seconds=1))
    assert r["eligible"] is False and any("사후 선언" in x for x in r["reasons"])
    assert elig(first_official_at=T0 + timedelta(days=1))["eligible"] is True
    r2 = elig(declaration=decl(commit=None, committed_at=None))
    assert r2["eligible"] is False and any("커밋" in x for x in r2["reasons"])
    r3 = elig(declaration=decl(version=2))
    assert r3["eligible"] is False and any("버전" in x for x in r3["reasons"])


def test_v304c_annual_trades_boundary_16_6_vs_16_7() -> None:
    assert elig(history_trades=166, history_days=3650, declaration=decl("1", "20"))["eligible"] is True
    r = elig(history_trades=167, history_days=3650, declaration=decl("1", "20"))
    assert r["eligible"] is False and r["items"]["E-2"] == "미충족" and r["annual_trades"] == "16.7000"
    # M1 7창 기준(100 ÷ 7 = 14.29)도 같은 식 — 시장 적용은 별도(Q-3)
    assert lf.e2_threshold(settings(), 7) == D(100) / D(7)


def test_v304d_actual_outside_declared_range_invalidates() -> None:
    r = elig(declaration=decl("8", "12"))           # 실측 6.0
    assert r["eligible"] is False and r["items"]["E-1"] == "미충족" and any("선언 범위" in x for x in r["reasons"])


def test_v304e_other_criterion_failing_does_not_open_path() -> None:
    for general in ({**GENERAL_C2_ONLY, "C-3": "미충족"}, {**GENERAL_C2_ONLY, "C-4": "미충족"},
                    {**GENERAL_C2_ONLY, "C-4": "미실행"}):
        r = elig(general=general)
        assert r["eligible"] is False and r["items"]["E-3"] == "미충족"
        assert r["result"] == "미충족(예외 경로 열지 않음)"
    assert elig(reproducible=False)["items"]["E-3"] == "미충족"
    assert elig(lookahead_ok=False)["items"]["E-3"] == "미충족"
    r = elig(general={"verdict": "통과(과대 추정 가능)", "C-2": "충족", "C-3": "충족", "C-4": "충족"})
    assert r["eligible"] is False and r["items"]["E-3"] == "해당 없음(C-2 충족 — 일반 경로)"


def test_v304f_verification_strategy_excluded() -> None:
    for sid in ("m2-verify", "paper0-verify", "paper0-verify-x"):
        r = elig(strategy_id=sid)
        assert r["eligible"] is False and r["result"] == "제외(검증용 전략, D-161)"


def test_v304g_unapproved_limits_profile_not_eligible() -> None:
    for prof, appr in ((None, None), ("paper0", None)):
        r = elig(limits_profile=prof, limits_approved_by=appr)
        assert r["eligible"] is False and r["result"] == "적격 불가(미승인 한도 프로필, E-5)"


def test_q3_market_not_in_settings_general_only() -> None:
    r = elig(market="M1", windows=7)
    assert r["eligible"] is False and any("Q-3" in x for x in r["reasons"])


def test_pre_eligibility_without_history_defers_e2() -> None:
    r = elig(history_trades=None, history_days=None)
    assert r["eligible"] is False and r["pre_ok"] is True and r["items"]["E-2"] == "미계산"


# --- V-305 거래 수·창 분포 ----------------------------------------------------------------------------------------

def test_v305_window_and_history_trade_boundaries() -> None:
    w29 = wins((5, "2"), (5, "2"), (5, "2"), (5, "2"), (5, "2"), (4, "2"))
    w29[2] = replace(w29[2], hold_return=D("-0.3"))
    r = judge(windows=w29)
    assert r["items"]["LF-1"]["status"] == UNDET and r["verdict"] == UNDET
    w30 = wins(*[(5, "2")] * 6)
    w30[2] = replace(w30[2], hold_return=D("-0.3"))
    assert judge(windows=w30)["items"]["LF-1"]["status"] == "충족"
    r = judge(history=[tr("2") for _ in range(49)])
    assert r["items"]["LF-2"]["status"] == UNDET and r["verdict"] == UNDET
    assert judge(history=[tr("2") for _ in range(50)])["items"]["LF-2"]["status"] == "충족"


def test_v305_zero_windows_and_positive_windows() -> None:
    def mk(spec: list[tuple[int, str]]) -> list[WindowLF]:
        w = wins(*spec)
        w[0] = replace(w[0], hold_return=D("-0.3"))
        return w
    assert judge(windows=mk([(8, "2"), (0, "0"), (8, "2"), (8, "2"), (8, "2"), (8, "2")]))["items"]["LF-3"]["status"] \
        == "충족"
    r = judge(windows=mk([(8, "2"), (0, "0"), (0, "0"), (8, "2"), (8, "2"), (8, "2")]))
    assert r["items"]["LF-3"]["status"] == UNDET and r["verdict"] == UNDET        # V-305: 0건 창 2개 → 판단 불가
    r = judge(windows=mk([(6, "2"), (6, "2"), (6, "2"), (6, "-1"), (6, "-1"), (6, "-1")]))
    assert r["items"]["LF-3"]["status"] == "미충족" and r["items"]["LF-3"]["required"] == 4 and r["verdict"] == FAIL
    assert judge(windows=mk([(6, "2"), (6, "2"), (6, "2"), (6, "2"), (6, "-1"), (6, "-1")]))["items"]["LF-3"][
        "status"] == "충족"
    r = judge(windows=mk([(8, "2"), (8, "2"), (8, "2"), (8, "-1"), (0, "0"), (8, "2")]))      # m = 5 → 기준 4
    assert r["items"]["LF-3"]["required"] == 4 and r["items"]["LF-3"]["status"] == "충족"


# --- V-306 상위 k 제외 ------------------------------------------------------------------------------------------

def test_v306_k_formula() -> None:
    s = settings()
    assert [lf.top_k(s, n) for n in (50, 61, 80, 10)] == [3, 4, 4, 3]


def test_v306_ties_by_earlier_entry_and_rest_sum_boundary() -> None:
    s = settings()
    d0 = date(2020, 1, 1)
    logs = [(D("0.5"), d0 + timedelta(days=3)), (D("0.5"), d0 + timedelta(days=1)), (D("0.5"), d0 + timedelta(days=2)),
            (D("0.5"), d0), (D("0.0001"), d0 + timedelta(days=9)), (D("-0.0002"), d0 + timedelta(days=10))]
    r = lf.lf4(s, logs)
    assert r["k"] == 3 and r["removed_entry_days"] == [d0.isoformat(), (d0 + timedelta(days=1)).isoformat(),
                                                        (d0 + timedelta(days=2)).isoformat()]
    assert r["rest_sum"] == "0.499900" and r["status"] == "충족"
    neg = [(D("0.5"), d0)] * 3 + [(D("0.0001"), d0), (D("-0.0002"), d0)]
    assert lf.lf4(s, neg)["status"] == "미충족"                                  # 나머지 −0.0001 < 0
    zero = [(D("0.5"), d0)] * 3 + [(D("0.0001"), d0), (D("-0.0001"), d0)]
    assert lf.lf4(s, zero)["status"] == "충족"                                   # 나머지 = 0 → 충족
    assert lf.lf4(s, neg)["top_share"] is not None


def test_v306_size_invariant() -> None:
    base = judge()
    doubled = judge(history=[TradeRec(t.instrument, t.entry_day, t.exit_day, t.pnl * 2, t.entry_amount * 2)
                             for t in [tr("2", day=date(2018, 1, 1 + i % 28)) for i in range(60)]])
    assert base["items"]["LF-4"] == doubled["items"]["LF-4"] and base["verdict"] == doubled["verdict"]


# --- V-307 하락 창 방어 -----------------------------------------------------------------------------------------

def test_v307_down_window_defense() -> None:
    s = settings()
    up = WindowLF(1, [tr("2")], D("0.1"), D("0"))
    assert lf.lf5(s, [up, replace(up, k=2)])["status"] == UNDET                   # 하락 창 0개
    # 보유 −20% = ln(0.8): 거래 r = −20% 1건이면 같음 → 미충족, −10%면 >, −30%면 <
    eq = WindowLF(2, [tr("-20")], D("-0.2"), D("-5"))
    assert lf.lf5(s, [up, eq])["status"] == "미충족"
    assert lf.lf5(s, [up, replace(eq, trades=[tr("-10")])])["status"] == "충족"
    r = lf.lf5(s, [up, replace(eq, trades=[tr("-30")])])
    assert r["status"] == "미충족" and r["windows"][0]["open_pnl"] == "-5"        # 미청산 평가 손익 별도 표시
    assert lf.lf5(s, [up, replace(eq, hold_return=None)])["status"] == UNDET      # 보유 수익률 없음 → 판단 불가


# --- V-308 전체 이력 C-3·C-4 -------------------------------------------------------------------------------------

def test_v308_full_history_lf6_lf7() -> None:
    assert judge(history_slip2=[tr("-1")] * 60)["items"]["LF-6"]["status"] == "미충족"
    r = judge(variants={"p-qtyplus": [tr("1")], "p-qtyminus": [tr("-1")]})
    assert r["items"]["LF-7"]["status"] == "미충족" and r["verdict"] == FAIL
    assert judge(variants={"p-qtyplus": [tr("1")], "p-qtyminus": []})["items"]["LF-7"]["status"] == "미충족"
    assert judge(variants={}, variants_run=True)["items"]["LF-7"]["status"] == UNDET


def test_v308_history_run_never_passes_2026_rows(tmp_path: Path) -> None:
    seen: list[datetime] = []
    bars = day_bars(date(2025, 12, 31), days=2)                        # 2026-01-01 봉 포함 데이터
    src = source_of(bars)

    def spy(start: datetime, stop: datetime):  # type: ignore[no-untyped-def]
        out = src(start, stop)
        seen.extend(b.at for b in out)  # type: ignore[misc]
        return out
    tpl = replace(cfg_of(tmp_path, Script(), bars, params={"period_minutes": 60, "qty": D("1")},
                         start=date(2025, 12, 31), end=date(2025, 12, 31)), source=spy)
    from trading_system.app.backtest_m2_minute import lf_history_runs
    runs = lf_history_runs(tpl, start=date(2025, 12, 31), end=date(2025, 12, 31), out_dir=tmp_path / "h",
                           slippage_base=D("0.001"), multiplier=2, variants=[])
    assert set(runs) == {"base", "slip2x"} and seen and max(seen) < datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(BacktestError, match="최종 확인"):
        lf_history_runs(tpl, start=date(2025, 12, 31), end=date(2026, 1, 1), out_dir=tmp_path / "h2",
                        slippage_base=D("0.001"), multiplier=2, variants=[])


# --- V-309 판정 조합·문구 ----------------------------------------------------------------------------------------

def test_v309_composite_wording() -> None:
    assert judge()["verdict"] == PASS_C7 == "통과(저빈도 예외, CP-038 — 과대 추정 가능)"
    assert judge(c7=False)["verdict"] == PASS == "통과(저빈도 예외, CP-038)"
    lf4_fail = [tr("50", day=date(2018, 1, 1))] * 3 + [tr("-1", day=date(2018, 2, 1))] * 57
    r = judge(history=lf4_fail)
    assert r["items"]["LF-4"]["status"] == "미충족" and r["verdict"] == FAIL == "미충족(저빈도 예외 기준)"
    assert judge(windows=wins(*[(6, "2")] * 6))["verdict"] == UNDET               # 하락 창 없음 → LF-5 판단 불가
    assert judge(declaration=decl(tried_combinations=None))["items"]["LF-0"]["status"] == UNDET


def test_v309_general_pass_unchanged_and_label_required() -> None:
    r = elig(general={"verdict": "통과(과대 추정 가능)", "C-2": "충족", "C-3": "충족", "C-4": "충족"})
    assert r["eligible"] is False
    o = {"low_frequency": {"applied": True}, "verdict": {"verdict": PASS_C7}}
    good = f"# 판정표\n\n- **{LABEL}** — C-7 생존편향 표시 옆\n\n- **판정: {PASS_C7}** ({LABEL})\n"
    assert lf.table_valid(good, o) is True
    assert lf.table_valid(good.replace(f"- **{LABEL}** — C-7 생존편향 표시 옆\n", ""), o) is False
    assert lf.table_valid(good.replace(f" ({LABEL})", "").replace(PASS_C7, "통과"), o) is False
    assert lf.table_valid("# 일반\n", {"verdict": {"verdict": "판단 불가"}}) is True


# --- V-311 부트스트랩 참고 ---------------------------------------------------------------------------------------

def test_v311_bootstrap_reference_seeded_and_not_in_verdict() -> None:
    s = settings()
    logs = [D("0.05"), D("-0.02"), D("0.01"), D("-0.03"), D("0.08")] * 8
    a = lf.bootstrap_reference(s, logs, lf.seed_of("t-lf", 1))
    b = lf.bootstrap_reference(s, logs, lf.seed_of("t-lf", 1))
    c = lf.bootstrap_reference(s, logs, lf.seed_of("t-lf", 2))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) and a != c
    assert a["seed"] == "cp038:t-lf-v1" and a["draws"] == 10000 and "독립" in a["limits"]
    assert 0 <= D(a["p_plus"]) <= 1
    items = judge()["items"]
    assert lf.compose(items, c7=True) == lf.compose({**items, "LF-R": {"status": "미충족"}}, c7=True)


# --- V-355·V-356 LF-8 기준·불변 ---------------------------------------------------------------------------------

def _runs(spec: dict[str, tuple[int, str, str]]) -> dict:
    return {"BTCUSDT": {sym: Lf8SymbolResult([tr(p, inst=sym) for _ in range(n)], [tr(p2, inst=sym) for _ in range(n)],
                                             D("0.1")) for sym, (n, p, p2) in spec.items()}}


def test_v355_lf8_boundaries() -> None:
    s = settings()
    r = lf.judge_lf8(s, _runs({"AUSDT": (50, "1", "1"), "BUSDT": (49, "1", "1")}), deployed=[], list_sha256="h")
    assert r["items"]["LF-8.1"] == "미충족" and r["status"] == "미충족"
    r = lf.judge_lf8(s, _runs({"AUSDT": (50, "1", "1"), "BUSDT": (50, "1", "1")}), deployed=[], list_sha256="h")
    assert r["items"]["LF-8.1"] == "충족" and r["status"] == "충족"
    zero = {"BTCUSDT": {"AUSDT": Lf8SymbolResult([tr("25"), tr("-20")] * 25, [tr("1")] * 50, D("0")),
                        "BUSDT": Lf8SymbolResult([tr("25"), tr("-20")] * 25, [tr("1")] * 50, D("0"))}}
    assert lf.judge_lf8(s, zero, deployed=[], list_sha256="h")["items"]["LF-8.2"] == "미충족"   # 평균 0
    r = lf.judge_lf8(s, _runs({"AUSDT": (50, "1", "-1"), "BUSDT": (50, "1", "-1")}), deployed=[], list_sha256="h")
    assert r["items"]["LF-8.2"] == "미충족"                                       # 2배 슬리피지 음수
    pos49 = {f"P{i}USDT": (1, "5", "5") for i in range(49)} | {f"N{i}USDT": (1, "-1", "-1") for i in range(51)}
    assert lf.judge_lf8(s, _runs(pos49), deployed=[], list_sha256="h")["items"]["LF-8.3"] == "미충족"
    pos50 = {f"P{i}USDT": (1, "5", "5") for i in range(50)} | {f"N{i}USDT": (1, "-1", "-1") for i in range(50)}
    assert lf.judge_lf8(s, _runs(pos50), deployed=[], list_sha256="h")["items"]["LF-8.3"] == "충족"
    top = {"AUSDT": (50, "40", "40"), "BUSDT": (25, "-1", "-1"), "CUSDT": (25, "-1", "-1")}
    r = lf.judge_lf8(s, _runs(top), deployed=[], list_sha256="h")
    assert r["items"]["LF-8.4"] == "미충족" and r["items"]["LF-8.2"] == "충족"


def test_v355_lf8_reference_role_does_not_change_verdict() -> None:
    s = replace(settings(), lf8_role="reference")
    bad = lf.judge_lf8(s, _runs({"AUSDT": (10, "-1", "-1")}), deployed=[], list_sha256="h")
    good = lf.judge_lf8(s, _runs({"AUSDT": (60, "1", "1"), "BUSDT": (60, "1", "1")}), deployed=[], list_sha256="h")
    a = lf.judge_lf(s, **{**_judge_kwargs(), "lf8": bad})
    b = lf.judge_lf(s, **{**_judge_kwargs(), "lf8": good})
    assert a["verdict"] == b["verdict"] == PASS_C7 and a["items"]["LF-8"]["role"] == "reference"


def _judge_kwargs() -> dict:
    w = wins(*[(6, "2")] * 6)
    w[2] = replace(w[2], hold_return=D("-0.30"))
    return dict(declaration=decl(), windows=w, history=[tr("2") for _ in range(60)],
                history_slip2=[tr("1") for _ in range(60)], variants={"p-qtyplus": [tr("1")]}, variants_run=True,
                lf8=None, c7=True)


def test_v355_reference_ratio_and_list_hash_shown() -> None:
    r = lf8_ok()
    assert r["list_sha256"] == "x" * 64 and r["reference"]["deployed_over_sample"] is not None
    assert r["reference"]["symbols"]["S0USDT"]["hold_return"] == "0.1"


def test_v356_lf8_does_not_fill_lf1() -> None:
    w = wins((5, "2"), (5, "2"), (5, "2"), (5, "2"), (5, "2"), (4, "2"))
    w[2] = replace(w[2], hold_return=D("-0.3"))
    r = judge(windows=w)
    assert r["items"]["LF-8"]["status"] == "충족" and r["items"]["LF-1"]["status"] == UNDET and r["verdict"] == UNDET


def test_v356_minute_strategy_without_sample_data_undetermined() -> None:
    r = lf.judge_lf8(settings(), None, deployed=[], list_sha256=None, no_data_reason=lf.NO_MINUTE_SAMPLE)
    assert r["status"] == UNDET and "표본 데이터 없음" in r["reason"]
    assert judge(lf8=r)["verdict"] == UNDET


def test_v356_param_or_settings_change_after_result_requires_new_version(tmp_path: Path) -> None:
    led = tmp_path / "led.jsonl"
    lf.check_ledger(led, "t-lf", 1, "p1", "s1")
    lf.record_ledger(led, "t-lf", 1, "p1", "s1", "v0.2")
    lf.check_ledger(led, "t-lf", 1, "p1", "s1")                      # 같은 입력 재실행 허용
    with pytest.raises(LowFrequencyError, match="새 버전"):
        lf.check_ledger(led, "t-lf", 1, "p2", "s1")                  # 파라미터 변경 (D-245 ②, D-237 (4))
    with pytest.raises(LowFrequencyError, match="새 버전"):
        lf.check_ledger(led, "t-lf", 1, "p1", "s2")                  # 설정(수치) 변경 (D-237 (2))
    lf.check_ledger(led, "t-lf", 2, "p2", "s2")                      # 새 버전은 허용


# --- V-354 LF-8 평가 구간·상장폐지 (실제 1분봉 실행기, 가상 봉) ----------------------------------------------------

def test_round_trip_records_match_engine_trades(tmp_path: Path) -> None:
    out = run_minute_backtest(cfg_of(tmp_path, Script(always=True), day_bars(), n=60))
    assert out.result.trades and [r.pnl for r in out.trade_records] == [t.pnl for t in out.result.trades]
    assert all(r.entry_amount > 0 for r in out.trade_records)


def _two_days(first: date) -> list[Bar]:
    return day_bars(first, days=2)


def test_v354_entries_only_in_list_years(tmp_path: Path) -> None:
    d1 = date(2024, 12, 31)
    bars = _two_days(d1)
    tpl = cfg_of(tmp_path, Script(enter_at="01:00", exit_at="02:00"), bars, params={"period_minutes": 60,
                                                                                  "qty": D("1")})
    sample = Lf8Sample(source=source_of(bars), years=frozenset({2024}), data_start=d1, data_end=d1 + timedelta(days=1))
    res = run_lf8_sample(tpl, settings(), alias="BTCUSDT", symbol="XUSDT", sample=sample, history_end=date(2025, 12, 31),
                         slippage=D("0.001"), out_dir=tmp_path / "s")
    assert [t.entry_day for t in res.trades] == [d1] and res.blocked_entries >= 1   # 2025(목록 밖) 진입 0


def test_v354_holding_kept_after_list_year_until_exit(tmp_path: Path) -> None:
    d1 = date(2024, 12, 31)
    bars = _two_days(d1)
    tpl = cfg_of(tmp_path, Script(enter_at="23:00", exit_at="01:00"), bars, params={"period_minutes": 60,
                                                                                  "qty": D("1")})
    sample = Lf8Sample(source=source_of(bars), years=frozenset({2024}), data_start=d1, data_end=d1 + timedelta(days=1))
    res = run_lf8_sample(tpl, settings(), alias="BTCUSDT", symbol="XUSDT", sample=sample, history_end=date(2025, 12, 31),
                         slippage=D("0.001"), out_dir=tmp_path / "s")
    assert [(t.entry_day, t.exit_day) for t in res.trades] == [(d1, d1 + timedelta(days=1))]


def test_v354_data_end_while_holding_closed_at_last_close_times_haircut(tmp_path: Path) -> None:
    d1 = date(2024, 6, 3)
    bars = day_bars(d1, days=1)
    tpl = cfg_of(tmp_path, Script(enter_at="01:00", exit_at="23:59"), bars, params={"period_minutes": 60,
                                                                                  "qty": D("1")})
    sample = Lf8Sample(source=source_of(bars), years=frozenset({2024}), data_start=d1, data_end=d1)
    res = run_lf8_sample(tpl, settings(), alias="BTCUSDT", symbol="XUSDT", sample=sample, history_end=date(2025, 12, 31),
                         slippage=D("0.001"), out_dir=tmp_path / "s")
    assert len(res.trades) == 1 and res.delisted == 1
    t = res.trades[0]
    assert t.exit_day == d1 and t.entry_amount == D("100.10")
    # 받은 수량 = 1 − 수수료 0.1% = 0.999, 청산 = 0.999 × 100 × 0.9 × (1 − 0.001)
    assert t.pnl == (D("0.999") * D("100.00") * D("0.9") * (1 - D("0.001")) - D("100.10"))


def test_v354_holding_at_history_end_excluded(tmp_path: Path) -> None:
    d1 = date(2025, 12, 31)
    bars = day_bars(d1, days=1)
    tpl = cfg_of(tmp_path, Script(enter_at="01:00", exit_at="23:59"), bars, params={"period_minutes": 60,
                                                                                  "qty": D("1")})
    sample = Lf8Sample(source=source_of(bars), years=frozenset({2025}), data_start=d1, data_end=d1)
    res = run_lf8_sample(tpl, settings(), alias="BTCUSDT", symbol="XUSDT", sample=sample, history_end=d1,
                         slippage=D("0.001"), out_dir=tmp_path / "s")
    assert res.trades == [] and res.delisted == 0 and res.open_at_end == 1


# --- 통합: 판정표 (가상 1분봉) ------------------------------------------------------------------------------------

DAYS = [DAY + timedelta(days=i) for i in range(6)]
BASES = [D("100"), D("100"), D("100"), D("60"), D("60"), D("60")]


def lf_bars() -> list[Bar]:
    """날마다 00:00~01:59 = 기준가, 02:00~23:59 = 기준가 × 1.03 (01:00 진입·02:00 청산이 이익). 4일째부터 60(하락 창)."""
    out: list[Bar] = []
    first = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC) - timedelta(minutes=30)
    for i in range(30 + 1440 * len(DAYS)):
        at = first + timedelta(minutes=i)
        k = max(0, (at.date() - DAY).days) if at.date() >= DAY else 0
        p = BASES[min(k, len(BASES) - 1)]
        if at.date() >= DAY and at.hour >= 2:
            p = (p * D("1.03")).quantize(D("0.01"))
        out.append(Bar(at, p, p + D("0.5"), p - D("0.5"), p, D("1000")))
    return out


def lf_inputs(tmp: Path, s: lf.LowFreqSettings, bars: list[Bar], *, lf8: Lf8Inputs | None) -> LowFrequencyInputs:
    return LowFrequencyInputs(settings=s, declaration=decl("1", "1000"), declaration_problems=[], first_official_at=None,
                              history_start=DAYS[0], history_end=DAYS[-1], ledger=tmp / "ledger.jsonl", lf8=lf8,
                              lf8_no_data_reason="" if lf8 else lf.NO_SAMPLE_WIRING, params_sha256="p")


def validation_of(tmp: Path, low: LowFrequencyInputs | None, bars: list[Bar]) -> dict:
    tpl = replace(cfg_of(tmp, Script(enter_at="01:00", exit_at="02:00"), bars,
                         params={"period_minutes": 60, "qty": D("1")}),
                  account_limits=m2_limits(profile="research", approved_by="A-093"))
    ws = tuple(Window(k + 1, date(2022, 1, 1), date(2024, 12, 31), DAYS[2 * k], DAYS[2 * k + 1]) for k in range(3))
    return run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp / "val", data_start=DAY - timedelta(1),
                                                        windows=ws, perturbation_days=1, low_frequency=low))


def test_integration_low_frequency_pass_on_virtual_data(tmp_path: Path) -> None:
    s = replace(settings(), c2_min_trades=10 ** 6, lf1_min_window_trades=6, lf2_min_history_trades=6,
                lf8_min_trades=12)                                   # 시험 전용 축소 — 실제 수치는 설정 파일(V-310)
    bars = lf_bars()
    samples = {sym: Lf8Sample(source=source_of(bars), years=frozenset({2025}), data_start=DAY, data_end=DAYS[-1])
               for sym in ("XUSDT", "YUSDT")}
    o = validation_of(tmp_path, lf_inputs(tmp_path, s, bars, lf8=Lf8Inputs(list_sha256="h" * 64, aliases=("BTCUSDT",),
                                                                      samples=samples)), bars)
    v = o["verdict"]
    assert v["general_verdict"] == "판단 불가" and v["C-2"] == "판단 불가"        # 일반 판정은 그대로 기록
    assert v["verdict"] == PASS_C7, json.dumps(o["low_frequency"], ensure_ascii=False)[:2000]
    low = o["low_frequency"]
    assert low["applied"] and low["eligibility"]["result"] == "적격"
    assert low["cp_version"] == "v0.2" and low["settings_sha256"] == s.sha256 and low["lf8"]["list_sha256"] == "h" * 64
    assert low["items"]["LF-5"]["status"] == "충족" and low["history"]["trades"] == 6
    md = (tmp_path / "val" / "validation.md").read_text(encoding="utf-8")
    assert LABEL in "\n".join(md.splitlines()[:4]) and f"**판정: {PASS_C7}** ({LABEL})" in md
    assert s.sha256 in md and "v0.2" in md and "LF-R" in md and "판정 제외" in md
    assert lf.table_valid(md, o)
    rec = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rec[0]["strategy"] == "t-script-1m" and rec[0]["settings_sha256"] == s.sha256


def test_integration_real_settings_e2_fails_general_verdict_kept(tmp_path: Path) -> None:
    bars = lf_bars()
    o = validation_of(tmp_path, lf_inputs(tmp_path, settings(), bars, lf8=None), bars)
    assert o["verdict"]["verdict"] == "판단 불가" and "general_verdict" not in o["verdict"]
    low = o["low_frequency"]
    assert low["applied"] is False and low["eligibility"]["items"]["E-2"] == "미충족"   # 연 365건 ≥ 100 ÷ 3
    md = (tmp_path / "val" / "validation.md").read_text(encoding="utf-8")
    assert "열지 않음" in md and "E-2" in md


def test_v313_general_path_bytes_unchanged(tmp_path: Path) -> None:
    """선언 없는 전략(low_frequency=None): T-053 시험과 같은 입력의 판정표·판정 JSON이 이 작업 전과 같다.
    기대 해시는 변경 전 코드(main 8a2b026)로 같은 입력을 실행해 얻은 값(실행 시간 seconds 칸 제외)."""
    bars = [b for b in day_bars(DAY, days=2) if b.at != utc(6, 0)]
    tpl = cfg_of(tmp_path, Script(always=True), bars, params={"period_minutes": 60, "qty": D("1")})
    ws = (Window(1, date(2024, 1, 1), date(2024, 12, 31), DAY, DAY),
          Window(2, date(2024, 1, 2), date(2025, 1, 1), DAY + timedelta(days=1), DAY + timedelta(days=1)))
    run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "val", data_start=date(2024, 6, 1),
                                                 windows=ws, perturbation_days=1))
    md = (tmp_path / "val" / "validation.md").read_bytes()
    o = json.loads((tmp_path / "val" / "validation.json").read_text(encoding="utf-8"))
    for w in o["windows"]:
        w.pop("seconds")
    assert hashlib.sha256(md).hexdigest() == "6f078b3372acfa7517d7c4a1dfd5b827761e2654c03bcade7938b513705eceb9"
    assert hashlib.sha256(json.dumps(o, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == \
        "d78d10169edc263f0f26def2cb2a3a5c2280214f0a1a1233871d69a98336b03a"


# --- 명령줄 조립 (저장소 읽기 — 가짜 git) -------------------------------------------------------------------------

def _repo(tmp: Path, doc: str | None = DOC, official: bool = False) -> Path:
    (tmp / "config").mkdir(parents=True)
    (tmp / "config" / "low-frequency.toml").write_bytes(CONFIG.read_bytes())
    (tmp / "docs" / "strategies" / "lf8").mkdir(parents=True)
    (tmp / "docs" / "approvals.md").write_text(APPROVALS_OK, encoding="utf-8")
    (tmp / "docs" / "strategies" / "lf8" / "lf8-list.csv").write_bytes(
        (ROOT / "docs" / "strategies" / "lf8" / "lf8-list.csv").read_bytes())
    if doc is not None:
        (tmp / "docs" / "strategies" / "t-lf").mkdir()
        (tmp / "docs" / "strategies" / "t-lf" / "strategy.md").write_text(doc, encoding="utf-8")
    if official:
        (tmp / "docs" / "strategies" / "t-lf" / "validation-v1.md").write_text("x", encoding="utf-8")
    return tmp


class FakeGit:
    def __init__(self, log: str = "abc1234|2026-10-01T00:00:00+00:00", dirty: str = "", added: str = "") -> None:
        self.log, self.dirty, self.added = log, dirty, added

    def __call__(self, root: Path, args: list[str]) -> str | None:
        if args[:2] == ["status", "--porcelain"]:
            return self.dirty
        if "--diff-filter=A" in args:
            return self.added
        return self.log


def _build(tmp: Path, git: FakeGit) -> LowFrequencyInputs | None:
    from trading_system.app.backtest_m2_cli import build_low_frequency_inputs
    return build_low_frequency_inputs(tmp, "t-lf", 1, {"period_minutes": 60}, out=tmp / "out",
                                      final_from=date(2026, 1, 1), data_start=date(2017, 8, 17), lookback=60, git=git)


def test_cli_builder_reads_declaration_and_commit(tmp_path: Path) -> None:
    low = _build(_repo(tmp_path), FakeGit())
    assert low is not None and low.declaration is not None
    assert low.declaration.commit == "abc1234" and low.declaration.committed_at == T0
    assert (low.history_start, low.history_end) == (date(2017, 8, 18), date(2025, 12, 31))
    assert low.first_official_at is None and low.lf8 is None and "T-068" in low.lf8_no_data_reason
    assert low.ledger == tmp_path / "out" / "low-frequency-ledger.jsonl"


def test_cli_builder_no_doc_or_no_declaration_is_general_path(tmp_path: Path) -> None:
    assert _build(_repo(tmp_path / "a", doc=None), FakeGit()) is None
    assert _build(_repo(tmp_path / "b", doc="# 전략 t-lf v1\n"), FakeGit()) is None


def test_cli_builder_uncommitted_or_untracked_official_table_conservative(tmp_path: Path) -> None:
    low = _build(_repo(tmp_path / "a"), FakeGit(dirty=" M docs/strategies/t-lf/strategy.md"))
    assert low is not None and low.declaration.commit is None                 # 작업 트리 변경 → 커밋 확인 불가
    low2 = _build(_repo(tmp_path / "b", official=True), FakeGit(added=""))
    assert low2 is not None and low2.first_official_at == lf.UNKNOWN_PAST     # 커밋 안 된 공식 판정표 → 사후로 간주
    low3 = _build(_repo(tmp_path / "c", official=True), FakeGit(added="2026-09-01T00:00:00+00:00"))
    assert low3 is not None and low3.first_official_at == datetime(2026, 9, 1, tzinfo=UTC)


def test_cli_builder_list_hash_mismatch_lf8_undetermined(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "docs" / "strategies" / "lf8" / "lf8-list.csv").write_text("year,symbol\n", encoding="utf-8")
    low = _build(root, FakeGit())
    assert low is not None and low.lf8 is None and "해시" in low.lf8_no_data_reason
