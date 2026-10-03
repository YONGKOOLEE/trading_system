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

## 2026-10-01 (5) — 구현 시작, T-000 개발 환경·골격
- 승인: 구현 시작(A-022), 1차 설치(A-023). 브랜치 `feat/T-000-dev-env` (main에서 분기).
- 수행: `.venv` 생성, `requirements-dev.txt`(pytest 9.1.1·iniconfig 2.3.0·packaging 26.3·pluggy 1.6.0·pygments 2.21.0·colorama 0.4.6, PyPI SHA-256 고정), `pyproject.toml`(pytest 설정, paper 마커 기본 제외), 03 §13 패키지 골격(15개 + broker/fake·kiwoom), tests 하위 폴더, 의존 방향 검사기·테스트(V-93), README 3개 갱신.
- 실행 명령과 결과:
  - `python -m venv .venv`; `pip install --require-hashes -r requirements-dev.txt` → 6개 설치 성공. `pytest --version` → 9.1.1 (Python 3.14.7)
  - 골격 생성 전 `pytest -q` → 1 failed(패키지 15개 없음, 기대한 실패), 23 passed
  - 골격 생성 후 → 24 passed
  - 임시 위반 모듈(strategy → broker, datetime.now) 추가 → 1 failed, 위반 2건 검출 → 모듈 삭제 → 24 passed
- 남은 위험: 03 §7에 규칙이 없는 패키지(evaluation 등 9개)는 app import 금지만 검사. broker 규칙을 fake까지 확장 적용(D-062 근거). V-94(주문 경로 단일성)는 T-001.
- 다음 세션 첫 행동: T-001 착수 전 준비 항목 안내(D-087: 외부 연결·설치 없음) → T-001 테스트 작성.

## 2026-10-01 (6) — T-001 최소 종단 간 흐름
- 브랜치 `feat/T-001-min-e2e` (T-000 미병합이라 `feat/T-000-dev-env` 위에서 분기).
- 구현: domain(clock·enums·models: Decimal 강제, 시간대 강제, 결정적 신호 ID), broker(base 인터페이스, fake: 체결/접수만/거부/전송 전 실패/결과 불명), storage(SQLite, 모드 기록, BEGIN IMMEDIATE 트랜잭션, 감사 추가 전용 트리거, 주문 식별 (계좌,거래일,주문번호)), gate(G1·G2·G3·G4·G6·G7(L2·L3)·G9, 판정·의도·예약·소유권 한 트랜잭션, 직렬화), execution(ApprovedIntent만, KILL 파일, 전송 시도 선기록, 4분류, 통보 반영·포지션·소유권·예약), halt(K1 발동 → 미체결 매수 취소를 게이트 경유). 테스트: 불변 조건 검사기(INV-01·03·04·05·06·07·08·09·10·12·15·17), 시나리오 17건, 주문 경로 단일성(V-94), 검사기 자체 검증.
- 실행 명령과 결과:
  - 구현 전 `pytest -q` → 수집 오류(구현 모듈 없음, 기대한 실패)
  - 구현 후 → 43 passed
  - 변이 시험: G7·G4·G2·G6·G9·KILL 파일 확인을 하나씩 무력화 → 각각 시나리오 실패 발생(G9는 종료 시 INV 오류도 발생)
  - V-04 동시성 30회 반복 → 실패 0
  - 검토 중 결함 발견: 거부된 신호가 재도착하면 한도 여유 시 승인됨(01 D-016 위반) → 재현 테스트 실패 확인 → G4가 같은 멱등키의 기존 판정(승인·거부 무관)을 중복으로 거부하도록 수정 → 48 passed
- 완료 기준 충족: V-01(L2·L3 경계), V-04, V-07, V-12, V-20(K1 부분·KILL 파일), V-26, V-55, V-94, INV 위반 0.
- 남은 위험·후속: G2는 활성 정지가 있으면 위험 확대를 일괄 차단(유형별 세부는 T-005). 예약 금액은 신호 가격 기준(시장가 상한가 기준은 T-003). 체결 소비액은 의도 가격 기준. 결과 불명 해소는 T-004. var/backtest/<실행ID> 실행기는 T-009(앱 배선). 
- 다음 세션 첫 행동: T-002 착수 전 준비 항목 안내(D-087: 설치·외부 연결 없음 예상).

## 2026-10-01 (7) — T-002 설정·모드·비밀
- 브랜치 `feat/T-002-config-mode` (T-000·T-001 미병합이라 `feat/T-001-min-e2e` 위에서 분기). 모델: Sonnet 5.5로 전환 후 수행.
- 구현: domain(clock: `next_kst_time_after`, limits: AccountLimits·논리 제약 검사), config(errors, schema: 필드 검증·float 거부·문제 일괄 보고, envfile: Secret·SecretStore, limits_file: TOML 로더, modes: TRADE_MODE, versioning: 방향 판정·적용 시점·원격 거부·감사), app(startup: 모드·저장소·자격정보 일치, LIVE 거부), storage(settings_version 표와 조회), 예제(`config/limits.example.toml` 자리표시자, `.env.example` 갱신), 불변 조건 INV-14 검사기.
- 실행 명령과 결과:
  - 구현 전 `pytest -q` → 수집 오류 6건(모듈 없음, 기대한 실패)
  - 구현 후 → 244 passed, 1 failed: 범위 오류가 누락 오류와 한 번에 보고되지 않음(설계 의도: 문제 일괄 보고) → 범위 검사를 필드 스키마로 이동(테스트 수정 없음) → 245 passed, 0 failed, 0 skipped
  - 변이 시험 11종(완화 즉시 적용, 원격 차단 해제, 대기 중 완화가 덮는 순서, 현재값 대신 최신값과 비교, LIVE 허용, 다른 모드 키 허용, 저장소 경로 검사 제거, 모드 문자열 관대화, 비밀 값 노출, float 허용, 기준 시각 유예 제거) → 전부 테스트 실패로 검출
- 완료 기준: V-100·V-101·V-102·V-86(일반 스키마, 전략별 스키마는 T-007)·V-87(규칙 단위, 채널 연동은 T-013)·V-109 통과, 불변 조건 위반 0.
- 결정·주의:
  - 방향 판정은 **현재 적용 중인 값**과 비교(대기 중 완화를 거쳐 즉시 적용을 얻는 우회 차단). 나중 제출이 대기 중인 이전 버전을 덮음(강화 뒤 완화가 되살아나지 않음). 현재 적용값과 같은 값을 다시 내면 대기 중인 완화를 취소.
  - ENT-08 방향 열거에 없는 NEUTRAL(위험 값 변경 없음: 보고 시각 등)을 내부 값으로 추가. 02 개정 필요 시 CP 제안(미제안).
  - 설정 파일의 시각은 시장 현지 시각(KST)의 시각-of-day. 다음 기준 시각 계산은 휴장·주말을 고려하지 않음(T-010).
  - L1~L10 범위·순서는 01 §2.3.1 그대로. 100% 초과는 오류가 아니라 경고.
  - 게이트(T-001)는 아직 SettingsManager를 쓰지 않음 → T-003에서 `effective_limits(...).to_gate_limits(기준 금액)` 연결.
  - 리스크 수치는 저장소에 두지 않음. 테스트의 값은 가상.
- 미해결: 없음(차단 항목 없음).
- 다음 세션 첫 행동: T-003 착수 전 준비 항목 안내(D-087: 설치·외부 연결 없음 예상) → 게이트 완성(G1~G15, 요청 유형 6종).

## 2026-10-02 (8) — 전체 검토, 견고성 수정, 컨텍스트 분할
- 요청: 요구사항-설계-검증 빈 곳, 비정상 입력에서 깨지는 곳, 근거 없는 가정 검토. 결과: `docs/changes/CP-016-review-2026-10-02.md`.
- 코드 수정(브랜치 `fix/review-2026-10-02`): DB 모드 고정, 저장소 질의 잠금, 게이트 G3(유형-방향·위험 확대는 신호만·소유자)·G10(수량·가격)·G11(시세 없음), 실행기 전송 후 기록 실패 차단, 비정상 통보 격리, 미처리 통보 보존.
- 명령·결과: 재현 테스트 수정 전 17 failed·2 errors·1 passed(B-7은 기존에도 안전) → 수정 후 전체 263 passed. 변이 시험 7종 모두 검출. B-7·B-8 5회 반복 통과.
- 문서 변경(03 D-088~D-090, 04 V-112~V-115·V-01 정정, 02 ENT-08 NEUTRAL 등)은 CP-016으로 제안만, 승인 대기.
- 컨텍스트 분할: `docs/handoff.md` 신설(다음 세션은 이 파일과 지정 절만 읽음).
- 다음 세션 첫 행동: CP-016 승인 여부 확인 → T-003.
- (추가) 사용자 '전부 승인': CP-016 반영(02 v0.5, 03 v0.3 D-088~D-091, 04 v0.3 V-112~V-115·V-01 정정, krx-market X-U4·X-U5, .env.example 안내), B-10 수용(A-024), 커밋(A-025, push 없음). 다음: /clear 후 'T-003 진행'.

## 2026-10-02 (9) — 묶음 A 무인 진행 (T-003~T-006)
- 지시: "묶음 A 무인 진행, 결정은 권장대로", 끝나면 30분 뒤 PC 종료 (A-026 전사).
- 커밋(로컬, push 없음): T-003 게이트 완성 5ba9866, T-004 주문 상태·결과 불명 53c2041, T-005 정지·운영 명령 5d1386a, T-006 장애 주입·무작위 8c60819.
- 명령·결과: `.venv\Scripts\python -m pytest -q` 263 → 338 → 361 → 398 → 407 passed (0 failed, 0 skipped). 변이 시험: T-003 21종, T-004 17종, T-005 21종, T-006 6종 모두 검출(처음 미검출 2종은 테스트 추가 후 검출). 다중 프로세스(scripts/kill) 5회 반복 통과.
- 발견·수정한 결함: 결과 불명 취소가 늦게 해소되면 취소 의도가 열린 채 남음(T-004), 종결된 주문에 늦은 체결이 오면 주문이 다시 열림(T-006 무작위·지정 시나리오).
- 테스트 변경: T-001 V-20의 "K1 중 전략 청산 허용" → 거부(01 §2.6 K1 표대로), T-003 임시 정정 테스트 → V-48로 대체. 사유는 커밋 본문.
- 문서(미커밋): CP-017 draft(해석·제안, §5 확인 요청 Q1~Q5), handoff, approvals A-026, .status.
- 미해결: CP-017 승인, 시세 신선도 값. 차단 조건 없음(묶음 B 착수 가능, 단 Q1 답에 따라 K1 규칙 수정 가능).
- 다음 세션 첫 행동: CP-017 §5 답 확인 → 반영 → 묶음 B(T-007) 착수 전 D-087 안내.

## 2026-10-02 (10) — CP-017 승인 반영, Q3 권장값, 2차 전체 재검토
- 사용자 "전부 승인": A-027 전사, 02 v0.6·03 v0.4·04 v0.4·05 v0.3 (승인본 archive, superseded 표기), .status 갱신.
- Q3 시세 신선도: 권장 60초(조회 응답 수신 시각 기준), 근거·확인 방법은 CP-018 §1. 확정은 사용자.
- 재검토: 문서·코드·구조 세 갈래(서브에이전트 병렬, 결과는 직접 확인). 코드 결함 11건 + 구조 결함 4건 수정 → 3658c51 (브랜치 fix/review-2026-10-02b).
- 명령·결과: pytest 407 → 429 passed. 회귀 22건(수정 전 17 실패), 변이 17종 검출, 재현 스크립트 7종 확인.
- 제안·결정 요청: CP-018 draft (§3 Q1~Q5 차단, §4 D-1~D-19). 가장 큰 공백: 앱 배선·키움 시장 데이터 작업이 계획에 없음(D-1), PAPER-0 검증 전략 진입 경로(Q2).
- 다음 세션 첫 행동: CP-018 답 확인 → 반영(문서 개정, 05 작업 추가) → 묶음 B.

## 2026-10-02~03 (11) — A-030 무인 진행: CP-018 반영 → 묶음 B → 묶음 C(오프라인) → 검토 수정 → T-014 → T-021 → T-020
- 지시: "전부 허용 무조건 완결성과 완벽한 프로그램을 위해 동작해"(A-030), 이후 "다음 진행사항 진행 후 끝나면 pc 종료".
- 커밋(로컬, push·main 병합 없음, 브랜치 사슬): a89cb15 문서 CP-017/018 → 7c1ec40 준비 → a9538ed T-007 → 94328bb T-008 →
  186c24b T-010 → 1e6fd64 T-009 → 7a5cdf4 T-011 → 7ff1198 T-012 → bfd896b T-013 → b4c7c98 검토 B 수정 15건 →
  1362ff2 T-014 → a77f7d4 T-021 → 5d9d8f8 T-020 (현재 브랜치 `feat/T-020-app`).
- 명령·결과: `.venv\Scripts\python -m pytest -q` 600(bfd896b) → 620(b4c7c98) → 669(T-014) → 676(T-021) → 700 passed(T-020), 실패·건너뜀 0.
  변이 시험: 검토 B 29종, T-014 38종, T-021 16종, T-020 28종 — 처음 미검출은 시험 보강 후 모두 검출.
- 커밋 메시지 정정: 7c1ec40 본문 "435 passed" → 실제 434, 7a5cdf4 본문 "571 passed" → 실제 570 (이후 커밋은 실행 결과를 그대로 기록).
- 외부 열람: 키움 공식 GitHub 공개 명세 JSON·클라이언트 코드·예제(로그인·API 호출 없음) → broker-specs §7a, K-U14·16·17·18. 키움·텔레그램·SNTP 실제 접속 없음.
- 발견·수정한 결함(주요): 외부 주문 인정 거래일·수량 차이 덮어쓰기·같은 버전 재등록·복구 판정 SENT 누락·오래된 일봉·
  마스킹 누락·폐기 K9·누락 체결 거부 시 해소 기록·열린 항목 남은 일치·';' 분할 마이그레이션·UTC 로그 날짜(B-1~B-15),
  전각 숫자 통과(엄격 파서), 엔진에서 전략에 미검증 파라미터 전달.
- 시험 변경(요구 변경): T-007 v83(보유 남은 전략 폐기 거부), T-011 마이그레이션(스키마 2 기준). 사유는 커밋 본문.
- 결정 대기 DP-10~DP-26 추가(모두 비차단, 보수적 임시값). capabilities M1 일부 "검증 중"(DP-24).
- 미해결: 결정 대기 일괄 검토, CP 필요 1건(DP-23 진입점 이름), PAPER 시작 전 사용자 확인(K-U1 IP·단말기, K-U6 거래정지 표시, X-U1 호가 표, X-U3 휴장일, 수수료 K-U9).
- 다음 세션 첫 행동: handoff.md 확인 → 사용자 결정 대기열 검토 → T-015(키움 모의 접속, 조회만) 승인 여부.

## 2026-10-03 (12) — 결정 대기 DP-1~26 확정(A-031), DP-5 변경 반영
- 사용자 확인: DP-1~4·6~26 임시 결정 확정, DP-5 변경(일봉 확정 시각 = max(정규장 마감, 20:00), 애프터마켓 고려).
- 변경: `marketdata/store.py`(DAILY_BAR_FINAL·daily_bar_final_at), `marketdata/kiwoom_feed.py`(당일 봉 저장 기준 동일화),
  시험 2개(test_t010 V-90, test_t021 당일 봉) — 15:30~20:00 당일 봉 제외로 기대값 변경(요구 변경 A-031).
  문서: approvals A-031, decisions-pending 상태 "확정", kiwoom-kr K-U19(ka10081 KRX/통합 범위, PAPER-0 확인).
- 명령·결과: 변경 시험 먼저 실행 → 2 failed(예상), 구현 후 `.venv\Scripts\python -m pytest -q` → 700 passed. 브랜치 `fix/dp-5-daily-bar-cutoff`(feat/T-020-app에서 분기).
- 발견: 텔레그램(ops/telegram)이 앱 실행 경로(app/main)에 연결되지 않았고 `.env` 키 이름도 없음 → T-013 실제 송수신 확인 불가. 보완 작업 승인 필요.
- 미결: DP-2(04 V-80 문구 CP 여부), DP-25(03 D-092와 차이 정리 방식), DP-24/DP-1과 PAPER-0 순서(T-016 때).
- 다음 세션 첫 행동: 사용자 T-015 준비(앱키·IP·.env.paper·config paper 3종) 확인 → T-015 승인 여부, 텔레그램 연결 보완 승인 여부.
- (같은 날 추가) A-032: DP-2·DP-25는 CP로 문서를 실제 동작에 맞추는 방향. CP-019 draft 작성(03 D-092, 04 V-80, 05 T-020 진입점) — 반영은 CP-019 승인 후.
- 사용자가 대화에 실제 모의 자격정보를 붙여넣음(유형: 앱키·시크릿·계좌번호, 값 미기록) → 재발급 권고. `.env.paper`는 저장소에 아직 없음(키 존재만 점검, 값 미열람).
- (같은 날 추가) CP-019 승인 A-033 → 03 v0.6·04 v0.6·05 v0.5 반영, 승인본 archive(superseded 표시), .status 갱신.
  PAPER 설정 A-034: `config/limits.paper.toml`·`market.paper.toml` 작성, 프로젝트 로더로 검증 통과(경고 0). 비용 요율 DP-27 보수 임시값.
  `.env.paper_virtual` → `.env.paper` 이름 변경(키 4개 존재·형식만 점검, 값 미열람, git 제외 확인). 텔레그램 연결은 사용자 "나중에".
  시험 미실행(코드 변경 없음).

## 2026-10-03 (13) — T-015 시작 점검 구현(오프라인), 권한 설정
- 승인: A-035 T-015(키움 모의 연결, 조회만), K-U1 IP 등록 사용자 보고.
- 구현: `app/preflight.py`(GuardedHttp: 주문 API 전송 전 차단·호출 분류 기록 / run_preflight: 토큰 발급 → 조회 8종 1회씩 → 시작 대조 → 토큰 폐기 / preflight_main: 연결 전 거부), `scripts/preflight.py`, `config/app.paper.toml`(connection_approved=false).
- 시험: `test_t015_preflight.py` 18건(구현 전 수집 오류 확인). 돌연변이 6종 중 2종 미검출 → 시험 보강 후 6종 검출. `.venv\Scripts\python -m pytest -q` → 718 passed. 커밋 `feat(T-015)` (브랜치 feat/T-015-preflight).
- 권한: `.claude/settings.local.json`(git 제외) — PowerShell·Edit·Write 허용, push·reset·rebase·merge·clean·pip install·.env 읽기/수정 차단. Remove-Item 차단 줄은 미반영(설정 파일 수정은 매번 확인 필요).
- 미실행: 실제 키움 모의 접속(PV-06·PV-11). 사용자: `.env.paper`에 `TRADE_MODE=PAPER` 추가, `connection_approved = true`.
- 다음 세션 첫 행동: 사용자 설정 확인 → `scripts/preflight.py` 실행 → 결과를 kiwoom-kr K-U8·PV-06·PV-11에 기록.

## 2026-10-03 (14) — T-015 실제 키움 모의 시작 점검 (조회만, 주문 0건)
- 사용자: `.env.paper` TRADE_MODE 추가, `connection_approved = true`. 실행 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` 3회.
- 1·2회 실패: kt00005 모의 미제공(return_code 20 + RC9000, 8104 아님), ka10054 1511(min_trde_qty 필수). 2회차 전 실패 응답 서버 문구(가림 처리) 기록 기능 추가.
- 수정: kt00001로 교체(`adapter.query_cash`), RC9000 = 8104 취급(`errors.specific_code`), ka10054 범위값 "0"(`kiwoom_feed`). 시험 서버도 실측대로 엄격화(kt00005 RC9000, ka10054 필수값). 공식 명세 JSON(S2) 내려받아 필드 확인(스크래치, 저장소 미포함).
- 3회: **통과** — 토큰 발급·폐기, 조회 8종 정상, 시작 대조 MATCH, 주문 API 0건. 경고: 달력 없음(일봉 품질 거짓 경고 포함), SNTP 미점검.
- 시험: `.venv\Scripts\python -m pytest -q` → 721 passed.
- 문서: kiwoom-kr §7b 실측·K-U1·K-U8 해소, CP-020 draft(01·03의 kt00005 문구), DP-28(qry_tp 3).
- 미결: CP-020 승인, PV-11 만료 전 갱신 관찰(장시간 실행 필요, PAPER-0), 달력 2024~2026(X-U3).

## 2026-10-03 (15) — CP-020 반영, T-016 준비(A-037: 종목·DP-24 a·달력)
- CP-020 승인(A-036) → 01 v0.6, 03 v0.7. A-037: PAPER-0 종목 005930, DP-24 해법 (a), 달력 자동 추출(LIVE 전 재확인 조건).
- DP-24 (a): `strategy_registry` verification_approval — PAPER-0 검증 전략만 "검증 중" 허용, 범위 강제(M1·S-4·U-A·시장가 능력·L7 필수). 시험 16건.
- 달력: `marketdata/calendar_derive.py`, `app/calendar_tool.py`, `scripts/derive-calendar.py`(키움 일봉 2종목 교차, 조회만). preflight 연결 검사를 `connect_paper`로 분리. 시험 5건.
  실행 결과: 2024 18일, 2025 19일, 2026 13일(~10-02), 한 종목만 빠진 날 0일. 2026 남은 휴장(10-05·10-09·12-25·12-31)은 토스증권 공지(웹 검색), 개장 시간 변경 2025-11-13·2026-01-02(보도), 2026-11-19(예상값).
  `config/calendar/krx-2024~2026.toml` 작성 → 시작 점검 재실행 통과, 달력·일봉 품질 경고 사라짐.
- 시험 변경(요구 변경 A-037): test_t010 `test_calendar_dir_ignores_examples`를 임시 디렉터리로. `.venv\Scripts\python -m pytest -q` → 743 passed.

## 2026-10-03 (16) — T-016 준비 진행 (A-038 비용, A-039 지시)
- 비용 확정 A-038: 수수료 0.015%, 매도 세금 0.20%. 백업 PC 안만, K-U6 권장안(A-039).
- K-U6 실측(ka10099 5,834행 집계, 조회만): 거래정지 state 26 vs auditInfo 112 → `master_statuses`가 auditInfo도 사용(DP-29). 시험 보강.
- 복원 시험: `scripts\backup.py --mode PAPER` → `scripts\restore-test.py --mode PAPER` 통과(무결성·모드·스키마 2·감사 연속·추가 전용).
- 구현: `strategy/paper0_verify.py`(09:30 1주 시장가 진입, 14:30 청산), `config/strategies/paper0-verify-v1.toml`, 배포 설정 `verification_approval`(PAPER만), `app/strategy_tool.py`+`scripts/strategy.py`(show/register/transition), `repository.strategy_states`. 시험 16건.
- 백테스트: 모의 일봉 005930 CSV(조회만) → 1천만 원 실행은 L1으로 전부 거부(설계대로) → 1억 원(모의 예수금) 재실행 통과: 212 거래, 재현성·룩어헤드 통과, G12 2건(개장 10:00일). DP-30.
- PAPER 저장소 등록: `scripts\strategy.py register --mode PAPER` → paper0-verify:1 초안.
- 문서: docs/strategies/paper0-verify/{strategy,backtest-v1,status,pv-procedure}.md, kiwoom-kr K-U6.
- 시험: `.venv\Scripts\python -m pytest -q` → 759 passed.
- 남은 사용자 항목: 전략 전환 승인 A-ID 2개(BACKTEST_PASSED·PAPER), SNTP 연결 승인, U-06 약관. 그 뒤 PAPER-0 실행(사용자).

## 2026-10-03 (17) — 세션 종료 인계
- 사용자에게 전체 로드맵(PAPER-0 → T-017 → T-018 전략 결정 → PAPER-1 → LIVE 별도 승인)과 LIVE 전 필수 항목 안내.
- handoff.md 전면 갱신, .status 현재 단계 갱신. 코드 변경 없음, 시험 미실행(문서만). 마지막 시험 759 passed.
- 다음 세션 첫 행동: 승인 3건 확인 → 전략 전환 → PAPER-0 실행 안내.

## 2026-10-03 (18) — 승인 3건, PAPER-0 전 전체 검토·수정 (A-040~A-043, 무인 2시간 20분)
- 승인 전사 A-040·A-041(paper0-verify v1 → BACKTEST_PASSED → PAPER, `scripts\strategy.py transition` 실행), A-042(SNTP, 1회 점검 bora -0.076s, kriss 1차 무응답·재시도 -0.077s; time2.kriss.re.kr도 조회함 — 승인 목록 밖), A-043(수정 범위·무인 작업).
- 검토: 읽기 전용 에이전트 5개 → RV-01~25(docs/changes/review-2026-10-03-pre-paper0.md). RV-24·25는 01 승인 문구와 구현 불일치라 CP-021 대신 구현 정정.
- 수정: 에이전트 4개 병렬(파일 소유 분리) + 리드 통합. 재검토 2회·e2e(다일 11, 장애 11)에서 추가 결함 F1(차단)~F9, 재검토2 #1~#4, 장애 결함 2건 수정.
- 커밋(feat/T-016-fix, 로컬): a7c3298, 304e538, 62d1ff7, 63c94d0, c019731, a9a970d, 472a42b, 847b669.
- 시험: 시작 759 passed → 최종 `.venv\Scripts\python -m pytest -q` 890 passed(228s). 시뮬레이터 응답 유실 e2e 간헐 실패 관찰(대기 추가로 대응, 재발 관찰).
- 임시 결정 DP-31~37, 새 미확인 K-U20(계좌번호 형식)·K-U21(빈 결과 목록 키).
- 설정·외부: SNTP 조회 외 브로커·네트워크 연결 없음. .claude/settings.local.json에 Bash 자동 허용(사용자 지시) + 차단 보강(git clean, pip install, .env).
- 다음: 사용자 확인(DP-31~37, 모의 예수금 ≥ 상한가×200, U-06) → main 병합 여부 → PAPER-0 실행(사용자, pv-procedure v0.2).
