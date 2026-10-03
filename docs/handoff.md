# 세션 인계 (짧게 유지 — 60줄 이내, 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-03 세션 종료 / T-016 준비 완료 / 다음: **사용자 승인 3건 받기 → 전략 전환 → PAPER-0 실행(사용자)**
- 브랜치(로컬만, push·main 병합 없음): … → feat/T-015-preflight → **feat/T-016-prep (최신, 작업 트리 깨끗)**
- 테스트 `.venv\Scripts\python -m pytest -q` → 759 passed (약 2분)

## 현재 상태
- 6단계 구현. 완료: T-000~T-015, T-020, T-021. T-016은 준비 완료(실행 대기).
- 승인본: 업무정의서 v0.6, 00 v0.4, 02 v0.7 (A-029), 04 v0.6, 05 v0.5 (A-033), 01 v0.6, 03 v0.7 (A-036). 마지막 승인 A-039.
- 실전 주문 코드·LIVE: 승인 없음. 모의 주문 실행은 사용자(D-086).
- PAPER 설정 완비: `.env.paper`(사용자 작성, 에이전트 읽기 차단), `config/{app,limits,market}.paper.toml`, 달력 2024~2026, 종목 005930.
  `connection_approved = true`(사용자 설정). 호가 표 없음(X-U1) → 지정가 거부, 시장가만.
- 키움 모의 실측(kiwoom-kr §7b): 조회 8종 정상, kt00005 모의 미제공(RC9000) → kt00001, ka10054 범위값 "0", 거래정지 = state·auditInfo 둘 다(K-U6).
- 전략 `paper0-verify` v1: PAPER 저장소에 **초안(DRAFT)** 등록. 백테스트 통과 기록 `docs/strategies/paper0-verify/backtest-v1.md`.

## 다음 세션 첫 행동
1. 사용자에게 승인 3건 요청(이미 안내함):
   ① paper0-verify 초안→백테스트 통과 ② →PAPER ③ SNTP 외부 연결(time.kriss.re.kr·time.bora.net, 승인 시 1회 점검으로 주소 확인)
2. 승인되면 approvals에 A-ID 기록 → `scripts\strategy.py transition --mode PAPER paper0-verify 1 BACKTEST_PASSED --approval A-xxx --evidence docs/strategies/paper0-verify/backtest-v1.md` → `... PAPER --approval A-yyy` → status.md 갱신
3. 사용자에게 PAPER-0 실행 안내: `docs/strategies/paper0-verify/pv-procedure.md` 3절(시작 후 K8 재개는 사용자)
4. 사용자가 "n일차 끝"이라 하면 `var/paper/trading.db` 분석 → PV 결과를 kiwoom-kr에 기록(T-017)

## 세션 시작 시 읽을 것 (이것만)
1. CLAUDE.md (자동), 이 파일, `git status`, `docs/.status`
2. `docs/decisions-pending.md` 대기: DP-28(kt00001 qry_tp 3), DP-29(auditInfo 판정), DP-30(백테스트 설정=PAPER 값)
3. `docs/strategies/paper0-verify/pv-procedure.md`, `docs/broker-specs/kiwoom-kr.md` §7b

## 도구 (사람 실행, 조회·운영)
- 시작 점검 `scripts\preflight.py`, 달력 초안 `scripts\derive-calendar.py`, 전략 `scripts\strategy.py show|register|transition`
- 운영 `scripts\{status,kill,resume,reconcile-resolve,backup,restore-test}.py --mode PAPER`
- 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run` (사용자만)
- 권한: `.claude/settings.local.json`(git 제외) — PowerShell·Edit·Write 자동 허용, push·reset·rebase·merge·pip install·.env 읽기 차단.

## 남은 위험·후속
- PAPER-0 실측 대상: K-U5 실시간 보장, K-U10 1700 대기, K-U14 kt00009·909, K-U16 911 누계, K-U17 문자열, K-U19 일봉 범위, PV-11 토큰 갱신.
- 미실행 확정: PV-01(오류 주문)·03·08·09·14(지정가·정지 종목·정정 미사용/미구현).
- 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거, 텔레그램 앱 연결(사용자 "나중에").

## LIVE 전 필수 (미뤄 둔 항목)
- 달력 KRX 원문 대조 + 사용자 검증 데이터 요청(A-037 조건), 2026-11-19 수능일 시간 공지 확인
- 백업 외부 저장소 재결정(A-039), 텔레그램 연결, 노출된 모의 키 재발급, 실전 API·비용 재확인(모의 근거 불가)
- 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자)

## 사용자에게 남은 결정·확인
- 승인 3건(위), U-06 키움 차트 API 약관, DP-28~30 확정, 보기 전용 대시보드 CP 여부(PAPER-1 전)
- 전체 로드맵: PAPER-0(T-016) → 결과 반영(T-017) → 실제 전략 결정(T-018, 사용자) → PAPER-1(T-019) → LIVE(별도 승인)

## 작업 방식
- 작업 하나 = 세션 하나 권장. 작업마다 테스트 통과 → 로컬 커밋 → 이 파일 갱신. worklog 항목 15줄 이내.
- 명령은 단순 단일 명령으로(권한 규칙 일치). 커밋 메시지는 스크래치 파일 + `git commit -F`.
