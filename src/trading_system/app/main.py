"""`python -m trading_system.app` 진입점 (T-020, DP-23). 사람이 직접 실행한다. 자동 실행 등록은 하지 않는다.

  run       TRADE_MODE(환경 변수 → .env 한 줄)로 모드를 정해 엔진을 돌린다. 값이 없거나 틀리면 연결 없이 종료.
            LIVE는 거부(실전 경로 없음). PAPER는 config/app.paper.toml의 [paper] connection_approved = true
            (사용자가 키움 모의 접속을 승인한 뒤 직접 바꿈, T-015·D-083)일 때만 키움 모의에 접속한다.
            BACKTEST는 fake 브로커로 엔진을 돌리는 연습 실행(운영 명령·정지 훈련용, 시세 없음 → 위험 확대 거부).
  backtest  일봉 CSV로 백테스트를 돌려 var/backtest/<실행ID>/에 DB·보고서를 남긴다.

스레드 (03 D-038 v0.5): 판정·실행 = 이 프로세스의 주 스레드. 실시간 수신·시세 조회·텔레그램은 각자 스레드이며
저장소 연결을 따로 쓰고, 정지 발동·대조 요청은 엔진에 맡긴다(call_soon). 주문은 판정·실행 스레드만 한다.
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import threading
import time
from collections.abc import Sequence
from datetime import date, datetime, time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.config.app_file import AppSettings, load_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.market_file import load_market_settings, parse_alert_settings
from trading_system.config.modes import parse_trade_mode, read_trade_mode
from trading_system.domain.clock import SystemClock
from trading_system.domain.enums import HaltType, Mode
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.marketdata.ticks import load_ticks
from trading_system.monitoring.liveness import LoopMonitor, write_heartbeat
from trading_system.ops.cli import REPO_ROOT
from trading_system.storage.repository import Repository

from .engine import Engine
from .startup import LiveNotAvailable, check_startup
from .wiring import StrategyInput, build_engine, load_strategy


class QueuedHalts:
    """다른 스레드용 HaltControl: 실제 발동은 판정·실행 스레드에서 (저장소 연결을 스레드 사이에 나누지 않음)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, **kw: object) -> str:
        self._engine.call_soon(lambda: self._engine.halts.activate(halt_type, actor, reason, account, **kw))
        return ""

    def report_condition(self, halt_type: HaltType, account: str | None, condition: str, ok: bool,
                         detail: str = "", **kw: object) -> None:
        self._engine.call_soon(lambda: self._engine.halts.report_condition(halt_type, account, condition, ok,
                                                                           detail, **kw))


# --- 공통 로딩 ------------------------------------------------------------------------------------

def _load_common(root: Path, mode: Mode) -> tuple:  # type: ignore[type-arg]
    market_path = root / "config" / f"market.{mode.value.lower()}.toml"
    gate_settings = load_market_settings(market_path)
    import tomllib
    with market_path.open("rb") as f:
        alert_thresholds = parse_alert_settings(tomllib.load(f))
    calendar = KrxCalendar.load_dir(root / "config" / "calendar")
    ticks_path = root / "config" / "ticks" / "krx.toml"
    ticks = load_ticks(ticks_path) if ticks_path.exists() else None
    caps = load_capabilities(root / "docs" / "capabilities.md")
    return gate_settings, alert_thresholds, calendar, ticks, caps


def _strategies(app: AppSettings) -> list[StrategyInput]:
    out = []
    for dep in app.strategies:
        strat, params = load_strategy(dep)
        out.append(StrategyInput(strat, params, dep.eval_times, dep.verification_approval))
    return out


# --- run ------------------------------------------------------------------------------------------

def run_main(root: Path, environ: dict[str, str] | None = None, max_seconds: float | None = None) -> int:
    env = os.environ if environ is None else environ
    try:
        mode = read_trade_mode(env, root / ".env")
    except ConfigError as exc:
        print(f"시작 거부(브로커 연결 없음): {exc}")
        return 2
    base = root / "var" / mode.value.lower()
    try:
        env_file = None if mode is Mode.BACKTEST else root / f".env.{mode.value.lower()}"
        startup = check_startup(mode, root, base / "trading.db", env_file)
        app = load_app_settings(root / "config" / f"app.{mode.value.lower()}.toml", mode, root)
        if app.account != startup.account:
            raise ConfigError("배포 설정의 계좌 별칭과 한도 파일의 계좌가 다르다")
        gate_settings, alert_thresholds, calendar, ticks, caps = _load_common(root, mode)
        strategies = _strategies(app)
        if mode is Mode.PAPER and not app.paper_connection_approved:
            raise ConfigError("키움 모의 접속 승인 전 (config/app.paper.toml [paper] connection_approved = true는 "
                              "사용자가 T-015 승인 후 직접 설정)")
    except (LiveNotAvailable, ConfigError) as exc:
        print(f"시작 거부(브로커 연결 없음): {exc}")
        return 2

    base.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    repo = Repository(startup.storage_path, mode)
    store = MarketDataStore(calendar, ticks)
    for info in app.instruments.values():
        store.register(info)
    stop = threading.Event()
    threads: list[threading.Thread] = []
    closers: list = []
    try:
        from trading_system.monitoring.sntp import ClockMonitor
        try:
            if mode is Mode.PAPER:
                broker, extra = _paper_broker(startup, app, clock, store)
            else:
                from trading_system.broker.fake.broker import FakeBroker
                broker, extra = FakeBroker(), None
            clock_monitor = ClockMonitor(repo, clock, list(app.sntp_servers), None, None)
            engine = build_engine(mode=mode, repo=repo, clock=clock, broker=broker, store=store,
                                  account=app.account, account_limits=startup.limits, gate_settings=gate_settings,
                                  caps=caps, strategies=strategies, kill_file=base / "KILL",
                                  clock_health=clock_monitor, alert_thresholds=alert_thresholds,
                                  fallback_log=base / "logs" / "alerts-fallback.log",
                                  reconcile_every=_minutes(app.reconcile_minutes))
        except ConfigError as exc:
            print(f"시작 거부(주문 없음): {exc}")
            return 2
        clock_monitor._halts, clock_monitor._alerts = engine.halts, engine.alerts  # noqa: SLF001 — 조립 지점
        clock_monitor.check()
        _hooks(engine, clock_monitor, base)
        if extra is not None:
            threads += extra(engine, stop, closers)
        rep = engine.start()
        print(f"시작: 모드 {mode.value}, 계좌 {app.account}, 복구 판정 {'통과' if rep.ok else '실패'} "
              f"— K8 재개: scripts/resume.py --mode {mode.value} K8")
        for t in threads:
            t.start()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        started = time.monotonic()
        while not stop.is_set():
            engine.tick()
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                break
            stop.wait(float(app.loop_seconds))
        return 0
    finally:
        stop.set()
        for t in threads:
            t.join(10)
        for c in closers:
            try:
                c()
            except Exception:  # noqa: BLE001 — 종료 중 오류는 무시
                pass
        repo.close()


def _minutes(n: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta
    return timedelta(minutes=n)


def _hooks(engine: Engine, clock_monitor, base: Path) -> None:  # type: ignore[no-untyped-def]
    loop = LoopMonitor(engine.halts, engine.alerts)
    last_clock = [time.monotonic()]

    def hook(now: datetime) -> None:
        write_heartbeat(base / "heartbeat", now)
        loop.tick(time.monotonic(), now)
        if time.monotonic() - last_clock[0] > 600:   # 10분마다 시계 점검 (01 §2.7)
            last_clock[0] = time.monotonic()
            clock_monitor.check()

    engine.on_tick.append(hook)


def _paper_broker(startup, app: AppSettings, clock, store: MarketDataStore):  # type: ignore[no-untyped-def]
    from trading_system.broker.kiwoom.adapter import KiwoomAdapter, KiwoomConfig
    from trading_system.broker.kiwoom.realtime import KiwoomRealtime, WsState, order_handlers
    from trading_system.marketdata.kiwoom_feed import KiwoomMarketFeed, MarketPoller
    s = startup.secrets
    need = ("KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_SECRET_KEY", "KIWOOM_PAPER_ACCOUNT_NO", "KIWOOM_PAPER_ACCOUNT_ALIAS")
    missing = [k for k in need if s is None or not s.has(k)]
    if missing:
        raise ConfigError(f".env.paper에 없는 키: {', '.join(missing)}")
    if s.get("KIWOOM_PAPER_ACCOUNT_ALIAS").reveal() != app.account:
        raise ConfigError(".env.paper의 계좌 별칭이 배포 설정과 다르다")
    holder: dict = {}

    def critical(kind: str, msg: str) -> None:
        e = holder.get("engine")
        if e is None:
            return
        QueuedHalts(e).activate(HaltType.K5, "broker", f"{kind}: {msg}", app.account)
        e.call_soon(lambda: e.alerts.alert("EMERGENCY", f"브로커 치명 오류 {kind} — K5", None))
        if kind == "MODE_MISMATCH":
            holder["stop"].set()   # 모드 불일치는 즉시 중단 (03 §4)

    adapter = KiwoomAdapter(KiwoomConfig(app.account, s.get("KIWOOM_PAPER_ACCOUNT_NO").reveal,
                                         s.get("KIWOOM_PAPER_APP_KEY").reveal, s.get("KIWOOM_PAPER_SECRET_KEY").reveal),
                            clock, on_critical=critical)

    def extra(engine: Engine, stop: threading.Event, closers: list) -> list[threading.Thread]:
        holder["engine"], holder["stop"] = engine, stop
        feed = KiwoomMarketFeed(adapter, store, clock, app.instruments,
                                held=lambda c: c in engine.repo.positions(app.account), alerts=None)

        def on_state(state: WsState, msg: str) -> None:
            if state is WsState.OFFLINE:
                QueuedHalts(engine).activate(HaltType.K5, "realtime", f"실시간 끊김: {msg}", app.account)
            elif state is WsState.RECONNECTED:
                engine.request_reconcile()   # 대조 일치 → K5 자동 해제 조건 (01 K5)

        handlers = {**order_handlers(adapter.push_event, app.account, clock,
                                     lambda v: adapter._check_account(v, "00")),  # noqa: SLF001
                    **feed.handlers()}
        rt = KiwoomRealtime(adapter.cfg.ws_url, adapter.tokens, clock,
                            registrations=[("00", []), ("04", [])] + feed.registrations(), handlers=handlers,
                            on_state=on_state, on_critical=critical)
        poller = MarketPoller(feed)
        closers.extend([rt.stop, adapter.close])
        for code in app.instruments:
            feed.refresh_daily(code)
        return [threading.Thread(target=rt.start, name="ws-start", daemon=True),
                threading.Thread(target=poller.run, args=(stop, 1.2), name="market-poller", daemon=True)]

    return adapter, extra


# --- backtest ---------------------------------------------------------------------------------------

def load_daily_csv(path: Path) -> list[RawDaily]:
    """일봉 CSV: date,open,high,low,close,volume[,corporate_action] (날짜 YYYY-MM-DD, 숫자는 문자열 그대로 Decimal)."""
    rows: list[RawDaily] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        need = {"date", "open", "high", "low", "close", "volume"}
        if reader.fieldnames is None or not need <= set(reader.fieldnames):
            raise ConfigError(f"{path.name}: 열이 부족 (필요: {', '.join(sorted(need))})")
        for i, r in enumerate(reader, start=2):
            try:
                rows.append(RawDaily(date.fromisoformat(r["date"]), Decimal(r["open"]), Decimal(r["high"]),
                                     Decimal(r["low"]), Decimal(r["close"]), Decimal(r["volume"]),
                                     (r.get("corporate_action") or "").strip().lower() in ("1", "true", "y")))
            except (ValueError, InvalidOperation) as exc:
                raise ConfigError(f"{path.name}:{i}: 형식 오류 ({type(exc).__name__})") from None
    return rows


def backtest_main(argv: Sequence[str], root: Path) -> int:
    from trading_system.app.backtest import BacktestConfig, BacktestError, perturbation_check, render_report, \
        run_backtest, run_sensitivity
    from trading_system.config.app_file import StrategyDeploy
    from trading_system.config.limits_file import load_limits_file
    p = argparse.ArgumentParser(prog="python -m trading_system.app backtest")
    p.add_argument("--strategy", required=True, help="패키지.모듈:클래스")
    p.add_argument("--params", required=True, help="파라미터 파일 (config/ 아래)")
    p.add_argument("--data", required=True, help="종목코드.csv 파일이 있는 폴더")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--cash", default="10000000")
    p.add_argument("--eval-time", default="09:05")
    p.add_argument("--segment", default="ALL", choices=["ALL", "TRAIN", "VALIDATION", "FINAL"])
    p.add_argument("--slippage-tick", default="10", help="호가 표가 없을 때 1틱 크기(원)")
    ns = p.parse_args(argv)
    try:
        mode = Mode.BACKTEST
        gate_settings, _, calendar, ticks, caps = _load_common(root, mode)
        limits = load_limits_file(root / "config" / "limits.backtest.toml").limits
        hh, mm = ns.eval_time.split(":")
        strat, params = load_strategy(StrategyDeploy(ns.strategy, (root / ns.params).resolve(), ()))
        data_dir = Path(ns.data)
        data = {f.stem: load_daily_csv(f) for f in sorted(data_dir.glob("*.csv"))}
        if not data:
            raise ConfigError("데이터 CSV가 없음")
        from trading_system.domain.market import InstrumentInfo, InstrumentKind
        infos = {code: InstrumentInfo(code, InstrumentKind.STOCK, Decimal("1"), Decimal("1")) for code in data}
        cfg = BacktestConfig(run_id=ns.run_id, strategy=strat, params=params, caps=caps, account_limits=limits,
                             gate_settings=gate_settings, calendar=calendar, ticks=ticks,
                             slippage_tick=Decimal(ns.slippage_tick), data=data, infos=infos,
                             start=date.fromisoformat(ns.start), end=date.fromisoformat(ns.end),
                             initial_cash=Decimal(ns.cash), out_dir=root / "var" / "backtest",
                             eval_time=dtime(int(hh), int(mm)), segment=ns.segment,
                             final_usage_ledger=root / "var" / "backtest" / "final-usage.json",
                             data_version=f"csv:{data_dir.name}")
        result = run_backtest(cfg)
    except (ConfigError, BacktestError, ValueError) as exc:
        print(f"백테스트 거부: {exc}")
        return 2
    from dataclasses import replace
    days = result.days
    bad = perturbation_check(cfg, days[:: max(1, len(days) // 10)])
    reproducible = None
    if ns.segment != "FINAL":   # 최종 구간은 한 번만 (D-075)
        reproducible = run_backtest(replace(cfg, run_id=f"{cfg.run_id}-repro")).digest == result.digest
    report = render_report(cfg, result, settings_hash=limits.version_hash(), reproducible=reproducible,
                           lookahead_bad=bad, sensitivity=run_sensitivity(cfg) if ns.segment == "ALL" else None)
    out = cfg.out_dir / cfg.run_id / "report.md"
    out.write_text(report, encoding="utf-8", newline="\n")
    print(f"백테스트 완료: {out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m trading_system.app")
    p.add_argument("command", choices=["run", "backtest"])
    p.add_argument("--root", default=str(REPO_ROOT))
    ns, rest = p.parse_known_args(argv)
    root = Path(ns.root).resolve()
    if ns.command == "backtest":
        return backtest_main(rest, root)
    if rest:
        p.error(f"알 수 없는 인자: {' '.join(rest)}")
    return run_main(root)


__all__ = ["main", "run_main", "backtest_main", "load_daily_csv", "parse_trade_mode"]
