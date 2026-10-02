"""키움 시장 데이터·거래 상태 공급자 (T-021, 03 D-057·D-095·§11, 01 §2.8, kiwoom-kr §5·§6·§7a).

게이트는 메모리 스냅샷(MarketDataStore)만 읽는다. 이 공급자가 IO 스레드에서 조회·실시간을 받아 스냅샷을 갱신한다.
모르면 갱신하지 않는다 → 시세 신선도(G11)·거래 상태 "없음"으로 위험 확대 거부.

- 일봉 ka10081(수정주가 1): 당일 봉은 그날 정규장 마감 뒤에 받은 것만 넣는다(장중의 미완성 봉을 확정 봉으로
  쓰지 않게). 해석 불가 행이 있으면 그 종목 갱신 안 함. 품질 실패 → K10(저장소가 판정).
- 시세 ka10001: 현재가·상·하한가. 시세 시각 = 응답 받은 시각(D-095). 하한가 < 상한가, 하한가 ≤ 현재가 ≤ 상한가가
  아니면 갱신 안 함(응답 예시 값이 어긋난 사례가 있어 그럴듯함 검사). 현재가 = 상/하한가면 UPPER/LOWER_LIMIT.
- 종목 상태 ka10099(코스피 0·코스닥 10·ETF 8·ETN 60): 투자유의 orderWarning 2 → DELISTING(정리매매),
  3·4·5 → CAUTION, state에 '관리' → CAUTION, '정지' → HALTED, '정리매매' → DELISTING.
  거래정지 표시 방법은 미확인(K-U6) — 문자열 포함 판정은 보수적 가정. 목록에 없는 종목은 상태를 "모름"으로 둔다.
- VI: ka10054(당일 발동 목록, 해제 시각이 지났으면 무시)와 실시간 1h. 해제 시각이 없으면 발동 후 2분(KRX 2분
  단일가, krx-market X2)까지 VI로 본다.
- 실시간 0g: 상·하한가 갱신. 0s: 장운영 215='3'(장 시작)을 받으면 달력과 대조(observe_live). 다르면 긴급 알림.
- 분봉(CAP-DAT-02)은 능력 표에서 미지원이라 만들지 않는다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom import parse
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, Clock
from trading_system.domain.market import InstrumentInfo, Quote, TradingStatus
from trading_system.domain.sinks import AlertSink

from .quality import Prepared, RawDaily
from .store import MarketDataStore

CHART_PATH, INFO_PATH = "/api/dostk/chart", "/api/dostk/stkinfo"
MASTER_MARKETS = ("0", "10", "8", "60")   # 코스피, 코스닥, ETF, ETN (ka10099 mrkt_tp)
VI_DEFAULT = timedelta(minutes=2)
LOOKBACK_DAYS = 400


class Querier(Protocol):  # KiwoomAdapter.query_raw 모양
    def query_raw(self, path: str, api_id: str, body: dict, list_key: str | None,
                  enough: Callable[[list[dict]], bool] | None = None) -> tuple[dict, list[dict]]: ...


def master_statuses(row: dict) -> frozenset[TradingStatus]:
    out: set[TradingStatus] = set()
    warn = str(row.get("orderWarning") or "0").strip()
    state = str(row.get("state") or "")
    if warn == "2" or "정리매매" in state:
        out.add(TradingStatus.DELISTING)
    if warn in ("3", "4", "5") or "관리" in state:
        out.add(TradingStatus.CAUTION)
    if "정지" in state:
        out.add(TradingStatus.HALTED)
    return frozenset(out)


@dataclass
class KiwoomMarketFeed:
    api: Querier
    store: MarketDataStore
    clock: Clock
    infos: dict[str, InstrumentInfo]
    held: Callable[[str], bool] = lambda code: False
    alerts: AlertSink | None = None
    lookback_days: int = LOOKBACK_DAYS
    _master: dict[str, frozenset[TradingStatus]] = field(default_factory=dict)
    _vi_until: dict[str, datetime] = field(default_factory=dict)
    _limits: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    _limit_status: dict[str, frozenset[TradingStatus]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    failures: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for info in self.infos.values():
            self.store.register(info)

    # --- 공통 -----------------------------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock.now()

    def _today(self) -> date:
        return self._now().astimezone(KST).date()

    def _alert(self, level: str, msg: str, ref: str | None = None) -> None:
        if self.alerts is not None:
            self.alerts.alert(level, msg, ref)

    def _fail(self, what: str, exc: Exception) -> None:
        n = self.failures.get(what, 0) + 1
        self.failures[what] = n
        if n == 3:  # 같은 대상 연속 3회 실패 시 한 번 알림 (시세 지연은 G11이 막는다)
            self._alert("WARNING", f"시장 데이터 조회 연속 실패 {what}: {exc}")

    def _ok(self, what: str) -> None:
        self.failures.pop(what, None)

    def _compose(self, code: str) -> None:
        """종목 상태 = 종목 정보 상태 ∪ VI ∪ 상·하한가. 종목 정보를 아직 못 받았으면 "모름"으로 둔다."""
        with self._lock:
            if code not in self._master:
                return
            st = set(self._master[code]) | set(self._limit_status.get(code, frozenset()))
            until = self._vi_until.get(code)
            if until is not None and self._now() < until:
                st.add(TradingStatus.VI)
            self.store.update_status(code, frozenset(st), held=self.held(code))

    # --- 일봉 -----------------------------------------------------------------------------
    def refresh_daily(self, code: str) -> Prepared | None:
        today = self._today()
        oldest = today - timedelta(days=self.lookback_days)

        def enough(rows: list[dict]) -> bool:
            return any(str(r.get("dt") or "") <= oldest.strftime("%Y%m%d") for r in rows)

        try:
            with call_priority(Priority.MARKET):
                _, rows = self.api.query_raw(CHART_PATH, "ka10081", {
                    "stk_cd": code, "base_dt": today.strftime("%Y%m%d"), "upd_stkpc_tp": "1"},
                    "stk_dt_pole_chart_qry", enough)
            raw = self._daily_rows(rows, today)
        except (BrokerQueryError, parse.KiwoomParseError) as exc:
            self._fail(f"ka10081:{code}", exc)
            return None
        self._ok(f"ka10081:{code}")
        return self.store.update_daily(code, raw, data_version=f"kiwoom-ka10081-adj@{today.isoformat()}")

    def _daily_rows(self, rows: list[dict], today: date) -> list[RawDaily]:
        session = self.store.session(today)
        closed_today = session is not None and session.is_open and session.regular_close is not None and \
            self._now() >= session.regular_close
        out: dict[date, RawDaily] = {}
        for r in rows:
            dt = str(r.get("dt") or "")
            if len(dt) != 8 or not (dt.isascii() and dt.isdigit()):
                raise parse.KiwoomParseError("dt 형식 아님")
            d = date(int(dt[:4]), int(dt[4:6]), int(dt[6:]))
            if d > today or (d == today and not closed_today):
                continue  # 미완성 당일 봉
            if d in out:
                raise parse.KiwoomParseError(f"일봉 날짜 중복 {d}")
            out[d] = RawDaily(d, parse.price(r.get("open_pric"), "open_pric"), parse.price(r.get("high_pric"), "high"),
                              parse.price(r.get("low_pric"), "low"), parse.price(r.get("cur_prc"), "cur_prc"),
                              parse.qty(r.get("trde_qty"), "trde_qty"))
        return [out[d] for d in sorted(out)]

    # --- 시세·상하한가 ------------------------------------------------------------------------
    def refresh_quote(self, code: str) -> Quote | None:
        try:
            with call_priority(Priority.MARKET):
                body, _ = self.api.query_raw(INFO_PATH, "ka10001", {"stk_cd": code}, None)
            price = parse.price(body.get("cur_prc"), "cur_prc")
            upper = parse.price(body.get("upl_pric"), "upl_pric")
            lower = parse.price(body.get("lst_pric"), "lst_pric")
            if not (Decimal("0") < lower < upper and lower <= price <= upper):
                raise parse.KiwoomParseError("현재가·상하한가가 그럴듯하지 않음")
        except (BrokerQueryError, parse.KiwoomParseError) as exc:
            self._fail(f"ka10001:{code}", exc)
            return None
        self._ok(f"ka10001:{code}")
        q = Quote(code, price, self._now(), upper_limit=upper, lower_limit=lower)  # 받은 시각 (D-095)
        with self._lock:
            self._limits[code] = (upper, lower)
            self._limit_status[code] = frozenset(
                {TradingStatus.UPPER_LIMIT} if price == upper else ({TradingStatus.LOWER_LIMIT} if price == lower
                                                                     else set()))
            self.store.update_quote(q)
        self._compose(code)
        return q

    # --- 종목 상태·VI --------------------------------------------------------------------------
    def refresh_master(self) -> bool:
        found: dict[str, frozenset[TradingStatus]] = {}
        try:
            with call_priority(Priority.MARKET):
                for mk in MASTER_MARKETS:
                    _, rows = self.api.query_raw(INFO_PATH, "ka10099", {"mrkt_tp": mk}, "list")
                    for r in rows:
                        code = str(r.get("code") or "").strip()
                        if code in self.infos:
                            found[code] = master_statuses(r) | found.get(code, frozenset())
        except BrokerQueryError as exc:
            self._fail("ka10099", exc)
            return False
        self._ok("ka10099")
        with self._lock:
            missing = sorted(set(self.infos) - set(found))
            self._master = found
        if missing:
            self._alert("WARNING", f"종목 정보 목록에 없음(상태 모름, 위험 확대 거부): {', '.join(missing)}")
        for code in found:
            self._compose(code)
        return True

    def refresh_vi(self) -> bool:
        body = {"mrkt_tp": "000", "bf_mkrt_tp": "1", "stk_cd": "", "motn_tp": "0", "skip_stk": "000000000",
                "trde_qty_tp": "0", "min_trde_qty": "", "max_trde_qty": "", "trde_prica_tp": "0",
                "min_trde_prica": "", "max_trde_prica": "", "motn_drc": "0", "stex_tp": "1"}
        try:
            with call_priority(Priority.MARKET):
                _, rows = self.api.query_raw(INFO_PATH, "ka10054", body, "motn_stk")
            today = self._today()
            for r in rows:
                code = parse.stock_code(r.get("stk_cd"))
                if code in self.infos:
                    self._vi(code, r.get("virelis_time"), r.get("trde_cntr_proc_time"), today)
        except (BrokerQueryError, parse.KiwoomParseError) as exc:
            self._fail("ka10054", exc)
            return False
        self._ok("ka10054")
        for code in self.infos:
            self._compose(code)
        return True

    def _vi(self, code: str, release: object, triggered: object, day: date) -> None:
        until: datetime | None = None
        if release not in (None, "") and str(release).strip("0 "):
            until = parse.hms(release, day, "VI해제시각")
        elif triggered not in (None, "") and str(triggered).strip("0 "):
            until = parse.hms(triggered, day, "VI발동시각") + VI_DEFAULT
        else:
            until = self._now() + VI_DEFAULT
        with self._lock:
            cur = self._vi_until.get(code)
            self._vi_until[code] = until if cur is None or until > cur else cur

    def tick(self) -> None:
        """VI 해제 시각 경과 반영 (감시 주기마다)."""
        for code in self.infos:
            self._compose(code)

    # --- 실시간 처리기 -------------------------------------------------------------------------
    def registrations(self) -> list[tuple[str, list[str]]]:
        codes = sorted(self.infos)
        return [("0s", []), ("1h", codes), ("0g", codes)]

    def handlers(self) -> dict[str, Callable[[str, dict], None]]:
        def h1h(item: str, values: dict) -> None:
            code = parse.stock_code(values.get("9001") or item, "9001")
            if code in self.infos:
                self._vi(code, values.get("1224"), values.get("1223"), self._today())
                self._compose(code)

        def h0g(item: str, values: dict) -> None:
            code = parse.stock_code(item, "item")
            if code not in self.infos:
                return
            upper, lower = parse.price(values.get("305"), "305"), parse.price(values.get("306"), "306")
            if not Decimal("0") < lower < upper:
                raise parse.KiwoomParseError("0g 상하한가가 그럴듯하지 않음")
            with self._lock:
                self._limits[code] = (upper, lower)
                q = self.store.quote(code)
                if q is not None:  # 가격·시각은 그대로, 한도만 갱신
                    self.store.update_quote(replace(q, upper_limit=upper, lower_limit=lower))

        def h0s(item: str, values: dict) -> None:
            if str(values.get("215") or "").strip() == "3":   # 장 시작(09:00)
                if not self.store.calendar.observe_live(self._today(), True):
                    self._alert("EMERGENCY", "장 시작 통보가 달력(휴장)과 다름 — 오늘 개장 여부 모름, 사용자 확인")

        return {"1h": h1h, "0g": h0g, "0s": h0s}


class MarketPoller:
    """시세 조회 주기 관리 (IO 스레드). 한 번 부를 때 한 종목 시세를 돌아가며 갱신, 주기마다 종목 상태·VI.

    호출 제한기 우선순위는 시세(최하)라 결과 불명 해소·대조 조회에 밀린다 (D-048).
    """

    def __init__(self, feed: KiwoomMarketFeed, master_every: int = 60) -> None:
        self.feed = feed
        self._codes = sorted(feed.infos)
        self._i = 0
        self._n = 0
        self._master_every = master_every

    def poll_once(self) -> None:
        if self._n % self._master_every == 0:
            self.feed.refresh_master()
            self.feed.refresh_vi()
        self._n += 1
        if self._codes:
            self.feed.refresh_quote(self._codes[self._i % len(self._codes)])
            self._i += 1
        self.feed.tick()

    def run(self, stop: threading.Event, interval: float) -> None:
        while not stop.is_set():
            self.poll_once()
            stop.wait(interval)
