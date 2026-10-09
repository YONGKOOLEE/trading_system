"""시작 검사 (R-01, CLAUDE.md §7, 03 §8.2·D-066 중 모드·설정 부분).

TRADE_MODE·저장소 경로·자격정보 파일·브로커 환경이 모두 같은 모드일 때만 시작한다.
- LIVE는 승인 기록과 사용자 활성화 설정이 모두 있어야 하는데, 이 구현에는 실전 경로가 없다.
  따라서 LIVE는 파일을 읽기 전에 거부한다 (승인 구조 3·4번 전).
- 브로커에 연결하지 않는다. 연결·토큰·API 점검은 T-015.
- 비밀 값은 SecretStore에만 담고 오류 메시지·repr에 싣지 않는다.
- [CP-024 D-096·D-097, T-M2-08] M2는 check_m2_startup: `config/m2/`(limits·market·app·universe) + 시작 일치 표 +
  DB 메타(읽기 전용). M1 check_startup은 그대로(M1 설정 위치·동작 불변). 시장 일치 검사는 run_main이 이어서 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from trading_system.config.app_file import AppSettings, load_app_settings
from trading_system.config.envfile import SecretStore, parse_env_file
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.m2_files import M2MarketSettings, Universe, load_m2_market, load_universe
from trading_system.config.market_scope import MarketScopeError, check_start_scope, config_dir
from trading_system.domain.enums import Mode
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market_time import Market
from trading_system.storage.market_meta import check_db_file
from trading_system.storage.repository import ModeMismatch, StorageFailure


class LiveNotAvailable(ConfigError):
    pass


class ModeConsistencyError(ConfigError):
    pass


@dataclass(frozen=True)
class StartupConfig:
    mode: Mode
    repo_root: Path
    storage_path: Path
    limits_path: Path
    account: str
    limits: AccountLimits
    broker_env: str
    warnings: tuple[str, ...] = ()
    secrets: SecretStore | None = field(default=None, repr=False)


_BROKER_ENV = {Mode.BACKTEST: "fake", Mode.PAPER: "mock", Mode.LIVE: "real"}
_MODE_TOKENS = {m.value for m in Mode}


def expected_broker_env(mode: Mode) -> str:
    return _BROKER_ENV[mode]


def _check_storage(mode: Mode, repo_root: Path, storage_path: Path) -> None:
    allowed = (repo_root / "var" / mode.value.lower()).resolve()
    if not storage_path.resolve().is_relative_to(allowed):
        raise ModeConsistencyError(f"저장소 경로가 {mode.value} 영역(var/{mode.value.lower()}/) 밖이다")


def _check_credentials(mode: Mode, repo_root: Path, env_file: Path | None) -> SecretStore | None:
    if mode is Mode.BACKTEST:
        if env_file is not None:
            raise ModeConsistencyError("BACKTEST는 자격정보 파일을 쓰지 않는다")
        return None
    if env_file is None:
        raise ModeConsistencyError(f"{mode.value}에는 자격정보 파일(.env.{mode.value.lower()})이 필요하다")
    expected = (repo_root / f".env.{mode.value.lower()}").resolve()
    if env_file.resolve() != expected:
        raise ModeConsistencyError(f"자격정보 파일이 {mode.value} 모드의 파일(.env.{mode.value.lower()})이 아니다")

    secrets = parse_env_file(env_file)
    if not secrets.has("TRADE_MODE"):
        raise ModeConsistencyError(f"{env_file.name}에 TRADE_MODE가 없다")
    if secrets.get("TRADE_MODE").reveal() != mode.value:
        raise ModeConsistencyError(f"{env_file.name}의 TRADE_MODE가 {mode.value}가 아니다")
    for key in secrets.keys():
        foreign = (set(key.split("_")) & _MODE_TOKENS) - {mode.value}
        if foreign:
            raise ModeConsistencyError(f"{env_file.name}에 다른 모드({', '.join(sorted(foreign))})의 키가 있다: {key}")
    return secrets


@dataclass(frozen=True)
class M2StartupConfig:
    """M2 시작 검사 결과 (CP-024 D-096·D-097, T-M2-08). 비밀 값은 repr에 싣지 않는다."""

    mode: Mode
    repo_root: Path
    storage_path: Path
    market: Market
    broker: str
    account: str
    limits: AccountLimits
    app: AppSettings
    market_settings: M2MarketSettings
    universe: Universe
    warnings: tuple[str, ...] = ()
    secrets: SecretStore | None = field(default=None, repr=False)


def check_m2_startup(mode: Mode, repo_root: Path, storage_path: Path, env_file: Path | None) -> M2StartupConfig:
    """M2 시작 검사: 모드·저장소 경로·자격정보 파일(M1과 같은 규칙) + `config/m2/` 네 파일 + 시작 일치 표 + DB 메타.

    DB를 만들거나 쓰지 않는다(메타 검사는 읽기 전용). 접속 도메인은 연결을 조립할 때(T-M2-14) 검사한다 — 지금은
    M2 연결 경로가 없으므로 None(검사 생략)으로 두고 호출자가 연결 전에 거부한다."""
    if mode is Mode.LIVE:
        raise LiveNotAvailable(
            "LIVE는 시작할 수 없다: 승인 기록과 사용자 활성화 설정이 모두 필요하며 이 구현에는 실전 경로가 없다")
    _check_storage(mode, repo_root, storage_path)
    secrets = _check_credentials(mode, repo_root, env_file)
    d, m = config_dir(repo_root, Market.M2), mode.value.lower()
    try:
        parsed = load_limits_file(d / f"limits.{m}.toml", market=Market.M2)
    except ConfigError as exc:
        raise ConfigError([f"limits.{m}.toml(M2): {p}" for p in exc.problems]) from None
    app = load_app_settings(d / f"app.{m}.toml", mode, repo_root)
    if app.market != Market.M2.value:
        raise MarketScopeError(f"config/m2/app.{m}.toml의 market이 M2가 아니다 ({app.market})")
    if app.account != parsed.account:
        raise ConfigError("M2 배포 설정의 계좌 별칭과 한도 파일의 계좌가 다르다")
    market_settings = load_m2_market(d / f"market.{m}.toml")
    universe = load_universe(d / f"universe.{m}.toml")
    if not universe.symbols:
        raise ConfigError("M2 유니버스 미결정 (config/m2/universe 심볼 없음 — D-105, PV-M2-03 뒤 사용자 결정)")
    check_start_scope(mode, Market.M2, app.broker, app.account, None)
    try:
        check_db_file(storage_path, mode, Market.M2, app.broker)
    except ModeMismatch as exc:
        raise MarketScopeError(f"DB 메타 불일치: {exc}") from None
    except StorageFailure as exc:
        raise ConfigError(f"DB 메타 확인 실패: {exc}") from None
    return M2StartupConfig(mode=mode, repo_root=repo_root, storage_path=storage_path, market=Market.M2,
                           broker=app.broker, account=app.account, limits=parsed.limits, app=app,
                           market_settings=market_settings, universe=universe, warnings=parsed.warnings,
                           secrets=secrets)


def check_startup(mode: Mode, repo_root: Path, storage_path: Path, env_file: Path | None,
                  limits_path: Path | None = None) -> StartupConfig:
    if mode is Mode.LIVE:
        raise LiveNotAvailable(
            "LIVE는 시작할 수 없다: 승인 기록과 사용자 활성화 설정이 모두 필요하며 이 구현에는 실전 경로가 없다")
    _check_storage(mode, repo_root, storage_path)
    secrets = _check_credentials(mode, repo_root, env_file)
    path = limits_path or repo_root / "config" / f"limits.{mode.value.lower()}.toml"
    parsed = load_limits_file(path)
    return StartupConfig(
        mode=mode, repo_root=repo_root, storage_path=storage_path, limits_path=path,
        account=parsed.account, limits=parsed.limits, broker_env=expected_broker_env(mode),
        warnings=parsed.warnings, secrets=secrets,
    )
