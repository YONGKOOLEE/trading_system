# 세션 인계 (짧게 유지 — 60줄 이내, 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-03 / T-016 준비 완료(전략·백테스트·등록·절차서·복원 시험·K-U6) / 다음: **사용자 승인 3건(전환 2·SNTP) → PAPER-0 실행(사용자, `docs/strategies/paper0-verify/pv-procedure.md`)**
- **LIVE 전 필수(A-037 조건)**: 달력 KRX 원문 대조 + 사용자에게 검증 데이터 요청. 2026-11-19 수능일 시간은 KRX 공지 후 확인.
- 브랜치: feat/T-015-preflight → **feat/T-016-prep (최신)**. 테스트 743 passed.

## 현재 상태
- 6단계 구현. 완료: T-000~T-015(T-015: PV-06 통과, PV-11은 발급·만료만 — 갱신 관찰은 PAPER-0), T-020, T-021.
- 테스트 `.venv\Scripts\python -m pytest -q` → 721 passed (약 2분).
- 실측으로 바뀐 것: 예수금 kt00005 → kt00001(모의 미제공 RC9000), ka10054 범위값 "0". 승인 문서 정정은 CP-020(draft).
- 브랜치(로컬만, push·main 병합 없음): … → feat/T-020-app → fix/dp-5-daily-bar-cutoff → **feat/T-015-preflight (최신)**.
- 실행 전 사용자 할 일: `.env.paper`에 `TRADE_MODE=PAPER` 줄 추가, `config/app.paper.toml` `connection_approved = true`.
  실행: `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` → 결과 `var/paper/preflight/*.json`.
- 승인본: 업무정의서 v0.6, 00 v0.4, 01 v0.5, 02 v0.7 (A-029), 03 v0.6, 04 v0.6, 05 v0.5 (A-033). 무인 진행 권한 A-030.
- PAPER 준비: `.env.paper`(키 4개, TRADE_MODE 줄 없음), `config/limits.paper.toml`·`market.paper.toml`(A-034, 비용 DP-27 임시값), `app.paper.toml`(종목·전략 없음, SNTP 주소 미확인). 없음: 달력, 호가 표.
- 실전 주문 코드·LIVE: 승인 없음. PAPER 접속: 사용자가 `config/app.paper.toml [paper] connection_approved = true`로 직접 켜야 함.

## 세션 시작 시 읽을 것 (이것만)
1. CLAUDE.md (자동), 이 파일, `git status`, `docs/.status`
2. `docs/decisions-pending.md` (DP-1~DP-26 확정 A-031. 미결: DP-2 04 V-80 문구, DP-25 03 D-092 차이, DP-24·DP-1과 PAPER-0 순서)
3. 다음 작업 행: `docs/05-plan.md` §2 T-015 + `docs/broker-specs/kiwoom-kr.md` §7a·§8

## 구현 구조 요약
- 진입점 `python -m trading_system.app run|backtest` (app/main.py). 조립 `app/wiring.build_engine`(시험과 공용), 엔진 `app/engine.Engine.tick`.
- 스레드: 판정·실행 = 주 스레드. 실시간(WS)·시세 조회·텔레그램은 별도 스레드, 저장소 연결 따로, 정지는 `engine.call_soon`.
- 키움: `broker/kiwoom/{adapter,http,token,parse,errors,realtime}.py`, 호출 제한기 `broker/ratelimit.py`, 시장 데이터 `marketdata/kiwoom_feed.py`.
- 시험용 키움 모의 서버 `tests/support/kiwoom_sim.py`(루프백 HTTP+WS).
- 결과 불명 해소 단계형 `execution/resolver.begin/step`. 스키마 버전 2(마이그레이션 1→2 등록).

## 남은 위험·후속
- PAPER-0 실측 필요(PV): K-U14 kt00009 행 단위·체결번호 일치, K-U16 911 누계 여부, K-U17 정정·취소·거부 문자열, K-U10 1700 대기, K-U5 실시간 보장.
- CAP-ORD-03 정정 미구현(취소만). 분봉(CAP-DAT-02) 미지원. 거래 이벤트 트리거(CAP-TRG-03·04) 미지원.
- 진입점 이름(DP-23)은 원하면 CP로 03 §7 개정.
- 텔레그램이 app/main에 연결 안 됨, `.env` 키 이름 없음 → T-013 실제 송수신 확인 전 보완 필요(승인 대기).
- K-U19 ka10081 일봉 KRX/통합 범위 — PAPER-0에서 KRX 종가와 비교.

## 사용자에게 남은 결정·확인
- DP-27 수수료·매도 세율 실제 값, 텔레그램 연결 보완(사용자 "나중에")
- K-U1 단말기 요건(IP 등록은 완료 보고), 노출된 모의 키 재발급 여부
- 실제 설정값: limits·market·app·calendar(휴장일 X-U3)·ticks(X-U1) — 저장소에 기본값 없음

## 작업 방식 (토큰 절약)
- 작업 하나 = 세션 하나 권장. 무인 진행 시 작업마다 테스트 통과 → 로컬 커밋 → 이 파일 갱신.
- 끝날 때: 이 파일 덮어쓰기 + worklog 항목은 15줄 이내.
