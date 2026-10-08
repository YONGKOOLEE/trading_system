"""코드 정리 (A-084, 2026-10-08): 기준 금액·하트비트 읽기 단일화, resume LIVE 거부, 내림 도우미.

- 일일 기준 금액 DailyBase: 감사 요약 형식(키·순서·값)이 이전과 바이트 동일해야 옛 행을 읽는다 (R-18, D-156).
- 하트비트 읽기: domain.heartbeat 한 곳. preflight·ops/cli가 같은 읽기(재시도 포함)를 쓴다 (V-111).
- resume·reconcile-resolve: --mode LIVE 거부 (LIVE 실행·재개는 사용자 주도). kill·status는 허용.
- round_price_down / round_qty_down: trend_sma의 ROUND_FLOOR와 수치 동일.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

from trading_system.domain.daily_base import DailyBase
from trading_system.domain.heartbeat import HEARTBEAT_STALE, watchdog_check
from trading_system.domain.sizing import round_price_down, round_qty_down

T0 = datetime(2026, 10, 8, 0, 5, tzinfo=UTC)


# --- 1. 일일 기준 금액 단일 정의 ---------------------------------------------------------------------

def test_daily_base_summary_is_byte_identical_to_legacy_format() -> None:
    at = datetime(2026, 10, 8, 0, 0, 7, tzinfo=UTC)
    base = DailyBase("2026-10-08", Decimal("99721280"), Decimal("0"), at)
    legacy = {"trade_date": "2026-10-08", "cash": str(Decimal("99721280")), "value": str(Decimal("0")),
              "at": at.isoformat()}
    assert json.dumps(base.to_summary(), ensure_ascii=False) == json.dumps(legacy, ensure_ascii=False)
    assert base.total == Decimal("99721280")


def test_daily_base_from_summary_round_trip_and_extra_keys() -> None:
    at = datetime(2026, 10, 8, 0, 0, 7, tzinfo=UTC)
    base = DailyBase("2026-10-08", Decimal("1000"), Decimal("500"), at)
    assert DailyBase.from_summary(base.to_summary()) == base
    rec = {"account": "X", **base.to_summary(), "extra": 1}   # 옛 행에 있던 추가 키는 무시
    assert DailyBase.from_summary(rec) == base
    assert DailyBase.from_summary(rec).total == Decimal("1500")


@pytest.mark.parametrize("bad", [
    {"trade_date": "2026-10-08", "cash": "abc", "value": "0", "at": "2026-10-08T00:00:00+00:00"},
    {"trade_date": "2026-10-08", "cash": "1", "at": "2026-10-08T00:00:00+00:00"},
    {"trade_date": "2026-10-08", "cash": "1", "value": "0", "at": "not-a-time"},
])
def test_daily_base_from_summary_rejects_malformed(bad: dict) -> None:
    with pytest.raises(ValueError):
        DailyBase.from_summary(bad)


# --- 2. 하트비트 읽기 단일화 ---------------------------------------------------------------------------

def test_heartbeat_constants_live_in_domain_only() -> None:
    from trading_system.app import preflight
    from trading_system.monitoring import liveness
    from trading_system.ops import cli
    assert HEARTBEAT_STALE == timedelta(seconds=30)
    assert liveness.HEARTBEAT_STALE is HEARTBEAT_STALE
    assert liveness.watchdog_check is watchdog_check
    assert not hasattr(preflight, "HEARTBEAT_FRESH_SECONDS")
    assert not hasattr(cli, "HEARTBEAT_STALE_SECONDS")


def _write(hb: Path, at: datetime | str) -> None:
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(at if isinstance(at, str) else at.isoformat(), encoding="utf-8")


def test_watchdog_check_reports_naive_timestamp_as_not_alive(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat"
    _write(hb, "2026-10-08T00:05:00")
    st = watchdog_check(hb, T0, sleep=lambda s: None)
    assert not st.alive and st.age_seconds is None and "시간대" in st.detail


def _flaky_reader(monkeypatch: pytest.MonkeyPatch, target: Path, failures: int) -> dict[str, int]:
    real = Path.read_text
    state = {"n": failures}

    def flaky(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if self == target and state["n"] > 0:
            state["n"] -= 1
            raise PermissionError("교체 중")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    return state


def test_preflight_engine_alive_uses_shared_reader_with_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_system.app.preflight import engine_alive
    hb = tmp_path / "heartbeat"
    _write(hb, T0 - timedelta(seconds=4))
    _flaky_reader(monkeypatch, hb, 2)
    reason = engine_alive(hb, T0)
    assert reason is not None and "4초 전" in reason
    assert engine_alive(hb, T0 + timedelta(seconds=120)) is None      # 오래됨 → 거부 없음
    hb.unlink()
    assert engine_alive(hb, T0) is None                                # 없음 → 거부 없음


@pytest.mark.parametrize(("age", "refused"), [
    (-31, True),    # 30초 넘게 미래 — 시계 이상, 엔진이 돌고 있을 수 있음 → 거부 (재검토 M-2, 6bac802 이전 동작)
    (-10, True),
    (10, True),     # 살아 있음 → 거부
    (30, True),     # 정확히 30초 — 보수적으로 살아 있음 (6bac802 동작 유지)
    (31, False),    # 멈춤 → 거부 없음
])
def test_preflight_engine_alive_refuses_future_heartbeat(tmp_path: Path, age: int, refused: bool) -> None:
    from trading_system.app.preflight import engine_alive
    hb = tmp_path / "heartbeat"
    _write(hb, T0 - timedelta(seconds=age))
    reason = engine_alive(hb, T0)
    assert (reason is not None) is refused, reason
    if age < -30:
        assert reason is not None and "미래" in reason and "시계" in reason


def test_cli_heartbeat_print_uses_shared_reader_with_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.ops.cli import _print_heartbeat
    hb = tmp_path / "heartbeat"
    _write(hb, T0 - timedelta(seconds=4))
    _flaky_reader(monkeypatch, hb, 2)
    _print_heartbeat(hb, T0)
    out = capsys.readouterr().out
    assert "하트비트: 4초 전" in out and "살아 있음" in out
    _print_heartbeat(hb, T0 + timedelta(minutes=5))
    assert "멈춤 의심" in capsys.readouterr().out
    _write(hb, "2026-10-08T00:05:00")
    _print_heartbeat(hb, T0)
    assert "시간대" in capsys.readouterr().out
    hb.unlink()
    _print_heartbeat(hb, T0)
    assert "하트비트: 없음" in capsys.readouterr().out


# --- 5. resume·reconcile-resolve --mode LIVE 거부 ----------------------------------------------------------

def test_resume_refuses_live_mode_without_touching_storage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.ops.cli import resume_main
    assert resume_main(["--mode", "LIVE", "--root", str(tmp_path), "K1", "--clear-kill-file"]) == 2
    out = capsys.readouterr().out
    assert out.startswith("거부") and "LIVE" in out
    assert not (tmp_path / "var" / "live").exists()


def test_reconcile_resolve_refuses_live_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.ops.cli import reconcile_resolve_main
    assert reconcile_resolve_main(["--mode", "LIVE", "--root", str(tmp_path), "--run", "--account", "KR-X"]) == 2
    assert reconcile_resolve_main(["--mode", "LIVE", "--root", str(tmp_path), "item-1", "EXTERNAL"]) == 2
    out = capsys.readouterr().out
    assert out.count("거부") == 2 and "LIVE" in out
    assert not (tmp_path / "var" / "live").exists()


def test_kill_and_status_still_allowed_in_live_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.ops.cli import kill_main, status_main
    assert status_main(["--mode", "LIVE", "--root", str(tmp_path)]) == 0
    assert "모드: LIVE" in capsys.readouterr().out
    assert kill_main(["--mode", "LIVE", "--root", str(tmp_path)]) == 0
    assert (tmp_path / "var" / "live" / "KILL").exists()


# --- 7. 내림 도우미 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["70123.7", "70123", "-3.2", "0.4", "12345678.123456789012345", "1E+2", "69999.99999"])
def test_round_price_down_matches_quantize_floor_for_tick_one(raw: str) -> None:
    x = Decimal(raw)
    expected = x.quantize(Decimal("1"), rounding=ROUND_FLOOR)   # strategy/trend_sma.py의 이전 표현
    got = round_price_down(x, Decimal("1"))
    assert got == expected and str(got) == str(expected)


def test_round_price_down_other_ticks_and_errors() -> None:
    assert round_price_down(Decimal("70123"), Decimal("5")) == Decimal("70120")
    assert round_price_down(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert str(round_price_down(Decimal("1.239"), Decimal("0.01"))) == "1.23"
    with pytest.raises(ValueError):
        round_price_down(Decimal("1"), Decimal("0"))


def test_round_qty_down() -> None:
    assert round_qty_down(Decimal("10.9"), Decimal("1")) == Decimal("10")
    assert round_qty_down(Decimal("0.0129"), Decimal("0.001")) == Decimal("0.012")
    with pytest.raises(ValueError):
        round_qty_down(Decimal("1"), Decimal("-1"))
