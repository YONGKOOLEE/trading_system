"""배포 설정 로더 (T-020). 파일: `config/app.<mode>.toml`. 비밀정보는 넣지 않는다.

[app]
  account                       계좌 별칭 (한도 파일의 account와 같아야 함)
  loop_seconds                  판정·실행 루프 주기 (문자열 소수, 0 초과 5 이하)
  reconcile_minutes             장중 주기 대조 (정수, 1 이상)
  sntp_servers                  ["host:port", ...] (2곳 권장, 03 §5.3)
  market                        (선택) M1|M2. 없으면 M1 (CP-024 D-097 — M1 설정 파일은 바꾸지 않는다)
  broker                        (선택) 브로커 이름. M1은 없으면 모드별 기본(PAPER kiwoom-mock, BACKTEST fake).
                                M2는 반드시 적는다(PAPER binance-spot-testnet, BACKTEST fake). 맞는지는 시작 일치 표가 본다
[paper]                         (PAPER만)
  connection_approved           키움 모의 접속 사용자 승인 여부. 기본 없음 = false. **사용자만 true로 바꾼다** (T-015, D-083)
[telegram]                      (PAPER만, 선택)
  enabled                       텔레그램 알림·원격 명령 연결 (true/false, 기본 false). true면 .env.paper에
                                TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID가 있어야 시작 (03 D-052, 01 §2.9·§2.10)
[[instruments]]  code, kind(STOCK|ETF|ETN), leverage(문자열, 부호), lot_size(문자열). M1만 — M2 종목은 config/m2/universe
[[strategies]]   module("패키지.모듈:클래스"), params(파라미터 파일 경로, 저장소 기준), eval_times(["HH:MM"] KST)
                 verification_approval (선택, PAPER만): PAPER-0 검증 전략 승인 A-ID (DP-24 a, A-037)

기본값을 두지 않는다(PAPER 접속 승인 제외). 누락·형식 오류·알 수 없는 키면 시작을 거부한다.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.domain.enums import Mode
from trading_system.domain.market import InstrumentInfo, InstrumentKind
from trading_system.domain.market_time import Market

from .errors import ConfigError
from .market_scope import default_broker

_MODULE = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")
_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


@dataclass(frozen=True)
class StrategyDeploy:
    module: str
    params: Path
    eval_times: tuple[time, ...]
    verification_approval: str | None = None   # PAPER-0 검증 전략 승인 A-ID (등록 검증 예외, DP-24 a)


@dataclass(frozen=True)
class AppSettings:
    account: str
    loop_seconds: Decimal
    reconcile_minutes: int
    sntp_servers: tuple[tuple[str, int], ...]
    instruments: dict[str, InstrumentInfo]
    strategies: tuple[StrategyDeploy, ...]
    paper_connection_approved: bool = False
    telegram_enabled: bool = False
    market: str = Market.M1.value   # CP-024 D-097: 키가 없으면 M1
    broker: str = ""                # 시작 일치 표로 검사 (config.market_scope)


def _dec(raw: object, name: str, problems: list[str]) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, str):
        problems.append(f"{name}: 문자열 소수여야 함 (부동소수 금지)")
        return None
    try:
        v = Decimal(raw)
    except InvalidOperation:
        problems.append(f"{name}: 소수 형식 아님")
        return None
    if not v.is_finite():
        problems.append(f"{name}: 유한한 수여야 함")
        return None
    return v


def parse_app_settings(raw: dict, mode: Mode, repo_root: Path) -> AppSettings:
    problems: list[str] = []
    allowed = {"app", "instruments", "strategies"} | ({"paper", "telegram"} if mode is Mode.PAPER else set())
    for k in raw:
        if k not in allowed:
            problems.append(f"{k}: 알 수 없는 키 (이 모드에서 허용: {', '.join(sorted(allowed))})")
    app = raw.get("app")
    if not isinstance(app, dict):
        problems.append("[app] 표가 없다")
        app = {}
    for k in app:
        if k not in ("account", "loop_seconds", "reconcile_minutes", "sntp_servers", "market", "broker"):
            problems.append(f"app.{k}: 알 수 없는 키")
    market, broker = _market_broker(app, mode, problems)
    account = app.get("account")
    if not isinstance(account, str) or not re.fullmatch(r"[A-Z]{2}-[A-Z]+-\d{2}", account or ""):
        problems.append("app.account: 계좌 별칭 형식(예: KR-PAPER-01) — 계좌번호를 쓰지 않는다")
    loop = _dec(app.get("loop_seconds"), "app.loop_seconds", problems)
    if loop is not None and not Decimal("0") < loop <= Decimal("5"):
        problems.append("app.loop_seconds: 0 초과 5 이하")
    rec = app.get("reconcile_minutes")
    if isinstance(rec, bool) or not isinstance(rec, int) or rec < 1:
        problems.append("app.reconcile_minutes: 1 이상 정수")
    servers: list[tuple[str, int]] = []
    sn = app.get("sntp_servers")
    if not isinstance(sn, list) or not sn:
        problems.append("app.sntp_servers: ['host:port', ...] 목록")
    else:
        for s in sn:
            host, _, port = str(s).rpartition(":")
            if not host or not port.isdigit() or not 0 < int(port) < 65536:
                problems.append(f"app.sntp_servers: 형식 오류 {s!r}")
            else:
                servers.append((host, int(port)))

    approved = False
    if mode is Mode.PAPER:
        paper = raw.get("paper", {})
        if not isinstance(paper, dict) or any(k != "connection_approved" for k in paper):
            problems.append("[paper]에는 connection_approved만 둔다")
        else:
            ca = paper.get("connection_approved", False)
            if not isinstance(ca, bool):
                problems.append("paper.connection_approved: true/false")
            approved = ca is True

    telegram = False
    if mode is Mode.PAPER and "telegram" in raw:
        tgr = raw["telegram"]
        if not isinstance(tgr, dict) or any(k != "enabled" for k in tgr):
            problems.append("[telegram]에는 enabled만 둔다 (토큰·채팅 ID는 .env.paper)")
        elif not isinstance(tgr.get("enabled", False), bool):
            problems.append("telegram.enabled: true/false")
        else:
            telegram = tgr.get("enabled", False) is True
    infos: dict[str, InstrumentInfo] = {}
    if market == Market.M2.value and raw.get("instruments"):
        problems.append("instruments: M2 종목은 config/m2/universe 파일에 둔다 ([[instruments]]는 M1 형식)")
    for i, it in enumerate([] if market == Market.M2.value else raw.get("instruments") or []):
        if not isinstance(it, dict) or set(it) != {"code", "kind", "leverage", "lot_size"}:
            problems.append(f"instruments[{i}]: code·kind·leverage·lot_size 네 키가 필요")
            continue
        code = it["code"]
        if not isinstance(code, str) or not (code.isascii() and code.isdigit() and len(code) == 6):
            problems.append(f"instruments[{i}].code: 6자리 숫자")
            continue
        if code in infos:
            problems.append(f"instruments[{i}].code: 중복 {code}")
            continue
        lev = _dec(it["leverage"], f"instruments[{i}].leverage", problems)
        lot = _dec(it["lot_size"], f"instruments[{i}].lot_size", problems)
        if it["kind"] not in {k.value for k in InstrumentKind}:
            problems.append(f"instruments[{i}].kind: STOCK|ETF|ETN")
            continue
        if lev is None or lot is None:
            continue
        try:
            infos[code] = InstrumentInfo(code, InstrumentKind(it["kind"]), lev, lot)
        except ValueError as exc:
            problems.append(f"instruments[{i}]: {exc}")

    strategies: list[StrategyDeploy] = []
    for i, st in enumerate(raw.get("strategies") or []):
        optional = {"verification_approval"} if mode is Mode.PAPER else set()
        if not isinstance(st, dict) or not {"module", "params", "eval_times"} <= set(st) or \
                set(st) - {"module", "params", "eval_times"} - optional:
            problems.append(f"strategies[{i}]: module·params·eval_times 세 키가 필요"
                            f"{' (PAPER는 verification_approval 선택)' if optional else ''}")
            continue
        va = st.get("verification_approval")
        if va is not None and (not isinstance(va, str) or not re.fullmatch(r"A-\d{3,}", va)):
            problems.append(f"strategies[{i}].verification_approval: 승인 A-ID(A-숫자)")
            continue
        if not isinstance(st["module"], str) or not _MODULE.fullmatch(st["module"]):
            problems.append(f"strategies[{i}].module: '패키지.모듈:클래스'")
            continue
        p = (repo_root / str(st["params"])).resolve()
        if not p.is_relative_to((repo_root / "config").resolve()):
            problems.append(f"strategies[{i}].params: config/ 아래 파일이어야 함")
            continue
        times = st["eval_times"]
        if not isinstance(times, list) or not times or not all(isinstance(t, str) and _HHMM.fullmatch(t)
                                                              for t in times):
            problems.append(f"strategies[{i}].eval_times: ['HH:MM', ...] (KST)")
            continue
        strategies.append(StrategyDeploy(st["module"], p,
                                         tuple(sorted(time(int(t[:2]), int(t[3:])) for t in set(times))), va))
    if problems:
        raise ConfigError(problems)
    return AppSettings(account, loop, rec, tuple(servers), infos, tuple(strategies), approved,  # type: ignore[arg-type]
                       telegram, market, broker)


def _market_broker(app: dict, mode: Mode, problems: list[str]) -> tuple[str, str]:
    """app.market·app.broker (CP-024 D-097). M1은 키가 없으면 M1·모드별 기본 브로커, M2는 broker를 명시해야 한다."""
    raw_market = app.get("market", Market.M1.value)
    if not isinstance(raw_market, str) or raw_market not in {m.value for m in Market}:
        problems.append("app.market: M1|M2 (대문자 정확히)")
        return Market.M1.value, ""
    raw_broker = app.get("broker")
    if raw_broker is None:
        if raw_market == Market.M2.value:
            problems.append("app.broker: M2 설정에는 broker가 필요 (기본값은 M1만)")
            return raw_market, ""
        try:
            return raw_market, default_broker(mode, Market.M1)
        except ConfigError:   # LIVE: 허용 표에 행이 없다 — 시작 검사가 거부한다
            return raw_market, ""
    if not isinstance(raw_broker, str) or not raw_broker:
        problems.append("app.broker: 비어 있지 않은 문자열")
        return raw_market, ""
    return raw_market, raw_broker


def load_app_settings(path: Path, mode: Mode, repo_root: Path) -> AppSettings:
    if not path.exists():
        raise ConfigError(f"배포 설정 파일 없음: {path.name} (예제: config/app.example.toml)")
    with path.open("rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path.name}: TOML 형식 오류 ({exc})") from None
    return parse_app_settings(raw, mode, repo_root)
