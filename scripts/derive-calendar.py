"""휴장일 달력 초안 추출: 키움 모의 일봉 조회만 (A-037, 03 D-055). 사람이 직접 실행한다.

조건은 시작 점검과 같다(TRADE_MODE=PAPER, .env.paper, connection_approved = true). 주문 API는 전송 전에 막는다.
결과: var/paper/calendar-derive/krx-<연도>.draft.toml, summary.json — 확인 후 config/calendar에 반영한다.

사용 예: $env:TRADE_MODE='PAPER'; .venv/Scripts/python scripts/derive-calendar.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.calendar_tool import derive_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(derive_main(ROOT))
