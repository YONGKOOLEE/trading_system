"""T-020 진입점 `python -m trading_system.app` (임시 저장소 루트, 가상 설정·가상 데이터). 네트워크 연결 없음
(SNTP 서버는 루프백의 닫힌 포트 → 시계 판정 불가로 K6, 키움 접속 경로는 승인 플래그 전 거부).
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_system.app.main import load_daily_csv, main, run_main
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import Mode
from trading_system.storage.repository import Repository

REPO = Path(__file__).resolve().parents[2]

LIMITS = """account = "{acct}"
[limits]
l1_max_loss_ratio = "0.2"
l2_max_symbol_ratio = "0.2"
l3_max_total_ratio = "0.5"
l4_daily_loss_ratio = "0.03"
l5_daily_new_orders = 10
l6_max_positions = 5
l7_max_order_amount = "3000000"
l10_leverage_total_ratio = "none"
warn_usage_ratio = "0.8"
[time]
order_window_start = "09:05"
order_window_end = "15:15"
daily_reference_time = "08:30"
report_time = "16:00"
"""
MARKET = """[gate]
quote_max_age_seconds = 60
price_limit_ratio = "0.30"
[costs]
commission_rate = "0"
sell_tax_rate = "0"
[alerts]
reject_surge_count = 10
reject_surge_minutes = 5
"""
APP = """[app]
account = "{acct}"
loop_seconds = "0.05"
reconcile_minutes = 1
sntp_servers = ["127.0.0.1:9"]
{paper}
[[instruments]]
code = "005930"
kind = "STOCK"
leverage = "1"
lot_size = "1"
[[strategies]]
module = "tests.support.sample_strategies:CrossStrategy"
params = "config/strategies/t-cross.toml"
eval_times = ["09:05"]
"""


def make_root(tmp: Path, mode: Mode, paper_approved: bool | None = None) -> Path:
    root = tmp / "root"
    (root / "config" / "calendar").mkdir(parents=True)
    (root / "config" / "strategies").mkdir()
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    m = mode.value.lower()
    acct = "KR-BT-01" if mode is Mode.BACKTEST else "KR-PAPER-01"
    (root / "config" / f"limits.{m}.toml").write_text(LIMITS.format(acct=acct), encoding="utf-8")
    (root / "config" / f"market.{m}.toml").write_text(MARKET, encoding="utf-8")
    paper = "" if paper_approved is None else f"[paper]\nconnection_approved = {str(paper_approved).lower()}"
    (root / "config" / f"app.{m}.toml").write_text(APP.format(acct=acct, paper=paper), encoding="utf-8")
    (root / "config" / "strategies" / "t-cross.toml").write_text(
        'strategy = "t-cross"\nversion = 1\n[params]\nentry_level = "9000"\nexit_level = "8000"\nqty = 10\n',
        encoding="utf-8")
    for y in (2025, 2026):
        (root / "config" / "calendar" / f"krx-{y}.toml").write_text(
            f'market = "KRX"\nyear = {y}\nsource = "가상"\nsource_version = "t"\nholidays = []\n', encoding="utf-8")
    if mode is Mode.PAPER:
        (root / ".env.paper").write_text("TRADE_MODE=PAPER\nKIWOOM_PAPER_APP_KEY=placeholder\n"
                                         "KIWOOM_PAPER_SECRET_KEY=placeholder\nKIWOOM_PAPER_ACCOUNT_NO=placeholder\n"
                                         "KIWOOM_PAPER_ACCOUNT_ALIAS=KR-PAPER-01\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("env,expect", [({}, "TRADE_MODE가 없다"), ({"TRADE_MODE": "paper"}, "잘못되었다"),
                                        ({"TRADE_MODE": "LIVE"}, "LIVE는 시작할 수 없다")])
def test_bad_or_live_mode_refused_without_connection(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                     env: dict, expect: str) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    assert run_main(root, env) == 2
    assert expect in capsys.readouterr().out
    assert not (root / "var").exists() or not any((root / "var").rglob("trading.db"))


def test_paper_refused_until_user_sets_connection_approved(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, Mode.PAPER, paper_approved=False)
    assert run_main(root, {"TRADE_MODE": "PAPER"}) == 2
    out = capsys.readouterr().out
    assert "모의 접속 승인 전" in out and "placeholder" not in out
    assert not (root / "var" / "paper" / "trading.db").exists()


def test_backtest_mode_dry_run_starts_with_k8_and_heartbeat(tmp_path: Path) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.3) == 0
    base = root / "var" / "backtest"
    assert (base / "heartbeat").exists()
    repo = Repository(base / "trading.db", Mode.BACKTEST)
    try:
        active = set(repo.active_halts("KR-BT-01"))
        assert {"K8", "K6"} <= active                  # 시작 = K8, 시계 판정 불가(루프백 닫힌 포트) = K6
        assert repo.strategy_state("t-cross", 1, "M1") is not None
    finally:
        repo.close()
    # 같은 설정으로 다시 시작해도 된다 (계좌·전략 등록 재사용)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1) == 0


def test_backtest_command_from_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    data = tmp_path / "data"
    data.mkdir()
    lines, d, i = ["date,open,high,low,close,volume"], date(2026, 1, 2), 0
    while d <= date(2026, 3, 31):
        if d.weekday() < 5:
            p = 9500 + (i % 7) * 100
            lines.append(f"{d.isoformat()},{p},{p + 200},{p - 200},{p + 50},10000")
            i += 1
        d += timedelta(days=1)
    (data / "005930.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc = main(["backtest", "--root", str(root), "--strategy", "tests.support.sample_strategies:CrossStrategy",
               "--params", "config/strategies/t-cross.toml", "--data", str(data), "--start", "2026-02-02",
               "--end", "2026-03-31", "--run-id", "bt1"])
    assert rc == 0, capsys.readouterr().out
    report = (root / "var" / "backtest" / "bt1" / "report.md").read_text(encoding="utf-8")
    assert "재현성(D-074): 통과" in report and "룩어헤드 교란 검사(D-073): 통과" in report


def test_csv_loader_rejects_bad_rows(tmp_path: Path) -> None:
    f = tmp_path / "x.csv"
    f.write_text("date,open,high,low,close,volume\n2026-01-02,1,2,0.5,1.5,abc\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="x.csv:2"):
        load_daily_csv(f)
    f.write_text("date,open,close\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="열이 부족"):
        load_daily_csv(f)


def test_account_mismatch_between_app_and_limits_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    p = root / "config" / "app.backtest.toml"
    p.write_text(p.read_text(encoding="utf-8").replace('account = "KR-BT-01"', 'account = "KR-BT-02"'),
                 encoding="utf-8")
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.5) == 2
    assert "계좌" in capsys.readouterr().out
