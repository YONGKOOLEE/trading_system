"""엔진 시나리오 공용 세계 (CP-034 T-041·T-042 시험용). fake 브로커·가상 종목·가상 일봉. 네트워크 없음.

test_t020_app.World와 같은 조립(build_engine)을 쓰되 전략·종목을 바꿀 수 있게 했다.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from tests.support.gate_fakes import scenario_limits
from tests.support.invariants import check_invariants
from trading_system.app.engine import Engine
from trading_system.app.wiring import StrategyInput, build_engine
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.market import CashView, CostRates, GateSettings, InstrumentInfo, InstrumentKind, Quote
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.storage.repository import Repository

ACCOUNT = "KR-BT-01"
CAL = KrxCalendar({2026: parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t"}, 2026)})
SETTINGS = GateSettings(60, Decimal("0.3"), CostRates(Decimal("0"), Decimal("0")))
DAY = date(2026, 10, 1)   # 목요일


def kst(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=KST).astimezone(UTC)


def daily_rows(day: date, close: str = "10000", n: int = 60) -> list[RawDaily]:
    rows, d = [], day - timedelta(days=1)
    while len(rows) < n:
        if CAL.is_trading_day(d):
            c = Decimal(close)
            rows.append(RawDaily(d, c, c + 100, c - 100, c, Decimal("1000")))
        d -= timedelta(days=1)
    return sorted(rows, key=lambda r: r.day)


class ClockOk:
    def clock_ok(self) -> bool | None:
        return True


class EngineWorld:
    def __init__(self, tmp: Path, strategies: Sequence[tuple[object, dict, tuple[time, ...]]],
                 instruments: Sequence[str], *, day: date = DAY, caps: dict | None = None,
                 behavior: FakeBehavior = FakeBehavior.FILL) -> None:
        from tests.support.sample_strategies import CAPS_ALL_SUPPORTED
        self.tmp = tmp
        self.day = day
        self.db = tmp / "trading.db"
        self.clock = FixedClock(kst(day, 8, 59))
        self.broker = FakeBroker(behavior)
        self.broker.cash[ACCOUNT] = CashView(Decimal("10000000"), Decimal("10000000"))
        self.instruments = list(instruments)
        self.strategies = list(strategies)
        self.caps = caps if caps is not None else CAPS_ALL_SUPPORTED
        self.engine = self.build()

    def build(self) -> Engine:
        self.repo = Repository(self.db, Mode.BACKTEST)
        self.store = MarketDataStore(CAL, None)
        for inst in self.instruments:
            self.store.register(InstrumentInfo(inst, InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
            self.store.update_status(inst, frozenset())
            self.store.update_daily(inst, daily_rows(self.day))
        self.quote()
        return build_engine(mode=Mode.BACKTEST, repo=self.repo, clock=self.clock, broker=self.broker,
                            store=self.store, account=ACCOUNT, account_limits=scenario_limits(),
                            gate_settings=SETTINGS, caps=self.caps, kill_file=self.tmp / "KILL",
                            clock_health=ClockOk(),
                            strategies=[StrategyInput(s, dict(p), t) for s, p, t in self.strategies],
                            alert_thresholds=(10, 5))

    def quote(self) -> None:
        for inst in self.instruments:
            self.store.update_quote(Quote(inst, Decimal("10000"), self.clock.now(), upper_limit=Decimal("10000"),
                                          lower_limit=Decimal("7000")))

    def command(self, cmd: str, args: dict, channel: str = "LOCAL") -> str:
        cid = f"{channel.lower()}-{random.random()}"
        with self.repo.tx():
            self.repo.insert_command(cid, channel, "tester", cmd, args, self.clock.now())
        return cid

    def start_and_resume(self) -> None:
        self.engine.start()
        self.command("RESUME", {"account": None, "halt_type": "K8"})
        self.engine.tick()
        assert "K8" not in self.repo.active_halts(ACCOUNT)

    def at(self, hh: int, mm: int, ss: int = 0, day: date | None = None) -> None:
        self.clock._now = kst(day or self.day, hh, mm, ss)  # noqa: SLF001
        self.quote()

    def intents(self) -> list[dict]:
        return self.repo.select("SELECT intent_id, state, request_type, instrument, quantity, idem_key, owner "
                                "FROM intent ORDER BY created_seq")

    def audits(self, kind: str) -> list[dict]:
        return [e for e in self.repo.audit_events() if e["kind"] == kind]

    def close(self) -> None:
        assert check_invariants(self.repo.connection, Mode.BACKTEST.value) == []
        self.repo.close()
