"""로컬 운영 도구: fix-external-order (A-109 ④ 1회 정정). 사람이 직접 실행한다. 본체는 trading_system.app.external_fix.

run·자동 운영이 꺼져 있을 때만 동작한다(잠금이 잡혀 있으면 거부). 브로커 연결 없음.
사용 예: .venv/Scripts/python scripts/fix-external-order.py --mode PAPER --account KR-PAPER-01 <주문번호>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.external_fix import fix_external_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(fix_external_main())
