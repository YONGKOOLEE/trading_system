"""SNTP 시계 점검 (R-24, 03 D-053·§5.3·§8.4, 01 K6, 04 V-72, 운영 보강 A-084).

- 표준 라이브러리 소켓(UDP 123)으로 SNTP v4 요청을 보내 오차를 잰다: offset = ((T2−T1) + (T3−T4)) / 2,
  왕복 지연 delay = (T4−T1) − (T3−T2).
- 서버당 표본 SAMPLES개(기본 3, 약 0.2초 간격)를 받아 **왕복 지연이 가장 짧은 표본**으로 판정한다(A-084 — 표본 1개로
  0.976초가 나온 사례: 지연이 큰 표본의 오차는 믿지 않는다). 허용 오차 의미는 그대로(|offset| ≤ 1초). 표본을 받다가
  실패하면 그때까지 받은 표본(1개 이상)으로 판정하고, 첫 표본부터 실패하면 다음 서버로 넘어간다.
- 왕복 지연이 음수인 표본(측정 중 시계 점프·이상 응답)은 버린다(재검토). 서버의 표본이 모두 그렇다면 그 서버는 실패와
  같게 보고 다음 서버로 넘어간다.
- 모든 서버가 실패하면 "판정 불가" → K6. 허용 오차 초과 → K6.
- 정상이면 K6 재개 조건 `clock_ok`를 보고해 자동 해제한다. 결과는 시계 점검 기록(ENT-28)에 남긴다.
- 실제 서버 연결은 사용자 승인 후에만 한다(05 T-012). 시험은 로컬 가짜 서버로만.
"""

from __future__ import annotations

import socket
import struct
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.storage.repository import Repository

NTP_EPOCH = datetime(1900, 1, 1, tzinfo=UTC)
TOLERANCE_SECONDS = Decimal("1")      # 01 R-24
CHECK_INTERVAL = timedelta(minutes=10)
SAMPLES = 3                           # A-084: 서버당 표본 수
SAMPLE_GAP_SECONDS = 0.2              # A-084: 표본 사이 간격(초)
_MICRO = Decimal("0.000001")


class SntpError(RuntimeError):
    pass


@dataclass(frozen=True)
class SntpSample:
    """표본 1개. delay는 왕복 지연(초) — 오차만 돌려주는 주입 질의(시험)에서는 None."""
    offset: Decimal
    delay: Decimal | None = None


def _to_ntp(t: datetime) -> tuple[int, int]:
    delta = t - NTP_EPOCH
    secs = delta.days * 86400 + delta.seconds
    frac = int(delta.microseconds * (2**32) / 1_000_000)
    return secs, frac


def _from_ntp(secs: int, frac: int) -> datetime:
    return NTP_EPOCH + timedelta(seconds=secs, microseconds=frac * 1_000_000 / 2**32)


def _seconds(d: timedelta) -> Decimal:
    return Decimal(str(d.total_seconds())).quantize(_MICRO)


def build_request(t1: datetime) -> bytes:
    secs, frac = _to_ntp(t1)
    packet = bytearray(48)
    packet[0] = (0 << 6) | (4 << 3) | 3  # LI=0, VN=4, Mode=3(client)
    struct.pack_into("!II", packet, 40, secs, frac)  # transmit timestamp
    return bytes(packet)


def parse_sample(data: bytes, t1: datetime, t4: datetime) -> SntpSample:
    """응답 1개 → (오차, 왕복 지연). 형식·originate 불일치는 SntpError."""
    if len(data) < 48:
        raise SntpError("응답 길이 부족")
    mode = data[0] & 0x7
    stratum = data[1]
    if mode != 4 or stratum == 0 or stratum > 15:
        raise SntpError(f"서버 응답 형식 이상 (mode={mode}, stratum={stratum})")
    orig = struct.unpack_from("!II", data, 24)
    if orig != _to_ntp(t1):
        raise SntpError("요청과 다른 응답 (originate timestamp 불일치)")
    t2 = _from_ntp(*struct.unpack_from("!II", data, 32))
    t3 = _from_ntp(*struct.unpack_from("!II", data, 40))
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return SntpSample(_seconds(offset), _seconds(delay))


def parse_response(data: bytes, t1: datetime, t4: datetime) -> Decimal:
    return parse_sample(data, t1, t4).offset


def query_sample(host: str, port: int = 123, timeout: float = 2.0,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> SntpSample:
    """서버 한 곳에 한 번 묻는다. 실패하면 SntpError (표본 반복·서버 교체는 호출자가)."""
    t1 = now()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(build_request(t1), (host, port))
            data, _ = s.recvfrom(512)
    except OSError as exc:
        raise SntpError(f"{host}:{port} 응답 없음: {type(exc).__name__}") from exc
    return parse_sample(data, t1, now())


def query_offset(host: str, port: int = 123, timeout: float = 2.0,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Decimal:
    return query_sample(host, port, timeout, now).offset


def valid_samples(samples: Sequence[SntpSample]) -> list[SntpSample]:
    """왕복 지연이 음수인 표본(시계 점프·이상 응답)을 뺀다. 지연을 모르는 표본(None)은 남긴다."""
    return [s for s in samples if s.delay is None or s.delay >= 0]


def best_sample(samples: Sequence[SntpSample]) -> SntpSample | None:
    """유효 표본 중 왕복 지연이 가장 짧은 표본 (지연을 모르는 표본은 뒤로, 같으면 먼저 받은 것).

    유효 표본이 없으면(빈 목록·모두 왕복 지연 음수) None."""
    valid = valid_samples(samples)
    if not valid:
        return None
    return min(valid, key=lambda s: (s.delay is None, s.delay if s.delay is not None else Decimal(0)))


@dataclass(frozen=True)
class ClockResult:
    ok: bool | None          # None = 판정 불가
    offset: Decimal | None
    source: str
    delay: Decimal | None = None   # 채택한 표본의 왕복 지연(초)
    samples: int = 0               # 판정에 쓴 유효 표본 수 (왕복 지연 음수 제외)

    @property
    def detail(self) -> str:
        """판정 근거(오차 제외): 채택 표본의 지연·표본 수·서버 — 기록·알림 문구용."""
        if self.ok is None:
            return f"판정 불가: {self.source}"
        delay = f"지연 {self.delay}초, " if self.delay is not None else ""
        return f"{delay}표본 {self.samples} ({self.source})"


QueryFn = Callable[[str, int], Decimal | SntpSample]


class ClockMonitor:
    """시계 상태의 원본. 게이트 입력(ClockHealth)을 겸한다."""

    def __init__(self, repo: Repository, clock: Clock, servers: list[tuple[str, int]], halts: HaltControl | None,
                 alerts: AlertSink | None, query: QueryFn | None = None,
                 tolerance: Decimal = TOLERANCE_SECONDS, *, samples: int = SAMPLES,
                 sample_gap: float = SAMPLE_GAP_SECONDS, sleep: Callable[[float], None] = _time.sleep) -> None:
        if not servers:
            raise ValueError("SNTP 서버가 필요 (서버 2곳 권장, 03 §5.3)")
        if samples < 1:
            raise ValueError("표본 수는 1 이상")
        self._repo = repo
        self._clock = clock
        self._servers = servers
        self._halts = halts
        self._alerts = alerts
        self._query: QueryFn = query or (lambda h, p: query_sample(h, p))
        self._tolerance = tolerance
        self._samples = samples
        self._gap = sample_gap
        self._sleep = sleep
        self.last: ClockResult | None = None
        self.last_at: datetime | None = None

    def _sample_server(self, host: str, port: int) -> tuple[list[SntpSample], str]:
        """서버 한 곳에서 표본을 모은다. (표본들, 마지막 오류). 실패하면 그때까지의 표본으로 멈춘다 (A-084)."""
        got: list[SntpSample] = []
        for i in range(self._samples):
            if i:
                self._sleep(self._gap)
            try:
                raw = self._query(host, port)
            except SntpError as exc:
                return got, str(exc)
            got.append(raw if isinstance(raw, SntpSample) else SntpSample(raw))
        return got, ""

    def check(self) -> ClockResult:
        errors: list[str] = []
        result: ClockResult | None = None
        for host, port in self._servers:
            got, err = self._sample_server(host, port)
            if not got:
                errors.append(err)
                continue
            best = best_sample(got)
            if best is None:   # 표본이 모두 왕복 지연 음수 — 유효 표본 없음, 이 서버는 실패와 같다
                errors.append(f"{host}:{port} 유효 표본 없음(왕복 지연 음수 {len(got)}개)" + (f", {err}" if err else ""))
                continue
            result = ClockResult(abs(best.offset) <= self._tolerance, best.offset, f"{host}:{port}", best.delay,
                                 len(valid_samples(got)))
            break
        if result is None:
            result = ClockResult(None, None, "; ".join(errors))
        now = self._clock.now()
        self.last, self.last_at = result, now
        with self._repo.tx():
            self._repo.insert_clock_check(now, result.source, result.offset,
                                          "OK" if result.ok else ("EXCEEDED" if result.ok is False else "UNKNOWN"))
        if self._halts is not None:
            if result.ok:
                self._halts.report_condition(HaltType.K6, None, "clock_ok", True,
                                             f"오차 {result.offset}초, {result.detail}")
            else:
                why = (f"시계 오차 {result.offset}초 > 허용 {self._tolerance}초 ({result.detail})" if result.ok is False
                       else f"시계 판정 불가: {result.source}")
                self._halts.activate(HaltType.K6, "monitor", why, None)
        return result

    def clock_ok(self) -> bool | None:
        """게이트 입력. 마지막 점검이 없거나 점검 주기의 2배 넘게 오래되면 모름(None)."""
        if self.last is None or self.last_at is None:
            return None
        if self._clock.now() - self.last_at > CHECK_INTERVAL * 2:
            return None
        return self.last.ok
