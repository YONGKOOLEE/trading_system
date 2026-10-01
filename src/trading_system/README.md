# src/trading_system — 모듈 구조 (확정)

구조는 [03-design.md](../../docs/03-design.md) §13(승인 A-011), 의존 방향은 §7을 따른다. 의존 방향은 `tests/architecture/test_imports.py`(V-93)가 자동으로 검사한다. 구조·규칙 변경은 CP로 한다.

| 패키지 | 책임 | 내부 의존 허용 |
|---|---|---|
| app | 시작·모드 검사·시작 점검·배선 | 전부 |
| domain | 엔티티·열거·Decimal·시각 | 없음 (표준 라이브러리만) |
| strategy | 신호 생성 (순수 계산) | domain (표준 라이브러리 중 네트워크·저장소·시계·난수·파일 금지) |
| evaluation | 판정 트리거·스냅샷 | 규칙 미정 (app 금지) |
| gate | 중앙 게이트·예약·소유권 | domain, storage |
| execution | 실행기·실행 정책·주문 추적 | domain, broker, storage |
| broker (fake, kiwoom) | 어댑터 인터페이스·구현 | domain, broker |
| marketdata · reconciliation · halt · ops · monitoring · reporting · storage · config | 03 §2 참조 | 규칙 미정 (app 금지) |

- `broker/kiwoom`은 모의(PAPER) 전용이다. 실전 주문 코드는 승인 구조 3번 전에는 두지 않는다.
- 금액·가격·수량은 `Decimal`, 시각은 시간대가 있는 값만 쓴다 (implementation-guide).
