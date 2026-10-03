"""시작 점검: 키움 모의 접속, 조회만 (T-015, 03 D-066). 사람이 직접 실행한다. 본체는 trading_system.app.preflight.

조건: TRADE_MODE=PAPER, .env.paper(TRADE_MODE=PAPER 포함), config/app.paper.toml [paper] connection_approved = true.
주문 API는 전송 전에 막는다. 결과는 var/paper/preflight/ 에 남는다(비밀값 없음).

사용 예: $env:TRADE_MODE='PAPER'; .venv/Scripts/python scripts/preflight.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.preflight import preflight_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(preflight_main(ROOT))
