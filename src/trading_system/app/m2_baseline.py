"""M2 기준선 확정·테스트넷 리셋 확인 — 로컬 운영 도구 (CP-036 D-210, CP-024 D-099·D-103, V-288). 사람이 직접 실행한다.

`scripts/reconcile-resolve.py --mode PAPER --market M2 initial-baseline` / `... reset-confirm`
- 로컬 전용: 원격(텔레그램) 명령은 없다 — 운영 명령 표(ops_command)를 거치지 않고 이 프로세스가 직접 쓴다.
- **엔진 정지 중에만**: 실행 잠금(var/<mode>/run.lock)이 잡혀 있거나 하트비트가 살아 있으면 거부(아무것도 쓰지 않음).
  DB를 열기 직전 실행 잠금을 **잡고 도구가 끝날 때까지 쥔다**(재검토) — 쓰는 사이 엔진이 시작하지 못한다(엔진은 이중 실행
  거부). 그때 잠금을 못 잡으면(그 사이 엔진 시작) 거부.
- 잔고 조회 실패(리셋 확인 안의 두 번째 조회 포함)는 분명한 거부(종료 코드 2) — 기준선·감사는 쓰지 않는다.
- 연결은 조회만(A-096 범위): PAPER = 바이낸스 테스트넷 계좌 잔고 조회(주문 스위치 꺼짐 전송 계층, scripts가 주입).
  BACKTEST(fake 연습) = 거래소가 없으므로 사람이 --quote-amount로 USDT 가상 잔고를 적는다.
- initial-baseline: 기준선이 이미 있으면 거부. reset-confirm: 리셋 확인(confirm_reset). 둘 다 활성 의도·열린 주문 있으면 거부.
- K4는 풀지 않는다 — 다음 run의 대조 일치 + 사용자 재개(D-099).
- DB: M2 메타·SYMBOL 식별. 다른 시장 DB면 거부. 없으면 M2 메타로 만든다(run 첫 시작과 같은 기록).
비밀 값은 출력하지 않는다. 이 모듈은 바이낸스 패키지를 import하지 않는다(D-119).
"""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_system.broker.base import BrokerQueryError
from trading_system.config.errors import ConfigError
from trading_system.config.modes import parse_trade_mode
from trading_system.domain.clock import Clock, SystemClock
from trading_system.domain.enums import Mode
from trading_system.domain.market import CostRates
from trading_system.domain.market_time import M2_TIME, Market
from trading_system.monitoring.spot_cash import SpotAccountFacts
from trading_system.reconciliation.spot import ResetError, confirm_reset, initial_baseline
from trading_system.storage.market_meta import bind_market
from trading_system.storage.repository import ModeMismatch, OrderIdentity, Repository, StorageFailure

from .instance_lock import InstanceLocked, acquire_instance_lock, instance_running
from .preflight import engine_alive
from .startup import BINANCE_SECRET_KEYS, LiveNotAvailable, check_m2_startup

SUBCOMMANDS = ("initial-baseline", "reset-confirm")
# PAPER 잔고 조회 자리: (api_key, secret, symbols, account, now_ms) → query_balances(account)를 가진 객체 또는 거부 사유
BalanceSource = Callable[..., Any]


class _Balances:
    """BACKTEST(fake 연습)의 사람이 적은 가상 잔고 — 조회 계약 모양만."""

    def __init__(self, quote_asset: str, amount: Decimal) -> None:
        self._b = {quote_asset: (amount, Decimal("0"))}

    def query_balances(self, account: str) -> dict[str, tuple[Decimal, Decimal]]:
        return dict(self._b)


def _refuse(msg: str) -> int:
    print(f"거부(아무것도 쓰지 않음): {msg}")
    return 2


def m2_baseline_main(argv: Sequence[str], root: Path, *, open_balances: BalanceSource | None = None,
                     clock: Clock | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/reconcile-resolve.py (M2)")
    p.add_argument("command", choices=SUBCOMMANDS)
    p.add_argument("--mode", required=True)
    p.add_argument("--market", required=True, help="M2만")
    p.add_argument("--root", default=str(root))
    p.add_argument("--operator", default=None, help="확정한 사람(기본: 로그인 사용자)")
    p.add_argument("--quote-amount", help="BACKTEST(fake 연습)만: USDT 가상 잔고 (사람이 적음)")
    ns = p.parse_args(list(argv))
    clock = clock or SystemClock()
    root = Path(ns.root)
    print(f"시장: {ns.market} · 디렉터리: {root.resolve()}")
    if ns.market != Market.M2.value:
        return _refuse("initial-baseline·reset-confirm은 M2 전용 (--market M2)")
    try:
        mode = parse_trade_mode(ns.mode)
    except ConfigError as exc:
        return _refuse(str(exc))
    if mode is Mode.LIVE:
        return _refuse("LIVE는 이 도구에서 다루지 않는다 (실전 경로 없음)")
    base = root / "var" / mode.value.lower()
    if instance_running(base):
        return _refuse("엔진 실행 중(실행 잠금 보유) — 엔진을 멈춘 뒤 실행 (D-210)")
    alive = engine_alive(base / "heartbeat", clock.now())
    if alive:
        return _refuse(alive)
    try:
        env_file = None if mode is Mode.BACKTEST else root / f".env.{mode.value.lower()}"
        cfg = check_m2_startup(mode, root, base / "trading.db", env_file)
    except (LiveNotAvailable, ConfigError) as exc:
        return _refuse(str(exc))
    quote = cfg.universe.quote_asset
    pairs = {s: (cfg.universe.base_asset(s) or "", quote) for s in cfg.universe.symbols}
    if mode is Mode.BACKTEST:
        try:
            amount = Decimal(ns.quote_amount) if ns.quote_amount is not None else None
        except InvalidOperation:
            amount = None
        if amount is None or not amount.is_finite() or amount < 0:
            return _refuse("BACKTEST는 --quote-amount(USDT 가상 잔고, 0 이상 소수)가 필요하다")
        source: Any = _Balances(quote, amount)
    else:
        if ns.quote_amount is not None:
            return _refuse("--quote-amount는 BACKTEST만 — PAPER는 테스트넷 잔고를 조회한다")
        if not cfg.app.paper_connection_approved:
            return _refuse("config/m2/app.paper.toml [paper] connection_approved = true 아님 (조회도 연결이다)")
        s = cfg.secrets
        missing = [k for k in BINANCE_SECRET_KEYS if s is None or not s.has(k) or not s.get(k).reveal()]
        if missing:
            return _refuse(f".env.paper에 없거나 빈 키: {', '.join(missing)}")
        if open_balances is None:
            return _refuse("PAPER 잔고 조회 연결이 주입되지 않았다 — scripts/reconcile-resolve.py로 실행 (D-119)")
        assert s is not None
        source = open_balances(api_key=s.get(BINANCE_SECRET_KEYS[0]), secret=s.get(BINANCE_SECRET_KEYS[1]),
                               symbols=tuple(pairs), account=cfg.account,
                               now_ms=lambda: int(clock.now().timestamp() * 1000))
        if isinstance(source, str):
            return _refuse(source)
    try:
        lock = acquire_instance_lock(base)     # 쓰는 동안 엔진 시작을 막는다 (끝날 때 해제)
    except InstanceLocked:
        return _refuse("엔진 실행 중(실행 잠금 보유) — 엔진을 멈춘 뒤 실행 (D-210)")
    try:
        return _write(ns, cfg, mode, clock, source, pairs, quote)
    finally:
        lock.release()


def _write(ns: argparse.Namespace, cfg: Any, mode: Mode, clock: Clock, source: Any,  # noqa: PLR0913
           pairs: dict[str, tuple[str, str]], quote: str) -> int:
    """실행 잠금을 쥔 채로: DB(M2 메타) → 잔고 조회 → 기준선 확정 또는 리셋 확인."""
    existed = cfg.storage_path.exists()
    cfg.storage_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        repo = Repository(cfg.storage_path, mode, order_identity=OrderIdentity.SYMBOL)
    except (StorageFailure, ModeMismatch) as exc:
        return _refuse(f"저장소: {exc}")
    try:
        try:
            bind_market(repo.connection, Market.M2, cfg.broker, created=not existed)
        except ModeMismatch as exc:
            return _refuse(f"DB 메타 불일치: {exc}")
        facts = SpotAccountFacts(repo, clock, quote_asset=quote, asset_pair=pairs.get, price_of=lambda _i: None,
                                 cost_rates=CostRates(cfg.market_settings.commission_rate, Decimal("0"),
                                                      Decimal("0.00000001")),
                                 reference_time=cfg.limits.daily_reference_time, max_age=timedelta(minutes=5),
                                 market_time=M2_TIME)
        operator = ns.operator or getpass.getuser()
        try:
            balances = source.query_balances(cfg.account)
        except BrokerQueryError as exc:
            return _refuse(f"잔고 조회 실패: {exc}")
        try:
            if ns.command == "initial-baseline":
                rep = initial_baseline(repo, clock, balances, cfg.account, universe=pairs, quote_asset=quote,
                                       existing_baseline=facts.baseline(cfg.account),
                                       set_baseline=facts.set_baseline, operator=operator)
            else:
                rep = confirm_reset(repo, clock, source, cfg.account, universe=pairs, quote_asset=quote,
                                    set_baseline=facts.set_baseline, operator=operator)
        except ResetError as exc:
            return _refuse(str(exc))
        except BrokerQueryError as exc:     # 리셋 확인의 두 번째 잔고 조회 — 쓰기 전(저장 단위 밖)에 실패
            return _refuse(f"잔고 조회 실패(리셋 확인): {exc}")
    finally:
        repo.close()
    print(f"{ns.command} 완료: USDT 기준선 {rep.quote_baseline}, 유니버스 보유 {rep.positions} (수동 보유로 기록), "
          f"해소한 대조 항목 {len(rep.resolved_items)}건")
    print("K4는 풀지 않았다 — 다음 run의 대조 일치 뒤 사용자 재개 (scripts/resume.py --market M2 ... K4)")
    return 0
