"""로컬 운영 도구: reconcile-resolve (03 §6.3·§13). 사람이 직접 실행한다. 본체는 trading_system.ops.cli.

사용 예: .venv/Scripts/python scripts/reconcile-resolve.py --mode PAPER <항목ID> EXTERNAL
        .venv/Scripts/python scripts/reconcile-resolve.py --mode PAPER --account KR-PAPER-01 --run
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.ops.cli import reconcile_resolve_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(reconcile_resolve_main())
