# 작업 기록

세션마다 한 항목씩 추가한다: 날짜, 수행 작업, 변경 파일, 실행 명령과 실제 결과, 미해결 질문·차단 조건, 다음 세션 첫 행동.

## 2026-10-01 — 지침·업무정의서 개선, 폴더 구조 생성
- 수행: CLAUDE.md를 공통 규칙과 단계별 가이드(계획 0~5 / 구현 6~7)로 분리. 업무정의서 v0.4 draft 작성(v0.3 기준본은 archive에 보존). docs 체계, .gitignore, .env.example, 폴더 골격 생성.
- 변경 파일: CLAUDE.md, README.md, .gitignore, .env.example, docs/**, config/README.md, src/trading_system/README.md, tests/.gitkeep, scripts/.gitkeep
- 실행 명령: `git status`(변경 없음 확인), `python --version` → 3.14.7, `py -0` → 3.14 64-bit 1개. 애플리케이션 코드·설치·브로커 연결 없음.
- 커밋: 하지 않음(커밋 정책 미승인).
- 미해결: 업무정의서 v0.4 CP-01~CP-10 승인 여부, U-18(단계 표현 해석), approvals.md에 v0.3 반영, 상위 폴더(C:\AI\trading_system)의 빈 git 저장소 처리, 커밋 정책.
- 다음 세션 첫 행동: v0.4 승인 결과 확인 → 0단계 00-strategy.md(전략 정의 형식) 초안 착수.

## 2026-10-01 (2) — v0.4 승인 반영, 커밋
- 사용자 답변: U-18 해석 맞음, CP-01~CP-10 전부 채택, R-21~R-27 채택, origin에 커밋 지시, 상위 폴더 git 확인·불필요 시 삭제.
- 수행: 업무정의서 v0.4 → approved, v0.3 → superseded 표시. approvals.md에 A-001~A-004 전사. .status·CLAUDE.md 커밋 정책 갱신.
- 상위 폴더 확인: `C:\AI\trading_system\.git`은 origin(같은 URL)만 설정되어 있고 커밋·객체·ref 없음 → 불필요로 판단. 삭제 시도는 도구 권한(자동 모드 분류기)에 막혀 **미실행**. 사용자가 직접 삭제 필요.
- 다음 세션 첫 행동: 0단계 docs/00-strategy.md(전략 정의 형식) 초안 작성.

## 2026-10-01 (3) — Git 사용자 설정, 0단계 초안
- 수행: 저장소 로컬 Git 사용자 설정(user.name/user.email, 사용자 지시). 0단계 `docs/00-strategy.md` v0.1 draft 작성(전략 정의 형식, 고정 코어·확장 지점(능력 CAP) 방식, 등록 명세, 신호 형식, 상태 전환, 부록 A 양식). docs/README.md·.status 현재 상태 갱신(게이트는 미승인 유지).
- 대화 합의: 매매전략은 추후 결정. 설계는 전략과 무관한 고정 코어 + 확장 지점으로 하고, 맞지 않는 전략은 기능 추가(CP)로 대응.
- 변경 파일: docs/00-strategy.md(신규), docs/README.md, docs/.status, docs/worklog.md. `.git/config`(추적 안 됨)
- 실행 명령: `git config user.name/user.email` → 조회로 값 확인. `git status` → 변경 없음(작업 전). 상위 `C:\AI\trading_system\.git` 없음 확인(삭제된 상태).
- 커밋: 하지 않음(지시 없음).
- 미해결(0단계 차단): Q1 종목 다중 전략 처리, Q2 신호 표현, Q3 레버리지·인버스 ETF/ETN, Q4 규칙 필터 범위, Q5 한도 초과 시 거부/축소.
- 다음 세션 첫 행동: Q1~Q5 답변 반영 → 00-strategy.md 승인 요청.
- (추가) Q1~Q5 사용자 답변: Q1 배타 소유, Q2 행위 신호, Q3 ETF·ETN 모두 허용(레버리지·인버스 포함), Q4 목록 안에서만, Q5 거부 → 00-strategy.md v0.2 draft 반영(D-013·D-014 추가, CAP-INS-04 신설). 새 미확인: 레버리지·인버스 상품 투자자 요건과 모의 적용 여부, 배수 노출 계산(1단계).
- 다음 세션 첫 행동: 00-strategy.md v0.2 승인 여부 확인 → 승인 시 approvals.md 전사, 부록 A를 templates로 분리, 1단계 착수.
- (추가) 외부 검토 의견 10건(A-4, A-5, B 8건) 검토 → 모두 타당, v0.3 draft 반영: 소유권 게이트 승인 시 예약·체결 0주 종결 시 해제, 버전 교체 절차, D-009 위험 확대에만 적용, 위험 축소 우선순위 고정, 초안에서 백테스트 허용, 절대 상한 기한, 미체결 정책 파라미터화, 거래 이력 입력, 판정 ID(이벤트 식별자), docs/capabilities.md 지정. "CP 미정의"는 CLAUDE.md §2에 정의되어 있어 참조만 추가.
- (추가) 사용자: 미확인 항목이 나중에 확인되는 것이면 00-strategy v0.3 승인 → 모든 미확인의 필요 시점이 1단계 이후임을 확인, 승인 전사(A-005). 00-strategy.md approved, .status 현재 단계 1·strategy 게이트 approved. 승인 후 작업: docs/templates/strategy-template.md, docs/capabilities.md(전 항목 미지원) 생성, docs/strategies/README.md·docs/README.md 갱신.
- 다음 세션 첫 행동: 1단계 docs/01-requirements.md 착수 — U-01~U-05, U-09, U-11, 레버리지 노출 계산, 위험 축소 신호 허용 조건, 정지 정책 표(교체 중지 포함) 질문 정리.
- (추가) 1단계 착수: docs/01-requirements.md v0.1 draft 작성(요청 유형 6종, 게이트 검사 G1~G15 유형별 적용, 한도 L1~L10 빈칸, 손실률 정의, 정지 유형 K1~K10 기본안, 장애 정책, 거래 상태 정책, 운영 요청·알림, 재시작 복구, R-01~R-28 상세, D-015~D-023). 1차 질문 Q-A~Q-E 대기. 한도 값은 2차 질문.
- (추가) 1차 질문 Q-A~Q-E 모두 권장안 선택 → 01-requirements.md v0.2 draft 반영(D-024 알림 장애 시 위험 확대 차단·K11, D-025 메신저 봇 원격 수단). 권장안이 없던 K6 위험 축소 범위·원격 재개 범위는 2차 Q-H로 이월. 2차 질문 Q-F~Q-I 대기.
- (추가) 2차 답변: Q-F·Q-G는 "실제 매매 시 선택 가능하게" 요청 → D-026 한도·시간 설정값화(기본값 없음, 논리 제약, 엄격 변경 즉시·완화 변경 다음 기준 시각, 원격 변경 불가, LIVE 승인값 초과 거부) 제안 반영. Q-H 권장, Q-I 수용. 01-requirements.md v0.3 draft, 승인 대기.
- (추가) 사용자: 01-requirements v0.3 승인 → A-006 전사, 문서 approved, .status 현재 단계 2·requirements 게이트 approved.
- 다음 세션 첫 행동: 2단계 docs/02-data-model.md 착수 (엔티티 경계, 상태 전이, 멱등키, 가상 전략 2개 적용, 부분 체결·중복·응답 유실·재시작·외부 수동 거래·거래정지 검증).
- (추가) 사용자 지시로 커밋·push (A-007). Git 작성자: 저장소 로컬 설정(YONGKOOLEE).
- (추가) 2단계 착수: docs/02-data-model.md v0.1 draft 작성(ENT-01~29, 멱등키, ST-01~07, INV-01~16, 가상 전략 2개 적용, 상황별 검증 15건, D-027~D-036). 질문 Q-1~Q-3(D-029 종목·방향당 활성 의도 1개, D-028 미전송 재전송 없음, D-034 수동 소유) 대기.
- (추가) Q-1~Q-3 권장안 → 02-data-model.md v0.2 draft. 사용자가 미확인 항목 확인 방법 질의 → 확인 경로(공식 문서/모의 실측/문의/사용자 판단)와 3단계 통과 기준 제안을 답변.
- (추가) 사용자: 02-data-model v0.2 승인 → A-008 전사(A-007은 커밋 지시), 문서 approved, .status 현재 단계 3.
- 다음 세션 첫 행동: 3단계 착수 — 키움 REST API·KRX 공식 문서 확인(docs/broker-specs/kiwoom-kr.md), 3단계 통과 기준(확인됨 또는 보수적 대안+PAPER 검증 등록) 제안, 03-design.md 초안.
- (추가) 사용자 지시로 커밋·push (A-009), 이어서 3단계 착수 지시(누락 없이 수행).

## 2026-10-01 (4) — 3단계 착수: 공식 문서 확인, 설계 초안
- 수행: 키움 REST API 공식 자료(포털 API 가이드·소개, 공식 GitHub 명세 JSON 커밋 953e5db) 열람·추출, KRX 규정 페이지·영문 가이드 열람, PyPI 메타데이터(requests·websockets·pytest·hypothesis·httpx·tzdata) 조회, 텔레그램 Bot API 문서 열람. docs/broker-specs/kiwoom-kr.md, krx-market.md, docs/03-design.md v0.1 draft 작성(D-037~D-066, PV-01~12, CP-011~013 제안).
- 실행 명령: curl로 공식 GitHub 파일을 scratchpad(저장소 밖)에 내려받아 python으로 필드 추출. `python -c sqlite3` → 3.50.4, `zoneinfo.ZoneInfo('Asia/Seoul')` → 실패(시간대 데이터 없음). 설치·API 호출·로그인 없음. PDF 렌더 도구(poppler) 없음 → 표준 라이브러리로 텍스트 추출.
- 주요 발견: 고객 주문 식별자 없음, 정정·취소 시 새 주문번호, 모의 호출 제한 TR당 1초 1회, 토큰 IP 바인딩(8010), 공식 클라이언트는 인증 실패 시 요청 자동 재전송(주문 경로 사용 금지), 조회·실시간 응답에 계좌번호 포함, KRX 호가단위 출처 간 불일치.
- 라이선스: 키움 명세는 복제·배포 금지 → 저장소에 원문 미포함, 사실만 요약.
- 미해결: Q-1~Q-5(03 §16), 사용자 확인 K-U1·K-U4·X-U3.
- 다음 세션 첫 행동: Q-1~Q-5 답변 반영 → 03-design 승인 요청 → 승인 시 CP-011~013 반영(승인본 archive 후 새 버전).
- (추가) Q-1~Q-5 권장안 → 03-design.md v0.2 draft. 사용자가 Claude 계정 전환 시 세션 유지 여부 질의.
- (추가) 사용자 지시로 커밋·push (A-010). 03-design은 draft 상태로 커밋.
- (추가) 사용자: 03-design v0.2 승인 → A-011 전사, 03 approved, .status 현재 단계 4. 승인 범위에 포함된 CP 반영: 승인본을 archive에 보관(02 v0.2, 01 v0.3, 업무정의서 v0.4 — 아직 유효한 승인본으로 표기) 후 새 초안 작성(02 v0.3 CP-011, 01 v0.4 CP-013, 업무정의서 v0.5 CP-012). .gitattributes 생성(D-063).
- 다음 세션 첫 행동: CP 반영 초안 3건 승인 확인 → 4단계 04-validation.md 착수.
- (추가) 사용자: CP 반영 초안 3건 승인 → A-012 전사. 02 v0.3·01 v0.4·업무정의서 v0.5 approved, 이전 승인본 superseded 표기.
- (추가) 사용자 지시로 커밋·push (A-013), 이어서 4단계 착수 지시.
- (추가) 4단계 착수: docs/04-validation.md v0.1 draft 작성(D-067~D-077, 백테스트 공통 틀, 실행 안전성 V-01~V-111, PAPER-0/PAPER-1, PV 확인 절차, LIVE 체크리스트 C-01~14, R-ID·E-ID 추적). 질문 Q-1~Q-5 대기.
- (추가) Q-1 수정안(공통 기준+전략별 기준, 거래 1건=왕복) + Q-2~Q-5 권장 → 04-validation.md v0.2 draft. CP-014(strategy-template 항목 추가) 제안.
- (추가) 사용자: 04-validation v0.2 승인 → A-014 전사, .status 현재 단계 5. CP-014: 양식 보관(archive/strategy-template-A005.md) 후 백테스트 통과 기준·PAPER-1 전략별 기준 항목 추가(draft, 승인 대기).
- (추가) 사용자: CP-014 양식 승인(A-015), 커밋·push 지시(A-016), 5단계 착수 지시.
- (추가) 5단계 착수: docs/05-plan.md v0.1 draft 작성(T-000~T-019, D-079~D-084, 추적표, 착수 차단 조건, 커밋 정책·설치 계획 제안). 질문 Q-1~Q-4 대기.
- (추가) Q-1~Q-4 권장안 → 05-plan.md v0.2 draft. D-087: 작업 착수 직전 사용자 준비·승인 항목 재안내(사용자 요청).
- (추가) 사용자: 05-plan v0.2 승인(A-017) 후 커밋·push(A-018). 0~5단계 게이트 모두 승인. 구현 시작은 미지시. 사용자가 산출물 전체 재검토 요청.
- (추가) 산출물 재검토 결과: ① CP-015(INV-15가 취소·정정 의도를 막는 충돌) → 02 v0.3 보관 후 v0.4 draft, ⑦ CLAUDE.md 커밋 정책(A-017)·폴더 구조 확정(A-011) 문구 갱신. ②~④ 정책 결과는 값 설정 시 재안내, ⑤·⑥은 T-014~T-016에서 처리.
- (추가) 사용자: CP-015 승인(A-019), 커밋·push(A-020).
- (추가) 사용자 지시로 docs/initial-setup → main fast-forward 병합 후 push (A-021). 이후 작업 브랜치 기준은 main.
