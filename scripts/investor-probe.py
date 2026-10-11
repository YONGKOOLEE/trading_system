"""키움 모의 서버 투자자별 일별 TR 실측 — 읽기 전용 (T-034, K-U42, CAP-DAT-05 Q-2). 사람이 직접 실행한다.

하는 일: ka10060(종목별 투자자·기관 일별, 금액·순매수)·ka10045(기관·외인 일별 추이)·ka10008(외국인 일별) 조회로
과거 제공 범위·1회 행 수·연속 조회 여부와, 그날 값이 언제 확정되는지(해시 비교)를 확인한다. 주문 없음.
HTTP 경계 허용 목록: au10001·au10002·ka10060·ka10045·ka10008 (--deep는 au10001·au10002·ka10060). 밖의 것은 전송 전 차단.
PAPER run·자동 운영이 꺼진 장외 시간에만 실행한다. 거래일 06:30~17:00 KST, 실행 잠금(run.lock·autopilot.lock),
하트비트 30초 이내, TRADE_MODE가 PAPER가 아님, 모의 도메인이 아님, connection_approved = true가 아님 → 연결 없이 거부.
결과: var/paper/probe/<YYYYMMDD-HHMMSS>-investor-probe.json·.md (필드 이름·행 수·일자 범위·반환 코드·해시만).
--deep만 값을 저장한다: var/backtest/data/kiwoom-investor-probe/<종목>-ka10060-amt.csv·meta-<시각>.json.

사용 (PowerShell, 저녁·주말·휴일에):
    cd C:\\AI\\trading_system\\trading_system
    $env:TRADE_MODE='PAPER'
    .venv\\Scripts\\python scripts\\investor-probe.py --mode PAPER

    선택: --codes 005930,000660,035420 (최대 3), --max-pages 30 (1~60), --back-days 365,1095,1825,3650
    확정 시각 비교: 거래일 17:10쯤 한 번, 다음 날 06:00 전(또는 휴일)에 이전 결과를 주고 다시 실행
    .venv\\Scripts\\python scripts\\investor-probe.py --mode PAPER --compare var\\paper\\probe\\<이전 시각>-investor-probe.json

    깊은 조회·값 저장 (정확히 1종목, 끝까지 연속 조회, 상한 --deep-max-pages 기본 400):
    .venv\\Scripts\\python scripts\\investor-probe.py --mode PAPER --codes 005930 --deep

기대 출력과 판정 방법은 src/trading_system/app/investor_probe.py 머리말 참고.
종료 코드: 0 = 모두 정상, 1 = 일부 실패(결과 파일의 '실패'), 2 = 실행 전 거부(연결 없음).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.investor_probe import investor_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(investor_main(ROOT, sys.argv[1:]))
