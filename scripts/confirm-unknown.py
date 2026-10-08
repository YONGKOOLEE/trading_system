"""로컬 운영 도구: confirm-unknown (CP-031 V-233, 03 §6.6·§13). 사람이 직접 실행한다. 본체는 trading_system.ops.cli.

결과 불명 의도(조회 5회로 해소되지 않아 K3로 정지)를 사람이 확정한다. 명령 테이블에 기록만 하고, 판정·반영은
run이 다음 tick(꺼져 있으면 다음 시작)에 한다. K3 재개는 확정 뒤 새 대조 일치 후 resume.

사용 예: .venv/Scripts/python scripts/confirm-unknown.py --mode PAPER --list
        .venv/Scripts/python scripts/confirm-unknown.py --mode PAPER <의도ID> --not-accepted
        .venv/Scripts/python scripts/confirm-unknown.py --mode PAPER <의도ID> --accepted --order-no <브로커주문번호>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.ops.cli import confirm_unknown_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(confirm_unknown_main())
