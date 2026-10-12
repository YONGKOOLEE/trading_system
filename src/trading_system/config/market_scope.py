"""시장 경계: 설정 위치·시작 일치 허용 표 (CP-024 D-096·D-097·D-098, T-M2-08, V-147).

- 설정 위치: M1 = `config/`(기존 경로 그대로), M2 = `config/m2/`(limits·market·app·universe).
- 시작 일치 허용 표: (모드, market, broker, 접속 도메인, 계좌 별칭 접두어)가 아래 표의 한 행과 **정확히** 맞아야 시작한다.
  LIVE 행은 없다(실전 경로 없음, 승인 구조 3·4 전). 모드 값만 바꿔 실전에 접속할 수 없다(CLAUDE.md §7).
- 접속 도메인: 브로커가 연결할 호스트 집합. BACKTEST(fake)는 연결이 없으므로 빈 집합이어야 한다. PAPER는 하나 이상이고
  전부 허용 집합 안이어야 한다. None은 "연결 대상을 아직 조립하지 않음"이다 — 그 경로는 연결 전에 거부되어야 한다(M2 실행
  경로는 T-M2-14 전까지 없음).
- 바이낸스 도메인은 테스트넷만 둔다(D-098). 실전·Demo 도메인 상수는 없다.
- 비밀정보 없음. 표준 라이브러리와 domain만 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market

from .errors import ConfigError

BROKER_FAKE = "fake"
BROKER_KIWOOM_MOCK = "kiwoom-mock"
BROKER_BINANCE_TESTNET = "binance-spot-testnet"

KIWOOM_MOCK_HOSTS = frozenset({"mockapi.kiwoom.com"})   # broker/kiwoom/adapter.py MOCK_REST·MOCK_WS (키움 모의)
BINANCE_TESTNET_HOSTS = frozenset({                      # BN §2.1 Spot 테스트넷 REST·WS API·스트림 (D-098)
    "testnet.binance.vision", "ws-api.testnet.binance.vision", "stream.testnet.binance.vision"})


class MarketScopeError(ConfigError):
    """시장 경계 불일치 → 시작 거부(브로커 연결 없음)."""


@dataclass(frozen=True)
class MarketRule:
    broker: str
    hosts: frozenset[str]       # 빈 집합 = 연결 없음(fake)
    alias_prefix: str


START_TABLE: dict[tuple[Mode, Market], MarketRule] = {
    (Mode.BACKTEST, Market.M1): MarketRule(BROKER_FAKE, frozenset(), "KR-"),
    (Mode.PAPER, Market.M1): MarketRule(BROKER_KIWOOM_MOCK, KIWOOM_MOCK_HOSTS, "KR-"),
    (Mode.BACKTEST, Market.M2): MarketRule(BROKER_FAKE, frozenset(), "BN-"),
    (Mode.PAPER, Market.M2): MarketRule(BROKER_BINANCE_TESTNET, BINANCE_TESTNET_HOSTS, "BN-"),
}


def parse_market(raw: object) -> Market:
    if not isinstance(raw, str) or raw not in {m.value for m in Market}:
        raise ConfigError(f"시장 값이 잘못되었다: {raw!r} (M1|M2, 대문자 정확히)")
    return Market(raw)


def market_rule(mode: Mode, market: Market) -> MarketRule:
    rule = START_TABLE.get((mode, market))
    if rule is None:
        raise MarketScopeError(f"시작 일치 허용 표에 ({mode.value}, {market.value}) 행이 없다 — 시작하지 않는다")
    return rule


def default_broker(mode: Mode, market: Market) -> str:
    """앱 설정에 broker 키가 없을 때의 값 (M1만 쓴다, D-097: M1·kiwoom-mock, BACKTEST는 fake)."""
    return market_rule(mode, market).broker


def config_dir(root: Path, market: Market) -> Path:
    return root / "config" if market is Market.M1 else root / "config" / "m2"


def check_start_scope(mode: Mode, market: Market, broker: str, account: str,
                      hosts: Iterable[str] | None) -> None:
    """허용 표와 정확히 맞지 않으면 MarketScopeError (V-147). 문제를 모두 모아 한 번에 알린다."""
    rule = market_rule(mode, market)
    problems: list[str] = []
    if broker != rule.broker:
        problems.append(f"broker {broker!r} ≠ 허용 {rule.broker!r} ({mode.value}, {market.value})")
    if not account.startswith(rule.alias_prefix):
        problems.append(f"계좌 별칭 접두어가 {market.value}의 {rule.alias_prefix}가 아니다: {account}")
    if hosts is not None:
        hs = frozenset(hosts)
        if not rule.hosts and hs:
            problems.append(f"접속 도메인: {mode.value}({broker})는 연결이 없어야 한다: {', '.join(sorted(hs))}")
        elif rule.hosts and not hs:
            problems.append("접속 도메인 없음")
        elif hs - rule.hosts:
            problems.append(f"접속 도메인이 허용 표 밖: {', '.join(sorted(hs - rule.hosts))}")
    if problems:
        raise MarketScopeError(problems)
