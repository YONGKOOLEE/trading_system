r"""바이낸스 실서버 공개 시세 klines 수집 도구 (승인 A-110 ②, 연구·백테스트용). 사람이 직접 실행한다.

실서버 호스트 상수가 src/에 있으면 D-098 시험(test_tm2_08)이 실패하므로 이 도구는 scripts/ 안 단일 파일이다(src의 M2 테스트넷 전용 보장 불변).
사용: .venv\Scripts\python scripts\fetch-binance-klines.py --symbol BTCUSDT --interval 1d --since listing --out var\backtest\data\binance-public-20261017

범위: https://api.binance.com 의 공개 GET 3종만 — /api/v3/klines, /api/v3/exchangeInfo, /api/v3/time.
API 키·서명·계좌·주문 경로 없음. 헤더는 비워 보낸다(키 헤더 불가). M2 테스트넷 실행 경로(m2_preflight·run)와 코드·허용 목록이
완전히 분리되어 있다(테스트넷 전용 보장 불변). 전송 계층 broker.rest_transport.RestTransport(범용·부수효과 없음)만 재사용한다.
fallback 호스트(api1~api3)는 쓰지 않는다 — 허용 목록은 api.binance.com 하나다(바꾸려면 재승인).

전송 전 가드(PublicGuard): 메서드 GET·https·허용 호스트·기본 포트·허용 경로·경로별 허용 쿼리 키·빈 헤더가 아니면 보내지 않는다.

출력
- `<out>/<SYMBOL>-<interval>.csv`: open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,
  taker_buy_base,taker_buy_quote. 시각은 ISO-8601 UTC(밀리초, Z), 가격·수량은 API가 준 10진 문자열 그대로(float 변환 없음).
- `<out>/meta/fetch-summary-<YYYYMMDD-HHMMSS>.json`.
- 확정(닫힌) 봉만 쓴다: close_time >= 서버 시각(/api/v3/time)인 봉은 버린다.
- 파일을 temp에 만들어 끝에 rename(원자적). 기존 파일이 있으면 --skip-existing 없이는 거부(덮어쓰기 없음).
  --skip-existing: 마지막 봉 다음부터 이어 받아 temp에 복사 뒤 추가한다(중복 없음).
  중간에 멈추면(429·418·재시도 소진) 그때까지 받은 완전한 쪽까지 저장하고 종료 코드 1 — 같은 명령에 --skip-existing으로 이어 받는다.
  (프로세스가 강제 종료되면 temp는 버려지고 직전 저장본이 남는다.)

호출 정책(모두 읽기 전용 조회)
- 호출 간격 >= 0.25초, 쪽당 limit=1000. 429·418은 재시도 없이 즉시 중단(Retry-After 표시).
- 5xx·타임아웃·연결 오류만 쪽당 최대 3회 재시도(대기 1·2·4초). 그 밖의 4xx는 즉시 중단.

종료 코드: 0 정상, 1 중단(일부 저장), 2 실행 전 거부(인자·기존 파일·가드).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.broker.rest_transport import RestAfterSendError, RestPreSendError, RestReply, RestTransport

HOST = "api.binance.com"
ALLOWED_HOSTS: frozenset[str] = frozenset({HOST})
ALLOWED_PATHS: dict[str, frozenset[str]] = {
    "/api/v3/klines": frozenset({"symbol", "interval", "startTime", "endTime", "limit"}),
    "/api/v3/exchangeInfo": frozenset({"symbol"}),
    "/api/v3/time": frozenset(),
}
INTERVAL_MS = {"1m": 60_000, "1d": 86_400_000}
HEADER = ("open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,"
          "taker_buy_base,taker_buy_quote")
LIMIT = 1000
MIN_GAP_S = 0.25
RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
APPROVAL = "A-110"
_SYMBOL = re.compile(r"^[A-Z0-9]{5,20}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

HttpFn = Callable[[str, str, Mapping[str, str]], RestReply]


class GuardError(RuntimeError):
    """허용되지 않은 요청 — 보내지 않았다."""


class StopFetch(RuntimeError):
    """수집 중단(저장 가능한 부분은 저장)."""


class PublicGuard:
    """공개 시세 GET만 통과시키는 전송 전 검사. 통과한 요청만 하위 전송을 호출한다."""

    def __init__(self, inner: HttpFn) -> None:
        self._inner = inner
        self.sent = 0

    def check(self, method: str, url: str, headers: Mapping[str, str]) -> None:
        p = urlsplit(url)
        if method != "GET":
            raise GuardError(f"허용되지 않은 메서드 {method}")
        if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.port is not None or p.username or p.password:
            raise GuardError("허용되지 않은 접속 대상")
        keys = ALLOWED_PATHS.get(p.path)
        if keys is None:
            raise GuardError(f"허용되지 않은 경로 {p.path}")
        if p.fragment:
            raise GuardError("프래그먼트 불가")
        for k, _ in parse_qsl(p.query, keep_blank_values=True):
            if k not in keys:
                raise GuardError(f"허용되지 않은 쿼리 키 {k}")
        if headers:
            raise GuardError("헤더를 보내지 않는다(키·서명 헤더 불가)")

    def __call__(self, method: str, url: str, headers: Mapping[str, str]) -> RestReply:
        self.check(method, url, headers)
        self.sent += 1
        return self._inner(method, url, headers)


def iso_ms(ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso_ms(s: str) -> int:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def date_ms(d: date) -> int:
    return (datetime(d.year, d.month, d.day, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)


@dataclass
class Gap:
    after_open: int      # 직전 봉 시작(ms)
    next_open: int       # 다음 봉 시작(ms)
    missing: int

    def as_dict(self) -> dict[str, object]:
        return {"missing_from_utc": iso_ms(self.after_open), "next_bar_utc": iso_ms(self.next_open),
                "missing_bars": self.missing}


def _dec(s: object) -> str:
    if not isinstance(s, str):
        raise ValueError("10진 문자열이 아님")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("10진 변환 불가") from None
    if not d.is_finite():
        raise ValueError("유한하지 않은 값")
    return s


def row_to_csv(k: list[object]) -> tuple[int, str]:
    """API kline 배열 → (open_time_ms, CSV 한 줄). 형식이 다르면 ValueError."""
    if not isinstance(k, list) or len(k) < 11:
        raise ValueError("kline 형식 오류")
    o, c, trades = k[0], k[6], k[8]
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (o, c, trades)):
        raise ValueError("kline 정수 필드 오류")
    if o > 10**14 or c > 10**14:
        raise ValueError("시각 단위가 밀리초가 아님")
    f = [_dec(k[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
    line = ",".join([iso_ms(o), f[0], f[1], f[2], f[3], f[4], iso_ms(c), f[5], str(trades), f[6], f[7]])
    return o, line


@dataclass
class FetchState:
    rows_added: int = 0
    first_open: int | None = None
    last_open: int | None = None
    gaps: list[Gap] = field(default_factory=list)
    gap_missing: int = 0
    duplicates_skipped: int = 0
    dropped_open_bar: int = 0
    calls: int = 0
    stop_reason: str | None = None


class Fetcher:
    def __init__(self, http: HttpFn, *, sleep: Callable[[float], None] = time.sleep,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self._http = http
        self._sleep = sleep
        self._mono = mono
        self._last = -1e9
        self.calls = 0

    def get_json(self, path: str, params: Mapping[str, object]) -> object:
        url = f"https://{HOST}{path}"
        if params:
            url += "?" + urlencode(params)
        for attempt in range(RETRIES + 1):
            wait = MIN_GAP_S - (self._mono() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._mono()
            self.calls += 1
            try:
                r = self._http("GET", url, {})
            except GuardError:
                raise
            except (RestPreSendError, RestAfterSendError) as exc:
                err = f"{type(exc).__name__}: {exc}"
            else:
                if r.status == 200:
                    try:
                        return json.loads(r.text)
                    except ValueError:
                        raise StopFetch(f"{path}: 응답이 JSON이 아님") from None
                if r.status in (418, 429):
                    ra = next((v for k, v in r.headers.items() if k.lower() == "retry-after"), "미제공")
                    raise StopFetch(f"{path}: HTTP {r.status} 호출 제한 — 재시도 없이 중단 (Retry-After={ra}초)")
                if r.status < 500:
                    raise StopFetch(f"{path}: HTTP {r.status} — 중단")
                err = f"HTTP {r.status}"
            if attempt >= RETRIES:
                raise StopFetch(f"{path}: 재시도 {RETRIES}회 소진 ({err})")
            self._sleep(BACKOFF_S[attempt])
        raise StopFetch("도달 불가")  # pragma: no cover


def read_existing(path: Path, interval_ms: int) -> tuple[int, int]:
    """기존 CSV의 (첫 봉, 마지막 봉) open_time(ms). 형식이 다르면 ValueError."""
    with path.open("rb") as f:
        head = f.readline().decode("utf-8").strip()
        if head != HEADER:
            raise ValueError("헤더가 이 도구 형식이 아님")
        first_line = f.readline().decode("utf-8").strip()
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8").splitlines()
    last_line = tail[-1].strip() if tail else ""
    if not first_line or not last_line or last_line == HEADER:
        raise ValueError("데이터 행이 없음")
    try:
        return parse_iso_ms(first_line.split(",")[0]), parse_iso_ms(last_line.split(",")[0])
    except ValueError:
        raise ValueError("마지막 행을 읽을 수 없음") from None


def collect(fetcher: Fetcher, symbol: str, interval: str, start_ms: int, end_ms: int | None, server_now_ms: int,
            prev_open: int | None, write: Callable[[str], None]) -> FetchState:
    st = FetchState(last_open=prev_open)
    step = INTERVAL_MS[interval]
    cursor = start_ms
    try:
        while True:
            params: dict[str, object] = {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": LIMIT}
            if end_ms is not None:
                params["endTime"] = end_ms
            data = fetcher.get_json("/api/v3/klines", params)
            if not isinstance(data, list):
                raise StopFetch("klines 응답 형식 오류")
            if not data:
                break
            lines: list[str] = []
            page_last_close = None
            try:
                parsed = [(row_to_csv(k), k[6]) for k in data]
            except ValueError as exc:
                raise StopFetch(f"klines 행 검증 실패: {exc}") from None
            for (o, line), close in parsed:
                page_last_close = close
                if close >= server_now_ms:
                    st.dropped_open_bar += 1
                    continue
                if st.last_open is not None and o <= st.last_open:
                    st.duplicates_skipped += 1
                    continue
                if st.last_open is not None and o - st.last_open != step:
                    miss = (o - st.last_open) // step - 1
                    st.gap_missing += miss
                    st.gaps.append(Gap(st.last_open + step, o, miss))
                lines.append(line)
                if st.first_open is None:
                    st.first_open = o
                st.last_open = o
            if lines:
                write("\n".join(lines) + "\n")
                st.rows_added += len(lines)
            if len(data) < LIMIT or page_last_close is None or page_last_close >= server_now_ms:
                break
            nxt = page_last_close + 1
            if nxt <= cursor:
                raise StopFetch("페이지가 전진하지 않음")
            cursor = nxt
    except StopFetch as exc:
        st.stop_reason = str(exc)
    st.calls = fetcher.calls
    return st


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="fetch-binance-klines", description="바이낸스 실서버 공개 klines 수집 (A-110)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", required=True, choices=sorted(INTERVAL_MS))
    ap.add_argument("--since", required=True, help="YYYY-MM-DD 또는 listing")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD (포함)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-existing", action="store_true")
    return ap.parse_args(argv)


def _default_http() -> HttpFn:
    return RestTransport(ALLOWED_HOSTS).send


def klines_main(argv: list[str], *, http: HttpFn | None = None,
                sleep: Callable[[float], None] = time.sleep,
                mono: Callable[[], float] = time.monotonic,
                out: Callable[[str], None] = print) -> int:
    try:
        a = _parse_args(argv)
    except SystemExit:
        return 2
    if not _SYMBOL.match(a.symbol):
        out("거부: --symbol 형식 오류 (대문자·숫자 5~20자)")
        return 2
    try:
        since_ms = 0 if a.since == "listing" else date_ms(date.fromisoformat(a.since))
        end_ms = None if a.until is None else date_ms(date.fromisoformat(a.until)) + 86_400_000 - 1
    except ValueError:
        out("거부: 날짜는 YYYY-MM-DD (--since는 listing 가능)")
        return 2
    if end_ms is not None and end_ms < since_ms:
        out("거부: --until이 --since보다 앞")
        return 2
    step = INTERVAL_MS[a.interval]
    folder = Path(a.out)
    target = folder / f"{a.symbol}-{a.interval}.csv"
    prev_open: int | None = None
    first_existing: int | None = None
    if target.exists():
        if not a.skip_existing:
            out(f"거부: {target} 이미 있음 — 덮어쓰지 않는다 (이어 받기는 --skip-existing)")
            return 2
        try:
            first_existing, prev_open = read_existing(target, step)
        except (ValueError, OSError) as exc:
            out(f"거부: 기존 파일을 이어 받을 수 없음 ({exc}) — 파일은 그대로")
            return 2
    guard = PublicGuard(http if http is not None else _default_http())
    fetcher = Fetcher(guard, sleep=sleep, mono=mono)
    try:
        server = fetcher.get_json("/api/v3/time", {})
        if not isinstance(server, dict) or not isinstance(server.get("serverTime"), int):
            raise StopFetch("서버 시각 응답 형식 오류")
        server_now = int(server["serverTime"])
        info = fetcher.get_json("/api/v3/exchangeInfo", {"symbol": a.symbol})
        sym = info["symbols"][0] if isinstance(info, dict) and info.get("symbols") else None
        if not isinstance(sym, dict) or sym.get("symbol") != a.symbol:
            raise StopFetch("exchangeInfo에 심볼이 없음")
    except GuardError as exc:
        out(f"거부(가드): {exc}")
        return 2
    except StopFetch as exc:
        out(f"중단: {exc}")
        return 1
    status = str(sym.get("status"))
    start = since_ms if prev_open is None else max(since_ms, prev_open + step)
    out(f"수집 시작: {a.symbol} {a.interval} {'listing' if a.since == 'listing' else a.since}"
        f"{'~' + a.until if a.until else '~최신'} 호스트 {HOST} (A-110, 공개 GET만) → {target}")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        if prev_open is not None:
            tmp.write_bytes(target.read_bytes())
        else:
            tmp.write_text(HEADER + "\n", encoding="utf-8", newline="\n")
        with tmp.open("a", encoding="utf-8", newline="\n") as f:
            try:
                st = collect(fetcher, a.symbol, a.interval, start, end_ms, server_now, prev_open, f.write)
            except GuardError as exc:
                out(f"거부(가드): {exc}")
                st = FetchState(stop_reason=f"가드 거부: {exc}", calls=fetcher.calls)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    first = first_existing if first_existing is not None else st.first_open
    last = st.last_open
    rows_total = _count_rows(target)
    meta_dir = folder / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    ts = (_EPOCH + timedelta(milliseconds=server_now)).strftime("%Y%m%d-%H%M%S")
    summary = {
        "approval": APPROVAL, "symbol": a.symbol, "interval": a.interval, "source_host": HOST,
        "endpoints": sorted(ALLOWED_PATHS), "symbol_status": status,
        "first_bar_utc": iso_ms(first) if first is not None else None,
        "last_bar_utc": iso_ms(last) if last is not None else None,
        "rows_total": rows_total, "rows_added": st.rows_added, "resumed": prev_open is not None,
        "gaps_detected": len(st.gaps), "gap_missing_bars": st.gap_missing,
        "first_gaps": [g.as_dict() for g in st.gaps[:5]],
        "open_bar_dropped": st.dropped_open_bar, "duplicates_skipped": st.duplicates_skipped,
        "calls": st.calls, "stopped": st.stop_reason, "server_time_utc": iso_ms(server_now),
        "output_file": target.name,
    }
    sp = meta_dir / f"fetch-summary-{ts}.json"
    sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out(f"요약: 추가 {st.rows_added}행 (총 {rows_total}), {summary['first_bar_utc']} ~ {summary['last_bar_utc']}, "
        f"공백 {len(st.gaps)}건/{st.gap_missing}봉, 호출 {st.calls}회, 메타 {sp.name}")
    if st.stop_reason:
        out(f"중단됨: {st.stop_reason} — 받은 부분까지 저장함. 같은 명령에 --skip-existing으로 이어 받기")
        return 1
    return 0


def _count_rows(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return max(0, n - 1)


if __name__ == "__main__":
    sys.exit(klines_main(sys.argv[1:]))
