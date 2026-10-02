"""로컬 운영 도구: backup (03 §9·§13, D-089). 사람이 직접 실행한다. 본체는 trading_system.ops.cli.

사용 예: .venv/Scripts/python scripts/backup.py --mode PAPER --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.ops.cli import backup_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(backup_main())
