"""SNTP 시계 점검 (R-24, 03 D-053·§5.3·§8.4, 01 K6, 04 V-72).

- 표준 라이브러리 소켓(UDP 123)으로 SNTP v4 요청 1개를 보내 오차를 잰다: offset = ((T2−T1) + (T3−T4)) / 2.
- 서버 2곳에 각 1회(응답 대기 2초). 둘 다 실패하면 "판정 불가" → K6. 허용 오차 1초 초과 → K6.
- 정상이면 K6 재개 조건 `clock_ok`를 보고해 자동 해제한다. 결과는 시계 점검 기록(ENT-28)에 남긴다.
- 실제 서버 연결은 사용자 승인 후에만 한다(05 T-012). 시험은 로컬 가짜 서버로만.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Callable
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


class SntpError(RuntimeError):
    pass


def _to_ntp(t: datetime) -> tuple[int, int]:
    delta = t - NTP_EPOCH
    secs = delta.days * 86400 + delta.seconds
    frac = int(delta.microseconds * (2**32) / 1_000_000)
    return secs, frac


def _from_ntp(secs: int, frac: int) -> datetime:
    return NTP_EPOCH + timedelta(seconds=secs, microseconds=frac * 1_000_000 / 2**32)


def build_request(t1: datetime) -> bytes:
    secs, frac = _to_ntp(t1)
    packet = bytearray(48)
    packet[0] = (0 << 6) | (4 << 3) | 3  # LI=0, VN=4, Mode=3(client)
    struct.pack_into("!II", packet, 40, secs, frac)  # transmit timestamp
    return bytes(packet)


def parse_response(data: bytes, t1: datetime, t4: datetime) -> Decimal:
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
    return Decimal(str(offset.total_seconds())).quantize(Decimal("0.000001"))


def query_offset(host: str, port: int = 123, timeout: float = 2.0,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Decimal:
    """서버 한 곳에 한 번 묻는다. 실패하면 SntpError (재시도는 호출자가 서버를 바꿔서)."""
    t1 = now()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(build_request(t1), (host, port))
            data, _ = s.recvfrom(512)
    except OSError as exc:
        raise SntpError(f"{host}:{port} 응답 없음: {type(exc).__name__}") from exc
    return parse_response(data, t1, now())


@dataclass(frozen=True)
class ClockResult:
    ok: bool | None          # None = 판정 불가
    offset: Decimal | None
    source: str


class ClockMonitor:
    """시계 상태의 원본. 게이트 입력(ClockHealth)을 겸한다."""

    def __init__(self, repo: Repository, clock: Clock, servers: list[tuple[str, int]], halts: HaltControl | None,
                 alerts: AlertSink | None, query: Callable[[str, int], Decimal] | None = None,
                 tolerance: Decimal = TOLERANCE_SECONDS) -> None:
        if not servers:
            raise ValueError("SNTP 서버가 필요 (서버 2곳 권장, 03 §5.3)")
        self._repo = repo
        self._clock = clock
        self._servers = servers
        self._halts = halts
        self._alerts = alerts
        self._query = query or (lambda h, p: query_offset(h, p))
        self._tolerance = tolerance
        self.last: ClockResult | None = None
        self.last_at: datetime | None = None

    def check(self) -> ClockResult:
        errors = []
        result: ClockResult | None = None
        for host, port in self._servers:
            try:
                off = self._query(host, port)
            except SntpError as exc:
                errors.append(str(exc))
                continue
            result = ClockResult(abs(off) <= self._tolerance, off, f"{host}:{port}")
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
                self._halts.report_condition(HaltType.K6, None, "clock_ok", True, f"오차 {result.offset}초")
            else:
                why = (f"시계 오차 {result.offset}초 > 허용 {self._tolerance}초" if result.ok is False
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
