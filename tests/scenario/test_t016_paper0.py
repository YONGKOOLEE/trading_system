"""T-016 PAPER-0 준비 — 검증 전략·배포 설정·전략 운영 도구 (04 §4.1, A-037·A-039, DP-24 a, INV-13).

네트워크 없이: 등록·전이는 임시 저장소, 전략 판정은 가상 스냅샷.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.app.strategy_tool import strategy_main
from trading_system.config.app_file import parse_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import validate_spec
from trading_system.config.limits_file import load_limits_file
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.market import TradingStatus
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.storage.repository import Repository
from trading_system.strategy.paper0_verify import SPEC, Paper0Verify

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 5, 0, 30, tzinfo=UTC)
CODE = "005930"


def snap(view: InstrumentView) -> EvaluationSnapshot:
    return EvaluationSnapshot("ev", "M1", "KR-PAPER-01", T0, Decimal("10000000"), (view,))


def view(**kw: object) -> InstrumentView:
    base: dict = {"instrument": CODE, "daily_bars": (Bar(T0.date(), *(Decimal("70000"),) * 4, Decimal("1")),),
                  "last_price": Decimal("70500")}
    base.update(kw)
    return InstrumentView(**base)


PARAMS = {"qty": 1, "hold_days": 0}


# --- 전략 판정 -------------------------------------------------------------------------------------

def test_entry_one_share_market_when_flat() -> None:
    sigs = Paper0Verify().evaluate(snap(view()), PARAMS)
    assert len(sigs) == 1 and sigs[0].kind is RequestType.ENTRY and sigs[0].quantity == 1
    assert sigs[0].order_type is OrderType.MARKET and sigs[0].instrument == CODE


def test_exit_all_when_holding() -> None:
    v = view(position=PositionView(Decimal("1"), Decimal("70000"), 0, True))
    sigs = Paper0Verify().evaluate(snap(v), PARAMS)
    assert [(s.kind, s.quantity) for s in sigs] == [(RequestType.EXIT, Decimal("1"))]


def test_hold_days_delays_exit() -> None:
    v = view(position=PositionView(Decimal("1"), Decimal("70000"), 0, True))
    assert Paper0Verify().evaluate(snap(v), {"qty": 1, "hold_days": 1}) == []


@pytest.mark.parametrize("over", [{"data_ok": False}, {"has_pending_order": True},
                                  {"statuses": frozenset({TradingStatus.HALTED})},
                                  {"instrument": "000660"},
                                  {"position": PositionView(Decimal("1"), Decimal("70000"), 3, False)}])
def test_no_signal_when_unsafe_or_not_mine(over: dict) -> None:
    assert Paper0Verify().evaluate(snap(view(**over)), PARAMS) == []


def test_spec_and_params_pass_paper0_registration_rules() -> None:
    caps = load_capabilities(ROOT / "docs" / "capabilities.md")
    limits = load_limits_file(ROOT / "config" / "limits.paper.toml").limits
    assert validate_spec(SPEC, caps, limits, Mode.PAPER) != []                       # 승인 없으면 거부 (DP-1)
    assert validate_spec(SPEC, caps, limits, Mode.PAPER, verification_approval="A-037") == []
    assert load_params_file(SPEC, ROOT / "config" / "strategies" / "paper0-verify-v1.toml") == {
        "qty": 1, "hold_days": 0}


# --- 배포 설정 --------------------------------------------------------------------------------------

def app_raw(**st: object) -> dict:
    s = {"module": "trading_system.strategy.paper0_verify:Paper0Verify",
         "params": "config/strategies/paper0-verify-v1.toml", "eval_times": ["09:30", "14:30"]}
    s.update(st)
    return {"app": {"account": "KR-PAPER-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                    "sntp_servers": ["x.invalid:123"]}, "strategies": [s]}


def test_app_settings_verification_approval_paper_only() -> None:
    a = parse_app_settings(app_raw(verification_approval="A-037"), Mode.PAPER, ROOT)
    assert a.strategies[0].verification_approval == "A-037"
    assert parse_app_settings(app_raw(), Mode.PAPER, ROOT).strategies[0].verification_approval is None
    with pytest.raises(Exception, match="verification_approval"):
        parse_app_settings(app_raw(verification_approval="X-1"), Mode.PAPER, ROOT)
    with pytest.raises(Exception):
        parse_app_settings(app_raw(verification_approval="A-037"), Mode.BACKTEST, ROOT)


# --- 전략 운영 도구 ----------------------------------------------------------------------------------

def tool_root(tmp_path: Path, approval: str | None = "A-037") -> Path:
    root = tmp_path / "repo"
    (root / "config" / "strategies").mkdir(parents=True)
    (root / "docs").mkdir()
    for rel in ("config/limits.paper.toml", "config/strategies/paper0-verify-v1.toml", "docs/capabilities.md"):
        (root / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    lines = ['[app]', 'account = "KR-PAPER-01"', 'loop_seconds = "0.5"', 'reconcile_minutes = 1',
             'sntp_servers = ["x.invalid:123"]', '[[strategies]]',
             'module = "trading_system.strategy.paper0_verify:Paper0Verify"',
             'params = "config/strategies/paper0-verify-v1.toml"', 'eval_times = ["09:30", "14:30"]']
    if approval:
        lines.append(f'verification_approval = "{approval}"')
    (root / "config" / "app.paper.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def state(root: Path) -> str | None:
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        row = repo.strategy_state("paper0-verify", 1, "M1")
        return row["state"] if row else None
    finally:
        repo.close()


def test_register_then_transitions_with_user_approvals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tool_root(tmp_path)
    assert strategy_main(["register", "--mode", "PAPER", "--root", str(root)]) == 0
    assert state(root) == "DRAFT"
    report = "var/backtest/paper0-verify-v1/report.md"
    # 증거 파일은 실제로 있어야 한다 (V-113 강화, review-2026-10-03 C절 — 기존 시험은 없는 경로로 통과했음)
    (root / report).parent.mkdir(parents=True, exist_ok=True)
    (root / report).write_text("# report", encoding="utf-8")
    assert strategy_main(["transition", "--mode", "PAPER", "--root", str(root), "paper0-verify", "1",
                          "BACKTEST_PASSED", "--approval", "A-901", "--evidence", report]) == 0
    assert strategy_main(["transition", "--mode", "PAPER", "--root", str(root), "paper0-verify", "1",
                          "PAPER", "--approval", "A-902"]) == 0
    assert state(root) == "PAPER"
    assert strategy_main(["show", "--mode", "PAPER", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "paper0-verify" in out and "PAPER" in out and "A-902" in out


def test_register_without_verification_approval_is_rejected(tmp_path: Path) -> None:
    root = tool_root(tmp_path, approval=None)
    assert strategy_main(["register", "--mode", "PAPER", "--root", str(root)]) == 1
    assert state(root) is None


@pytest.mark.parametrize("args", [
    ["transition", "--mode", "PAPER", "paper0-verify", "1", "PAPER", "--approval", "A-902"],    # DRAFT → PAPER 불가
    ["transition", "--mode", "PAPER", "paper0-verify", "1", "BACKTEST_PASSED", "--approval", "A-901"],  # 증거 없음
    ["transition", "--mode", "PAPER", "paper0-verify", "1", "BACKTEST_PASSED", "--approval", "ok",
     "--evidence", "x"],                                                                          # A-ID 아님
    ["transition", "--mode", "PAPER", "paper0-verify", "1", "BACKTEST_PASSED", "--approval", "A-901",
     "--evidence", "var/backtest/none/report.md"],                                                # 증거 파일 없음 (V-113)
])
def test_bad_transitions_refused(tmp_path: Path, args: list[str]) -> None:
    root = tool_root(tmp_path)
    assert strategy_main(["register", "--mode", "PAPER", "--root", str(root)]) == 0
    assert strategy_main(args[:3] + ["--root", str(root)] + args[3:]) == 1
    assert state(root) == "DRAFT"


def test_live_mode_refused(tmp_path: Path) -> None:
    assert strategy_main(["show", "--mode", "LIVE", "--root", str(tmp_path)]) == 2
