# paper0-verify 상태 전환 이력 (R-20, INV-13)

| 일자 | 시장 | 버전 | 전환 | 승인 | 근거 |
|---|---|---|---|---|---|
| 2026-10-03 | M1 | v1 | (없음) → 초안 | 등록 검증 통과(PAPER 대상, verification_approval A-037) | `scripts/strategy.py register --mode PAPER` |
| 2026-10-03 | M1 | v1 | 초안 → 백테스트 통과 | A-040 | backtest-v1.md, `scripts/strategy.py transition --mode PAPER ... BACKTEST_PASSED` |
| 2026-10-03 | M1 | v1 | 백테스트 통과 → PAPER | A-041 | `scripts/strategy.py transition --mode PAPER ... PAPER` |
| 2026-10-07 (저녁, 보유 0 확인 뒤) | M1 | v2 | (없음) → 초안 | 등록 검증 통과(PAPER 대상, verification_approval A-067) | 배포 설정 acb839b(`config/app.paper.toml` → `Paper0VerifyV2`, `paper0-verify-v2.toml`), `scripts/strategy.py register --mode PAPER` |
| 2026-10-07 | M1 | v2 | 초안 → 백테스트 통과 | A-067 | backtest-v2.md, `scripts/strategy.py transition --mode PAPER paper0-verify 2 BACKTEST_PASSED --approval A-067 --evidence docs/strategies/paper0-verify/backtest-v2.md` |
| 2026-10-07 | M1 | v2 | 백테스트 통과 → PAPER | A-067 | `scripts/strategy.py transition --mode PAPER paper0-verify 2 PAPER --approval A-067` |
| 2026-10-07 | M1 | v1 | PAPER → STOPPED (REPLACE) | A-067 | `scripts/strategy.py transition --mode PAPER paper0-verify 1 STOPPED --stop-reason REPLACE --approval A-067`. 이후 `status`에 K9(paper0-verify:1, 교체 중지) 1건이 남는 것은 정상 |

- 2026-10-07 v2 전환 4건은 사용자가 순서대로 실행했고 "3건 정상"으로 보고됨(worklog (37)). 명령 실행 시각(초 단위)은 이 문서에 기록하지 않았다 — DB `strategy_state`·`ops_command` 조회로 확인 가능(**미확인**).
- v1 보유·소유권은 v2로 넘어가지 않는다(00 D-011). 전환은 보유 0건 상태에서 했다.
- 3일차(2026-10-08)부터 v2로 실행: 09:30 매수·14:30 매도 정상(07-paper-review §12B).
