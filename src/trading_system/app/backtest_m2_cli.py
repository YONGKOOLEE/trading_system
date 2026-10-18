"""M2 백테스트 명령 `python -m trading_system.app backtest --market M2` (T-M2-15, A-111 Q-1 (가)).

[CP-037 T-053] `--interval 1m`이면 1분봉 실행기(app/backtest_m2_minute — D-221 체결·D-222 검증 창, backtest_m2_minute_main).
`--interval` 없음·`1d` = 아래 일봉 경로 그대로.

- 데이터: 바이낸스 공개 1d klines CSV(A-110 수집 형식: open_time_utc,open,high,low,close,volume,close_time_utc,…, ISO UTC 시각).
  `load_binance_daily_csv`가 형식을 검사해 UTC 일봉(Bar.at = 봉 시작 날짜)으로 바꾼다. 파일은 읽기만 한다.
- 불완전 봉(거래 중단으로 일찍 끝난 1d 봉)은 빼고 날짜를 보고서·요약에 적는다 → 그날은 판정 데이터 없음(04 §2.1 결측, 주문 없음).
- 실행 끝 날짜(--end) 뒤의 행은 실행기에 넘기지 않는다(판정에 쓰이지 않는 미래 행 — 개수를 요약에 기록).
  최종 확인 구간 보호(CP-030 D-163)는 실행기 그대로: 실행 날짜가 config/validation.toml final_from 이후와 겹치면 거부.
  M2에는 최종 확인 실행(--final-check·장부)이 없다 — 겹치면 항상 거부.
- 실운영 시작 검사와 같은 검사: 전략 validate_deploy(판정 시각), 거래 규칙 검사(app.wiring.spot_rule_problems — CP-041
  D-270·D-271: validate_symbol_rules 우선, 기존 validate_rules는 범위 1심볼만), 전략 등록(docs/capabilities.md M2 열, BACKTEST 대상).
  규칙 검사 범위 = 전략 명세 유니버스 ∩ 거래 규칙 표(TESTNET_RULES 전체 — 설정 유니버스 역할, D-271 (5)). 실행 심볼은 --symbol 하나.
- 거래 규칙: 테스트넷 실측값 BTCUSDT(broker-specs/binance-spot.md §12, 2026-10-14)·ETHUSDT(§15, 2026-10-18)만 있다 — PAPER 대상과
  같은 규칙으로 과거 가격을 돌린다(실서버 과거 시점의 규칙이 아님, 보고서 한계). 다른 심볼은 규칙이 없어 거부.
- 비용: 수수료 모델 기본 = config/m2/market.paper.toml [costs](CP-040 D-267 — maker·taker·rate_source, 기존 commission_rate만
  있으면 maker = taker = 그 값, 출처 placeholder). 체결 = taker, 게이트 비용 추정 = max(maker, taker). --fee를 주면
  maker = taker = 그 값, 출처 "user"(사용자 지정). 슬리피지는 필수 인자(기본값 없음).
- 결과: <out>/<실행 ID>/trading.db·report.md·summary.json. 재현성(같은 입력 재실행 지문 비교)·교란 검사(V-91, 표본 = 기간을 약 10등분한 날짜, M1과 같음)를 함께 한다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2 import (
    SpotBacktestConfig, dust_summary, fee_model_summary, perturbation_check_m2, render_report_m2, run_backtest_m2,
)
from trading_system.app.low_frequency import (
    NO_SAMPLE_WIRING, UNKNOWN_PAST, LowFrequencyError, LowFrequencyInputs, load_low_frequency_settings,
    params_hash, parse_declaration,
)
from trading_system.app.validation_tool import load_validation_settings
from trading_system.app.wiring import StrategyInput, load_strategy, period_grid_of, spot_rule_problems
from trading_system.config.app_file import StrategyDeploy
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.m2_files import load_m2_market
from trading_system.domain.fees import FeeModel
from trading_system.domain.market import CostRates
from trading_system.domain.market_time import Market
from trading_system.domain.strategy import Bar
from trading_system.domain.trading_rules import SymbolRules

# 테스트넷 거래 규칙 실측값. 가격·잔고 아님. 백테스트 전용 표 — 운영 M2 유니버스(config/m2/universe.*.toml)와 별개
TESTNET_RULES: dict[str, SymbolRules] = {
    # BTCUSDT: binance-spot.md §12 (2026-10-14 PV-M2-03)
    "BTCUSDT": SymbolRules(
        instrument="BTCUSDT", tick_size=Decimal("0.01"), min_price=Decimal("0.01"), max_price=Decimal("1000000"),
        lot_min_qty=Decimal("0.00001"), lot_max_qty=Decimal("9000"), lot_step=Decimal("0.00001"),
        market_min_qty=Decimal("0"), market_max_qty=Decimal("133.97193096"), market_step=Decimal("0"),
        min_notional=Decimal("5"), max_notional=Decimal("9000000"), apply_min_to_market=True,
        apply_max_to_market=False),
    # ETHUSDT: binance-spot.md §15 (2026-10-18, 공개 exchangeInfo) — PRICE_FILTER 0.01 (0.01~1,000,000), LOT_SIZE 0.0001
    # (0.0001~9000), MARKET_LOT_SIZE minQty 0·stepSize 0, NOTIONAL minNotional 5·applyMinToMarket True (CP-041 D-270, T-070).
    # §15에 기록이 없는 MARKET_LOT_SIZE maxQty·NOTIONAL maxNotional·applyMaxToMarket은 [미확인] → 비활성(0·None·False)으로 둔다
    "ETHUSDT": SymbolRules(
        instrument="ETHUSDT", tick_size=Decimal("0.01"), min_price=Decimal("0.01"), max_price=Decimal("1000000"),
        lot_min_qty=Decimal("0.0001"), lot_max_qty=Decimal("9000"), lot_step=Decimal("0.0001"),
        market_min_qty=Decimal("0"), market_max_qty=Decimal("0"), market_step=Decimal("0"),
        min_notional=Decimal("5"), max_notional=None, apply_min_to_market=True, apply_max_to_market=False),
}
PAIRS: dict[str, tuple[str, str]] = {"BTCUSDT": ("BTC", "USDT"), "ETHUSDT": ("ETH", "USDT")}
QUOTE_PRECISION = Decimal("0.00000001")   # quoteAssetPrecision 8 (§12)
KLINE_COLUMNS = ("open_time_utc", "open", "high", "low", "close", "volume", "close_time_utc")


def _utc(text: str, name: str, line: int, file: str) -> datetime:
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ConfigError(f"{file}:{line}: {name} 시각 형식 오류") from None
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ConfigError(f"{file}:{line}: {name} 시각에 UTC 표시가 없음")
    return at.astimezone(UTC)


def _dec(text: str, name: str, line: int, file: str) -> Decimal:
    try:
        v = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise ConfigError(f"{file}:{line}: {name} 숫자 형식 오류") from None
    if not v.is_finite():
        raise ConfigError(f"{file}:{line}: {name} 숫자 형식 오류")
    return v


def load_binance_daily_csv(path: Path, incomplete: list[date] | None = None) -> list[Bar]:
    """바이낸스 공개 1d klines CSV → UTC 일봉. 봉 시작 = UTC 00:00, 봉 끝 = 시작 + 1일 − 1ms, 날짜 오름차순·중복 없음.

    형식 오류는 ConfigError(파일 이름·줄 번호만, 값 없음). 가격 관계·음수 검사는 실행기 품질 검사(_check_config)가 한다.
    불완전 봉(봉 끝이 그날 안이지만 23:59:59.999 전 — 거래소 거래 중단으로 일찍 끝난 봉)은 기본 거부. `incomplete` 목록을
    주면 그 봉은 돌려주지 않고 날짜만 목록에 넣는다(04 §2.1: 결측·이상치는 채우지 않음 → 그날은 판정 데이터 없음, 주문 없음)."""
    bars: list[Bar] = []
    prev: date | None = None
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not set(KLINE_COLUMNS) <= set(reader.fieldnames):
            raise ConfigError(f"{path.name}: 열이 부족 (필요: {', '.join(KLINE_COLUMNS)})")
        for i, r in enumerate(reader, start=2):
            opened = _utc(r["open_time_utc"] or "", "open_time_utc", i, path.name)
            closed = _utc(r["close_time_utc"] or "", "close_time_utc", i, path.name)
            if opened.timetz().replace(tzinfo=None) != time(0, 0):
                raise ConfigError(f"{path.name}:{i}: 봉 시작이 UTC 00:00이 아님 (1d 봉 아님)")
            day = opened.date()
            if prev is not None and day <= prev:
                raise ConfigError(f"{path.name}:{i}: 날짜가 오름차순이 아님(중복·역순)")
            prev = day
            o, h, low, c, v = (_dec(r[k] or "", k, i, path.name) for k in ("open", "high", "low", "close", "volume"))
            full_end = opened + timedelta(days=1) - timedelta(milliseconds=1)
            if closed != full_end:
                if incomplete is not None and opened < closed < full_end:
                    incomplete.append(day)
                    continue
                raise ConfigError(f"{path.name}:{i}: 봉 끝이 시작 + 1일 − 1ms가 아님 (1d 봉 아님 또는 불완전 봉)")
            bars.append(Bar(day, o, h, low, c, v))
    if not bars:
        raise ConfigError(f"{path.name}: 데이터 행이 없음")
    return bars


def _fees(fee_arg: str | None, configured: FeeModel) -> FeeModel:
    """--fee(단일 요율, 사용자 지정) 또는 설정 [costs]의 수수료 모델 (D-267). 형식·범위 오류는 ValueError·InvalidOperation."""
    if fee_arg is None or fee_arg == "":
        return configured
    rate = Decimal(fee_arg)
    return FeeModel(rate, rate, "user")


def _times(text: str) -> tuple[time, ...]:
    try:
        return tuple(time(int(hh), int(mm)) for hh, mm in (part.strip().split(":") for part in text.split(",")))
    except ValueError:
        raise ConfigError("--eval-time 형식: HH:MM[,HH:MM…] (UTC)") from None


def backtest_m2_main(argv: Sequence[str], root: Path) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--interval", default="1d", choices=["1d", "1m"])   # [CP-037 T-053] 1m = 1분봉 실행기
    first, argv = pre.parse_known_args(argv)
    if first.interval == "1m":
        return backtest_m2_minute_main(argv, root)
    p = argparse.ArgumentParser(prog="python -m trading_system.app backtest --market M2")
    p.add_argument("--strategy", required=True, help="패키지.모듈:클래스")
    p.add_argument("--params", required=True, help="파라미터 파일 (저장소 기준)")
    p.add_argument("--data", required=True, help="바이낸스 공개 1d klines CSV 파일 (읽기만)")
    p.add_argument("--symbol", default="BTCUSDT", help="심볼 (거래 규칙이 있는 심볼만: BTCUSDT, ETHUSDT)")
    p.add_argument("--start", default=None, help="시작 UTC 날짜 (기본: 첫 봉)")
    p.add_argument("--end", required=True, help="끝 UTC 날짜 (final_from 전)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cash", default="10000", help="초기 USDT")
    p.add_argument("--eval-time", default="00:15", help="판정 시각 HH:MM(UTC), 여러 개는 쉼표 (예: 01:00,04:00)")
    p.add_argument("--slippage", required=True, help="불리 슬리피지 비율 (예: 0.001). 기본값 없음 — 사용 값을 기록")
    p.add_argument("--fee", default=None,
                   help="수수료율 — maker = taker = 이 값, 출처 user (기본: config/m2/market.paper.toml [costs], D-267)")
    p.add_argument("--limits", default="config/m2/limits.paper.toml", help="M2 한도 파일 (저장소 기준)")
    p.add_argument("--market-config", default="config/m2/market.paper.toml", help="M2 시장 설정 (저장소 기준)")
    p.add_argument("--out", default="var/backtest", help="결과 폴더 (저장소 기준 또는 절대 경로)")
    p.add_argument("--data-source", default="", help="보고서 데이터 출처 문구 (기본: 파일 이름·해시)")
    ns = p.parse_args(argv)
    try:
        times = _times(ns.eval_time)
        strat, params = load_strategy(StrategyDeploy(ns.strategy, (root / ns.params).resolve(), times))
        check = getattr(strat, "validate_deploy", None)
        problems = check(params, times) if check is not None else []
        if problems:
            raise ConfigError([f"배포 검사: {x}" for x in problems])
        if ns.symbol not in TESTNET_RULES:
            raise ConfigError(f"거래 규칙이 없는 심볼: {ns.symbol}")
        rules, pairs = {ns.symbol: TESTNET_RULES[ns.symbol]}, {ns.symbol: PAIRS[ns.symbol]}
        # [CP-041 D-271 (1)(5)] 검사 범위 = 명세 유니버스 ∩ 거래 규칙 표 전체 (실행 심볼 하나로 좁히지 않는다)
        problems = spot_rule_problems([StrategyInput(strat, params, times)], TESTNET_RULES, PAIRS)
        if problems:
            raise ConfigError([f"거래 규칙 검사: {x}" for x in problems])
        market = load_m2_market(root / ns.market_config)
        limits = load_limits_file(root / ns.limits, market=Market.M2).limits
        caps = load_capabilities(root / "docs" / "capabilities.md")
        final_from = load_validation_settings(root / "config" / "validation.toml")["final_from"]
        data_path = Path(ns.data)
        raw = data_path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        skipped: list[date] = []
        bars = load_binance_daily_csv(data_path, incomplete=skipped)
        start = date.fromisoformat(ns.start) if ns.start else bars[0].at
        end = date.fromisoformat(ns.end)
        used = [b for b in bars if b.at <= end]  # type: ignore[operator]
        skipped_used = [d for d in skipped if d <= end]
        source = ns.data_source or f"바이낸스 공개 1d klines CSV `{data_path.name}` (sha256 {sha[:16]})"
        if skipped_used:
            source += (f". 불완전 봉(거래 중단으로 일찍 끝난 봉) {len(skipped_used)}개 제외 — "
                       f"{', '.join(d.isoformat() for d in skipped_used)}: 그날 판정 데이터 없음(04 §2.1 결측, 주문 없음)")
        fees = _fees(ns.fee, market.fees)
        fee = fees.fill_rate
        out = Path(ns.out) if Path(ns.out).is_absolute() else root / ns.out
        cfg = SpotBacktestConfig(
            run_id=ns.run_id, strategy=strat, params=dict(params), account_limits=limits, rules=rules, pairs=pairs,
            cost_rates=CostRates(commission_rate=fees.estimate_rate, sell_tax_rate=Decimal("0"),
                                 cost_quantum=QUOTE_PRECISION),
            data={ns.symbol: used}, start=start, end=end, initial_cash=Decimal(ns.cash), out_dir=out,  # type: ignore[arg-type]
            slippage=Decimal(ns.slippage), eval_time=times[0], eval_times=times, buy_buffer=market.buy_buffer,
            sell_buffer=market.sell_buffer, quote_max_age=timedelta(seconds=market.quote_max_age_seconds),
            data_version=f"{data_path.parent.name}/{data_path.name} sha256:{sha[:16]}",
            final_from=final_from, caps=caps,  # type: ignore[arg-type]
            data_source=source, fees=fees)
        result = run_backtest_m2(cfg)
    except (ConfigError, BacktestError, ValueError, InvalidOperation, OSError) as exc:
        print(f"백테스트 거부: {exc}")
        return 2
    days = result.days
    bad = perturbation_check_m2(cfg, days[:: max(1, len(days) // 10)])
    reproducible = run_backtest_m2(replace(cfg, run_id=f"{cfg.run_id}-repro")).digest == result.digest
    run_dir = out / cfg.run_id
    (run_dir / "report.md").write_text(render_report_m2(cfg, result, reproducible=reproducible, lookahead_bad=bad),
                                       encoding="utf-8", newline="\n")
    dust_n, dust_left = dust_summary(result.fills, cfg)
    summary = {
        "run_id": cfg.run_id, "strategy": strat.spec.strategy_id, "version": strat.spec.version,
        "eval_times_utc": [t.strftime("%H:%M") for t in times], "period": [days[0].isoformat(), days[-1].isoformat()],
        "days": len(days), "data_version": cfg.data_version, "data_rows_used": len(used),
        "data_rows_after_end_dropped": len(bars) - len(used),
        "incomplete_bars_skipped": [d.isoformat() for d in skipped_used], "final_from": final_from.isoformat(),  # type: ignore[attr-defined]
        "fee_rate": str(fee), "fees": fee_model_summary(fees), "slippage": str(cfg.slippage),
        "initial_cash": str(cfg.initial_cash),
        "buy_buffer": str(cfg.buy_buffer), "sell_buffer": str(cfg.sell_buffer),
        "evaluations": len(result.signals_by_eval), "signals": len(result.signals),
        "fills": {side: sum(1 for f in result.fills if f[3] == side) for side in ("BUY", "SELL")},
        "round_trips": len(result.trades), "dust_events": dust_n, "dust_remaining": str(dust_left),
        "rejects": dict(sorted(result.rejects.items())), "k2_resumes": result.k2_resumes, "registration": "PASSED",
        "reproducible": reproducible, "lookahead_bad": bad, "digest": result.digest, "metrics": result.metrics,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                          newline="\n")
    print(f"백테스트 완료: {run_dir / 'report.md'}")
    return 0


GitRun = Callable[[Path, list[str]], "str | None"]


def _git_run(root: Path, args: list[str]) -> str | None:
    """읽기 전용 git 조회(log·status). 실패하면 None — 커밋을 확인하지 못한 것으로 본다(보수)."""
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def build_low_frequency_inputs(root: Path, strategy_id: str, version: int, params: dict[str, object], *, out: Path,
                               final_from: date, data_start: date, lookback: int,
                               git: GitRun = _git_run) -> LowFrequencyInputs | None:
    """[CP-038 T-058] 저빈도 선언 전략이면 판정 입력을 만든다. 명세 docs/strategies/<ID>/strategy.md에 선언 절이 없으면 None
    (일반 경로 그대로, V-313). 수치는 config/low-frequency.toml에서만(명령줄 옵션 없음, D-237 (1)).

    - E-1 선언 커밋: 명세 파일에 작업 트리 변경이 없을 때 그 파일의 마지막 커밋(해시·시각). 변경이 있거나 git이 없으면 None.
    - 공식 판정표 docs/strategies/<ID>/validation-v<n>.md가 있으면 처음 추가된 커밋 시각, 커밋 안 됐으면 UNKNOWN_PAST(사후로 간주).
    - 전체 이력 = 데이터 시작 + 워밍업(⌈lookback/1440⌉일) ~ final_from 전날(C-8·D-163).
    - LF-8: 표본 목록 해시를 설정 값과 대조만 한다. 표본 1분봉 실행 연결은 T-068 뒤 → 지금은 '판단 불가(표본 데이터 없음)'."""
    doc = root / "docs" / "strategies" / strategy_id / "strategy.md"
    if not doc.is_file():
        return None
    declaration, problems = parse_declaration(doc.read_text(encoding="utf-8"))
    if declaration is None and not problems:
        return None
    approvals = root / "docs" / "approvals.md"
    settings = load_low_frequency_settings(root / "config" / "low-frequency.toml",
                                           approvals_text=approvals.read_text(encoding="utf-8")
                                           if approvals.is_file() else "")
    rel = doc.relative_to(root).as_posix()
    if declaration is not None:
        commit = committed = None
        dirty = git(root, ["status", "--porcelain", "--", rel])
        log = git(root, ["log", "-1", "--format=%H|%cI", "--", rel]) if dirty is not None and not dirty.strip() else None
        if log and "|" in log.strip():
            h, _, at = log.strip().partition("|")
            try:
                commit, committed = h, datetime.fromisoformat(at)
            except ValueError:
                commit = committed = None
        declaration = replace(declaration, commit=commit, committed_at=committed)
    official = doc.parent / f"validation-v{version}.md"
    first_official: datetime | None = None
    if official.is_file():
        added = git(root, ["log", "--diff-filter=A", "--format=%cI", "--", official.relative_to(root).as_posix()])
        stamps = [x for x in (added or "").splitlines() if x.strip()]
        try:
            first_official = datetime.fromisoformat(stamps[-1].strip()) if stamps else UNKNOWN_PAST
        except ValueError:
            first_official = UNKNOWN_PAST
    list_path = root / settings.lf8_list_path
    sha = hashlib.sha256(list_path.read_bytes()).hexdigest() if list_path.is_file() else ""
    reason = NO_SAMPLE_WIRING if sha == settings.lf8_list_sha256 else (
        "LF-8 표본 목록 해시 불일치(설정 list_sha256과 다름 또는 파일 없음) — 표본 데이터 없음으로 판단 불가 (D-244 (6))")
    return LowFrequencyInputs(
        settings=settings, declaration=declaration, declaration_problems=problems, first_official_at=first_official,
        history_start=data_start + timedelta(days=-(-lookback // 1440)), history_end=final_from - timedelta(days=1),
        ledger=out / "low-frequency-ledger.jsonl", lf8=None, lf8_no_data_reason=reason,
        params_sha256=params_hash(params))


def backtest_m2_minute_main(argv: Sequence[str], root: Path) -> int:
    """`backtest --market M2 --interval 1m` (CP-037 T-053, D-221·D-222). 데이터 = A-110 공개 1m CSV(읽기만, 스트리밍).
    전략은 CAP-TRG-02(파라미터 period_minutes) — 판정 일정은 실운영과 같은 주기 격자. `--validate`면 D-222 6창(2020~2025)."""
    from functools import partial

    from trading_system.app.backtest_m2_minute import (
        MinuteBacktestConfig, MinuteValidationInputs, first_minute_start, iter_binance_minute_csv,
        perturbation_check_minute, render_minute_report, run_minute_backtest, run_minute_validation, summary_of,
    )
    p = argparse.ArgumentParser(prog="python -m trading_system.app backtest --market M2 --interval 1m")
    p.add_argument("--strategy", required=True, help="패키지.모듈:클래스 (CAP-TRG-02 주기 판정 전략)")
    p.add_argument("--params", required=True, help="파라미터 파일 (저장소 기준 또는 절대 경로)")
    p.add_argument("--data", required=True, help="바이낸스 공개 1m klines CSV (A-110 형식, 읽기만)")
    p.add_argument("--symbol", default="BTCUSDT", help="심볼 (거래 규칙이 있는 심볼만: BTCUSDT, ETHUSDT)")
    p.add_argument("--start", default=None, help="시작 UTC 날짜 (--validate가 아니면 필수)")
    p.add_argument("--end", default=None, help="끝 UTC 날짜 (final_from 전, --validate가 아니면 필수)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cash", default="10000", help="초기 USDT")
    p.add_argument("--lookback", required=True, type=int, help="판정 창 1분봉 수 (설정 [minute] lookback_bars와 같은 의미)")
    p.add_argument("--margin-seconds", default=2, type=int, help="확정 여유 초 (설정 [minute] confirm_margin_seconds, 기본 2)")
    p.add_argument("--slippage", required=True, help="불리 슬리피지 비율 (CP-030 D-164 기본 0.001). 기본값 없음 — 사용 값 기록")
    p.add_argument("--fee", default=None,
                   help="수수료율 — maker = taker = 이 값, 출처 user (기본: config/m2/market.paper.toml [costs], D-267)")
    p.add_argument("--limits", default="config/m2/limits.paper.toml", help="M2 한도 파일 (저장소 기준)")
    p.add_argument("--market-config", default="config/m2/market.paper.toml", help="M2 시장 설정 (저장소 기준)")
    p.add_argument("--out", default="var/backtest", help="결과 폴더 (저장소 기준 또는 절대 경로)")
    p.add_argument("--data-source", default="", help="보고서 데이터 출처 문구 (기본: 파일 이름·크기)")
    p.add_argument("--validate", action="store_true", help="D-222 검증 6창(2020~2025) 실행 — --start/--end 무시")
    p.add_argument("--skip-variants", action="store_true", help="검증: ±20%% 파라미터 변형(C-4) 생략 — 판정표에 미실행 표시")
    ns = p.parse_args(argv)
    try:
        if ns.margin_seconds < 0 or ns.margin_seconds > 59:
            raise ConfigError("--margin-seconds: 0~59")
        strat, params = load_strategy(StrategyDeploy(ns.strategy, (root / ns.params).resolve(), ()))
        if "CAP-TRG-02" not in strat.spec.capabilities:
            raise ConfigError("1분봉 백테스트는 CAP-TRG-02 주기 판정 전략만 (파라미터 period_minutes, CP-037 D-219)")
        # 실운영 조립 지점과 같은 일정 검사 (M2·주기 격자·주문 창 — app.wiring.period_grid_of, DP-79). 등록 범위(15·60분,
        # CP-040 D-258)는 실운영 등록에만 — 백테스트는 1440의 약수 그대로(공식 판정 경로 적용 여부는 T-068 몫)
        period_grid_of(StrategyInput(strat, params, ()), Market.M2.value, margin=timedelta(seconds=ns.margin_seconds),
                       registration=False)
        check = getattr(strat, "validate_deploy", None)
        problems = check(params, ()) if check is not None else []
        if problems:
            raise ConfigError([f"배포 검사: {x}" for x in problems])
        if ns.symbol not in TESTNET_RULES:
            raise ConfigError(f"거래 규칙이 없는 심볼: {ns.symbol}")
        rules, pairs = {ns.symbol: TESTNET_RULES[ns.symbol]}, {ns.symbol: PAIRS[ns.symbol]}
        # [CP-041 D-271 (1)(5)] 일봉 경로와 같은 검사 범위
        problems = spot_rule_problems([StrategyInput(strat, params, ())], TESTNET_RULES, PAIRS)
        if problems:
            raise ConfigError([f"거래 규칙 검사: {x}" for x in problems])
        market = load_m2_market(root / ns.market_config)
        limits = load_limits_file(root / ns.limits, market=Market.M2).limits
        caps = load_capabilities(root / "docs" / "capabilities.md")
        final_from = load_validation_settings(root / "config" / "validation.toml")["final_from"]
        data_path = Path(ns.data)
        if not data_path.is_file():
            raise ConfigError(f"데이터 파일 없음: {data_path.name}")
        size = data_path.stat().st_size
        source = ns.data_source or f"바이낸스 공개 1m klines CSV `{data_path.name}` ({size} 바이트, A-110)"
        fees = _fees(ns.fee, market.fees)
        out = Path(ns.out) if Path(ns.out).is_absolute() else root / ns.out
        if not ns.validate and (ns.start is None or ns.end is None):
            raise ConfigError("--start·--end가 필요 (--validate가 아니면)")
        start = date.fromisoformat(ns.start) if ns.start else date(2020, 1, 1)
        end = date.fromisoformat(ns.end) if ns.end else date(2020, 1, 1)
        cfg = MinuteBacktestConfig(
            run_id=ns.run_id, strategy=strat, params=dict(params), account_limits=limits, rules=rules, pairs=pairs,
            cost_rates=CostRates(commission_rate=fees.estimate_rate, sell_tax_rate=Decimal("0"),
                                 cost_quantum=QUOTE_PRECISION),
            symbol=ns.symbol, source=partial(iter_binance_minute_csv, data_path, final_from=final_from),  # type: ignore[arg-type]
            start=start, end=end, initial_cash=Decimal(ns.cash), out_dir=out, slippage=Decimal(ns.slippage),
            lookback_bars=ns.lookback, margin=timedelta(seconds=ns.margin_seconds), buy_buffer=market.buy_buffer,
            sell_buffer=market.sell_buffer, quote_max_age=timedelta(seconds=market.quote_max_age_seconds),
            final_from=final_from, caps=caps, data_version=f"{data_path.parent.name}/{data_path.name} {size}B",  # type: ignore[arg-type]
            data_source=source, fees=fees)
        if ns.validate:
            v_out = out / ns.run_id
            if v_out.exists():
                raise ConfigError(f"같은 실행 ID의 검증 폴더가 이미 있음: {v_out.name}")
            data_start = first_minute_start(data_path)
            # [CP-038 T-058] 명세에 저빈도 선언이 있을 때만 (없으면 None → 일반 경로 결과 불변)
            low = build_low_frequency_inputs(root, strat.spec.strategy_id, strat.spec.version, dict(params), out=out,
                                             final_from=final_from, data_start=data_start, lookback=ns.lookback)
            res = run_minute_validation(MinuteValidationInputs(
                template=cfg, out_dir=v_out, data_start=data_start, skip_variants=ns.skip_variants,
                command=" ".join(["backtest --market M2 --interval 1m", *argv]), low_frequency=low))
            print(f"검증 완료: {v_out / 'validation.md'} — 판정 {res['verdict']['verdict']}")
            return 0
        result = run_minute_backtest(cfg)
    except (ConfigError, BacktestError, LowFrequencyError, ValueError, InvalidOperation, OSError) as exc:
        print(f"백테스트 거부: {exc}")
        return 2
    days = result.result.days
    import time as _t
    t1 = _t.perf_counter()
    bad = perturbation_check_minute(cfg, days[:: max(1, len(days) // 10)])
    t2 = _t.perf_counter()
    repro_run = run_minute_backtest(replace(cfg, run_id=f"{cfg.run_id}-repro"))
    reproducible = repro_run.result.digest == result.result.digest
    result.extras = {"perturbation_seconds": round(t2 - t1, 2), "repro_seconds": repro_run.seconds}
    run_dir = out / cfg.run_id
    (run_dir / "report.md").write_text(render_minute_report(cfg, result, reproducible=reproducible, lookahead_bad=bad),
                                       encoding="utf-8", newline="\n")
    (run_dir / "summary.json").write_text(json.dumps(summary_of(cfg, result, reproducible=reproducible,
                                                                lookahead_bad=bad), ensure_ascii=False, indent=2)
                                          + "\n", encoding="utf-8", newline="\n")
    print(f"백테스트 완료: {run_dir / 'report.md'} (판정 {result.decisions}회, 거래 {len(result.result.trades)}건, "
          f"{result.seconds}초)")
    return 0


__all__ = ["backtest_m2_main", "backtest_m2_minute_main", "load_binance_daily_csv", "TESTNET_RULES"]
