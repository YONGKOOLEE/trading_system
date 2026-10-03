"""로컬 운영 도구: PAPER 실행 기록 집계 (T-017 준비). 사람이 직접 실행한다. 읽기 전용. 본체는 trading_system.app.review_tool.

사용 예: .venv/Scripts/python scripts/paper-review.py --mode PAPER
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.review_tool import review_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(review_main())
