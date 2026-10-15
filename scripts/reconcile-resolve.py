"""로컬 운영 도구: reconcile-resolve (03 §6.3·§13). 사람이 직접 실행한다. 본체는 trading_system.ops.cli.

사용 예: .venv/Scripts/python scripts/reconcile-resolve.py --mode PAPER <항목ID> EXTERNAL
        .venv/Scripts/python scripts/reconcile-resolve.py --mode PAPER --account KR-PAPER-01 --run
M2 하위 명령 (CP-036 D-210, 로컬 전용·엔진 정지 중에만·조회만 — 본체 trading_system.app.m2_baseline):
        .venv/Scripts/python scripts/reconcile-resolve.py initial-baseline --mode PAPER --market M2
        .venv/Scripts/python scripts/reconcile-resolve.py reset-confirm --mode PAPER --market M2
  PAPER는 바이낸스 테스트넷 계좌 잔고를 조회한다(주문 전송 차단 전송 계층). BACKTEST(fake 연습)는 --quote-amount로 가상 USDT.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.m2_baseline import SUBCOMMANDS, m2_baseline_main  # noqa: E402
from trading_system.ops.cli import reconcile_resolve_main  # noqa: E402


def _balances(**kw):  # type: ignore[no-untyped-def]
    """PAPER 잔고 조회 연결 (조회만). 바이낸스 패키지는 이 스크립트만 import한다(app은 모름, D-119)."""
    from trading_system.broker.binance.run_start import open_balance_queries
    from trading_system.broker.rest_transport import RestTransport

    def make_transport(pre, after):  # type: ignore[no-untyped-def]
        return RestTransport(frozenset({"testnet.binance.vision"}), pre_send_error=pre, after_send_error=after)
    return open_balance_queries(make_transport, sleep=time.sleep, **kw)


if __name__ == "__main__":
    if any(a in SUBCOMMANDS for a in sys.argv[1:]):
        sys.exit(m2_baseline_main(sys.argv[1:], ROOT, open_balances=_balances))
    sys.exit(reconcile_resolve_main())
