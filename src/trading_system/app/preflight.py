"""시작 점검(preflight) — 키움 모의 접속, 조회만 (T-015, 03 D-049·D-066, 04 PV-06·PV-11, 승인 A-035).

- 모드·자격정보·설정이 PAPER로 일치하고 사용자가 `config/app.paper.toml [paper] connection_approved = true`로
  켠 경우에만 연결한다. 하나라도 어긋나면 네트워크 객체를 만들기 전에 거부한다.
- 토큰 발급(au10001) → 사용할 조회 API 1회씩(8104 = 모의 미지원 확인) → 시작 대조 → 토큰 폐기(au10002).
- HTTP 경계(GuardedHttp)에서 주문 API(kt10000~kt10003, 주문 경로)를 **전송 전에** 막는다. 이 경로로는 주문이 나갈 수 없다.
- 기록은 API별 호출 결과(HTTP 상태·코드·분류)와 건수만 남긴다. 응답 본문·금액·앱키·토큰·계좌번호는 남기지 않는다.
- 시계(SNTP)는 외부 연결 승인 전이라 점검하지 않고 보고서에 미점검으로 적는다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom.adapter import (
    MOCK_REST, MOCK_WS, ORDER_PATH, KiwoomAdapter, KiwoomConfig, check_endpoint)
from trading_system.broker.kiwoom.errors import ErrorClass, classify, normalize_code, specific_code
from trading_system.broker.kiwoom.http import AfterSendError, HttpReply, KiwoomHttp, PreSendError
from trading_system.broker.kiwoom.token import TokenError
from trading_system.config.app_file import AppSettings, load_app_settings
from trading_system.config.errors import ConfigError
from trading_system.config.modes import read_trade_mode
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.domain.enums import Mode
from trading_system.domain.market import InstrumentInfo, InstrumentKind
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.errors import MarketDataError
from trading_system.marketdata.kiwoom_feed import KiwoomMarketFeed
from trading_system.marketdata.store import MarketDataStore
from trading_system.reconciliation.reconciler import Reconciler
from trading_system.storage.jsonlog import Masker
from trading_system.storage.repository import Repository

from .startup import check_startup

ORDER_API_IDS = frozenset({"kt10000", "kt10001", "kt10002", "kt10003"})
# 이 시스템이 쓰는 조회 API (adapter: 예수금·잔고·주문·미체결, kiwoom_feed: 일봉·시세·종목 정보·VI)
QUERY_APIS = ("kt00001", "kt00018", "kt00009", "ka10075", "ka10081", "ka10001", "ka10099", "ka10054")
PROBE_DEFAULT = "005930"   # 배포 설정에 종목이 없을 때 시세 조회에 쓸 종목 (조회만)
SECRET_KEYS = ("KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_SECRET_KEY", "KIWOOM_PAPER_ACCOUNT_NO",
               "KIWOOM_PAPER_ACCOUNT_ALIAS")


@dataclass(frozen=True)
class CallRecord:
    api_id: str
    status: int | None        # 응답이 없으면 None
    code: int | None
    error_class: str          # ErrorClass 값 또는 TRANSPORT
    message: str = ""         # 실패 응답의 서버 문구(가림 처리·길이 제한). 성공 응답은 남기지 않는다


MESSAGE_MAX = 160


def _server_message(body: dict | None) -> str:
    if body is None:
        return ""
    return Masker().text(str(body.get("return_msg") or ""))[:MESSAGE_MAX]


class GuardedHttp:
    """KiwoomHttp를 감싸 주문 API를 전송 전에 막고, 호출마다 결과 분류만 기록한다."""

    def __init__(self, inner: KiwoomHttp) -> None:
        self._inner = inner
        self.records: list[CallRecord] = []
        self.blocked: list[str] = []

    @property
    def calls(self) -> int:
        return self._inner.calls

    def post(self, path: str, api_id: str, body: dict, token: str | None,
             cont_yn: str | None = None, next_key: str | None = None) -> HttpReply:
        if path == ORDER_PATH or api_id in ORDER_API_IDS:
            self.blocked.append(api_id)
            raise PreSendError(f"{api_id}: 시작 점검 경로에서는 주문 API를 보내지 않는다")
        try:
            r = self._inner.post(path, api_id, body, token, cont_yn, next_key)
        except (PreSendError, AfterSendError):
            self.records.append(CallRecord(api_id, None, None, "TRANSPORT"))
            raise
        code = None if r.body is None else \
            specific_code(normalize_code(r.body.get("return_code")), r.body.get("return_msg"))
        if r.status == 401 and code in (None, 0):
            code = 8005
        if r.body is None:
            cls = ErrorClass.UNEXPECTED
        else:
            cls = classify(code if code is not None else (0 if r.status < 400 else None))
            if r.status >= 400 and cls is ErrorClass.OK:
                cls = ErrorClass.UNEXPECTED
        msg = "" if cls is ErrorClass.OK else _server_message(r.body)
        self.records.append(CallRecord(api_id, r.status, code, cls.value, msg))
        return r


@dataclass(frozen=True)
class ApiStatus:
    api_id: str
    calls: int
    ok: bool                  # 정상 응답이 한 번 이상
    error_class: str = ""     # 마지막 실패 분류
    code: int | None = None   # 마지막 실패 코드
    message: str = ""         # 마지막 실패 서버 문구(가림 처리)


@dataclass
class PreflightReport:
    started_at: str
    account: str
    probe_code: str
    apis: list[ApiStatus] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    token_expires_at: str | None = None
    token_revoked: bool = False
    reconcile: str | None = None
    reconcile_id: str | None = None
    order_api_blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


class _Alerts:
    def __init__(self) -> None:
        self.items: list[str] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.items.append(f"{level}: {message}")


def probe_info(instruments: Mapping[str, InstrumentInfo], code: str) -> InstrumentInfo:
    return instruments.get(code) or InstrumentInfo(code, InstrumentKind.STOCK, Decimal("1"), Decimal("1"))


def _masked(exc: Exception) -> str:
    return Masker().text(str(exc))


def run_preflight(adapter: KiwoomAdapter, guard: GuardedHttp, store: MarketDataStore, repo: Repository,
                  clock: Clock, account: str, probe_code: str,
                  instruments: Mapping[str, InstrumentInfo] | None = None) -> PreflightReport:
    rep = PreflightReport(clock.now().isoformat(), account, probe_code)
    alerts = _Alerts()
    try:
        try:
            adapter.tokens.token()
        except TokenError as exc:
            rep.failures.append(f"토큰 발급 실패: {_masked(exc)}")
            return rep
        exp = adapter.tokens.expires_at
        rep.token_expires_at = exp.astimezone(KST).isoformat() if exp else None

        account_steps: list[tuple[str, Callable[[], object]]] = [
            ("예수금 kt00001",lambda: adapter.query_cash(account)),
            ("잔고 kt00018", lambda: adapter.query_positions(account)),
            ("주문·미체결 kt00009·ka10075", lambda: adapter.query_orders(account)),
            ("체결 kt00009", lambda: adapter.query_fills(account)),
        ]
        account_ok = True
        for name, step in account_steps:
            try:
                step()
            except BrokerQueryError as exc:
                account_ok = False
                rep.failures.append(f"{name} 조회 실패: {_masked(exc)}")

        info = probe_info(instruments or {}, probe_code)
        feed = KiwoomMarketFeed(adapter, store, clock, {probe_code: info})
        prepared = feed.refresh_daily(probe_code)
        if prepared is not None and not prepared.report.ok:
            rep.warnings.append(f"일봉 품질 검사 실패({probe_code}) — 조회는 됨: "
                                f"{'; '.join(prepared.report.issues[:5])}")
        feed.refresh_quote(probe_code)
        feed.refresh_master()
        feed.refresh_vi()

        if account_ok:
            res = Reconciler(repo, adapter, clock, None, alerts).run(account, "PREFLIGHT")
            rep.reconcile, rep.reconcile_id = res.result, res.recon_id
            if res.result == "MISMATCH":
                rep.failures.append(f"시작 대조 불일치 {len(res.items)}건 — scripts/reconcile-resolve.py로 확인")
            elif res.result != "MATCH":
                rep.failures.append(f"시작 대조 실패 ({res.result})")
    finally:
        if rep.token_expires_at is not None:
            rep.token_revoked = adapter.close()
            if not rep.token_revoked:
                rep.warnings.append("토큰 폐기 실패 — 만료 시각에 정리됨")
        _summarize(rep, guard, adapter)
        rep.warnings.extend(alerts.items)
        repo.audit(clock.now(), "PREFLIGHT", "preflight", rep.reconcile_id,
                   {"ok": rep.ok, "unsupported": rep.unsupported, "failures": len(rep.failures),
                    "reconcile": rep.reconcile})
    return rep


def _summarize(rep: PreflightReport, guard: GuardedHttp, adapter: KiwoomAdapter) -> None:
    by_api: dict[str, list[CallRecord]] = {}
    for r in guard.records:
        by_api.setdefault(r.api_id, []).append(r)
    issued = rep.token_expires_at is not None
    for api in QUERY_APIS:
        recs = by_api.get(api, [])
        ok = any(r.error_class == ErrorClass.OK.value for r in recs)
        failed = [r for r in recs if r.error_class != ErrorClass.OK.value]
        bad = [r.error_class for r in failed]
        last = failed[-1] if failed else None
        rep.apis.append(ApiStatus(api, len(recs), ok, last.error_class if last else "",
                                  last.code if last else None, last.message if last else ""))
        if any(r.code == 8104 for r in recs):
            rep.unsupported.append(api)
        if issued and not recs:
            rep.failures.append(f"{api}: 호출되지 않음")
        elif recs and not ok:
            rep.failures.append(f"{api}: 정상 응답 없음 ({bad[-1]})")
    if rep.unsupported:
        rep.failures.append(f"모의 미지원(8104): {', '.join(rep.unsupported)} — 설계 오류, 시작 거부")
    for kind, msg in adapter.critical:
        rep.failures.append(f"브로커 치명 {kind}: {_masked(Exception(msg))}")
    rep.order_api_blocked = list(guard.blocked)
    if guard.blocked:
        rep.failures.append(f"점검 중 주문 API 호출 시도(전송 전 차단): {', '.join(guard.blocked)}")


# --- 진입점 -----------------------------------------------------------------------------------------

def _refuse(msg: str) -> int:
    print(f"시작 점검 거부(연결 없음): {msg}")
    return 2


@dataclass
class PaperConnection:
    """연결 전 검사를 통과한 키움 모의 조회 연결 (주문 API는 GuardedHttp가 막는다)."""
    adapter: KiwoomAdapter
    guard: GuardedHttp
    app: AppSettings
    storage_path: Path


def connect_paper(root: Path, environ: Mapping[str, str] | None, clock: Clock, *, rest_url: str = MOCK_REST,
                  ws_url: str = MOCK_WS, per_tr_interval: float = 1.1) -> PaperConnection | str:
    """PAPER 모드·자격정보·설정·사용자 접속 스위치를 확인한다. 하나라도 어긋나면 거부 사유(문자열)를 돌려주고
    네트워크 객체를 만들지 않는다."""
    env = os.environ if environ is None else environ
    try:
        mode = read_trade_mode(env, root / ".env")
    except ConfigError as exc:
        return str(exc)
    if mode is not Mode.PAPER:
        return f"TRADE_MODE={mode.value} — 키움 모의 조회는 PAPER에서만 한다 (LIVE·BACKTEST는 연결하지 않는다)"
    base = root / "var" / "paper"
    try:
        startup = check_startup(Mode.PAPER, root, base / "trading.db", root / ".env.paper")
        app = load_app_settings(root / "config" / "app.paper.toml", Mode.PAPER, root)
        if app.account != startup.account:
            raise ConfigError("배포 설정의 계좌 별칭과 한도 파일의 계좌가 다르다")
        if not app.paper_connection_approved:
            raise ConfigError("config/app.paper.toml [paper] connection_approved = true가 아니다 "
                              "(키움 모의 접속은 사용자가 직접 켠다)")
        s = startup.secrets
        missing = [k for k in SECRET_KEYS if s is None or not s.has(k)]
        if missing:
            raise ConfigError(f".env.paper에 없는 키: {', '.join(missing)}")
        assert s is not None
        if s.get("KIWOOM_PAPER_ACCOUNT_ALIAS").reveal() != app.account:
            raise ConfigError(".env.paper의 계좌 별칭이 배포 설정과 다르다")
        endpoint = check_endpoint(rest_url, MOCK_REST)
    except ConfigError as exc:
        return str(exc)
    except ValueError as exc:
        return str(exc)
    base.mkdir(parents=True, exist_ok=True)
    guard = GuardedHttp(KiwoomHttp(endpoint))
    cfg = KiwoomConfig(app.account, s.get("KIWOOM_PAPER_ACCOUNT_NO").reveal, s.get("KIWOOM_PAPER_APP_KEY").reveal,
                       s.get("KIWOOM_PAPER_SECRET_KEY").reveal, rest_url=rest_url, ws_url=ws_url,
                       per_tr_interval=per_tr_interval)
    return PaperConnection(KiwoomAdapter(cfg, clock, http=guard), guard, app,  # type: ignore[arg-type]
                           startup.storage_path)


def preflight_main(root: Path, environ: Mapping[str, str] | None = None, *, rest_url: str = MOCK_REST,
                   ws_url: str = MOCK_WS, clock: Clock | None = None, probe_code: str | None = None,
                   per_tr_interval: float = 1.1) -> int:
    clock = clock or SystemClock()
    conn = connect_paper(root, environ, clock, rest_url=rest_url, ws_url=ws_url, per_tr_interval=per_tr_interval)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard, app = conn.adapter, conn.guard, conn.app
    base = root / "var" / "paper"
    try:
        calendar = KrxCalendar.load_dir(root / "config" / "calendar")
    except MarketDataError as exc:
        adapter.close()
        return _refuse(f"달력 파일 오류: {exc}")
    code = probe_code or next(iter(app.instruments), PROBE_DEFAULT)
    repo = Repository(conn.storage_path, Mode.PAPER)
    try:
        rep = run_preflight(adapter, guard, MarketDataStore(calendar, None), repo, clock, app.account, code,
                            app.instruments)
    finally:
        repo.close()
    if calendar.session(clock.now().astimezone(KST).date()) is None:
        rep.warnings.append("올해 시장 달력 없음(config/calendar) — 운영 시 모든 주문 거부. X-U3 작성 필요")
    rep.warnings.append("시계(SNTP) 미점검 — SNTP 외부 연결 승인 전")
    _write(base, rep, clock.now())
    _print(rep)
    return 0 if rep.ok else 1


def _write(base: Path, rep: PreflightReport, now: datetime) -> Path:
    out = base / "preflight"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"preflight-{now.astimezone(KST).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print(rep: PreflightReport) -> None:
    print(f"시작 점검 (PAPER, 계좌 {rep.account}, 시세 점검 종목 {rep.probe_code})")
    print(f"  토큰: {'발급' if rep.token_expires_at else '실패'}"
          f"{' (만료 ' + rep.token_expires_at + ')' if rep.token_expires_at else ''}, "
          f"폐기 {'완료' if rep.token_revoked else '안 됨'}")
    for a in rep.apis:
        detail = f", {a.error_class} 코드 {a.code} {a.message}" if a.error_class else ""
        print(f"  {a.api_id}: {'정상' if a.ok else '실패'} (호출 {a.calls}회{detail})")
    print(f"  시작 대조: {rep.reconcile or '안 함'}")
    print(f"  주문 API 전송: 0건{' (차단 ' + str(len(rep.order_api_blocked)) + '회)' if rep.order_api_blocked else ''}")
    for w in rep.warnings:
        print(f"  경고: {w}")
    for f in rep.failures:
        print(f"  실패: {f}")
    print(f"결과: {'통과' if rep.ok else '실패'}")
