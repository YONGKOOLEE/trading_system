"""1분봉 불완전 봉(거래소 거래 중단으로 분 안에서 일찍 끝난 봉) 처리 — 일봉 load_binance_daily_csv(incomplete=) 선례(A-112)와 같게.

기본은 거부(종전 동작). incomplete 집합을 넘기면 일찍 끝난 봉만 건너뛰고 시작 시각을 기록한다(결측이 되어 그 창의 판정은
무효 — 보수적). 끝 시각이 분을 넘거나 시작과 같지 않은 다른 형식 위반은 여전히 거부. 2026-10-19 crypto-trend 재현에서
BTC·ETH 1m 공개 데이터의 불완전 봉 17개(같은 시각 — 거래소 점검, 예: 2020-02-19 11:35)로 발견.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from trading_system.app.backtest_m2_minute import MINUTE_COLUMNS, iter_binance_minute_csv
from trading_system.config.errors import ConfigError

HEAD = ",".join(MINUTE_COLUMNS) + "\n"


def line(start: str, end: str) -> str:
    return f"{start},100,101,99,100,1,{end},100,1,0.5,50\n"


def write(tmp: Path, rows: list[str]) -> Path:
    p = tmp / "X-1m.csv"
    p.write_text(HEAD + "".join(rows), encoding="utf-8")
    return p


ROWS = [line("2020-02-19T11:34:00.000Z", "2020-02-19T11:34:59.999Z"),
        line("2020-02-19T11:35:00.000Z", "2020-02-19T11:35:32.286Z"),       # 거래 중단으로 일찍 끝난 봉
        line("2020-02-19T17:30:00.000Z", "2020-02-19T17:30:59.999Z")]
START, STOP = datetime(2020, 2, 19, tzinfo=UTC), datetime(2020, 2, 20, tzinfo=UTC)


def test_incomplete_bar_rejected_by_default(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="불완전 봉"):
        list(iter_binance_minute_csv(write(tmp_path, ROWS), START, STOP, final_from=date(2026, 1, 1)))


def test_incomplete_bar_skipped_and_recorded_when_set_given(tmp_path: Path) -> None:
    seen: set[datetime] = set()
    bars = list(iter_binance_minute_csv(write(tmp_path, ROWS), START, STOP, final_from=date(2026, 1, 1), incomplete=seen))
    assert [b.at.strftime("%H:%M") for b in bars] == ["11:34", "17:30"]          # 11:35는 결측으로
    assert seen == {datetime(2020, 2, 19, 11, 35, tzinfo=UTC)}


def test_zero_length_bar_also_skipped_when_set_given(tmp_path: Path) -> None:
    """[개정 2026-10-19] 끝 = 시작인 길이 0 봉(공개 데이터 2017-09-06 16:00 — 거래소 결함)도 결측으로. 처음에는 일봉 규칙
    (시작 < 끝)과 같게 거부로 고정했으나, crypto-trend 공식 검증의 전체 이력 실행이 이 봉에서 거부되어 같은 결함 범주로 처리한다
    (결측 → 판정 무효, 보수적). 기본(집합 없음)은 여전히 거부."""
    rows = [ROWS[0], line("2020-02-19T11:35:00.000Z", "2020-02-19T11:35:00.000Z"), ROWS[2]]
    with pytest.raises(ConfigError, match="불완전 봉"):
        list(iter_binance_minute_csv(write(tmp_path, rows), START, STOP, final_from=date(2026, 1, 1)))
    seen: set[datetime] = set()
    bars = list(iter_binance_minute_csv(write(tmp_path, rows), START, STOP, final_from=date(2026, 1, 1), incomplete=seen))
    assert [b.at.strftime("%H:%M") for b in bars] == ["11:34", "17:30"] and len(seen) == 1


@pytest.mark.parametrize("end", ["2020-02-19T11:36:00.000Z", "2020-02-19T11:34:59.000Z"])
def test_other_close_time_violations_still_rejected(tmp_path: Path, end: str) -> None:
    rows = [ROWS[0], line("2020-02-19T11:35:00.000Z", end), ROWS[2]]
    with pytest.raises(ConfigError, match="불완전 봉"):
        list(iter_binance_minute_csv(write(tmp_path, rows), START, STOP, final_from=date(2026, 1, 1), incomplete=set()))


def test_cli_skip_incomplete_minutes_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """명령줄: 기본은 불완전 봉에서 거부(종료 2), --skip-incomplete-minutes면 결측으로 실행하고 incomplete-minutes.json 기록."""
    import json

    from tests.scenario.test_t053_m2_minute_backtest import DAY, ROOT, example_params, minute_csv
    from trading_system.app.backtest_m2_cli import backtest_m2_main
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", DAY, 2)
    text = data.read_text(encoding="utf-8").split("\n")
    i = next(k for k, s in enumerate(text) if s.startswith("2025-03-03T10:07:00.000Z"))
    text[i] = text[i].replace("2025-03-03T10:07:59.999Z", "2025-03-03T10:07:21.500Z")   # 일찍 끝난 봉
    data.write_text("\n".join(text), encoding="utf-8")
    base = ["--interval", "1m", "--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
            "--params", str(example_params(tmp_path / "p.toml")), "--data", str(data), "--start", "2025-03-03",
            "--end", "2025-03-04", "--lookback", "31", "--slippage", "0.001", "--out", str(tmp_path / "out")]
    assert backtest_m2_main(base + ["--run-id", "a"], ROOT) == 2
    assert "불완전 봉" in capsys.readouterr().out
    assert backtest_m2_main(base + ["--run-id", "b", "--skip-incomplete-minutes"], ROOT) == 0
    out = capsys.readouterr().out
    assert "불완전 1분봉 결측 처리: BTCUSDT 1개" in out
    got = json.loads((tmp_path / "out" / "b" / "incomplete-minutes.json").read_text(encoding="utf-8"))
    assert got == {"BTCUSDT": ["2025-03-03T10:07:00Z"]}
    s = json.loads((tmp_path / "out" / "b" / "summary.json").read_text(encoding="utf-8"))
    assert s["missing_bars"] >= 1
