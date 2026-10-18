"""텔레그램 run 연결 (03 D-052·D-038, 01 §2.9·§2.10, R-07·R-13·R-21). 조립 지점.

- 알림 전송 스레드(telegram-notify): notify_every초마다 Notifier.pump — 감사 ALERT → 텔레그램. 연속 실패 3회 K11.
  pump 처리 예외(TelegramError가 아닌 예외) 연속 3회도 K11 (CP-028 D-146). 이 스레드가 죽으면 감시 스레드가 K11 (D-147).
- 명령 수신 스레드(telegram-poll): CommandBot.poll_once 롱 폴링 — 확인된 명령을 ops_command 표에 넣기만 한다
  (처리는 판정·실행 스레드의 OpsProcessor). 반복 예외·종료는 알림만 (CP-028 Q-2). 두 스레드 모두 주문하지 않는다(D-038).
- 정지 발동·조건 보고는 큐 경유(QueuedHalts), 알림은 call_soon으로 판정·실행 스레드에서 (RV-22).
- 처음 켤 때 그 전의 알림은 보내지 않는다(skip_old_alerts_on_first_enable). 연결 이후 밀린 알림은 보낸다(V-30).
- 토큰은 Secret으로만 다루고 오류 메시지·로그에 남기지 않는다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from trading_system.config.envfile import SecretStore
from trading_system.config.errors import ConfigError
from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType
from trading_system.domain.sinks import HaltSink
from trading_system.ops.telegram import POLL_SECONDS, CommandBot, Notifier, TelegramApi, TelegramError
from trading_system.storage.oplog import log_exception
from trading_system.storage.repository import Repository

from .supervisor import QueuedHalts

if TYPE_CHECKING:
    from .engine import Engine

TOKEN_KEY, CHAT_KEY = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
# [CP-036 D-213] M2 전용 봇 — M2 체크아웃 .env.paper의 다른 키 이름. M1 봇 키를 복사해 넣어도 M2 run이 쓰지 않는다(D-117)
M2_TOKEN_KEY, M2_CHAT_KEY = "M2_TELEGRAM_BOT_TOKEN", "M2_TELEGRAM_CHAT_ID"
NOTIFY_EVERY = 2.0      # 초
ERROR_WAIT = 5.0        # 수신 실패 뒤 대기 (초)
NOTIFY_FAULTS_TO_K11 = 3   # CP-028 D-146: 알림 전송 처리 예외 연속 3회 → K11 (01 §2.7 "최대 3회"와 같은 값)


def telegram_threads(engine: "Engine", repo: Repository, enabled: bool, secrets: SecretStore | None, clock: Clock,
                     kill_file: Path, stop: threading.Event, *, base_url: str = "https://api.telegram.org",
                     notify_every: float = NOTIFY_EVERY, poll_timeout: int = POLL_SECONDS,
                     log_dir: Path | None = None, market: str = "M1",
                     keys: tuple[str, str] = (TOKEN_KEY, CHAT_KEY)) -> list[threading.Thread]:
    """enabled면 시작 전 스레드 2개를 만들어 돌려준다(시작은 호출자). 비밀값이 없으면 시작 거부(ConfigError).
    market: 메시지 시장 표시·/exit·/reduce 수량 단위 (CP-024 D-117). 봇 토큰은 그 체크아웃의 비밀 설정에서 읽는다."""
    if not enabled:
        return []
    check_telegram_secrets(secrets, keys)
    assert secrets is not None
    token = secrets.get(keys[0])
    chat = secrets.get(keys[1]).reveal().strip()
    api = TelegramApi(token.reveal, base_url, market=market)
    halts = QueuedHalts(engine)

    def alert(level: str, text: str) -> None:
        engine.call_soon(lambda: engine.alerts.alert(level, text, None))

    with repo.tx():
        skipped = repo.skip_old_alerts_on_first_enable()
    if skipped:
        alert("INFO", f"텔레그램 첫 연결: 이전 알림 {skipped}건은 보내지 않음")
    notifier = Notifier(repo, api, chat, clock, halts, sleep=stop.wait, should_stop=stop.is_set)
    faults = NotifyFaults(halts)
    bot = CommandBot(repo, api, chat, engine.account, clock, kill_file, alert, market=market)

    def notify_loop() -> None:
        while not stop.is_set():
            _guarded(lambda: notifier.pump(), "telegram.notify", log_dir, faults)
            stop.wait(notify_every)

    def poll_loop() -> None:
        while not stop.is_set():
            ok = _guarded(lambda: bot.poll_once(poll_timeout), "telegram.poll", log_dir)
            stop.wait(0.2 if ok else ERROR_WAIT)

    return [threading.Thread(target=notify_loop, name="telegram-notify", daemon=True),
            threading.Thread(target=poll_loop, name="telegram-poll", daemon=True)]


def check_telegram_secrets(secrets: SecretStore | None, keys: tuple[str, str] = (TOKEN_KEY, CHAT_KEY)) -> None:
    """[telegram] enabled일 때 비밀값 확인 — 브로커 접속 전에 부른다. 오류 메시지에 값을 담지 않는다.
    keys: (토큰 키, 채팅 키) — M1 기본, M2는 M2_TOKEN_KEY·M2_CHAT_KEY (CP-036 D-213)."""
    token_key, chat_key = keys
    missing = [k for k in (token_key, chat_key) if secrets is None or not secrets.has(k)]
    if missing:
        raise ConfigError(f"[telegram] enabled = true인데 .env.paper에 없는 키: {', '.join(missing)}")
    assert secrets is not None
    if not secrets.get(chat_key).reveal().strip().lstrip("-").isdigit():
        raise ConfigError(f".env.paper {chat_key}: 숫자(채팅 ID)여야 함")


class NotifyFaults:
    """알림 전송 처리 예외(TelegramError가 아닌 예외) 연속 계수 (CP-028 D-146, A-074 Q-1 B).

    연속 NOTIFY_FAULTS_TO_K11회마다 K11을 발동한다(같은 범위의 미해제 K11이 있으면 정지 관리자가 그 ID를 돌려준다).
    계수는 메모리에 둔다 — 저장소가 원인일 수 있으므로. pump가 예외 없이 끝나면 0. 텔레그램 전송 오류(채널 장애)는
    Notifier가 DB 계수로 따로 센다(D-024). 해제는 기존 notify_ok 경로(알림 전송 성공 → 자동 해제).
    halts는 다른 스레드용 QueuedHalts — 발동은 판정·실행 스레드에서 (D-038).
    """

    def __init__(self, halts: HaltSink | None, threshold: int = NOTIFY_FAULTS_TO_K11) -> None:
        self._halts = halts
        self._threshold = threshold
        self.count = 0

    def ok(self) -> None:
        self.count = 0

    def failed(self, exc: BaseException) -> None:
        self.count += 1
        if self._halts is not None and self.count % self._threshold == 0:
            # 사유에는 예외 유형만 (값·토큰이 섞일 수 있는 메시지는 넣지 않는다)
            self._halts.activate(HaltType.K11, "notifier",
                                 f"알림 전송 처리 예외 연속 {self.count}회: {type(exc).__name__}", None)


def _guarded(fn: Callable[[], object], name: str, log_dir: Path | None, faults: NotifyFaults | None = None) -> bool:
    """입출력 스레드는 예외로 죽지 않는다. 전송 실패의 K11 판정은 Notifier가 한다.

    faults(알림 전송 스레드만): 처리 예외 연속 계수 → K11 (D-146). 명령 수신 스레드는 계수하지 않는다(Q-2 알림만)."""
    try:
        fn()
    except TelegramError:
        return False
    except Exception as exc:  # noqa: BLE001
        if log_dir is not None:
            log_exception(log_dir, name, exc)
        if faults is not None:
            faults.failed(exc)
        return False
    if faults is not None:
        faults.ok()
    return True
