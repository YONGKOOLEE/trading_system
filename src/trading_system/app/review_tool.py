"""PAPER 실행 기록 집계 도구 (T-017 준비, A-044) — 사람이 실행. 읽기 전용, 브로커 연결 없음.

사용: .venv/Scripts/python scripts/paper-review.py --mode PAPER [--root .] [--out 파일]
결과: 마크다운 집계를 var/<mode>/review/review-<시각>.md(기본)에 쓰고 경로를 출력한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from trading_system.config.errors import ConfigError
from trading_system.config.market_file import load_market_settings
from trading_system.config.modes import parse_trade_mode
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode
from trading_system.reporting.paper_review import render_markdown, review


def review_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/paper-review.py", description="PAPER 실행 기록 집계(읽기 전용)")
    p.add_argument("--mode", required=True, help="PAPER|BACKTEST")
    p.add_argument("--root", default=".")
    p.add_argument("--out", help="출력 파일 (기본 var/<mode>/review/review-<시각>.md)")
    ns = p.parse_args(argv)
    try:
        mode = parse_trade_mode(ns.mode)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    if mode not in (Mode.PAPER, Mode.BACKTEST):
        print(f"거부: {mode.value}는 이 도구에서 다루지 않는다")
        return 2
    root = Path(ns.root)
    base = root / "var" / mode.value.lower()
    rates = None
    try:
        rates = load_market_settings(root / "config" / f"market.{mode.value.lower()}.toml").cost_rates
    except (ConfigError, OSError) as exc:
        print(f"경고: 비용 요율을 읽지 못해 추정 비용 없이 집계 ({type(exc).__name__})")
    try:
        result = review(base / "trading.db", rates)
    except FileNotFoundError:
        print(f"거부: 저장소 없음 ({base / 'trading.db'})")
        return 1
    out = Path(ns.out) if ns.out else base / "review" / f"review-{datetime.now(KST):%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(result), encoding="utf-8")
    print(f"집계 완료: {out} (거래일 {len(result.trade_dates)}, 의도 {sum(result.intent_states.values())}, "
          f"정지 {len(result.halts)}, 긴급 알림 {len(result.emergencies)})")
    return 0
