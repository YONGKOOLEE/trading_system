"""검증 실행기 명령 (T-026, CP-030 D-170). 사람이 직접 실행한다. BACKTEST 전용(네트워크·브로커 없음).

    python scripts/validate-strategy.py --strategy trading_system.strategy.trend_sma:TrendSmaV1 \
        --params config/strategies/trend-sma-v1.toml --data <일봉 CSV 폴더> --report-version 1

- 7개 워크포워드 창(D-162) × (기본 슬리피지, 2배) + 재현성 + ±20% 변형(D-165) + 단순 보유(D-166).
  무작위 진입 비교는 A-083으로 공통 기준에서 제외 — 기본 미실행, --random-runs N으로 선택 실행(판정 제외).
- 결과: `var/backtest/validation/<전략ID>-v<n>/` (창별 결과 파일, verdict.json, validation-v<n>.md).
  판정표는 docs/strategies/<ID>/로 사람이 옮긴다(이 명령은 docs/를 쓰지 않는다).
- 최종 확인 구간(`config/validation.toml` final_from 이후)은 `--final-check --confirmed-by A-xxx`로만, 전략 버전당 1회(D-163).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from pathlib import Path

from trading_system.app.backtest import BacktestConfig, BacktestError
from trading_system.app.validation import (
    FINAL_FROM, RANDOM_RUNS, SLIPPAGE_BASE, ValidationError, ValidationInputs, run_final_check, run_validation,
)
from trading_system.config.errors import ConfigError
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar


def load_validation_settings(path: Path) -> dict[str, object]:
    """config/validation.toml: final_from(D-163), slippage_base(D-164). 없으면 CP-030 값."""
    out: dict[str, object] = {"final_from": FINAL_FROM, "slippage_base": SLIPPAGE_BASE}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)
        if "final_from" in raw:
            out["final_from"] = date.fromisoformat(str(raw["final_from"]))
        if "slippage_base" in raw:
            if not isinstance(raw["slippage_base"], str):
                raise ConfigError("slippage_base는 문자열 소수로 (부동소수 금지)")
            out["slippage_base"] = Decimal(raw["slippage_base"])
    return out


def extend_calendar(base: KrxCalendar, holidays_json: Path, source: str) -> KrxCalendar:
    """일봉에서 추출한 휴장일(연도 → 날짜 목록)로 달력이 없는 연도를 채운다. 저장소 달력 연도는 그대로 둔다."""
    hol = json.loads(holidays_json.read_text(encoding="utf-8"))
    years = {int(y): parse_calendar({"market": "KRX", "year": int(y), "source": source,
                                     "source_version": holidays_json.name,
                                     "holidays": [date.fromisoformat(x) for x in v]}, int(y)) for y, v in hol.items()}
    years.update(base._years)  # noqa: SLF001
    return KrxCalendar(years)


def _git_commit(root: Path) -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return f"{head}{' (작업 트리 변경 있음)' if dirty else ''}" if head else "미기재"
    except (OSError, subprocess.SubprocessError):
        return "미기재"


def validate_main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    from trading_system.app.main import _load_common, load_daily_csv
    from trading_system.app.wiring import load_strategy
    from trading_system.config.app_file import StrategyDeploy
    from trading_system.config.limits_file import load_limits_file
    from trading_system.config.strategy_params import load_params_file
    from trading_system.domain.enums import Mode
    from trading_system.domain.market import InstrumentInfo, InstrumentKind

    root = (root or Path(__file__).resolve().parents[3]).resolve()
    p = argparse.ArgumentParser(prog="scripts/validate-strategy.py")
    p.add_argument("--strategy", required=True, help="패키지.모듈:클래스")
    p.add_argument("--params", required=True, help="파라미터 파일 (저장소 기준 경로)")
    p.add_argument("--data", required=True, help="종목코드.csv 일봉 폴더")
    p.add_argument("--report-version", type=int, required=True, help="판정표 버전 n (validation-v<n>)")
    p.add_argument("--cash", default="100000000", help="창마다 초기 자금(원)")
    p.add_argument("--eval-time", default="09:05", help="판정 시각 HH:MM, 여러 개는 쉼표")
    p.add_argument("--slippage-tick", default="10", help="호가 표가 없을 때 상·하한가 호가 맞춤 단위(원)")
    p.add_argument("--limits", default="config/limits.backtest.toml")
    p.add_argument("--holidays-json", default=None, help="저장소 달력이 없는 연도의 휴장일 JSON (일봉 추출)")
    p.add_argument("--holidays-source", default="일봉 추출 휴장일(검증 실행기 전용)")
    p.add_argument("--universe-from-data", action="store_true",
                   help="전략 유니버스를 데이터 폴더 종목으로 바꿔 실행(실행 객체만, 판정표에 기록)")
    p.add_argument("--universe-point-in-time", action="store_true",
                   help="유니버스가 판정 시점 목록임을 선언(생존편향 표시 없음). 기본은 사후 선택 목록으로 본다")
    p.add_argument("--universe-note", default="")
    p.add_argument("--window-params", action="append", default=[], metavar="K=PATH",
                   help="창 k의 파라미터 파일(사람이 학습 창으로 고른 값). 여러 번 지정")
    p.add_argument("--random-runs", type=int, default=RANDOM_RUNS,
                   help="무작위 진입 비교 횟수 (A-083: 공통 기준 아님, 기본 0 = 미실행)")
    p.add_argument("--random-seed", default="cp030")
    p.add_argument("--workers", type=int, default=1, help="병렬 프로세스 수 (결과는 같음)")
    p.add_argument("--out-root", default="var/backtest/validation")
    p.add_argument("--final-check", action="store_true", help="최종 확인 실행(D-163): 전략 확정 뒤 1회")
    p.add_argument("--confirmed-by", default="", help="--final-check 때 전략 확정 사용자 선언 A-ID")
    p.add_argument("--end", default=None, help="--final-check 때 종료일(기본 데이터 마지막 날)")
    p.add_argument("--ledger", default="var/backtest/final-usage.json", help="최종 구간 사용 장부(백테스트 FINAL과 공용)")
    ns = p.parse_args(argv)
    try:
        settings = load_validation_settings(root / "config" / "validation.toml")
        gate_settings, _, calendar, ticks, caps = _load_common(root, Mode.BACKTEST)
        if ns.holidays_json:
            calendar = extend_calendar(calendar, Path(ns.holidays_json), ns.holidays_source)
        limits = load_limits_file(root / ns.limits).limits
        eval_times = tuple(time(int(hh), int(mm)) for hh, mm in
                           (x.strip().split(":") for x in ns.eval_time.split(",")))
        strat, params = load_strategy(StrategyDeploy(ns.strategy, (root / ns.params).resolve(), ()))
        data_dir = Path(ns.data)
        data = {f.stem: load_daily_csv(f) for f in sorted(data_dir.glob("*.csv"))}
        if not data:
            raise ConfigError("데이터 CSV가 없음")
        note = ns.universe_note
        if ns.universe_from_data:
            strat.spec = replace(strat.spec, universe=tuple(sorted(data)))  # type: ignore[misc]
            note = (note + " · " if note else "") + f"유니버스 = 데이터 폴더 {len(data)}종목(실행 객체만 교체)"
        infos = {c: InstrumentInfo(c, InstrumentKind.STOCK, Decimal("1"), Decimal("1")) for c in data}
        out_root = root / ns.out_root
        template = BacktestConfig(
            run_id="template", strategy=strat, params=params, caps=caps, account_limits=limits,
            gate_settings=gate_settings, calendar=calendar, ticks=ticks, slippage_tick=Decimal(ns.slippage_tick),
            data=data, infos=infos, start=date(1900, 1, 1), end=date(1900, 1, 1), initial_cash=Decimal(ns.cash),
            out_dir=out_root, eval_time=eval_times[0], eval_times=eval_times, data_version=f"csv:{data_dir.name}",
            code_commit=_git_commit(root))
        spec = strat.spec
        name = f"{spec.strategy_id}-v{ns.report_version}"
        if ns.final_check:
            end = date.fromisoformat(ns.end) if ns.end else max(r.day for raw in data.values() for r in raw)
            res = run_final_check(template, out_root / f"{name}-final", final_from=settings["final_from"],  # type: ignore[arg-type]
                                  end=end, ledger=root / ns.ledger,
                                  confirmed_by=ns.confirmed_by, slippage_pct=settings["slippage_base"])  # type: ignore[arg-type]
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0
        wp: dict[int, dict[str, object]] = {}
        wpf: dict[int, str] = {}
        for item in ns.window_params:
            k_s, _, path_s = item.partition("=")
            wp[int(k_s)] = load_params_file(spec, (root / path_s).resolve())
            wpf[int(k_s)] = path_s
        inputs = ValidationInputs(
            template=template, out_dir=out_root / name, report_version=ns.report_version,
            final_from=settings["final_from"], slippage_base=settings["slippage_base"],  # type: ignore[arg-type]
            window_params=wp, window_params_files=wpf, random_runs=ns.random_runs, random_seed=ns.random_seed,
            universe_point_in_time=ns.universe_point_in_time, universe_note=note, workers=ns.workers,
            command="python scripts/validate-strategy.py " + " ".join(argv if argv is not None else []))
        out = run_validation(inputs)
    except (ConfigError, BacktestError, ValidationError, ValueError, OSError) as exc:
        print(f"검증 거부: {exc}")
        return 2
    v = out["verdict"]
    print(f"판정: {v['verdict']}  ({inputs.out_dir.as_posix()}/validation-v{ns.report_version}.md)")  # type: ignore[index]
    print(f"실행 수 {out['runs']} {out['runs_by_kind']}, 시간 {out['runtime_seconds']}")
    return 0
