"""M2 시작 점검 — 바이낸스 Spot 테스트넷 접속, **조회만**, 주문 API 전송 전 차단 (T-M2-14, 승인 A-096).

근거: CP-024 D-096(M2 체크아웃)·D-097(시작 일치 표·DB 메타)·D-098(테스트넷 도메인만)·D-105(유니버스 BTCUSDT)·D-106(규칙)·
D-109(시작 대조)·D-113(rateLimits·429·418)·D-114(HMAC)·D-115(서버 시각), M1 선례 app/preflight.py(T-015).

연결 조건 — 하나라도 어긋나면 전송 객체(세션·HTTP)를 만들기 전에 거부(종료 코드 2)
1. 실행 중 엔진 없음(run.lock·하트비트) — M1 preflight와 같은 검사(M2 체크아웃의 var/paper/).
2. `--market M2`.
3. TRADE_MODE = PAPER (환경 변수 → `.env` 한 줄, config/modes).
4. app/startup.check_m2_startup: 저장소 경로(var/paper/), 자격정보 파일 `.env.paper`(TRADE_MODE=PAPER, 다른 모드 키 없음),
   config/m2/ 네 파일, 시작 일치 표(PAPER·M2 → binance-spot-testnet, 별칭 BN-), 유니버스 비어 있지 않음, DB 메타(M2만).
5. config/m2/app.paper.toml `[paper] connection_approved = true` — **사용자가 직접 켠다**.
6. 비밀 키 BINANCE_TESTNET_API_KEY·BINANCE_TESTNET_SECRET_KEY가 있고 비어 있지 않다(값은 읽어 Secret으로만 넘긴다).
7. 접속 대상: https + 테스트넷 REST 호스트(testnet.binance.vision)만, 포트·경로·사용자 정보 없음. 시작 일치 표의
   접속 도메인 검사(check_start_scope hosts)도 통과해야 한다. 실전·Demo 도메인은 표에 없으므로 어떤 경우에도 거부.

점검 (통과 = 실패 0, 종료 코드 0 / 실패 있으면 1 — 결과 파일은 남긴다)
① 바이낸스 서버 시각 차이(ms) — 경고 기준 config/m2/market.paper.toml server_time_warn_ms(A-064 500ms, D-115).
   SNTP(설정 sntp_servers, 외부 연결 승인 A-042) 오차 1초 초과 = 실패, 모두 무응답 = 경고(M1과 같음).
② exchangeInfo: 유니버스 심볼 존재·TRADING·필터(PRICE_FILTER·LOT_SIZE·NOTIONAL/MIN_NOTIONAL)·rateLimits (PV-M2-03 기록).
   L7(1회 주문 최대 금액)이 최소 주문 금액보다 작으면 실패(A-064 "L7과의 정합은 시작 점검에서 확인").
③ 계좌: 자산별 free·locked(테스트넷 가상 잔고라 금액 기록, 계좌 식별값 없음).
④ 미체결 0 (아니면 실패). 최근 24시간 주문·체결 수는 기록만.
⑤ 시작 대조: M2 DB가 없거나 비어 있으면 "대조 기준 없음"(정상, DB를 만들지 않는다). 기준이 있으면 reconciliation/spot.
⑥ 주문 API 전송 0 — 바이낸스 가드가 허용 목록 밖 요청을 전송 전에 막는다. 막은 시도가 있으면 실패.
⑦ 결과: var/paper/preflight/<KST 시각>-m2-preflight.json(기계용)·.md(사람용). 키·시크릿·서명·계좌 식별값 없음.

의존 경계: 이 모듈은 바이낸스 패키지를 import하지 않는다(바이낸스 전용 타입은 broker/binance 밖에서 쓰지 않음 —
tests/contract/test_binance_boundary). 바이낸스 세션 생성 함수는 scripts/binance-preflight.py가 주입한다.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from trading_system.broker.rest_transport import RestTransport
from trading_system.config.errors import ConfigError
from trading_system.config.market_scope import check_start_scope
from trading_system.config.modes import read_trade_mode
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.domain.enums import Mode
from trading_system.domain.market import CostRates
from trading_system.domain.market_time import M2_TIME, Market
from trading_system.monitoring.sntp import ClockMonitor, SntpError, query_sample
from trading_system.monitoring.spot_cash import QUOTE_BASELINE, SpotAccountFacts
from trading_system.reconciliation.spot import SpotReconciler
from trading_system.storage.jsonlog import Masker
from trading_system.storage.market_meta import bind_market
from trading_system.storage.repository import OrderIdentity, Repository

from .instance_lock import instance_running
from .preflight import SntpQuery, engine_alive
from .startup import M2StartupConfig, check_m2_startup

TESTNET_REST_HOST = "testnet.binance.vision"
TESTNET_REST_URL = f"https://{TESTNET_REST_HOST}"
SECRET_KEYS = ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET_KEY")
RECONCILE_TRIGGER = "PREFLIGHT"
CASH_MAX_AGE = timedelta(minutes=5)   # 시작 대조의 기대 USDT 계산에는 쓰이지 않는 자리 값(SpotAccountFacts 생성 인자)

# app → broker/binance 의존 없이 주입받는 자리 (scripts/binance-preflight.py가 open_testnet_session을 넘긴다)
SessionFactory = Callable[..., Any]
MakeTransport = Callable[[type[Exception], type[Exception]], Any]


def default_make_transport(pre: type[Exception], after: type[Exception]) -> RestTransport:
    return RestTransport(frozenset({TESTNET_REST_HOST}), pre_send_error=pre, after_send_error=after)


@dataclass
class M2PreflightReport:
    started_at: str
    account: str
    market: str
    broker: str
    host: str
    symbols: list[str]
    server_time_offset_ms: int | None = None
    server_time_warn_ms: int | None = None
    clock_offset: str | None = None          # SNTP 오차(초). 판정 불가면 None
    clock_source: str | None = None
    clock_delay: str | None = None
    exchange: dict = field(default_factory=dict)
    rate_limits: list = field(default_factory=list)
    account_info: dict | None = None
    open_orders: dict = field(default_factory=dict)
    recent_orders: dict = field(default_factory=dict)
    recent_fills: dict = field(default_factory=dict)
    reconcile: str | None = None             # NO_BASELINE / MATCH / MISMATCH / QUERY_FAILED / SKIPPED
    reconcile_detail: str = ""
    reconcile_id: str | None = None
    calls: list = field(default_factory=list)
    order_api_sent: int = 0
    order_api_blocked: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        d = asdict(self)
        d["account_snapshot"] = d.pop("account_info")   # 테스트넷 가상 잔고(A-096). 계좌 식별값 없음
        d["ok"] = self.ok
        return d


# --- 연결 조건 ----------------------------------------------------------------------------------------------------

@dataclass
class M2Connection:
    startup: M2StartupConfig
    session: Any
    host: str
    secret_values: tuple[str, ...] = field(repr=False, default=())


def _refuse(msg: str) -> int:
    print(f"시작 점검 거부(연결 없음): {msg}")
    return 2


def _endpoint_host(rest_url: str) -> str:
    p = urlsplit(rest_url)
    if p.scheme != "https" or p.port is not None or p.username or p.password or p.path not in ("", "/") \
            or p.query or p.fragment or not p.hostname:
        raise ConfigError("접속 대상은 https://<테스트넷 REST 호스트> 형식만(포트·경로·사용자 정보 없음)")
    if p.hostname != TESTNET_REST_HOST:
        raise ConfigError(f"접속 대상 {p.hostname}은 테스트넷 REST({TESTNET_REST_HOST})가 아니다 — 실전·Demo 도메인 거부(D-098)")
    return p.hostname


def connect_m2_paper(root: Path, environ: Mapping[str, str] | None, clock: Clock, open_session: SessionFactory, *,
                     market: str | None, rest_url: str, make_transport: MakeTransport,
                     sleep: Callable[[float], None]) -> M2Connection | str:
    """연결 조건 1~7. 어긋나면 거부 사유(문자열)를 돌려주고 세션·전송 객체를 만들지 않는다."""
    if market != Market.M2.value:
        return f"--market M2가 필요하다(받은 값 {market!r}) — 이 도구는 M2(바이낸스 테스트넷) 전용"
    env = os.environ if environ is None else environ
    try:
        mode = read_trade_mode(env, root / ".env")
    except ConfigError as exc:
        return str(exc)
    if mode is not Mode.PAPER:
        return f"TRADE_MODE={mode.value} — 테스트넷 조회는 PAPER에서만 한다 (LIVE·BACKTEST는 연결하지 않는다)"
    base = root / "var" / "paper"
    try:
        startup = check_m2_startup(Mode.PAPER, root, base / "trading.db", root / ".env.paper")
        if not startup.app.paper_connection_approved:
            raise ConfigError("config/m2/app.paper.toml [paper] connection_approved = true가 아니다 "
                              "(테스트넷 접속은 사용자가 직접 켠다, A-096)")
        s = startup.secrets
        missing = [k for k in SECRET_KEYS if s is None or not s.has(k) or not s.get(k).reveal()]
        if missing:
            raise ConfigError(f".env.paper에 없거나 빈 키: {', '.join(missing)}")
        assert s is not None
        host = _endpoint_host(rest_url)
        check_start_scope(Mode.PAPER, Market.M2, startup.broker, startup.account, {host})
    except ConfigError as exc:
        return "; ".join(exc.problems) if getattr(exc, "problems", None) else str(exc)
    key, secret = s.get(SECRET_KEYS[0]), s.get(SECRET_KEYS[1])

    def now_ms() -> int:
        return int(clock.now().timestamp() * 1000)
    session = open_session(TESTNET_REST_URL, key, secret, make_transport, now_ms=now_ms, sleep=sleep)
    return M2Connection(startup, session, host, (key.reveal(), secret.reveal()))


# --- 시계 (SNTP) --------------------------------------------------------------------------------------------------

class _NullClockStore:
    """ClockMonitor의 기록 자리 — 시작 점검은 DB를 만들지 않으므로 결과는 보고서에만 남긴다."""

    @contextmanager
    def tx(self) -> Iterator[None]:
        yield

    def insert_clock_check(self, *args: object) -> None:
        return None


def _query(query: SntpQuery | None) -> SntpQuery:
    if query is not None:
        return query

    def ask(host: str, port: int):  # type: ignore[no-untyped-def]
        if host.lower().rstrip(".").endswith(".invalid"):
            raise SntpError(f"{host}:{port} 예약된 이름(.invalid) — 조회하지 않음")
        return query_sample(host, port)
    return ask


def check_sntp(rep: M2PreflightReport, clock: Clock, servers: Sequence[tuple[str, int]], query: SntpQuery | None,
               sleep: Callable[[float], None]) -> None:
    if not servers:
        rep.warnings.append("시계(SNTP) 미점검 — 설정에 sntp_servers 없음")
        return
    try:
        res = ClockMonitor(_NullClockStore(), clock, list(servers), None, None,  # type: ignore[arg-type]
                           query=_query(query), sleep=sleep).check()
    except Exception as exc:  # noqa: BLE001 — 점검 오류도 결과로 보고
        rep.warnings.append(f"시계(SNTP) 점검 오류: {type(exc).__name__}")
        return
    if res.ok is None:
        rep.warnings.append(f"시계(SNTP) 판정 불가 — 모든 서버 무응답: {Masker().text(res.source)}")
        return
    rep.clock_offset, rep.clock_source = str(res.offset), res.source
    rep.clock_delay = None if res.delay is None else str(res.delay)
    if not res.ok:
        rep.failures.append(f"시계(SNTP) 오차 {res.offset}초 > 허용 1초 ({res.detail}) — PC 시간 동기화 후 재점검")


# --- 시작 대조 ----------------------------------------------------------------------------------------------------

class _Alerts:
    def __init__(self) -> None:
        self.items: list[str] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.items.append(f"{level}: {message}")


def _has_basis(repo: Repository, account: str) -> bool:
    if any(i.account == account for i in repo.intents()):
        return True
    if any(p.quantity != 0 for p in repo.positions(account).values()):
        return True
    if repo.open_recon_items(account):
        return True
    return bool(repo.select("SELECT 1 FROM audit_event WHERE kind=? AND ref=? LIMIT 1", (QUOTE_BASELINE, account)))


def reconcile_start(rep: M2PreflightReport, startup: M2StartupConfig, session: Any, clock: Clock,
                    result: dict) -> None:
    db = startup.storage_path
    if not db.exists():
        rep.reconcile, rep.reconcile_detail = "NO_BASELINE", "M2 DB 없음 — 대조 기준 없음(첫 시작 전 정상, DB는 만들지 않음)"
        return
    if not (result.get("signed_ok") and result.get("exchange_ok")):
        rep.reconcile, rep.reconcile_detail = "SKIPPED", "계좌·거래 규칙 조회 실패로 대조하지 않음"
        return
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        bind_market(repo.connection, Market.M2, startup.broker, created=False)   # 검사만(시작 검사와 같은 결과)
        account, quote = startup.account, startup.universe.quote_asset
        if not _has_basis(repo, account):
            rep.reconcile, rep.reconcile_detail = "NO_BASELINE", "M2 DB에 기준선·의도·포지션 없음 — 대조 기준 없음(정상)"
            return
        pairs = {s: (startup.universe.base_asset(s) or "", quote) for s in startup.universe.symbols}
        precision = min(int(result["exchange"][s]["quote_asset_precision"]) for s in pairs)
        rates = CostRates(commission_rate=startup.market_settings.commission_rate, sell_tax_rate=Decimal("0"),
                          cost_quantum=Decimal(1).scaleb(-precision))
        facts = SpotAccountFacts(repo, clock, quote_asset=quote, asset_pair=pairs.get, price_of=lambda _i: None,
                                 cost_rates=rates, reference_time=startup.limits.daily_reference_time,
                                 max_age=CASH_MAX_AGE, market_time=M2_TIME)
        alerts = _Alerts()
        res = SpotReconciler(repo, session.queries(list(pairs)), clock, None, alerts, universe=pairs,
                             quote_asset=quote, expected_quote=facts.expected_quote).run(account, RECONCILE_TRIGGER)
        rep.reconcile, rep.reconcile_id = res.result, res.recon_id
        items = [f"{i['kind']} {i['instrument'] or ''} 내부 {i['internal_value']} / 거래소 {i['broker_value']}"
                 for i in repo.open_recon_items(account)]
        rep.reconcile_detail = "; ".join(items)
        rep.warnings.extend(alerts.items)
        if res.result == "MISMATCH":
            rep.failures.append(f"시작 대조 불일치 {len(items)}건 — scripts/reconcile-resolve.py로 확인: "
                                f"{rep.reconcile_detail}")
        elif res.result != "MATCH":
            rep.failures.append(f"시작 대조 실패 ({res.result})")
        repo.audit(clock.now(), "PREFLIGHT", "m2-preflight", res.recon_id,
                   {"ok": not rep.failures, "reconcile": res.result, "market": "M2"})
    finally:
        repo.close()


# --- 진입점 -------------------------------------------------------------------------------------------------------

def m2_preflight_main(root: Path, open_session: SessionFactory, environ: Mapping[str, str] | None = None, *,
                      market: str | None, clock: Clock | None = None, rest_url: str = TESTNET_REST_URL,
                      make_transport: MakeTransport | None = None, sntp_query: SntpQuery | None = None,
                      sleep: Callable[[float], None] = time.sleep) -> int:
    clock = clock or SystemClock()
    base = root / "var" / "paper"
    if instance_running(base):
        return _refuse("PAPER 엔진이 실행 중(실행 잠금 보유) — 동시에 점검하지 않는다. 엔진 종료 후 다시 실행")
    alive = engine_alive(base / "heartbeat", clock.now())
    if alive:
        return _refuse(alive)
    conn = connect_m2_paper(root, environ, clock, open_session, market=market, rest_url=rest_url,
                            make_transport=make_transport or default_make_transport, sleep=sleep)
    if isinstance(conn, str):
        return _refuse(conn)
    st = conn.startup
    symbols = list(st.universe.symbols)
    rep = M2PreflightReport(clock.now().isoformat(), st.account, st.market.value, st.broker, conn.host, symbols,
                            server_time_warn_ms=st.market_settings.server_time_warn_ms)
    try:
        result = conn.session.run(symbols, st.universe.quote_asset)
    except Exception as exc:  # noqa: BLE001 — 예상 밖 오류도 결과 파일로 남긴다(원문·비밀값 없이 형식 이름만)
        result = {"failures": [f"점검 중 예외: {type(exc).__name__} — 결과 파일·호출 기록 확인"]}
    _apply(rep, result, st)
    check_sntp(rep, clock, st.app.sntp_servers, sntp_query, sleep)
    try:
        reconcile_start(rep, st, conn.session, clock, result)
    except Exception as exc:  # noqa: BLE001 — 대조 오류도 결과로 보고(실패)
        rep.reconcile = "ERROR"
        rep.failures.append(f"시작 대조 오류: {type(exc).__name__}: {Masker(conn.secret_values).text(str(exc))}")
    guard = conn.session.guard
    rep.calls = conn.session.calls()
    rep.order_api_sent = guard.order_sent
    rep.order_api_blocked = list(guard.blocked)
    if guard.blocked:
        rep.failures.append(f"점검 중 허용 목록 밖 호출 시도(전송 전 차단): {', '.join(guard.blocked)}")
    if guard.order_sent:
        rep.failures.append(f"주문 계열 요청 {guard.order_sent}건 전송 — 있어서는 안 됨, 즉시 확인")
    paths = _write(base, rep, clock.now(), conn.secret_values)
    print(_scrub(_summary(rep, paths), conn.secret_values))
    return 0 if rep.ok else 1


def m2_preflight_cli(root: Path, argv: Sequence[str] | None, open_session: SessionFactory,
                     environ: Mapping[str, str] | None = None, **kw: Any) -> int:
    p = argparse.ArgumentParser(prog="binance-preflight",
                                description="M2 시작 점검: 바이낸스 Spot 테스트넷 조회만(주문 API 전송 전 차단)")
    p.add_argument("--market", default=None, help="M2만 허용 (필수)")
    try:
        args = p.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:   # 잘못된 인자: 연결 없이 종료
        return int(exc.code or 2) if exc.code not in (0,) else 0
    return m2_preflight_main(root, open_session, environ, market=args.market, **kw)


def _apply(rep: M2PreflightReport, result: dict, st: M2StartupConfig) -> None:
    rep.server_time_offset_ms = result.get("server_time_offset_ms")
    rep.exchange = result.get("exchange", {})
    rep.rate_limits = result.get("rate_limits", [])
    rep.account_info = result.get("account")
    rep.open_orders = result.get("open_orders", {})
    rep.recent_orders = result.get("recent_orders", {})
    rep.recent_fills = result.get("recent_fills", {})
    rep.alerts = list(result.get("alerts", []))
    rep.failures.extend(result.get("failures", []))
    rep.warnings.extend(result.get("warnings", []))
    off, warn = rep.server_time_offset_ms, st.market_settings.server_time_warn_ms
    if off is not None and abs(off) >= warn:
        rep.warnings.append(f"바이낸스 서버 시각 오차 {off}ms ≥ 경고 기준 {warn}ms (A-064, D-115) — recvWindow 5000ms·"
                            "-1021 위험, PC 시계 동기화 확인")
    for sym, n in rep.open_orders.items():
        if n:
            rep.failures.append(f"{sym} 미체결 {n}건 — 시작 점검은 미체결 0이어야 한다(테스트넷 화면·주문 조회로 확인)")
    l7 = st.limits.l7_max_order_amount
    for sym, info in rep.exchange.items():
        mn = info.get("min_notional") if isinstance(info, dict) else None
        if l7 is not None and mn is not None and Decimal(mn) > l7:
            rep.failures.append(f"{sym}: L7 1회 주문 최대 {l7} {st.universe.quote_asset} < 최소 주문 금액 {mn} — "
                                "주문 불가(A-064 L7 정합)")


# --- 결과 기록 ----------------------------------------------------------------------------------------------------

def _scrub(text: str, secrets: Sequence[str]) -> str:
    for s in sorted((x for x in secrets if x), key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def _write(base: Path, rep: M2PreflightReport, now: datetime, secrets: Sequence[str]) -> tuple[Path, Path]:
    out = base / "preflight"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(KST).strftime('%Y%m%d-%H%M%S')}-m2-preflight"
    js, md = out / f"{stem}.json", out / f"{stem}.md"
    js.write_text(_scrub(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), secrets), encoding="utf-8")
    md.write_text(_scrub(_markdown(rep), secrets), encoding="utf-8")
    return js, md


def _fmt(v: object) -> str:
    return "—" if v is None else str(v)


def _markdown(rep: M2PreflightReport) -> str:
    L: list[str] = [
        "# M2 시작 점검 — 바이낸스 Spot 테스트넷 (조회만)", "",
        f"- 시각: {rep.started_at}",
        f"- 시장·브로커·접속: {rep.market} · {rep.broker} · {rep.host}",
        f"- 계좌 별칭: {rep.account} (계좌 식별값 기록 안 함)",
        f"- 유니버스: {', '.join(rep.symbols)}",
        f"- **결과: {'통과' if rep.ok else '실패'}** (실패 {len(rep.failures)}, 경고 {len(rep.warnings)})", "",
        "## 1. 시계", "", "| 항목 | 값 | 기준 |", "|---|---|---|",
        f"| 바이낸스 서버 시각 − 로컬(ms) | {_fmt(rep.server_time_offset_ms)} | 경고 {rep.server_time_warn_ms}ms (A-064) |",
        f"| SNTP 오차(초) | {_fmt(rep.clock_offset)} ({_fmt(rep.clock_source)}, 지연 {_fmt(rep.clock_delay)}) | 실패 1초 초과 |",
        "", "## 2. 거래 규칙 (exchangeInfo, PV-M2-03)", "",
    ]
    for sym, info in rep.exchange.items():
        if not info.get("found"):
            L += [f"- {sym}: 확인 못 함 ({_fmt(info.get('rejected'))})", ""]
            continue
        L += [f"### {sym}", "",
              f"- 상태 {info['status']} · 거래 가능 {'예' if info['trading'] else '아니오'} · 기초 {info['base_asset']} · "
              f"호가 {info['quote_asset']} (quoteAssetPrecision {info['quote_asset_precision']})",
              f"- 규칙 해석 {'정상' if info['rules_ok'] else '실패 ' + _fmt(info.get('rejected'))} · "
              f"최소 주문 금액 {_fmt(info.get('min_notional'))}",
              f"- 주문 유형: {', '.join(info['order_types'])}",
              f"- 해석하지 않은 필터(기록만): {', '.join(info['unhandled_filters']) or '없음'}", "",
              "| 필터 | 값 |", "|---|---|"]
        for name, vals in info["filters"].items():
            L.append(f"| {name} | {'없음' if vals is None else ', '.join(f'{k}={v}' for k, v in vals.items())} |")
        L.append("")
    L += ["## 3. 호출 한도 (rateLimits, 내부 예산 50% — D-113)", "", "| 종류 | 창(ms) | 한도 | 내부 예산 |",
          "|---|---|---|---|"]
    L += [f"| {r['type']} | {r['interval_ms']} | {r['limit']} | {_fmt(r.get('budget'))} |" for r in rep.rate_limits]
    L += ["", "## 4. 계좌 잔고 (테스트넷 가상 잔고, 0 아닌 자산)", ""]
    a = rep.account_info
    if a is None:
        L.append("- 조회 못 함")
    else:
        L += [f"- canTrade {a['can_trade']} · 계좌 유형 {_fmt(a['account_type'])} · 권한 {', '.join(a['permissions'])}",
              "", "| 자산 | free | locked |", "|---|---|---|"]
        L += [f"| {b['asset']} | {b['free']} | {b['locked']} |" for b in a["balances"]]
    L += ["", "## 5. 미체결·최근 24시간", "", "| 심볼 | 미체결 | 최근 주문 | 최근 체결 |", "|---|---|---|---|"]
    L += [f"| {s} | {_fmt(rep.open_orders.get(s))} | {_fmt(rep.recent_orders.get(s))} | {_fmt(rep.recent_fills.get(s))} |"
          for s in rep.symbols]
    L += ["", "## 6. 시작 대조", "", f"- 결과: {_fmt(rep.reconcile)} {rep.reconcile_detail}".rstrip(),
          f"- 대조 ID: {_fmt(rep.reconcile_id)}", "",
          "## 7. 호출 기록 (보낸 요청만, 경로별)", "", "| 메서드 | 경로 | 호출 | 정상 | 마지막 상태 | 코드 | 분류 |",
          "|---|---|---|---|---|---|---|"]
    L += [f"| {c['method']} | {c['path']} | {c['calls']} | {c['ok']} | {_fmt(c.get('last_status'))} | "
          f"{_fmt(c.get('last_code'))} | {c.get('last_kind')} |" for c in rep.calls]
    L += ["", "## 8. 주문 API", "", f"- 주문 API 전송: {rep.order_api_sent}건",
          f"- 전송 전 차단: {', '.join(rep.order_api_blocked) or '없음'}", ""]
    for title, items in (("경보", rep.alerts), ("경고", rep.warnings), ("실패", rep.failures)):
        if items:
            L += [f"## {title}", ""] + [f"- {x}" for x in items] + [""]
    return "\n".join(L)


def _summary(rep: M2PreflightReport, paths: tuple[Path, Path]) -> str:
    L = [f"M2 시작 점검 (PAPER, 바이낸스 테스트넷 {rep.host}, 계좌 {rep.account}, 심볼 {', '.join(rep.symbols)})",
         f"  서버 시각 차이: {_fmt(rep.server_time_offset_ms)}ms (경고 기준 {rep.server_time_warn_ms}ms)",
         f"  시계(SNTP): {'오차 ' + rep.clock_offset + '초' if rep.clock_offset else '판정 불가'}"]
    for sym, info in rep.exchange.items():
        if info.get("found"):
            L.append(f"  {sym}: 상태 {info['status']}, 규칙 {'정상' if info['rules_ok'] else '실패'}, "
                     f"최소 주문 금액 {_fmt(info.get('min_notional'))}")
        else:
            L.append(f"  {sym}: 확인 못 함")
    weight = [r for r in rep.rate_limits if r["type"] == "REQUEST_WEIGHT"]
    L.append(f"  호출 한도: {', '.join(str(r['limit']) + '/' + str(r['interval_ms'] // 1000) + '초' for r in weight) or '모름'}")
    if rep.account_info is not None:
        held = {b["asset"]: b for b in rep.account_info["balances"]}
        shown = [f"{x} {held[x]['free']}(+{held[x]['locked']})" for x in _focus(rep) if x in held]
        L.append(f"  잔고: {', '.join(shown) or '유니버스 자산 없음'} (그 밖 {len(held) - len(shown)}개 자산은 결과 파일)")
    else:
        L.append("  잔고: 조회 못 함")
    L.append(f"  미체결: {', '.join(f'{s} {_fmt(n)}건' for s, n in rep.open_orders.items()) or '조회 못 함'}")
    L.append(f"  시작 대조: {_fmt(rep.reconcile)}{' — ' + rep.reconcile_detail if rep.reconcile == 'NO_BASELINE' else ''}")
    L.append(f"  주문 API 전송: {rep.order_api_sent}건"
             + (f" (전송 전 차단 {len(rep.order_api_blocked)}회)" if rep.order_api_blocked else ""))
    L += [f"  경보: {x}" for x in rep.alerts]
    L += [f"  경고: {x}" for x in rep.warnings]
    L += [f"  실패: {x}" for x in rep.failures]
    L.append(f"결과: {'통과' if rep.ok else '실패'}")
    L.append(f"결과 파일: {paths[0]}")
    L.append(f"요약 파일: {paths[1]}")
    return "\n".join(L)


def _focus(rep: M2PreflightReport) -> list[str]:
    out: list[str] = []
    for sym, info in rep.exchange.items():
        for k in ("base_asset", "quote_asset"):
            v = info.get(k) if isinstance(info, dict) else None
            if v and v not in out:
                out.append(v)
    return out


__all__ = ["SECRET_KEYS", "TESTNET_REST_URL", "M2PreflightReport", "connect_m2_paper", "m2_preflight_cli",
           "m2_preflight_main"]
