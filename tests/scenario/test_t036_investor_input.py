"""T-036 CAP-DAT-05a 판정 입력·백테스트·등록 판정 (CP-033 D-187·D-189·D-193, 승인 A-100 Q-7).

모든 값·전략은 가상이다(시험용 — 실제 매매전략 아님). 네트워크 없음.
- V-258 시점 규칙: 판정일 d 입력에 일자 ≥ d 값 0건. 값은 확정 기준 시각(그 일자 다음 거래일 06:00 KST — A-100 Q-7) 이후,
  판정 시각 이전에 관측된 회차만. 판정 시각 뒤에 받은 회차·관측은 쓰지 않는다(그 시각에 몰랐던 값).
- V-259 룩어헤드 교란: 판정일 이후(당일 포함) 05a 값을 교란해도 신호 불변(V-91 확장). 누출을 심으면 검사가 잡는다.
- V-263 등록 판정: 05a + 표 "검증 중" → BACKTEST 허용·PAPER 거부, 05b(선언 불가)·05c(미지원) → 거부,
  "CAP-DAT-05" → 05a·05b·05c 모두 요구 → 거부. 지금 배포 설정(paper0-verify v2)은 바뀐 표로도 통과.
- V-264 백테스트 공급: 저장 데이터에서 일자 < d만, 제공 범위 밖 기간 거부, 창이 비면 "05a 데이터 없음" 표시, 같은 입력
  같은 지문.
- V-266 회귀: 05a 미선언 전략은 판정 스냅샷·백테스트 결과 불변.
- [DP-44 임시] 백테스트의 관측 시각 = 그 일자 다음 거래일 저녁 수집 시각(D-192 저녁 수집만) — PAPER와 같은 지연.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t009_backtest import CAL as BT_CAL, DATA, END, START, config
from tests.support.gate_fakes import scenario_limits
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, CrossStrategy, cross_spec
from trading_system.app import backtest as bt
from trading_system.app.backtest import BacktestError, perturbation_check, render_report, run_backtest
from trading_system.app.engine import investor_flow_source
from trading_system.app.investor_probe import COLLECT_COLUMNS
from trading_system.app.wiring import load_strategy
from trading_system.config.app_file import load_app_settings
from trading_system.config.capabilities import load_capabilities, parse_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import validate_params
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.investor_flow import INVESTOR_KEYS, UNIT_AMOUNT, FlowRevision, InvestorFlow
from trading_system.domain.market import InstrumentInfo, InstrumentKind
from trading_system.domain.models import Signal
from trading_system.domain.strategy import EvaluationSnapshot, RuleSpec
from trading_system.marketdata import investor_flow as mf
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.investor_flow import (
    InvestorFlowBook, book_from_flows, confirmed_flows, expected_last_day, flow_window, load_investor_dir,
    modeled_seen_at)
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.storage.repository import Repository
from trading_system.strategy.base import ParamValues, make_signal

ROOT = Path(__file__).resolve().parents[2]
CAL = KrxCalendar.load_dir(ROOT / "config" / "calendar")


def k(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def flow(day: date, frgn: int, code: str = "005930") -> InvestorFlow:
    vals = {key: Decimal(0) for key in INVESTOR_KEYS}
    vals["frgnr_invsr"], vals["ind_invsr"] = Decimal(frgn), Decimal(-frgn)
    return InvestorFlow(code, day, UNIT_AMOUNT, tuple((key, vals[key]) for key in INVESTOR_KEYS))


def rev(day: date, frgn: int, collected: datetime, *seen: datetime, n: int = 1) -> FlowRevision:
    return FlowRevision(flow(day, frgn), n, collected.astimezone(UTC),
                        tuple(s.astimezone(UTC) for s in (collected, *seen)))


# --- V-258 시점 규칙 ----------------------------------------------------------------------------------------------

def test_v258_value_confirmed_only_after_next_day_0600_observation() -> None:
    d7 = date(2026, 10, 7)
    r = rev(d7, 100, k(2026, 10, 7, 16))                            # 그날 저녁 수집만 — 확정 기준(10-08 06:00) 전
    assert confirmed_flows([r], k(2026, 10, 8, 9), CAL) == []
    r2 = rev(d7, 100, k(2026, 10, 7, 16), k(2026, 10, 8, 16))       # 다음 날 저녁 수집이 같은 값을 다시 받음
    assert confirmed_flows([r2], k(2026, 10, 8, 9), CAL) == []      # 판정 시각 뒤 관측은 모른다
    assert confirmed_flows([r2], k(2026, 10, 8, 16, 30), CAL) == [r2.flow]
    assert confirmed_flows([r2], k(2026, 10, 12, 9), CAL) == [r2.flow]


def test_v258_never_same_or_later_day_even_if_observed() -> None:
    d12 = date(2026, 10, 12)
    r = rev(d12, 5, k(2026, 10, 12, 16), k(2026, 10, 13, 7))
    assert confirmed_flows([r], k(2026, 10, 12, 17), CAL) == []
    assert confirmed_flows([r], k(2026, 10, 13, 9), CAL) == [r.flow]
    assert all(f.day < date(2026, 10, 13) for f in confirmed_flows([r], k(2026, 10, 13, 9), CAL))


def test_v258_latest_revision_as_of_decision_time() -> None:
    d7 = date(2026, 10, 7)
    r1 = rev(d7, 100, k(2026, 10, 7, 16), k(2026, 10, 8, 16))
    r2 = rev(d7, -40, k(2026, 10, 12, 16, 30), n=2)                 # 정정 — 확정 기준 뒤 수집이라 그 자체로 확정
    assert [f.value("frgnr_invsr") for f in confirmed_flows([r1, r2], k(2026, 10, 12, 16, 10), CAL)] == [100]
    assert [f.value("frgnr_invsr") for f in confirmed_flows([r1, r2], k(2026, 10, 12, 17), CAL)] == [-40]


def test_v258_unconfirmed_latest_revision_hides_day() -> None:
    d8 = date(2026, 10, 8)
    r1 = rev(d8, 100, k(2026, 10, 8, 16))                           # 미확정
    r2 = rev(d8, 90, k(2026, 10, 8, 16, 5), n=2)                    # 같은 저녁 정정 — 역시 미확정
    assert confirmed_flows([r1, r2], k(2026, 10, 9, 12), CAL) == []


def test_v258_expected_last_day_follows_evening_cadence() -> None:
    assert modeled_seen_at(date(2026, 10, 7), CAL) == k(2026, 10, 8, 15, 45).astimezone(UTC)
    assert modeled_seen_at(date(2026, 10, 8), CAL) == k(2026, 10, 12, 15, 45).astimezone(UTC)   # 10-09 휴장
    assert modeled_seen_at(date(2026, 11, 18), CAL) == k(2026, 11, 19, 16, 45).astimezone(UTC)  # 수능일 마감 + 15분
    assert expected_last_day(k(2026, 10, 12, 9), CAL) == date(2026, 10, 7)
    assert expected_last_day(k(2026, 10, 12, 16), CAL) == date(2026, 10, 8)


def test_v258_window_complete_only_when_all_expected_days_present() -> None:
    days = [date(2026, 10, 2), date(2026, 10, 6), date(2026, 10, 7)]
    revs = [rev(d, 10, k(d.year, d.month, d.day, 16), k(2026, 10, 8, 16)) for d in days]
    flows, ok = flow_window(revs, k(2026, 10, 12, 9), CAL, 3)
    assert [f.day for f in flows] == days and ok
    flows, ok = flow_window([revs[0], revs[2]], k(2026, 10, 12, 9), CAL, 3)
    assert [f.day for f in flows] == [days[0], days[2]] and not ok        # 10-06 없음(무효·누락) → 불완전
    flows, ok = flow_window(revs[:2], k(2026, 10, 12, 9), CAL, 2)
    assert not ok                                                          # 10-07 없음 = 지연


# --- 판정 스냅샷 연결 (PAPER 공급자) -----------------------------------------------------------------------------

def daily_bars(upto: date) -> list[RawDaily]:
    out, d, px = [], upto - timedelta(days=60), Decimal(70000)
    while d <= upto:
        if CAL.is_trading_day(d):
            out.append(RawDaily(d, px, px + 500, px - 500, px + 100, Decimal(1000)))
            px += 100
        d += timedelta(days=1)
    return out


def store_with(code: str = "005930") -> MarketDataStore:
    s = MarketDataStore(CAL, None)
    s.register(InstrumentInfo(code, InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
    s.update_daily(code, daily_bars(date(2026, 10, 8)))
    return s


def test_snapshot_carries_flows_and_marks_incomplete_data_not_ok() -> None:
    s = store_with()
    as_of = k(2026, 10, 12, 9)
    base = s.snapshot("ev", "M1", "A", as_of, Decimal(1), ["005930"], lambda i: None, lambda i: False)
    assert base.view("005930").data_ok and base.view("005930").investor_flows == ()   # type: ignore[union-attr]
    f = (flow(date(2026, 10, 7), 1),)
    ok = s.snapshot("ev", "M1", "A", as_of, Decimal(1), ["005930"], lambda i: None, lambda i: False,
                    investor_flows=lambda i: (f, True))
    bad = s.snapshot("ev", "M1", "A", as_of, Decimal(1), ["005930"], lambda i: None, lambda i: False,
                     investor_flows=lambda i: (f, False))
    assert ok.view("005930").investor_flows == f and ok.view("005930").data_ok    # type: ignore[union-attr]
    assert bad.view("005930").investor_flows == f and not bad.view("005930").data_ok   # type: ignore[union-attr]
    assert replace(ok.view("005930"), investor_flows=()) == base.view("005930")   # 다른 필드 불변 (V-266)


def test_engine_source_reads_store_as_of_decision(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    for d, at in ((date(2026, 10, 6), k(2026, 10, 6, 16)), (date(2026, 10, 7), k(2026, 10, 7, 16)),
                  (date(2026, 10, 8), k(2026, 10, 8, 16))):
        with repo.tx():
            repo.record_investor_flows([flow(d, 7)], "ka10060", at, "r")
    with repo.tx():   # 10-08 저녁 수집이 10-06·10-07을 다시 받음 (같은 값 → 관측만)
        repo.record_investor_flows([flow(date(2026, 10, 6), 7), flow(date(2026, 10, 7), 7)], "ka10060",
                                   k(2026, 10, 8, 16, 1), "r2")
    src = investor_flow_source(repo, CAL, k(2026, 10, 12, 9).astimezone(UTC), 2)
    flows, ok = src("005930")
    assert [f.day for f in flows] == [date(2026, 10, 6), date(2026, 10, 7)] and ok
    flows, ok = src("000660")
    assert flows == () and not ok
    repo.close()


# --- V-263 등록 판정 ----------------------------------------------------------------------------------------------

CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")


def flow_spec(*caps: str):  # type: ignore[no-untyped-def]
    base = cross_spec("t-flow")
    rules = tuple(replace(r, inputs=(*r.inputs, *(c for c in caps if c.startswith("CAP-DAT-05")))) for r in base.rules)
    return replace(base, capabilities=(*base.capabilities, *caps), rules=rules)


def test_v263_table_states() -> None:
    assert CAPS["CAP-DAT-05a"] == {"M1": "검증 중", "M2": "미지원", "M3": "미지원"}
    assert CAPS["CAP-DAT-05b"]["M1"] == "해당 없음" and CAPS["CAP-DAT-05c"]["M1"] == "미지원"
    assert CAPS["CAP-DAT-05"] == {"M1": "미지원", "M2": "미지원", "M3": "미지원"}   # 기존 묶음 행 불변


def test_v263_05a_backtest_allowed_paper_refused() -> None:
    assert validate_spec(flow_spec("CAP-DAT-05a"), CAPS, None, Mode.BACKTEST) == []
    problems = validate_spec(flow_spec("CAP-DAT-05a"), CAPS, None, Mode.PAPER)
    assert any("CAP-DAT-05a" in p and "검증 중" in p for p in problems), problems
    p0 = validate_spec(flow_spec("CAP-DAT-05a"), CAPS, None, Mode.PAPER, verification_approval="A-067")
    assert any("CAP-DAT-05a" in p for p in p0)                    # PAPER-0 예외로도 못 씀(시장가 능력만)


@pytest.mark.parametrize("cap, word", [("CAP-DAT-05b", "선언 불가"), ("CAP-DAT-05c", "미지원")])
@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER])
def test_v263_05b_05c_refused(cap: str, word: str, mode: Mode) -> None:
    problems = validate_spec(flow_spec(cap), CAPS, None, mode)
    assert any(cap in p and word in p for p in problems), problems


def test_v263_05b_refused_even_if_table_says_supported() -> None:
    caps = {**CAPS, "CAP-DAT-05b": {"M1": "지원", "M2": "지원", "M3": "지원"}}
    assert any("선언 불가" in p for p in validate_spec(flow_spec("CAP-DAT-05b"), caps, None, Mode.BACKTEST))


def test_v263_bundle_requires_all_parts() -> None:
    problems = validate_spec(flow_spec("CAP-DAT-05"), CAPS, None, Mode.BACKTEST)
    assert any("CAP-DAT-05b" in p and "(CAP-DAT-05 선언)" in p for p in problems), problems
    assert any(p.startswith("CAP-DAT-05c") and "미지원" in p for p in problems), problems
    assert not any(p.startswith("CAP-DAT-05a") for p in problems)


def test_v263_deployed_paper_config_still_validates_with_updated_table() -> None:
    app = load_app_settings(ROOT / "config" / "app.paper.toml", Mode.PAPER, ROOT)
    limits = load_limits_file(ROOT / "config" / "limits.paper.toml").limits
    assert app.strategies
    for dep in app.strategies:
        strat, _ = load_strategy(dep)
        assert strat.spec.strategy_id == "paper0-verify" and strat.spec.version == 2
        assert validate_spec(strat.spec, CAPS, limits, Mode.PAPER, verification_approval=dep.verification_approval) == []


def test_capabilities_doc_existing_rows_unchanged() -> None:
    text = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    old = parse_capabilities(text)
    for cap in ("CAP-TRG-01", "CAP-DAT-01", "CAP-DAT-05d", "CAP-ORD-01", "CAP-ORD-02", "CAP-ORD-03b", "CAP-INS-01"):
        assert old[cap]["M1"] == "검증 중", cap
    for cap in ("CAP-TRG-02", "CAP-DAT-02", "CAP-ORD-03", "CAP-ORD-03a", "CAP-INS-02"):
        assert old[cap]["M1"] == "미지원", cap


# --- V-264 백테스트 공급 ------------------------------------------------------------------------------------------

class FlowStrategy:
    """가상: 최근 N거래일 외국인 순매수 합 > 0이면 진입, 보유 중 < 0이면 청산. 받은 입력을 기록(시험용)."""

    investor_flow_days = 3

    def __init__(self) -> None:
        spec = flow_spec("CAP-DAT-05a")
        self.spec = replace(spec, universe=tuple(sorted(DATA)))
        self.seen: list[tuple[date, date | None, int, bool]] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        d = snapshot.as_of.astimezone(KST).date()
        for v in snapshot.instruments:
            last = max((f.day for f in v.investor_flows), default=None)
            self.seen.append((d, last, len(v.investor_flows), v.data_ok))
            if not v.data_ok or not v.daily_bars or not v.investor_flows:
                continue
            total = sum((f.value("frgnr_invsr") or Decimal(0) for f in v.investor_flows), Decimal(0))
            price = v.daily_bars[-1].close
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                if total < 0:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                           pos.quantity, OrderType.MARKET, price, timedelta(minutes=30)))
            elif total > 0:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       Decimal(params["qty"]), OrderType.MARKET, price, timedelta(minutes=30)))  # type: ignore[arg-type]
        return out


def pattern(code: str, d: date) -> int:
    return [300, -100, 200, -400, 150, -50, 100][(d.toordinal() + len(code)) % 7]


def book(first: date = date(2025, 11, 3), last: date = END, *, skip: frozenset[date] = frozenset()) -> InvestorFlowBook:
    flows: dict[str, list[InvestorFlow]] = {}
    for code in DATA:
        d = first
        while d <= last:
            if BT_CAL.is_trading_day(d) or (BT_CAL.is_trading_day(d) is None and d.weekday() < 5):
                if d not in skip:
                    flows.setdefault(code, []).append(flow(d, pattern(code, d), code))
            d += timedelta(days=1)
    return book_from_flows(flows, BT_CAL)


def flow_cfg(tmp_path: Path, run_id: str = "f1", **over: object):  # type: ignore[no-untyped-def]
    strat = FlowStrategy()
    caps = {**CAPS_ALL_SUPPORTED, "CAP-DAT-05a": {"M1": "검증 중", "M2": "미지원", "M3": "미지원"}}
    fields: dict[str, object] = {"investor_flows": book(), **over}
    return replace(config(tmp_path, run_id), strategy=strat, params=validate_params(strat.spec, PARAMS), caps=caps,
                   **fields)


def test_v264_backtest_supplies_only_earlier_confirmed_days(tmp_path: Path) -> None:
    cfg = flow_cfg(tmp_path)
    r = run_backtest(cfg)
    seen = cfg.strategy.seen  # type: ignore[attr-defined]
    assert seen and r.signals
    for d, last, n, ok in seen:
        assert last is None or last < d
        if ok:
            assert last == expected_last_day(datetime.combine(d, time(9, 5), tzinfo=KST), BT_CAL) and n == 3
    assert r.investor_flow is not None and r.investor_flow["no_data_evaluations"] == 0


def test_v264_same_input_same_digest(tmp_path: Path) -> None:
    a = run_backtest(flow_cfg(tmp_path, "a"))
    b = run_backtest(flow_cfg(tmp_path, "b"))
    assert a.digest == b.digest and a.metrics == b.metrics


def test_v264_declared_without_data_refused(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="CAP-DAT-05a"):
        run_backtest(flow_cfg(tmp_path, investor_flows=None))


@pytest.mark.parametrize("first, last", [(date(2026, 2, 1), END), (date(2025, 11, 3), date(2026, 3, 2))])
def test_v264_period_outside_provided_range_refused(tmp_path: Path, first: date, last: date) -> None:
    with pytest.raises(BacktestError, match="제공 범위"):
        run_backtest(flow_cfg(tmp_path, investor_flows=book(first, last)))


def test_v264_gap_marked_no_data_and_reported(tmp_path: Path) -> None:
    gap = frozenset({date(2026, 2, 3)})
    cfg = flow_cfg(tmp_path, investor_flows=book(skip=gap))
    r = run_backtest(cfg)
    stats = r.investor_flow
    assert stats is not None and stats["no_data_evaluations"] == 3          # 2-5·2-6·2-9 판정의 3일 창에 2-3이 낌
    assert stats["first_no_data"] == "2026-02-05"
    text = render_report(cfg, r, settings_hash="h", reproducible=True, lookahead_bad=[])
    assert "05a 데이터 없음" in text and "3회" in text


def test_v264_load_investor_dir_csv(tmp_path: Path) -> None:
    d = tmp_path / "inv"
    d.mkdir()
    days = [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    with (d / "005930.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(COLLECT_COLUMNS), lineterminator="\n")
        w.writeheader()
        for x in days:
            row = {c: "0" for c in COLLECT_COLUMNS}
            row.update(dt=x.strftime("%Y%m%d"), acc_trde_prica="123", frgnr_invsr="+15", ind_invsr="-15")
            w.writerow(row)
    (d / "meta-1.json").write_text("{}", encoding="utf-8")
    b = load_investor_dir(d, CAL)
    assert b.first_day == days[0] and b.last_day == days[-1] and set(b.revisions) == {"005930"}
    revs = b.revisions["005930"]
    assert [r.flow.value("frgnr_invsr") for r in revs] == [15, 15, 15]
    assert revs[0].seen_at == (modeled_seen_at(days[0], CAL),)
    flows, ok = b.window("005930", k(2026, 10, 12, 9), CAL, 2)
    assert [f.day for f in flows] == days[:2] and ok


# --- V-259 룩어헤드 교란 -----------------------------------------------------------------------------------------

def test_v259_perturbing_flows_after_decision_day_keeps_signals(tmp_path: Path) -> None:
    cfg = flow_cfg(tmp_path, end=date(2026, 2, 27))
    days = [date(2026, 2, 3), date(2026, 2, 10), date(2026, 2, 20)]
    assert perturbation_check(cfg, days) == []


def test_v259_check_catches_planted_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def leaky(revisions, as_of, calendar):  # type: ignore[no-untyped-def]
        d = as_of.astimezone(KST).date()
        return [r.flow for r in revisions if r.flow.day <= d]      # 당일 값까지 — 누출

    monkeypatch.setattr(mf, "confirmed_flows", leaky)
    cfg = flow_cfg(tmp_path, end=date(2026, 2, 27))
    assert perturbation_check(cfg, [date(2026, 2, 3), date(2026, 2, 10), date(2026, 2, 20)], stateful=False)


# --- V-266 회귀 ---------------------------------------------------------------------------------------------------

def test_v266_undeclared_strategy_backtest_identical_with_or_without_book(tmp_path: Path) -> None:
    a = run_backtest(config(tmp_path, "plain"))
    b = run_backtest(replace(config(tmp_path, "withbook"), investor_flows=book()))
    assert a.digest == b.digest and a.signals == b.signals and b.investor_flow is None


def test_v266_undeclared_strategy_never_reads_flow_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("05a 미선언 전략이 투자자 데이터를 읽음")

    monkeypatch.setattr(InvestorFlowBook, "window", boom)
    monkeypatch.setattr(Repository, "investor_flow_revisions", boom)
    run_backtest(replace(config(tmp_path, "nb"), investor_flows=book()))
    assert isinstance(CrossStrategy().spec.capabilities, tuple) and scenario_limits and RuleSpec and bt


def test_backtest_cli_investor_dir_loader(tmp_path: Path) -> None:
    from trading_system.app.main import _load_investor_dir
    from trading_system.config.errors import ConfigError
    assert _load_investor_dir(None, CAL) is None
    with pytest.raises(ConfigError, match="폴더 없음"):
        _load_investor_dir(str(tmp_path / "none"), CAL)
    (tmp_path / "empty").mkdir()
    with pytest.raises(ConfigError, match="CSV가 없음"):
        _load_investor_dir(str(tmp_path / "empty"), CAL)
