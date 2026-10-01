# docs/ — 계획·승인·기록

| 파일/폴더 | 역할 | 단계 | 게이트 | 현재 |
|---|---|---|---|---|
| business-definition.md | 업무정의서 (범위·R-ID 원천) | 사전 | — | v0.5 approved (A-012) |
| 00-strategy.md | 전략 정의 형식·등록 절차 | 0 | strategy:approved | v0.3 approved (A-005) |
| capabilities.md | 시장별 능력(CAP) 지원 목록 | 0~ | — | 초기 생성, 전 항목 미지원 |
| templates/strategy-template.md | 개별 전략 문서 양식 | 0~ | — | A-015 (CP-014 반영) |
| strategies/<전략ID>/ | 개별 전략 명세·백테스트·상태 기록 | 0~ | 전략별 | 없음 |
| 01-requirements.md | 요구사항·리스크 정책 | 1 | requirements:approved | v0.4 approved (A-012) |
| 02-data-model.md | 범용 논리 데이터 모델 | 2 | data-model:approved | v0.4 approved (A-019) |
| 03-design.md | 시스템 구조·기술 선택 | 3 | design:approved | v0.2 approved (A-011) |
| broker-specs/ | 브로커 API 확인 기록 (출처·확인일·버전) | 3~ | — | kiwoom-kr.md, krx-market.md (2026-10-01) |
| 04-validation.md | 검증 계획 | 4 | validation:approved | v0.2 approved (A-014) |
| 05-plan.md | 구현 계획 (T-ID) | 5 | plan:approved | v0.2 approved (A-017) |
| 07-paper-review.md | PAPER 검토·LIVE 인계 (시장별) | 7 | — | 미작성 |
| approvals.md | 사용자 승인 기록 (사용자만 기록) | 전체 | — | A-001~A-020 |
| .status | 현재 단계 보조 기록 (승인 근거 아님) | 전체 | — | — |
| worklog.md | 세션별 작업 기록 | 전체 | — | — |
| process/ | 에이전트 단계별 가이드 (계획 / 구현) | — | — | — |
| templates/ | 산출물 양식 | — | — | — |
| changes/ | 승인 문서 변경 제안 CP-xxx (규모가 클 때) | — | — | — |
| archive/ | superseded 또는 기준본 보존 | — | — | superseded: 업무정의서 v0.3·v0.4, 01 v0.3, 02 v0.2·v0.3, 전략 양식(A-005) |

- 6단계(구현)는 별도 문서가 없다. 작업 기록은 worklog.md에, 작업 정의는 05-plan.md에 둔다.
- 다중 시장 문서는 공통 본문과 시장별 부록(M1/M2/M3)으로 나눈다.
