"""시세 기록기 (T-023, CP-027 draft D-141·D-142·D-143) — PV-10 슬리피지·PV-15 시세 조회 주기 관찰용 진단 기록.

판정·실행 스레드의 tick 훅(Engine.on_tick)으로 돈다. 주문 경로(게이트·실행기·브로커)를 부르지 않고, 원장(의도·전송
시도·주문·체결)은 **읽기만** 한다. 하는 일:
1. 지난 tick 뒤 새로 생긴 전송 시도(신규·증액·감액·청산)와 체결을 찾아, 그 시각에 시스템이 알던 시세(메모리 시세
   이력, 받은 시각 기준)를 `quote_snapshot`에 한 줄씩 남긴다. 그 시각의 시세를 모르면 시세 칸을 비워 남긴다.
2. 시세 조회 통계 창이 끝났으면 요약을 `quote_poll_stat`에 남긴다.

실패 정책 (D-143): 진단 기록이므로 실패는 운영 로그 + 경고 알림 1회(연속 실패 중 반복 없음)만. 정지(K7·K8)·주문
차단을 일으키지 않는다. 실패하면 기준 위치를 올리지 않아 회복 뒤 밀린 사건을 기록한다.
시작 시점 이전 사건은 기록하지 않는다(메모리 이력이 비어 있어 "모름"만 쌓이므로).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from trading_system.domain.clock import Clock, from_iso
from trading_system.domain.market import Quote
from trading_system.domain.sinks import AlertSink
from trading_system.storage.oplog import log_exception
from trading_system.storage.repository import Repository

SNAPSHOT_REQUESTS = frozenset({"ENTRY", "INCREASE", "REDUCE", "EXIT"})   # 취소·정정은 가격 비교 대상 아님


class KnownQuote(Protocol):
    quote: Quote
    source: str


class MarketDataRecorder:
    def __init__(self, repo: Repository, clock: Clock, account: str,
                 quote_at: Callable[[str, datetime], KnownQuote | None],
                 tick_of: Callable[[str, Decimal], Decimal | None],
                 drain_stats: Callable[[datetime], dict | None],
                 alerts: AlertSink | None = None, log_dir_of: Callable[[], Path | None] = lambda: None) -> None:
        self.repo, self.clock, self.account = repo, clock, account
        self._quote_at, self._tick_of, self._drain_stats = quote_at, tick_of, drain_stats
        self._alerts, self._log_dir_of = alerts, log_dir_of
        self._attempt_mark = repo.last_rowid("attempt")
        self._fill_mark = repo.last_rowid("fill")
        self._pending_stats: list[dict] = []
        self.failing = False

    def on_tick(self, now: datetime) -> None:
        """예외를 밖으로 내보내지 않는다 — 엔진 훅 오류 처리(경고 알림)까지 가지 않게."""
        try:
            self._record(now)
        except Exception as exc:  # noqa: BLE001 — 진단 기록 실패는 주문 경로에 영향 없음 (D-143)
            self._failed(exc)
        else:
            self.failing = False

    def _record(self, now: datetime) -> None:
        attempts = self.repo.attempts_after(self._attempt_mark, self.account)
        fills = self.repo.fills_after(self._fill_mark, self.account)
        rows = [self._send_row(a, now) for a in attempts if a["request_type"] in SNAPSHOT_REQUESTS]
        rows += [self._fill_row(f, now) for f in fills]
        stat = self._drain_stats(now)
        if stat is not None:
            self._pending_stats.append({**stat, "recorded_at": now})
        if rows or self._pending_stats:
            with self.repo.tx():
                for r in rows:
                    self.repo.insert_quote_snapshot(r)
                for s in self._pending_stats:
                    self.repo.insert_poll_stat(s)
        self._pending_stats = []
        if attempts:
            self._attempt_mark = attempts[-1]["rid"]
        if fills:
            self._fill_mark = fills[-1]["rid"]

    def _snapshot(self, instrument: str, at: datetime, price_hint: Decimal | None) -> dict:
        known = self._quote_at(instrument, at)
        if known is None:
            tick = self._tick_of(instrument, price_hint) if price_hint is not None and price_hint > 0 else None
            return {"quote_price": None, "quote_at": None, "quote_source": None, "quote_age_seconds": None,
                    "tick": tick}
        q = known.quote
        age = Decimal(str((at - q.at).total_seconds())).quantize(Decimal("0.001"))
        return {"quote_price": q.price, "quote_at": q.at, "quote_source": known.source, "quote_age_seconds": age,
                "tick": self._tick_of(instrument, q.price)}

    def _send_row(self, a: dict, now: datetime) -> dict:
        at = from_iso(a["created_at"])
        price = Decimal(a["price"]) if a["price"] is not None else None
        return {"event": "SEND", "ref": a["attempt_id"], "account": self.account, "instrument": a["instrument"],
                "side": a["side"], "intent_id": a["intent_id"], "event_at": at, "event_price": price,
                "event_quantity": None, "recorded_at": now, **self._snapshot(a["instrument"], at, price)}

    def _fill_row(self, f: dict, now: datetime) -> dict:
        at = from_iso(f["at"])
        price = Decimal(f["price"])
        return {"event": "FILL", "ref": f["fill_key"], "account": self.account, "instrument": f["instrument"],
                "side": f["side"], "intent_id": f["intent_id"], "event_at": at, "event_price": price,
                "event_quantity": Decimal(f["quantity"]), "recorded_at": now,
                **self._snapshot(f["instrument"], at, price)}

    def _failed(self, exc: Exception) -> None:
        log_exception(self._log_dir_of(), "monitoring.market_log", exc)
        if self.failing:
            return
        self.failing = True
        if self._alerts is not None:
            try:
                self._alerts.alert("WARNING", f"시세 기록 실패 {type(exc).__name__} — 진단 기록만 빠짐(주문·정지 영향 없음), "
                                              "회복하면 밀린 사건을 기록", None)
            except Exception:  # noqa: BLE001 — 알림 실패도 삼킨다 (운영 로그에는 이미 남김)
                pass
