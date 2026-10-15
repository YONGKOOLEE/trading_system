"""M2 run (바이낸스 Spot 테스트넷 PAPER) — 사람이 직접 실행한다. 자동 실행 등록 없음 (CP-036 D-209·D-214, A-106 Q-2 (가)).

본체는 trading_system.app.main(`run --market M2`와 같은 경로). 이 스크립트는 바이낸스 연결 묶음(어댑터·사용자 스트림·시세 공급,
broker/binance/run_start.M2RunConnector)에 실제 전송(REST: broker/rest_transport, WS: broker/ws_transport)을 넣어 넘기기만
한다(app은 바이낸스 패키지를 import하지 않는다 — 의존 경계 D-119).

연결 전 조건(하나라도 어긋나면 연결 없이 거부, 종료 코드 2 — 빠진 것을 모두 출력한다):
  M2 체크아웃(C:\\AI\\trading_system\\m2-binance)에서 실행, TRADE_MODE=PAPER, .env.paper(TRADE_MODE=PAPER +
  BINANCE_TESTNET_API_KEY·BINANCE_TESTNET_SECRET_KEY), config/m2/app.paper.toml [paper] connection_approved = true
  (사용자가 켬 — T-050 연결 범위 승인 뒤), 주문 전송 스위치(D-204): orders_approved가 없거나 false면 **주문 차단 run**
  (전략 배포 시 시작 거부), true면 orders_approval = "A-숫자" 필수. 시작 일치 표(테스트넷 REST·WS API 호스트만).
시작 순서: 서버 시각 → exchangeInfo(규칙·rateLimits, 실패 시 거부) → 전략 규칙 검사 → DB → 엔진(K8) → 시작 대조 →
  시세 공급·사용자 스트림 스레드. K8 재개는 사용자만(scripts/resume.py --mode PAPER --market M2 K8 또는 텔레그램 /resume K8).

사용 (PowerShell):
    cd C:\\AI\\trading_system\\m2-binance
    $env:TRADE_MODE='PAPER'
    C:\\AI\\trading_system\\trading_system\\.venv\\Scripts\\python.exe scripts\\run-m2.py
정지: Ctrl+C 또는 var\\paper\\STOP 파일.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.main import run_main  # noqa: E402
from trading_system.broker.binance.run_start import M2RunConnector  # noqa: E402
from trading_system.broker.rest_transport import RestTransport  # noqa: E402
from trading_system.broker.ws_transport import WsTransport  # noqa: E402

REST_HOSTS = frozenset({"testnet.binance.vision"})          # 시작 일치 표(config/market_scope)의 테스트넷 REST
WS_HOSTS = frozenset({"ws-api.testnet.binance.vision"})     # 시작 일치 표의 테스트넷 WS API


def make_transport(pre: type[Exception], after: type[Exception]) -> RestTransport:
    return RestTransport(REST_HOSTS, pre_send_error=pre, after_send_error=after)


if __name__ == "__main__":
    connector = M2RunConnector(make_transport, WsTransport(WS_HOSTS).connect)
    sys.exit(run_main(ROOT, market="M2", m2_connector=connector))
