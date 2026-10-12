"""`python -m trading_system.app` 진입점 (T-020, DP-23). 사람이 직접 실행한다. 자동 실행 등록은 하지 않는다.
[CP-029 draft] 예외: PAPER 무인 자동 운영(app/autopilot.py)이 이 run을 자식 프로세스로 시작하고, 정지 요청 파일
var/<mode>/STOP으로 정상 종료시킨다(Ctrl+C와 같은 경로). LIVE는 자동 시작 대상이 아니다.

  run       TRADE_MODE(환경 변수 → .env 한 줄)로 모드를 정해 엔진을 돌린다. 값이 없거나 틀리면 연결 없이 종료.
            LIVE는 거부(실전 경로 없음). PAPER는 config/app.paper.toml의 [paper] connection_approved = true
            (사용자가 키움 모의 접속을 승인한 뒤 직접 바꿈, T-015·D-083)일 때만 키움 모의에 접속한다.
            BACKTEST는 fake 브로커로 엔진을 돌리는 연습 실행(운영 명령·정지 훈련용, 시세 없음 → 위험 확대 거부).
  backtest  일봉 CSV로 백테스트를 돌려 var/backtest/<실행ID>/에 DB·보고서를 남긴다.

스레드 (03 D-038 v0.5): 판정·실행 = 이 프로세스의 주 스레드. 감시(app/supervisor.py: KILL 즉시 반영·하트비트·
루프 감시·SNTP·스레드 생존, RV-22)·실시간 수신·시세 조회(일봉 갱신 포함)·텔레그램은 각자 스레드이며 저장소는
잠금으로 직렬화된 연결 하나를 같이 쓴다. 정지 발동·조건 보고·대조 요청은 엔진에 맡긴다(call_soon, QueuedHalts) —
K1 행 기록만 감시 스레드가 직접(Engine.fast_kill). 주문은 판정·실행 스레드만 한다.
단일 실행: var/<mode>/run.lock (RV-15). 치명 예외는 var/<mode>/logs/fatal.log에 남긴다 (RV-06).
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from datetime import date, time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.config.app_file import AppSettings, load_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.market_file import load_market_settings, parse_alert_settings
from trading_system.config.modes import parse_trade_mode, read_trade_mode
from trading_system.domain.clock import SystemClock
from trading_system.domain.enums import HaltType, Mode
from trading_system.halt.manager import K5_LINK_REALTIME
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.marketdata.ticks import load_ticks
from trading_system.ops.cli import REPO_ROOT
from trading_system.config.market_scope import MarketScopeError, check_start_scope, parse_market
from trading_system.domain.market_time import Market
from trading_system.storage.market_meta import bind_market
from trading_system.storage.oplog import log_exception
from trading_system.storage.repository import ModeMismatch, Repository

from .engine import Engine
from .instance_lock import InstanceLocked, acquire_instance_lock
from .startup import LiveNotAvailable, check_m2_startup, check_startup
from .supervisor import QueuedHalts, Supervisor, write_fallback
from .wiring import StrategyInput, build_engine, load_strategy


def record_fatal(path: Path, exc: BaseException) -> None:
    """프로세스를 끝내는 예외를 저장소와 독립된 파일에 남긴다 (RV-06). 실패해도 원래 예외를 가리지 않는다."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    write_fallback(path, f"치명 오류로 종료: {type(exc).__name__}\n{text}")
    log_exception(path.parent, "main.fatal", exc)   # 운영 로그(storage/oplog)에도 — 예외를 내보내지 않음


REQUERY_WAIT_SECONDS = 2.0   # 대조 불일치 재조회 전 대기 — 체결 통보 지연 확인 (01 장애 정책, 임시값 DP-37)
STOP_REQUEST = "STOP"        # var/<mode>/STOP — 정상 종료 요청 파일 (CP-029 D-155, 무인 실행에는 Ctrl+C를 보낼 창이 없음)


def take_stop_request(base: Path, *, retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> bool:
    """정지 요청 파일이 있으면 지우고 True. 판정·실행 스레드가 tick 사이에 본다 — Ctrl+C(SIGINT → stop 이벤트)와 같은
    종료 경로(루프 종료 → 스레드 join → 닫기 → 저장소 닫기 → 잠금 해제)를 탄다. 진행 중 tick(주문 전송 포함)은 끝까지 한다.
    쓰는 쪽이 파일을 연 순간이면 Windows가 지우기를 거부하므로 짧게 재시도한다(2026-10-09 간헐 실패). 그래도 실패하면
    요청은 따르고 파일은 남는다 — 다음 시작이 남은 요청 파일을 지운다."""
    path = base / STOP_REQUEST
    if not path.exists():
        return False
    for i in range(retries):
        try:
            path.unlink()
            break
        except FileNotFoundError:
            break
        except OSError:
            if i < retries - 1:
                sleep(0.05)
    return True


def start_summary(engine: Engine, rep) -> str:  # type: ignore[no-untyped-def]
    """시작 콘솔 요약 (RV-16): 복구 실패 사유·결과 불명 건수·활성 정지·전략 상태. 비밀정보 없음(계좌는 별칭)."""
    lines = [f"  결과 불명 의도: {engine.start_unknown}건"]
    lines += [f"  복구 미충족: {f}" for f in rep.failures] or ["  복구 조건: 충족"]
    try:
        halts = sorted(set(engine.repo.active_halts(engine.account)))
        lines.append(f"  활성 정지: {', '.join(halts) or '없음'}")
        for s in engine.repo.strategy_states():
            lines.append(f"  전략 {s['strategy_id']} v{s['version']} {s['market']}: {s['state']}"
                         + (f" ({s['stop_reason']})" if s.get("stop_reason") else ""))
    except Exception as exc:  # noqa: BLE001 — 표시 실패로 시작을 막지 않는다
        lines.append(f"  상태 표시 실패: {type(exc).__name__}")
    return "\n".join(lines)


class _WsThreadRef:
    """감시 스레드용: KiwoomRealtime 내부 수신 스레드(kiwoom-ws)를 Thread처럼 본다. 시작 전이면 ident None(감시 안 함)."""

    def __init__(self, rt: object) -> None:
        self._rt = rt

    def _t(self) -> threading.Thread | None:
        return getattr(self._rt, "_thread", None)

    def is_alive(self) -> bool:
        t = self._t()
        return t is None or t.is_alive()

    @property
    def ident(self) -> int | None:
        t = self._t()
        return None if t is None else t.ident


TELEGRAM_THREAD_LABELS = {"telegram-notify": "텔레그램 알림 전송", "telegram-poll": "텔레그램 명령 수신"}


def watched_threads(io_threads: Sequence[threading.Thread], rt: object | None) -> tuple[list, list, list]:
    """감시 스레드가 생존을 볼 입출력 스레드: (죽으면 긴급 알림 + K5, 죽으면 긴급 알림 + K11, 죽으면 긴급 알림만).

    텔레그램 알림 전송 스레드는 K11(CP-028 D-147, A-074 Q-1 B), 명령 수신 스레드는 알림만(Q-2)."""
    halting: list = [(t, "시세 조회") for t in io_threads if t.name == "market-poller"]
    if rt is not None:
        halting.append((_WsThreadRef(rt), "실시간 수신"))   # 3차 검토 낮음: WS 스레드 생존
    k11: list = [(t, TELEGRAM_THREAD_LABELS[t.name]) for t in io_threads if t.name == "telegram-notify"]
    alert_only: list = [(t, TELEGRAM_THREAD_LABELS[t.name]) for t in io_threads if t.name == "telegram-poll"]
    return halting, k11, alert_only


def ws_state_handler(engine: Engine, account: str):  # type: ignore[no-untyped-def]
    """실시간 연결 상태 → K5 재개 조건 broker_connected (RV-02, 01 K5). 판정·실행 스레드로 넘긴다(QueuedHalts).

    ONLINE·RECONNECTED: 실시간 경로(K5_LINK_REALTIME) 정상 보고 → 대조 요청. 순서가 중요하다: tick에서 calls 단계가
    대조보다 먼저라 대조 시각이 경로 복구 시각보다 늦게 된다(재접속 전 대조는 reconciled로 인정하지 않음).
    OFFLINE·FAILED: K5 발동(actor "realtime" → 실시간 경로 끊김). 경로 구분 없는 broker_connected는 보고하지 않는다
    (조회 경로 끊김까지 함께 풀림). 조회 경로 정상은 대조 성공이 보고한다.
    """
    from trading_system.broker.kiwoom.realtime import WsState
    halts = QueuedHalts(engine)

    def on_state(state: WsState, msg: str) -> None:
        if state in (WsState.OFFLINE, WsState.FAILED):
            halts.activate(HaltType.K5, "realtime", f"실시간 끊김: {msg}", account)
            halts.report_condition(HaltType.K5, account, K5_LINK_REALTIME, False, f"실시간 끊김: {msg}")
        elif state in (WsState.ONLINE, WsState.RECONNECTED):
            halts.report_condition(HaltType.K5, account, K5_LINK_REALTIME, True, f"실시간 {state.value}")
            engine.call_soon(engine.request_reconcile)   # 경로 보고 뒤에 대조 요청 — 같은 큐 순서 보장 (재검토 F6)

    return on_state


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
        check = getattr(strat, "validate_deploy", None)   # 전략별 배포 조건 검사 (T-017 v2 split_time)
        problems = check(params, dep.eval_times) if check is not None else []
        if problems:
            raise ConfigError(f"전략 배포 설정 오류 {strat.spec.strategy_id} v{strat.spec.version}: " + "; ".join(problems))
        out.append(StrategyInput(strat, params, dep.eval_times, dep.verification_approval))
    return out


# --- run ------------------------------------------------------------------------------------------

def _m1_hosts(mode: Mode) -> frozenset[str]:
    """M1 브로커가 연결할 호스트 (시작 일치 표 검사용, CP-024 D-097). BACKTEST(fake)는 연결 없음."""
    if mode is not Mode.PAPER:
        return frozenset()
    from urllib.parse import urlsplit

    from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
    return frozenset(h for h in (urlsplit(MOCK_REST).hostname, urlsplit(MOCK_WS).hostname) if h)


def run_main(root: Path, environ: dict[str, str] | None = None, max_seconds: float | None = None,
             market: str = "M1") -> int:
    """market: 실행할 시장 (CP-024 D-097). 기본 M1 = 기존 경로(config/*.<mode>.toml) 그대로.
    M2는 config/m2/ 검사(설정·시작 일치 표·DB 메타)까지만 하고 거부한다 — M2 엔진 조립·바이낸스 연결은 아직 없다
    (테스트넷 연결은 T-M2-14 별도 승인)."""
    env = os.environ if environ is None else environ
    try:
        mode = read_trade_mode(env, root / ".env")
        mkt = parse_market(market)
    except ConfigError as exc:
        print(f"시작 거부(브로커 연결 없음): {exc}")
        return 2
    base = root / "var" / mode.value.lower()
    if mkt is Market.M2:
        try:
            env_file = None if mode is Mode.BACKTEST else root / f".env.{mode.value.lower()}"
            check_m2_startup(mode, root, base / "trading.db", env_file)
            raise ConfigError("M2 시작 검사 통과 — M2 실행 경로(엔진 조립·바이낸스 연결)는 아직 없다. "
                              "테스트넷 연결은 T-M2-14 별도 승인 뒤")
        except (LiveNotAvailable, ConfigError) as exc:
            print(f"시작 거부(브로커 연결 없음): {exc}")
            return 2
    try:
        env_file = None if mode is Mode.BACKTEST else root / f".env.{mode.value.lower()}"
        startup = check_startup(mode, root, base / "trading.db", env_file)
        app = load_app_settings(root / "config" / f"app.{mode.value.lower()}.toml", mode, root)
        if app.account != startup.account:
            raise ConfigError("배포 설정의 계좌 별칭과 한도 파일의 계좌가 다르다")
        if app.market != Market.M1.value:   # M1 설정 위치(config/)에 다른 시장 설정 (D-097)
            raise MarketScopeError(f"config/app.{mode.value.lower()}.toml의 market이 M1이 아니다 ({app.market}) — "
                                   "M2는 config/m2/와 --market M2")
        check_start_scope(mode, Market.M1, app.broker, app.account, _m1_hosts(mode))
        gate_settings, alert_thresholds, calendar, ticks, caps = _load_common(root, mode)
        strategies = _strategies(app)
        if app.telegram_enabled:
            from .telegram_run import check_telegram_secrets
            check_telegram_secrets(startup.secrets)      # 브로커 접속 전에 확인
        if mode is Mode.PAPER and not app.paper_connection_approved:
            raise ConfigError("키움 모의 접속 승인 전 (config/app.paper.toml [paper] connection_approved = true는 "
                              "사용자가 T-015 승인 후 직접 설정)")
    except (LiveNotAvailable, ConfigError) as exc:
        print(f"시작 거부(브로커 연결 없음): {exc}")
        return 2

    try:
        lock = acquire_instance_lock(base)    # 단일 실행 (RV-15): 프로세스 수명 동안 유지
    except InstanceLocked as exc:
        print(f"시작 거부(브로커 연결 없음): {exc}")
        return 2
    if take_stop_request(base):   # 이전 실행 뒤 남은 요청 — 새 실행을 바로 멈추지 않게 지운다 (잠금을 잡은 뒤라 경합 없음)
        print(f"이전 정지 요청 파일을 지움: {base / STOP_REQUEST}")
    fatal_log = base / "logs" / "fatal.log"
    prev_hook = threading.excepthook

    def thread_died(args: threading.ExceptHookArgs) -> None:   # 입출력 스레드가 죽으면 파일에 남김 (RV-06·RV-18)
        if args.exc_value is not None:
            record_fatal(fatal_log, args.exc_value)
        prev_hook(args)

    threading.excepthook = thread_died
    try:
        return _run(root, mode, base, startup, app, gate_settings, alert_thresholds, calendar, ticks, caps,
                    strategies, max_seconds)
    except BaseException as exc:
        record_fatal(fatal_log, exc)
        raise
    finally:
        threading.excepthook = prev_hook
        lock.release()


def _run(root: Path, mode: Mode, base: Path, startup, app: AppSettings, gate_settings, alert_thresholds,  # type: ignore[no-untyped-def]
         calendar, ticks, caps, strategies: list[StrategyInput], max_seconds: float | None) -> int:
    clock = SystemClock()
    existed = startup.storage_path.exists()
    repo = Repository(startup.storage_path, mode)
    try:   # DB 메타 market·broker (CP-024 D-097): 새 DB는 기록, 다른 시장 DB는 브로커 연결 전에 거부
        bind_market(repo.connection, Market.M1, app.broker, created=not existed)
    except ModeMismatch as exc:
        repo.close()
        print(f"시작 거부(브로커 연결 없음): {exc}")
        return 2
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
                                  reconcile_every=_minutes(app.reconcile_minutes),
                                  requery_wait=lambda: time.sleep(REQUERY_WAIT_SECONDS))
        except ConfigError as exc:
            print(f"시작 거부(주문 없음): {exc}")
            return 2
        engine.fallback_file = base / "logs" / "engine-fallback.log"
        engine.log_dir = base / "logs"
        clock_monitor._halts, clock_monitor._alerts = engine.halts, engine.alerts  # noqa: SLF001 — 조립 지점
        clock_monitor.check()                         # 시작 점검은 판정·실행 스레드(이 스레드)에서 직접
        clock_monitor._halts = QueuedHalts(engine)    # noqa: SLF001 — 이후 점검은 감시 스레드 → 정지는 큐로 (RV-22)
        io_threads = extra(engine, stop, closers) if extra is not None else []
        from .telegram_run import telegram_threads
        io_threads += telegram_threads(engine, repo, app.telegram_enabled, startup.secrets, clock, base / "KILL",
                                       stop, log_dir=base / "logs",   # 03 D-052: 알림·원격 명령 (기본 꺼짐)
                                       market=app.market)             # [M1] 표시·정수 수량 (CP-024 D-117)
        # 시작 일봉(extra 안, 이 스레드) 뒤로는 시세 스레드가 K10을 걸므로 큐 경유 (3차 검토 F-5, RV-22)
        store.halts = QueuedHalts(engine)
        watched, watched_k11, watched_alert_only = watched_threads(io_threads, getattr(extra, "rt", None))
        # 감시 스레드 (RV-22, D-038): KILL 즉시 반영·하트비트·루프 감시·SNTP·입출력 스레드 생존. 주문은 하지 않는다
        sup = Supervisor(engine, heartbeat=base / "heartbeat", fallback=engine.fallback_file,
                         clock_monitor=clock_monitor, watched=watched, watched_k11=watched_k11,
                         watched_alert_only=watched_alert_only)
        sup_thread = threading.Thread(target=sup.run, args=(stop,), name="supervisor", daemon=True)
        sup_thread.start()
        threads.append(sup_thread)
        rep = engine.start()
        print(f"시작: 모드 {mode.value}, 계좌 {app.account}, 복구 판정 {'통과' if rep.ok else '실패'} "
              f"— K8 재개: scripts/resume.py --mode {mode.value} K8")
        print(start_summary(engine, rep))
        for t in io_threads:
            t.start()
            threads.append(t)
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        started = time.monotonic()
        while not stop.is_set():
            engine.tick()
            if take_stop_request(base):   # 정지 요청 파일 = Ctrl+C와 같은 종료 경로 (CP-029 D-155)
                print("정지 요청 파일 확인 — 정상 종료 (Ctrl+C와 같은 순서)")
                stop.set()
                break
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


def _paper_broker(startup, app: AppSettings, clock, store: MarketDataStore):  # type: ignore[no-untyped-def]
    from trading_system.broker.kiwoom.adapter import KiwoomAdapter, KiwoomConfig
    from trading_system.broker.kiwoom.realtime import KiwoomRealtime, order_handlers
    from trading_system.marketdata.kiwoom_feed import KiwoomMarketFeed, MarketPoller
    s = startup.secrets
    need = ("KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_SECRET_KEY", "KIWOOM_PAPER_ACCOUNT_NO", "KIWOOM_PAPER_ACCOUNT_ALIAS")
    missing = [k for k in need if s is None or not s.has(k)]
    if missing:
        raise ConfigError(f".env.paper에 없는 키: {', '.join(missing)}")
    if s.get("KIWOOM_PAPER_ACCOUNT_ALIAS").reveal() != app.account:
        raise ConfigError(".env.paper의 계좌 별칭이 배포 설정과 다르다")
    holder: dict = {}

    def critical(kind: str, msg: str, actor: str = "broker") -> None:
        e = holder.get("engine")
        if e is None:
            return
        # actor "realtime" = 실시간 경로 끊김, 그 밖 = 조회 경로 (halt.manager K5 경로 구분, RV-02)
        QueuedHalts(e).activate(HaltType.K5, actor, f"{kind}: {msg}", app.account)
        e.call_soon(lambda: e.alerts.alert("EMERGENCY", f"브로커 치명 오류 {kind} — K5", None))
        if kind == "MODE_MISMATCH":
            holder["stop"].set()   # 모드 불일치는 즉시 중단 (03 §4)

    adapter = KiwoomAdapter(KiwoomConfig(app.account, s.get("KIWOOM_PAPER_ACCOUNT_NO").reveal,
                                         s.get("KIWOOM_PAPER_APP_KEY").reveal, s.get("KIWOOM_PAPER_SECRET_KEY").reveal),
                            clock, on_critical=critical)

    def extra(engine: Engine, stop: threading.Event, closers: list) -> list[threading.Thread]:
        holder["engine"], holder["stop"] = engine, stop
        feed = KiwoomMarketFeed(adapter, store, clock, app.instruments,
                                held=lambda c: c in engine.repo.positions(app.account), alerts=engine.alerts)
        on_state = ws_state_handler(engine, app.account)   # K5 broker_connected 보고 (RV-02)

        handlers = {**order_handlers(adapter.push_event, app.account, clock,
                                     lambda v: adapter._check_account(v, "00")),  # noqa: SLF001
                    **feed.handlers()}
        rt = KiwoomRealtime(adapter.cfg.ws_url, adapter.tokens, clock,
                            registrations=[("00", []), ("04", [])] + feed.registrations(), handlers=handlers,
                            on_state=on_state, on_critical=lambda k, m: critical(k, m, "realtime"),
                            on_warning=lambda m: engine.alerts.alert("WARNING", m, None))
        extra.rt = rt  # type: ignore[attr-defined]   # 감시 스레드가 WS 수신 스레드 생존을 본다
        poller = MarketPoller(feed, alerts=engine.alerts)
        closers.extend([rt.stop, adapter.close])
        poller.refresh_daily_if_due()   # 시작 시 1회 (이후 확정일이 바뀌면 시세 스레드가 다시 받음, RV-04)
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
    from trading_system.app.validation_tool import load_validation_settings
    from trading_system.config.limits_file import load_limits_file
    p = argparse.ArgumentParser(prog="python -m trading_system.app backtest")
    p.add_argument("--strategy", required=True, help="패키지.모듈:클래스")
    p.add_argument("--params", required=True, help="파라미터 파일 (config/ 아래)")
    p.add_argument("--data", required=True, help="종목코드.csv 파일이 있는 폴더")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--cash", default="10000000")
    p.add_argument("--eval-time", default="09:05", help="판정 시각 HH:MM, 여러 개는 쉼표로 (예: 09:30,14:30)")
    p.add_argument("--segment", default="ALL", choices=["ALL", "TRAIN", "VALIDATION", "FINAL"])
    p.add_argument("--slippage-tick", default="10", help="호가 표가 없을 때 1틱 크기(원)")
    p.add_argument("--final-check", action="store_true",
                   help="[CP-030 D-163] 최종 확인 실행: config/validation.toml final_from 이후만, 전략 버전당 1회")
    p.add_argument("--confirmed-by", default="", help="--final-check 때 전략 확정 사용자 선언 A-ID")
    p.add_argument("--limits", default="config/limits.backtest.toml",
                   help="한도(정책 프로필) 파일, 저장소 기준 경로 [CP-034 D-196]. 보고서에 프로필·승인 여부 표시")
    ns = p.parse_args(argv)
    try:
        mode = Mode.BACKTEST
        gate_settings, _, calendar, ticks, caps = _load_common(root, mode)
        limits = load_limits_file(root / ns.limits).limits
        eval_times = tuple(dtime(int(hh), int(mm)) for hh, mm in
                           (part.strip().split(":") for part in ns.eval_time.split(",")))
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
                             eval_time=eval_times[0], eval_times=eval_times, segment=ns.segment,
                             final_usage_ledger=root / "var" / "backtest" / "final-usage.json",
                             data_version=f"csv:{data_dir.name}",
                             final_from=load_validation_settings(root / "config" / "validation.toml")["final_from"],  # type: ignore[arg-type]
                             final_check=ns.final_check)
        if ns.final_check:
            # 인자·날짜를 먼저 검사하고(실패하면 장부 미기록) 장부에 1회 사용을 기록한 뒤 실행 (D-163)
            from trading_system.app.backtest import _run_days
            from trading_system.app.validation import ValidationError, claim_final_check
            if ns.segment != "ALL":
                raise BacktestError("최종 확인 실행은 --segment ALL로만 (날짜로 구간 지정, D-163)")
            if (cfg.out_dir / cfg.run_id / "trading.db").exists():
                raise BacktestError(f"같은 실행 ID의 DB가 이미 있음: {cfg.out_dir / cfg.run_id} (실행마다 새 ID)")
            days = _run_days(cfg)
            if not days or days[0] < cfg.final_from:
                raise BacktestError(f"최종 확인 실행은 {cfg.final_from} 이후 거래일만 (D-163)")
            try:
                claim_final_check(cfg.final_usage_ledger, strat.spec, cfg.run_id,  # type: ignore[arg-type]
                                  confirmed_by=ns.confirmed_by)
            except ValidationError as exc:
                raise BacktestError(str(exc)) from None
        result = run_backtest(cfg)
    except (ConfigError, BacktestError, ValueError) as exc:
        print(f"백테스트 거부: {exc}")
        return 2
    from dataclasses import replace
    days = result.days
    once = ns.segment == "FINAL" or ns.final_check   # 최종 구간은 한 번만 (D-075·D-163): 재실행·교란·민감도 없음
    bad = None if ns.final_check else perturbation_check(cfg, days[:: max(1, len(days) // 10)])
    reproducible = None
    if not once:
        reproducible = run_backtest(replace(cfg, run_id=f"{cfg.run_id}-repro")).digest == result.digest
    report = render_report(cfg, result, settings_hash=limits.version_hash(), reproducible=reproducible,
                           lookahead_bad=bad,
                           sensitivity=run_sensitivity(cfg) if ns.segment == "ALL" and not ns.final_check else None)
    out = cfg.out_dir / cfg.run_id / "report.md"
    out.write_text(report, encoding="utf-8", newline="\n")
    print(f"백테스트 완료: {out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m trading_system.app")
    p.add_argument("command", choices=["run", "backtest"])
    p.add_argument("--root", default=str(REPO_ROOT))
    p.add_argument("--market", default="M1", help="run의 시장 M1|M2 (기본 M1, CP-024 D-097)")
    ns, rest = p.parse_known_args(argv)
    root = Path(ns.root).resolve()
    if ns.command == "backtest":
        if ns.market != "M1":
            p.error("backtest는 M1만 (M2 백테스트는 T-M2-12)")
        return backtest_main(rest, root)
    if rest:
        p.error(f"알 수 없는 인자: {' '.join(rest)}")
    return run_main(root, market=ns.market)


__all__ = ["main", "run_main", "backtest_main", "load_daily_csv", "parse_trade_mode", "STOP_REQUEST",
           "take_stop_request"]
