"""텔레그램 연결 시험 도구 — 사람이 실행. 시험 메시지 1건만 보낸다(브로커 연결·명령 수신·저장소 변경 없음).

사용: .venv/Scripts/python scripts/telegram-test.py --mode PAPER [--root .]
.env.<mode>의 TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID를 쓴다. 값·토큰은 출력하지 않는다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trading_system.config.envfile import parse_env_file
from trading_system.config.errors import ConfigError
from trading_system.config.modes import parse_trade_mode
from trading_system.domain.enums import Mode
from trading_system.ops.telegram import TelegramApi, TelegramError

from .telegram_run import CHAT_KEY, TOKEN_KEY, check_telegram_secrets


def telegram_test_main(argv: list[str] | None = None, *, base_url: str = "https://api.telegram.org") -> int:
    p = argparse.ArgumentParser(prog="scripts/telegram-test.py", description="텔레그램 연결 시험(메시지 1건 전송)")
    p.add_argument("--mode", required=True, help="PAPER")
    p.add_argument("--root", default=".")
    ns = p.parse_args(argv)
    try:
        mode = parse_trade_mode(ns.mode)
        if mode is not Mode.PAPER:
            raise ConfigError(f"{mode.value}는 이 도구에서 다루지 않는다 (PAPER만)")
        secrets = parse_env_file(Path(ns.root) / f".env.{mode.value.lower()}")
        check_telegram_secrets(secrets)
    except (ConfigError, OSError) as exc:
        print(f"거부: {exc if isinstance(exc, ConfigError) else type(exc).__name__}")
        return 2
    api = TelegramApi(secrets.get(TOKEN_KEY).reveal, base_url)
    try:
        api.send_message(secrets.get(CHAT_KEY).reveal().strip(), f"[정보] 연결 시험 ({mode.value}) — 이 메시지가 보이면 성공")
    except TelegramError as exc:
        print(f"실패: {exc}")
        return 1
    print("전송 성공 — 텔레그램에서 '연결 시험' 메시지를 확인하세요. 다음: config/app.paper.toml에 [telegram] enabled = true")
    return 0
