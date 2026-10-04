"""텔레그램 run 연결 (03 D-052·D-038, 01 §2.9·§2.10, R-07·R-13·R-21). 조립 지점.

- 알림 전송 스레드(telegram-notify): notify_every초마다 Notifier.pump — 감사 ALERT → 텔레그램. 연속 실패 3회 K11.
- 명령 수신 스레드(telegram-poll): CommandBot.poll_once 롱 폴링 — 확인된 명령을 ops_command 표에 넣기만 한다
  (처리는 판정·실행 스레드의 OpsProcessor). 두 스레드 모두 주문하지 않는다(D-038).
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
from trading_system.ops.telegram import POLL_SECONDS, CommandBot, Notifier, TelegramApi, TelegramError
from trading_system.storage.oplog import log_exception
from trading_system.storage.repository import Repository

from .supervisor import QueuedHalts

if TYPE_CHECKING:
    from .engine import Engine

TOKEN_KEY, CHAT_KEY = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
NOTIFY_EVERY = 2.0      # 초
ERROR_WAIT = 5.0        # 수신 실패 뒤 대기 (초)


def telegram_threads(engine: "Engine", repo: Repository, enabled: bool, secrets: SecretStore | None, clock: Clock,
                     kill_file: Path, stop: threading.Event, *, base_url: str = "https://api.telegram.org",
                     notify_every: float = NOTIFY_EVERY, poll_timeout: int = POLL_SECONDS,
                     log_dir: Path | None = None) -> list[threading.Thread]:
    """enabled면 시작 전 스레드 2개를 만들어 돌려준다(시작은 호출자). 비밀값이 없으면 시작 거부(ConfigError)."""
    if not enabled:
        return []
    check_telegram_secrets(secrets)
    assert secrets is not None
    token = secrets.get(TOKEN_KEY)
    chat = secrets.get(CHAT_KEY).reveal().strip()
    api = TelegramApi(token.reveal, base_url)
    halts = QueuedHalts(engine)

    def alert(level: str, text: str) -> None:
        engine.call_soon(lambda: engine.alerts.alert(level, text, None))

    with repo.tx():
        skipped = repo.skip_old_alerts_on_first_enable()
    if skipped:
        alert("INFO", f"텔레그램 첫 연결: 이전 알림 {skipped}건은 보내지 않음")
    notifier = Notifier(repo, api, chat, clock, halts)
    bot = CommandBot(repo, api, chat, engine.account, clock, kill_file, alert)

    def notify_loop() -> None:
        while not stop.is_set():
            _guarded(lambda: notifier.pump(), "telegram.notify", log_dir)
            stop.wait(notify_every)

    def poll_loop() -> None:
        while not stop.is_set():
            ok = _guarded(lambda: bot.poll_once(poll_timeout), "telegram.poll", log_dir)
            stop.wait(0.2 if ok else ERROR_WAIT)

    return [threading.Thread(target=notify_loop, name="telegram-notify", daemon=True),
            threading.Thread(target=poll_loop, name="telegram-poll", daemon=True)]


def check_telegram_secrets(secrets: SecretStore | None) -> None:
    """[telegram] enabled일 때 비밀값 확인 — 브로커 접속 전에 부른다. 오류 메시지에 값을 담지 않는다."""
    missing = [k for k in (TOKEN_KEY, CHAT_KEY) if secrets is None or not secrets.has(k)]
    if missing:
        raise ConfigError(f"[telegram] enabled = true인데 .env.paper에 없는 키: {', '.join(missing)}")
    assert secrets is not None
    if not secrets.get(CHAT_KEY).reveal().strip().lstrip("-").isdigit():
        raise ConfigError(f".env.paper {CHAT_KEY}: 숫자(채팅 ID)여야 함")


def _guarded(fn: Callable[[], object], name: str, log_dir: Path | None) -> bool:
    """입출력 스레드는 예외로 죽지 않는다. 전송 실패의 K11 판정은 Notifier가 한다."""
    try:
        fn()
        return True
    except TelegramError:
        return False
    except Exception as exc:  # noqa: BLE001
        if log_dir is not None:
            log_exception(log_dir, name, exc)
        return False
