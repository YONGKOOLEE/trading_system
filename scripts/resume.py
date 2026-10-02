"""로컬 운영 도구: resume (03 §6.5·§13). 사람이 직접 실행한다. 본체는 trading_system.ops.cli.

사용 예: .venv/Scripts/python scripts/resume.py --mode PAPER --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.ops.cli import resume_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(resume_main())
