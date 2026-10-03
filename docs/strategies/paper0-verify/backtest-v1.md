# paper0-verify v1 백테스트 기록 (R-10, 04 §2.7 v0.5 — 실행 기록, 성과 기준 아님)

- 실행일: 2026-10-03, 에이전트 (`python -m trading_system.app backtest`)
- 보고서: `var/backtest/paper0-verify-v1-cash100m/report.md` (git 제외 — 아래에 요지)
- 명령: `--strategy trading_system.strategy.paper0_verify:Paper0Verify --params config/strategies/paper0-verify-v1.toml --data var/backtest/data/kiwoom-ka10081-adj-20261002 --start 2025-01-02 --end 2026-10-02 --eval-time 09:30 --cash 100000000`
- 데이터: 키움 모의 ka10081 수정주가 005930, 2024-01-02~2026-10-02 670행(조회만, A-039), 백테스트 구간 426거래일, 품질 실패 0
- 설정: `config/limits.backtest.toml`·`market.backtest.toml` = PAPER 값과 같음(DP-30), 초기 자금 = 모의 예수금과 같은 1억 원

| 항목 | 결과 |
|---|---|
| 오류 없이 완료 | 예 |
| 재현성(D-074) | 통과 |
| 룩어헤드 교란 검사(D-073) | 통과 |
| 거래 수 | 212 (왕복) |
| 비용 반영 총수익률 | -0.115% (비용 69,949원 — 검증 전략이라 의미 없음) |
| 게이트 거부 | G12 2건 (개장 10:00인 날 09:30 판정 — 달력대로 정상) |
| 결과 지문 | `d4fbe592d7b2eb992716e4f5862b150e466dccc95990437fb7fb61c04bf642ea` |

참고: 같은 설정에 초기 자금 1천만 원(기본값)으로 먼저 실행(`paper0-verify-v1`)했을 때는 L1(0.5% = 5만 원) < 1주 금액이라 426건 모두 G7 거부, 거래 0 — 한도가 설계대로 동작. 자금을 모의 계좌와 맞춰 다시 실행했다.

판정: **백테스트 통과 조건(A-029) 충족** → 초안 → 백테스트 통과 전환은 사용자 승인 A-ID로.
