"""전략 등록·상태 운영 도구 (R-17, R-20, 00 §2.11, INV-13, T-016). 사람이 실행한다. 본체는 이 모듈, `scripts/strategy.py`가 부른다.

- show: 모드 저장소의 전략 상태와 전이 이력(승인 A-ID)을 보여 준다.
- register: 배포 설정(`config/app.<mode>.toml`)의 전략을 그 모드 저장소에 등록 검증한다(엔진 시작 시와 같은 검증,
  PAPER-0 검증 전략은 verification_approval 사용 — DP-24 a). 상태가 없으면 초안(DRAFT)으로 만든다.
- transition: 사용자 승인 A-ID로 상태를 바꾼다. 백테스트 통과에는 이 시스템 백테스트 보고서 경로가 필요하다(A-029).
  허용 전이·승인 형식·증거 검사는 StrategyRegistry가 한다 — 이 도구는 우회하지 않는다.
- BACKTEST·PAPER만. LIVE는 실전 경로가 없어 거부한다. 브로커에 연결하지 않는다.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from trading_system.config.app_file import load_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.modes import parse_trade_mode
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import SystemClock
from trading_system.domain.enums import Mode
from trading_system.domain.strategy import StopReason, StrategyState
from trading_system.storage.repository import Repository

from .wiring import load_strategy

REPO_ROOT = Path(__file__).resolve().parents[3]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scripts/strategy.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("show", "register", "transition"):
        s = sub.add_parser(name)
        s.add_argument("--mode", required=True, help="BACKTEST|PAPER")
        s.add_argument("--root", default=str(REPO_ROOT))
        if name == "transition":
            s.add_argument("strategy_id")
            s.add_argument("version", type=int)
            s.add_argument("to_state", choices=[x.value for x in StrategyState])
            s.add_argument("--approval", required=True, help="사용자 승인 A-ID (docs/approvals.md)")
            s.add_argument("--evidence", help="백테스트 통과: 이 시스템 백테스트 보고서 경로")
            s.add_argument("--stop-reason", choices=[x.value for x in StopReason])
    return p


def strategy_main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    ns = _parser().parse_args(argv)
    try:
        mode = parse_trade_mode(ns.mode)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    if mode not in (Mode.BACKTEST, Mode.PAPER):
        print(f"거부: {mode.value}는 이 도구에서 다루지 않는다 (실전 경로 없음)")
        return 2
    base = Path(ns.root)
    db = base / "var" / mode.value.lower() / "trading.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    repo = Repository(db, mode)
    try:
        registry = StrategyRegistry(repo, SystemClock(), None)
        if ns.cmd == "show":
            return _show(repo)
        if ns.cmd == "register":
            return _register(registry, base, mode)
        try:
            registry.transition(ns.strategy_id, ns.version, StrategyState(ns.to_state), ns.approval,
                                evidence=ns.evidence, stop_reason=StopReason(ns.stop_reason) if ns.stop_reason else None)
        except ValueError as exc:
            print(f"전이 거부: {exc}")
            return 1
        print(f"전이 완료: {ns.strategy_id} v{ns.version} → {ns.to_state} (승인 {ns.approval})")
        return 0
    finally:
        repo.close()


def _register(registry: StrategyRegistry, root: Path, mode: Mode) -> int:
    try:
        app = load_app_settings(root / "config" / f"app.{mode.value.lower()}.toml", mode, root)
        limits = load_limits_file(root / "config" / f"limits.{mode.value.lower()}.toml").limits
        caps = load_capabilities(root / "docs" / "capabilities.md")
        if not app.strategies:
            raise ConfigError("배포 설정에 전략이 없다")
        loaded = [(dep, *load_strategy(dep)) for dep in app.strategies]
    except ConfigError as exc:
        print(f"등록 거부: {exc}")
        return 1
    code = 0
    for dep, strat, params in loaded:
        r = registry.register(strat.spec, dict(params), caps, limits, mode,
                              verification_approval=dep.verification_approval)
        if r.ok:
            print(f"등록 통과: {strat.spec.owner} ({mode.value} 대상"
                  f"{', PAPER-0 검증 승인 ' + dep.verification_approval if dep.verification_approval else ''})")
        else:
            code = 1
            print(f"등록 거부: {strat.spec.owner}")
            for p in r.problems:
                print(f"  - {p}")
    return code


def _show(repo: Repository) -> int:
    rows = repo.strategy_states()
    print(f"전략 {len(rows)}개")
    for r in rows:
        print(f"  {r['strategy_id']} v{r['version']} {r['market']}: {r['state']}"
              f"{' (중지 사유 ' + r['stop_reason'] + ')' if r['stop_reason'] else ''}")
        for t in repo.strategy_transitions(r["strategy_id"], r["version"]):
            print(f"    {t['from_state'] or '-'} → {t['to_state']} 승인 {t['approval_id']}"
                  f"{' 증거 ' + t['evidence'] if t['evidence'] else ''}")
    return 0
