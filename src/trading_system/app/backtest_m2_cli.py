"""M2 백테스트 명령 `python -m trading_system.app backtest --market M2` (T-M2-15, A-111 Q-1 (가)).

- 데이터: 바이낸스 공개 1d klines CSV(A-110 수집 형식: open_time_utc,open,high,low,close,volume,close_time_utc,…, ISO UTC 시각).
  `load_binance_daily_csv`가 형식을 검사해 UTC 일봉(Bar.at = 봉 시작 날짜)으로 바꾼다. 파일은 읽기만 한다.
- 불완전 봉(거래 중단으로 일찍 끝난 1d 봉)은 빼고 날짜를 보고서·요약에 적는다 → 그날은 판정 데이터 없음(04 §2.1 결측, 주문 없음).
- 실행 끝 날짜(--end) 뒤의 행은 실행기에 넘기지 않는다(판정에 쓰이지 않는 미래 행 — 개수를 요약에 기록).
  최종 확인 구간 보호(CP-030 D-163)는 실행기 그대로: 실행 날짜가 config/validation.toml final_from 이후와 겹치면 거부.
  M2에는 최종 확인 실행(--final-check·장부)이 없다 — 겹치면 항상 거부.
- 실운영 시작 검사와 같은 검사: 전략 validate_deploy(판정 시각), validate_rules(거래 규칙 — app.wiring.spot_rule_problems),
  전략 등록(docs/capabilities.md M2 열, BACKTEST 대상).
- 거래 규칙: 테스트넷 BTCUSDT 실측값(broker-specs/binance-spot.md §12, 2026-10-14)만 있다 — PAPER 대상과 같은 규칙으로 과거
  가격을 돌린다(실서버 과거 시점의 규칙이 아님, 보고서 한계). 다른 심볼은 규칙이 없어 거부.
- 비용: 수수료율 기본 = config/m2/market.paper.toml [costs] commission_rate(M2 비용 추정 요율), 슬리피지는 필수 인자(기본값 없음).
- 결과: <out>/<실행 ID>/trading.db·report.md·summary.json. 재현성(같은 입력 재실행 지문 비교)·교란 검사(V-91, 표본 = 기간을 약 10등분한 날짜, M1과 같음)를 함께 한다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2 import (
    SpotBacktestConfig, dust_summary, perturbation_check_m2, render_report_m2, run_backtest_m2,
)
from trading_system.app.validation_tool import load_validation_settings
from trading_system.app.wiring import StrategyInput, load_strategy, spot_rule_problems
from trading_system.config.app_file import StrategyDeploy
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.m2_files import load_m2_market
from trading_system.domain.market import CostRates
from trading_system.domain.market_time import Market
from trading_system.domain.strategy import Bar
from trading_system.domain.trading_rules import SymbolRules

# 테스트넷 BTCUSDT 거래 규칙 실측값 (binance-spot.md §12, 2026-10-14 PV-M2-03). 가격·잔고 아님
TESTNET_RULES: dict[str, SymbolRules] = {
    "BTCUSDT": SymbolRules(
        instrument="BTCUSDT", tick_size=Decimal("0.01"), min_price=Decimal("0.01"), max_price=Decimal("1000000"),
        lot_min_qty=Decimal("0.00001"), lot_max_qty=Decimal("9000"), lot_step=Decimal("0.00001"),
        market_min_qty=Decimal("0"), market_max_qty=Decimal("133.97193096"), market_step=Decimal("0"),
        min_notional=Decimal("5"), max_notional=Decimal("9000000"), apply_min_to_market=True,
        apply_max_to_market=False),
}
PAIRS: dict[str, tuple[str, str]] = {"BTCUSDT": ("BTC", "USDT")}
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


def _times(text: str) -> tuple[time, ...]:
    try:
        return tuple(time(int(hh), int(mm)) for hh, mm in (part.strip().split(":") for part in text.split(",")))
    except ValueError:
        raise ConfigError("--eval-time 형식: HH:MM[,HH:MM…] (UTC)") from None


def backtest_m2_main(argv: Sequence[str], root: Path) -> int:
    p = argparse.ArgumentParser(prog="python -m trading_system.app backtest --market M2")
    p.add_argument("--strategy", required=True, help="패키지.모듈:클래스")
    p.add_argument("--params", required=True, help="파라미터 파일 (저장소 기준)")
    p.add_argument("--data", required=True, help="바이낸스 공개 1d klines CSV 파일 (읽기만)")
    p.add_argument("--symbol", default="BTCUSDT", help="심볼 (거래 규칙이 있는 심볼만: BTCUSDT)")
    p.add_argument("--start", default=None, help="시작 UTC 날짜 (기본: 첫 봉)")
    p.add_argument("--end", required=True, help="끝 UTC 날짜 (final_from 전)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cash", default="10000", help="초기 USDT")
    p.add_argument("--eval-time", default="00:15", help="판정 시각 HH:MM(UTC), 여러 개는 쉼표 (예: 01:00,04:00)")
    p.add_argument("--slippage", required=True, help="불리 슬리피지 비율 (예: 0.001). 기본값 없음 — 사용 값을 기록")
    p.add_argument("--fee", default=None, help="수수료율 (기본: config/m2/market.paper.toml [costs] commission_rate)")
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
        problems = spot_rule_problems([StrategyInput(strat, params, times)], rules, pairs)
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
        fee = Decimal(ns.fee) if ns.fee else market.commission_rate
        out = Path(ns.out) if Path(ns.out).is_absolute() else root / ns.out
        cfg = SpotBacktestConfig(
            run_id=ns.run_id, strategy=strat, params=dict(params), account_limits=limits, rules=rules, pairs=pairs,
            cost_rates=CostRates(commission_rate=fee, sell_tax_rate=Decimal("0"), cost_quantum=QUOTE_PRECISION),
            data={ns.symbol: used}, start=start, end=end, initial_cash=Decimal(ns.cash), out_dir=out,  # type: ignore[arg-type]
            slippage=Decimal(ns.slippage), eval_time=times[0], eval_times=times, buy_buffer=market.buy_buffer,
            sell_buffer=market.sell_buffer, quote_max_age=timedelta(seconds=market.quote_max_age_seconds),
            data_version=f"{data_path.parent.name}/{data_path.name} sha256:{sha[:16]}",
            final_from=final_from, caps=caps,  # type: ignore[arg-type]
            data_source=source)
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
        "fee_rate": str(fee), "slippage": str(cfg.slippage), "initial_cash": str(cfg.initial_cash),
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


__all__ = ["backtest_m2_main", "load_binance_daily_csv", "TESTNET_RULES"]
