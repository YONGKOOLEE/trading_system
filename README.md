# trading_system

개인용 자동매매 시스템 (국내주식·ETF → 크립토 현물 → 미국주식).
목표는 수익 보장이 아니라, 승인된 전략과 리스크 규칙이 의도대로 실행되고 불확실한 상태에서 위험이 커지지 않는 것이다.

- 현재 상태: **6단계 구현 중** (`docs/.status`, 계획 0~5단계 승인 완료). 작업 목록: [docs/05-plan.md](docs/05-plan.md)
- 개발 환경: Python 3.14, 프로젝트 가상환경 `.venv` + `requirements-dev.txt`(해시 고정). 테스트: `.venv\Scripts\python -m pytest`
- 에이전트 규칙: [CLAUDE.md](CLAUDE.md)
- 범위·요구사항: [docs/business-definition.md](docs/business-definition.md)
- 문서 체계: [docs/README.md](docs/README.md)

비밀정보는 `.env`에만 두며 커밋하지 않는다. `.env.example`을 참고한다.

## 실행 (사람이 직접. 자동 실행 등록은 CP-029 PAPER 예약 작업만 — 사용자가 등록)
- 엔진: `.venv\Scripts\python -m trading_system.app run` — 모드는 `TRADE_MODE`(환경 변수 → `.env` 한 줄).
  LIVE는 거부. PAPER는 `config/app.paper.toml`의 `[paper] connection_approved = true`(사용자가 키움 모의 접속을 승인한 뒤 직접 설정)일 때만 접속한다.
  시작하면 항상 K8(재시작 복구 대기) — 대조 확인 후 `scripts/resume.py --mode <모드> K8`로 재개.
- M1 PAPER 무인 자동 운영(CP-029, A-080): `scripts/autopilot.py --mode PAPER`가 시작 점검 → run → K8 자동 재개(같은 복구 조건) → 15:45 정상 종료·paper-review·백업·텔레그램 요약을 한다. 평일 예약 작업 2개(`TradingSystem-PAPER-ClockSync` 06:40, `-Autopilot` 06:45)는 **사용자가 관리자 PowerShell에서 `scripts/schedule-register.ps1`로 등록·`schedule-unregister.ps1`로 해제**한다. PAPER 외 모드는 거부. LIVE는 자동화하지 않는다. 절차: `docs/strategies/paper0-verify/pv-procedure.md` §3.5.
- 백테스트: `.venv\Scripts\python -m trading_system.app backtest --strategy 패키지.모듈:클래스 --params config/strategies/<전략>.toml --data <종목코드.csv 폴더> --start YYYY-MM-DD --end YYYY-MM-DD --run-id <ID>` → `var/backtest/<ID>/report.md`
- 필요한 설정: `config/limits.<mode>.toml`, `config/market.<mode>.toml`, `config/app.<mode>.toml`, `config/calendar/krx-<연도>.toml` (각 `*.example.toml` 참고, 값은 사용자가 정함)
- 운영 도구(`--mode <모드>`): `scripts/kill.py`, `resume.py`, `status.py`, `reconcile-resolve.py`, `backup.py`, `restore-test.py`, `archive-logs.py`, `migrate.py`, `watchdog.py`, `preflight.py`(시작 점검), `paper-review.py`(일일 집계), `telegram-test.py`, `strategy.py`(등록·상태 전환), `autopilot.py`·`ops-stop.py`(자동 운영 시작·정지 요청), `schedule-register.ps1`·`schedule-unregister.ps1`(예약 작업, 사용자)
- 데이터·백테스트 도구: `scripts/fetch-daily.py`(키움 일봉 조회, run 종료 뒤에만), `scripts/validate-strategy.py`(CP-030 공통 성과 기준 검증 실행기 — 브랜치 `feat/T-026-validate`, 병합 전)
- 현재 상태 요약·다음 행동: [docs/handoff.md](docs/handoff.md)
