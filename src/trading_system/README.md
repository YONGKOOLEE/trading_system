# src/trading_system — [제안] 모듈 구조

**6단계 전까지 코드를 두지 않는다.** 아래 구조는 CLAUDE.md §8 주문 통제 원칙에서 나온 제안이며, 3단계 설계(03-design.md)에서 확정·수정한다.

```
trading_system/
├── core/            모드(TRADE_MODE) 검증, 시계 인터페이스, 공통 타입(금액·수량·시각)
├── strategies/      신호 생성만 (순수 계산). brokers/execution import 금지 (R-02)
├── risk/            중앙 주문 통제·리스크 게이트, 예약 노출, 킬 스위치 상태 (R-04, R-07)
├── execution/       주문 실행기, 주문 의도·전송 시도 상태, 결과 불명 처리 (R-03, R-05, R-06)
├── brokers/         공통 어댑터 인터페이스 (R-16)
│   ├── fake/        테스트·PAPER 1차용 가짜 브로커
│   ├── kiwoom/      M1(국내), M3(미국)
│   └── binance/     M2(크립토 현물)
├── reconciliation/  브로커 ↔ 내부 상태 대조, 재시작 복구 (R-08)
├── data/            시장 데이터 수집·품질 검사·캘린더 (R-14, R-22, R-27)
├── ops/             운영 요청 수신·인증 (R-21)
├── monitoring/      프로세스 감시, 알림, 일일 보고 (R-13, R-18, R-26)
├── audit/           감사 기록, 마스킹 (R-11)
└── backtest/        백테스트 엔진·비용 모델 (R-10)
```

의존 방향(제안): strategies → core 만 허용. risk → core. execution → risk, brokers(인터페이스). brokers/* 는 상위 모듈을 import하지 않는다.
