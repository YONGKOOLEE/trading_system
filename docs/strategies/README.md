# 전략 문서

전략마다 폴더 하나: `docs/strategies/<전략ID>/`
- `strategy.md` — 등록 명세(R-17)와 규칙. 양식: [../templates/strategy-template.md](../templates/strategy-template.md) (00-strategy.md v0.3 부록 A, A-005)
- `backtest-<버전>.md` — 백테스트 보고(R-10)
- `status.md` — 시장별 상태 전환 이력(R-20). 전환마다 사용자 승인 A-ID를 적는다.

파라미터 값은 `config/strategies/`에 두고, 파라미터를 바꾸면 전략 버전을 올린다(R-19).
