"""LF-8 표본 목록 생성 도구 (CP-038 D-244 (1)(2)(6), V-353, T-073). 가상 일봉(tmp_path)만 쓴다. 심볼 이름·값은 가상이다."""

from __future__ import annotations

import ast
import csv
import hashlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_system.app import lf8_list
from trading_system.app.lf8_list import (
    REASON_DEPLOY,
    REASON_ENDED,
    REASON_GAP,
    REASON_LEVERAGE,
    Lf8Error,
    is_leverage_token,
    lf8_main,
)

ROOT = Path(__file__).resolve().parents[2]
HEAD = "open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,taker_buy_base,taker_buy_quote\n"
D2019 = (date(2019, 1, 1), date(2019, 12, 31))


def write_sym(
    d: Path,
    sym: str,
    spans: list[tuple[date, date]],
    vol: str = "100",
    close: str = "5.0",
    skip: frozenset[date] = frozenset(),
    blank: frozenset[date] = frozenset(),
    extra: list[str] | None = None,
) -> None:
    lines = [HEAD]
    for a, b in spans:
        day = a
        while day <= b:
            if day not in skip:
                qv = "" if day in blank else vol
                lines.append(f"{day.isoformat()}T00:00:00.000Z,1,1,1,{close},1,{day.isoformat()}T23:59:59.999Z,{qv},1,1,1\n")
            day += timedelta(days=1)
    lines += extra or []
    (d / f"{sym}-1d.csv").write_text("".join(lines), encoding="utf-8", newline="")


def write_symbols(d: Path, syms: list[str]) -> None:
    (d / "meta").mkdir(exist_ok=True)
    (d / "meta" / "symbols.csv").write_text("symbol,status\n" + "".join(f"{s},TRADING\n" for s in syms), encoding="utf-8")


def write_table(path: Path, entries: dict[str, str]) -> Path:
    path.write_text("version = 1\n[exclude]\n" + "".join(f'{k} = "{v}"\n' for k, v in entries.items()), encoding="utf-8")
    return path


class Env:
    def __init__(self, tmp: Path) -> None:
        self.data = tmp / "data"
        self.data.mkdir()
        self.table = write_table(tmp / "fixed.toml", {"USDC": "스테이블", "PAXG": "자산 연동(금)"})
        self.tmp = tmp
        self.syms: list[str] = []
        self.lines: list[str] = []

    def add(self, base: str, spans: list[tuple[date, date]] = [D2019], **kw: object) -> None:
        write_sym(self.data, f"{base}USDT", spans, **kw)  # type: ignore[arg-type]
        self.syms.append(f"{base}USDT")

    def run(self, name: str = "out.csv") -> tuple[int, Path]:
        write_symbols(self.data, self.syms)
        out = self.tmp / name
        rc = lf8_main(ROOT, ["--data-dir", str(self.data), "--out", str(out), "--exclude-table", str(self.table)], self.lines.append)
        return rc, out


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def rows_of(out: Path) -> list[dict[str, str]]:
    with out.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def y(rows: list[dict[str, str]], year: int) -> list[dict[str, str]]:
    return [r for r in rows if r["year"] == str(year)]


def test_ranking_and_exclusions_then_top10(env: Env) -> None:
    # 거래대금(일 vol) 내림차순: BTC, ETH, USDC(표), ADAUP/ADADOWN(레버리지, ADA 있음), JUP(J 없음), 그 뒤 N01..N12
    env.add("BTC", vol="9000")
    env.add("ETH", vol="8000")
    env.add("USDC", vol="7000")
    env.add("ADA", vol="50")
    env.add("ADAUP", vol="6000")
    env.add("ADADOWN", vol="5900")
    env.add("JUP", vol="5800")
    for i in range(12):
        env.add(f"N{i + 1:02d}", vol=str(1000 - i * 10))
    rc, out = env.run()
    assert rc == 0
    r = y(rows_of(out), 2020)
    reasons = {x["symbol"]: x["excluded_reason"] for x in r if x["excluded_reason"]}
    assert reasons == {
        "BTCUSDT": REASON_DEPLOY, "ETHUSDT": REASON_DEPLOY, "USDCUSDT": "스테이블",
        "ADAUPUSDT": REASON_LEVERAGE, "ADADOWNUSDT": REASON_LEVERAGE,
    }
    chosen = [x["symbol"] for x in r if x["rank"]]
    assert chosen == ["JUPUSDT"] + [f"N{i + 1:02d}USDT" for i in range(9)]  # JUP은 제외되지 않고 1위
    assert [int(x["rank"]) for x in r if x["rank"]] == list(range(1, 11))
    assert [x["symbol"] for x in r][:5] == ["BTCUSDT", "ETHUSDT", "USDCUSDT", "ADAUPUSDT", "ADADOWNUSDT"]
    # ADA(50)는 10위 밖 -> 행 없음. 합계 = 일 vol x 365일, 소수 8자리
    assert [x["prev_year_quote_volume"] for x in r][0] == "3285000.00000000"
    assert all(x["year"] == "2020" for x in rows_of(out))  # 다른 해는 2018 데이터 없음 -> 후보 없음


def test_leverage_requires_underlying_in_list() -> None:
    assert is_leverage_token("ADAUP", frozenset({"ADA", "ADAUP"}))
    assert is_leverage_token("ETHBEAR", frozenset({"ETH"}))
    assert not is_leverage_token("JUP", frozenset({"JUP", "BTC"}))  # 'J' 없음
    assert not is_leverage_token("UP", frozenset({"UP"}))  # 접미사만 있는 이름
    assert not is_leverage_token("BULL", frozenset({"BULL"}))


def test_seven_day_gap_excludes_only_that_year(env: Env) -> None:
    span = [(date(2019, 1, 1), date(2020, 12, 31))]
    gap7 = frozenset(date(2019, 3, 1) + timedelta(days=i) for i in range(7))
    gap6 = frozenset(date(2019, 3, 1) + timedelta(days=i) for i in range(6))
    env.add("GAP", span, vol="300", skip=gap7)
    env.add("OK6", span, vol="200", skip=gap6)
    rc, out = env.run()
    assert rc == 0
    rows = rows_of(out)
    g = {x["year"]: x for x in rows if x["symbol"] == "GAPUSDT"}
    assert g["2020"]["excluded_reason"] == REASON_GAP and g["2020"]["rank"] == ""
    assert g["2021"]["rank"] == "1" and g["2021"]["excluded_reason"] == ""  # 다음 해는 후보
    assert {x["year"]: x["rank"] for x in rows if x["symbol"] == "OK6USDT"}["2020"] == "1"  # 6일 결측은 통과
    # 2021년은 2025-12-31 전에 데이터가 끝났으므로(2020-12-31 끝, Y-1=2020 12-31) 통과
    assert {x["year"] for x in rows} == {"2020", "2021"}


def test_rows_from_2026_not_read_or_summed(env: Env) -> None:
    span = [(date(2024, 1, 1), date(2025, 12, 31))]
    # 2026 행: 거대한 값과 해석 불가 값 — 읽혔다면 합계가 바뀌거나 오류가 난다
    bad = ["2026-01-01T00:00:00.000Z,1,1,1,notanumber,1,x,99999999999,1,1,1\n", "garbage line\n"]
    env.add("AAA", span, vol="10", extra=bad)
    rc, out = env.run()
    assert rc == 0
    r = [x for x in rows_of(out) if x["year"] == "2025"]
    assert r[0]["prev_year_quote_volume"] == "3660.00000000"  # 2024는 윤년 366일 x 10
    assert [x["year"] for x in rows_of(out)] == ["2025"]  # 2026 이후 연도는 목록 대상 아님


def test_blank_quote_volume_is_zero_and_missing_day(env: Env) -> None:
    span = [(date(2019, 1, 1), date(2020, 12, 31))]
    few = frozenset(date(2019, 5, 1) + timedelta(days=i) for i in range(3))
    many = frozenset(date(2019, 5, 1) + timedelta(days=i) for i in range(7))
    env.add("FEW", span, vol="100", blank=few)
    env.add("MANY", span, vol="100", blank=many)
    rc, out = env.run()
    assert rc == 0
    r = {x["symbol"]: x for x in y(rows_of(out), 2020)}
    assert r["FEWUSDT"]["prev_year_quote_volume"] == "36200.00000000"  # 362일 x 100
    assert r["FEWUSDT"]["rank"] == "1"
    assert r["MANYUSDT"]["excluded_reason"] == REASON_GAP  # 빈 칸 7일 연속 = 결측 7일
    assert any("빈 quote_volume 일수 10" in ln for ln in env.lines)


def test_name_reuse_gap_and_leading_gap(env: Env) -> None:
    # 이름 재사용: 2019-04-01~10-31 데이터 없음 뒤 재등장 -> 결측 214일
    reuse = [(date(2018, 6, 1), date(2019, 3, 31)), (date(2019, 11, 1), date(2019, 12, 31))]
    # 선두 공백: 이전(2018) 데이터가 있고 2019는 2월 1일부터 -> 선두 31일 결측
    lead = [(date(2018, 12, 1), date(2018, 12, 31)), (date(2019, 2, 1), date(2019, 12, 31))]
    # 신규 상장: 이전 데이터 없이 2019-02-01부터 -> 결측 아님
    newly = [(date(2019, 2, 1), date(2019, 12, 31))]
    env.add("LUNAX", reuse, vol="500")
    env.add("LEAD", lead, vol="400")
    env.add("NEWB", newly, vol="300")
    rc, out = env.run()
    assert rc == 0
    r = {x["symbol"]: x for x in y(rows_of(out), 2020)}
    assert r["LUNAXUSDT"]["excluded_reason"] == REASON_GAP
    assert r["LEADUSDT"]["excluded_reason"] == REASON_GAP
    assert r["NEWBUSDT"]["rank"] == "1"


def test_data_ended_before_prev_year_end_excluded(env: Env) -> None:
    env.add("DEAD", [(date(2019, 1, 1), date(2019, 12, 20))], vol="900")
    env.add("LIVE", [(date(2019, 1, 1), date(2019, 12, 31))], vol="100")
    rc, out = env.run()
    assert rc == 0
    r = {x["symbol"]: x for x in y(rows_of(out), 2020)}
    assert r["DEADUSDT"]["excluded_reason"] == REASON_ENDED and r["DEADUSDT"]["rank"] == ""
    assert r["LIVEUSDT"]["rank"] == "1"


def test_stable_suspect_warns_but_does_not_exclude(env: Env) -> None:
    env.add("STBL", vol="900", close="1.003")  # 표에 없음
    env.add("USDC", vol="800", close="1.0")  # 표에 있음 -> 경고 대상 아님
    env.add("COIN", vol="100", close="5.0")
    rc, out = env.run()
    assert rc == 0
    r = {x["symbol"]: x for x in y(rows_of(out), 2020)}
    assert r["STBLUSDT"]["rank"] == "1" and r["STBLUSDT"]["excluded_reason"] == ""  # 자동 제외 안 함
    warns = [ln for ln in env.lines if ln.startswith("경고:") and "스테이블 의심" in ln]
    assert len(warns) == 1 and "STBLUSDT" in warns[0] and "USDCUSDT" not in warns[0]
    assert any("스테이블 의심 기준" in ln for ln in env.lines)


def test_deterministic_csv_and_hash(env: Env) -> None:
    for i, b in enumerate(["AAA", "BBB", "CCC", "BTC", "USDC"]):
        env.add(b, vol=str(100 + i))
    rc1, o1 = env.run("a.csv")
    rc2, o2 = env.run("b.csv")
    assert rc1 == rc2 == 0
    assert o1.read_bytes() == o2.read_bytes()
    digest = hashlib.sha256(o1.read_bytes()).hexdigest()
    assert (o1.with_name("a.csv.sha256")).read_text(encoding="utf-8") == f"{digest}  a.csv\n"
    assert (o2.with_name("b.csv.sha256")).read_text(encoding="utf-8").split()[0] == digest
    assert env.lines.count(f"SHA-256: {digest}") == 2
    raw = o1.read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8").splitlines()[0] == "year,symbol,prev_year_quote_volume,rank,excluded_reason"


def test_refuses_overwrite_and_unknown_options(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.add("AAA")
    rc, out = env.run()
    assert rc == 0
    before = out.read_bytes()
    rc2, _ = env.run()  # 같은 경로 재실행 -> 거부
    assert rc2 == 2 and out.read_bytes() == before
    assert "덮어쓰지 않음" in capsys.readouterr().err
    for flag in (["--years", "2018"], ["--top", "5"], ["-n", "5"]):
        with pytest.raises(SystemExit) as ei:
            lf8_main(ROOT, ["--data-dir", str(env.data), "--out", str(env.tmp / "x.csv"), *flag])
        assert ei.value.code == 2


def test_bad_inputs_fail_closed(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.add("AAA")
    write_sym(env.data, "BBBUSDT", [D2019], extra=["2019-12-31T00:00:00.000Z,1,1,1,1,1,x,5,1,1,1\n"])  # 날짜 중복
    env.syms.append("BBBUSDT")
    rc, out = env.run()
    assert rc == 2 and not out.exists()
    assert "증가하지 않음" in capsys.readouterr().err
    bad = write_table(env.tmp / "bad.toml", {"usdc": "x"})
    with pytest.raises(Lf8Error):
        lf8_list.load_fixed_exclude(bad)
    with pytest.raises(Lf8Error):
        lf8_list.load_fixed_exclude(env.tmp / "none.toml")


def test_repo_fixed_exclude_table_is_valid() -> None:
    table = lf8_list.load_fixed_exclude(ROOT / lf8_list.DEFAULT_EXCLUDE)
    assert {"USDC", "PAXG", "WBTC"} <= set(table)
    assert not (set(table) & lf8_list.DEPLOY_BASES)


def test_module_has_no_network_imports() -> None:
    tree = ast.parse(Path(lf8_list.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    forbidden = {"socket", "ssl", "http", "urllib", "requests", "httpx", "websockets", "aiohttp", "asyncio", "subprocess", "sqlite3", "trading_system"}
    assert not (names & forbidden), names & forbidden
