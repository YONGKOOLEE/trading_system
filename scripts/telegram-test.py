"""로컬 운영 도구: 텔레그램 연결 시험(메시지 1건). 사람이 직접 실행한다. 본체는 trading_system.app.telegram_tool.

사용 예: .venv/Scripts/python scripts/telegram-test.py --mode PAPER
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.telegram_tool import telegram_test_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(telegram_test_main())
