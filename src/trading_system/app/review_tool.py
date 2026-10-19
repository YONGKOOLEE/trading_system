"""PAPER 실행 기록 집계 도구 (T-017 준비, A-044) — 사람이 실행. 읽기 전용, 브로커 연결 없음.

사용: .venv/Scripts/python scripts/paper-review.py --mode PAPER [--market M1|M2] [--root .] [--out 파일]
결과: 마크다운 집계를 var/<mode>/review/review-<시각>.md(기본)에 쓰고 경로를 출력한다.
--market M2 (CP-037 T-055, D-232): M2 체크아웃(config/m2)에서만. 비용 요율은 config/m2/market.<mode>.toml, 끝에 M2 PAPER-1
집계 표(reporting/m2_paper1 — 판정 없음, 표만)를 붙인다. M1(기본)은 기존 동작 그대로.
[CP-039 T-063] M2 DB의 신호 출처가 binance-public이면 신호 출처 분리 집계(①실행 ②신호 대비 ③괴리 ④백테스트 가정 대비 —
reporting/m2_signal_review)를 더 붙인다(testnet DB는 붙이는 것 없음 — 기존 결과 불변). --compare-bars CSV --symbol SYM:
A-110 수집 도구가 같은 기간을 재수집한 1분봉 CSV와 저장 봉(binance-public)을 대조한다(D-255 (a), 판정 아님).
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
    p.add_argument("--market", default="M1", help="M1|M2 (기본 M1). M2는 M2 체크아웃에서 — PAPER-1 집계 표 추가")
    p.add_argument("--compare-bars", help="(M2) 재수집 1분봉 CSV(A-110 형식) — 저장 신호 출처 봉과 대조 (CP-039 D-255)")
    p.add_argument("--symbol", help="(M2) --compare-bars 대상 심볼")
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
    if ns.market not in ("M1", "M2"):
        print(f"거부: --market {ns.market!r}는 M1·M2만")
        return 2
    m2 = ns.market == "M2"
    if ns.compare_bars and (not m2 or not ns.symbol):
        print("거부: --compare-bars는 --market M2와 --symbol이 함께 필요")
        return 2
    if m2 and not (root / "config" / "m2" / f"app.{mode.value.lower()}.toml").is_file():
        print(f"거부: --market M2인데 config/m2/app.{mode.value.lower()}.toml이 없음 — M2 체크아웃에서 실행")
        return 2
    rates = None
    try:
        rates = _m2_rates(root, mode) if m2 else             load_market_settings(root / "config" / f"market.{mode.value.lower()}.toml").cost_rates
    except (ConfigError, OSError) as exc:
        print(f"경고: 비용 요율을 읽지 못해 추정 비용 없이 집계 ({type(exc).__name__})")
    try:
        result = review(base / "trading.db", rates)
    except FileNotFoundError:
        print(f"거부: 저장소 없음 ({base / 'trading.db'})")
        return 1
    out = Path(ns.out) if ns.out else base / "review" / f"review-{datetime.now(KST):%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(result)
    if m2:
        text += "\n" + _m2_paper1_section(root, mode, base / "trading.db")
        text += m2_signal_section(base / "trading.db", mode, _m2_account(root, mode),
                                  Path(ns.compare_bars) if ns.compare_bars else None, ns.symbol)
    out.write_text(text, encoding="utf-8")
    print(f"집계 완료: {out} (거래일 {len(result.trade_dates)}, 의도 {sum(result.intent_states.values())}, "
          f"정지 {len(result.halts)}, 긴급 알림 {len(result.emergencies)})")
    return 0


def _m2_account(root: Path, mode: Mode) -> str | None:
    from trading_system.config.app_file import load_app_settings
    try:
        return load_app_settings(root / "config" / "m2" / f"app.{mode.value.lower()}.toml", mode, root).account
    except (ConfigError, OSError):
        return None


def m2_signal_section(db: Path, mode: Mode, account: str | None, bars_csv: Path | None = None,
                      symbol: str | None = None) -> str:
    """[CP-039 T-063] 신호 출처 분리 집계 + (선택) 봉 대조. testnet DB·계좌 미확인이면 빈 문자열(봉 대조는 계좌와 무관)."""
    from datetime import timedelta

    from trading_system.reporting.m2_signal_review import (
        collect_signal_review, compare_recollected_bars, load_recollected_csv, render_bar_compare, render_signal_review,
    )
    from trading_system.storage.repository import OrderIdentity, Repository
    try:
        repo = Repository(db, mode, order_identity=OrderIdentity.SYMBOL)
    except Exception as exc:  # noqa: BLE001 — 집계 실패가 기존 집계 파일을 막지 않게
        return f"\n## M2 신호 출처 분리 집계\n\n저장소를 열지 못해 생략 ({type(exc).__name__})\n"
    out = ""
    try:
        if account is not None:
            out += render_signal_review(collect_signal_review(repo, account))
        if bars_csv is not None and symbol:
            try:
                recol = load_recollected_csv(bars_csv)
            except (OSError, KeyError, ValueError) as exc:
                return out + f"\n## 신호 출처 봉 대조\n\n재수집본을 읽지 못함 ({type(exc).__name__})\n"
            if recol:
                out += "\n" + render_bar_compare(symbol, compare_recollected_bars(
                    repo, symbol, recol, start=min(recol), end=max(recol) + timedelta(minutes=1)))
    finally:
        repo.close()
    return ("\n" + out) if out else ""


def _m2_rates(root: Path, mode: Mode):  # type: ignore[no-untyped-def]
    from decimal import Decimal

    from trading_system.config.m2_files import load_m2_market
    from trading_system.domain.market import CostRates
    ms = load_m2_market(root / "config" / "m2" / f"market.{mode.value.lower()}.toml")
    return CostRates(ms.commission_rate, Decimal("0"))


def _m2_paper1_section(root: Path, mode: Mode, db: Path) -> str:
    """[CP-037 T-055] M2 PAPER-1 집계 표. 배포 일정을 읽지 못하면 "일정 미확인"으로 표시(보수 — 추정하지 않음)."""
    from datetime import UTC, timedelta

    from trading_system.config.app_file import load_app_settings
    from trading_system.reporting.m2_paper1 import build_m2_paper1, render_m2_paper1
    try:
        app = load_app_settings(root / "config" / "m2" / f"app.{mode.value.lower()}.toml", mode, root)
    except (ConfigError, OSError) as exc:
        return f"## M2 PAPER-1 집계\n\n배포 설정을 읽지 못해 생략 ({type(exc).__name__})\n"
    schedules = _m2_schedules(root, mode, app)
    gap = timedelta(minutes=3 * max(1, int(app.reconcile_minutes)))
    try:
        table = build_m2_paper1(db, app.account, datetime.now(UTC), schedules, reconcile_gap=gap)
    except Exception as exc:  # noqa: BLE001 — 표 실패가 기존 집계 파일을 막지 않게
        return f"## M2 PAPER-1 집계\n\n집계 실패 ({type(exc).__name__})\n"
    return render_m2_paper1(table)


def _m2_schedules(root: Path, mode: Mode, app):  # type: ignore[no-untyped-def]
    """배포마다 판정 일정: eval_times(UTC) 또는 파라미터 period_minutes(주기 격자). 하나라도 못 읽으면 빈 목록(일정 미확인)."""
    import tomllib
    from datetime import timedelta

    from trading_system.config.m2_files import load_m2_market
    from trading_system.domain.minute_series import PeriodGrid
    from trading_system.reporting.m2_paper1 import EvalSchedule
    out = []
    try:
        margin = timedelta(seconds=load_m2_market(root / "config" / "m2" /
                                                  f"market.{mode.value.lower()}.toml").minute.confirm_margin_seconds)
        for dep in app.strategies:
            if dep.eval_times:
                out.append(EvalSchedule(times_utc=tuple(dep.eval_times)))
                continue
            with Path(dep.params).open("rb") as f:
                n = (tomllib.load(f).get("params") or {}).get("period_minutes")
            out.append(EvalSchedule(period=PeriodGrid(n, margin)))
    except Exception:  # noqa: BLE001 — 형식·파일 문제는 일정 미확인으로
        return []
    return out
