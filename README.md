# trading_system

개인용 자동매매 시스템 (국내주식·ETF → 크립토 현물 → 미국주식).
목표는 수익 보장이 아니라, 승인된 전략과 리스크 규칙이 의도대로 실행되고 불확실한 상태에서 위험이 커지지 않는 것이다.

- 현재 상태: **6단계 구현 중** (`docs/.status`, 계획 0~5단계 승인 완료). 작업 목록: [docs/05-plan.md](docs/05-plan.md)
- 개발 환경: Python 3.14, 프로젝트 가상환경 `.venv` + `requirements-dev.txt`(해시 고정). 테스트: `.venv\Scripts\python -m pytest`
  - 빠른 확인(리허설·종단 간 제외): `.venv\Scripts\python -m pytest -q -m "not rehearsal and not e2e"`
  - 전체(커밋 전 기준): `.venv\Scripts\python -m pytest -q` — `-m` 없이. 기본 선택은 PAPER 연동(`paper`)만 뺀다 (A-084 ⑥)
- 에이전트 규칙: [CLAUDE.md](CLAUDE.md)
- 범위·요구사항: [docs/business-definition.md](docs/business-definition.md)
- 문서 체계: [docs/README.md](docs/README.md)

비밀정보는 `.env`에만 두며 커밋하지 않는다. `.env.example`을 참고한다.

## 실행 (사람이 직접. 자동 실행 등록 없음)
- 엔진: `.venv\Scripts\python -m trading_system.app run` — 모드는 `TRADE_MODE`(환경 변수 → `.env` 한 줄).
  LIVE는 거부. PAPER는 `config/app.paper.toml`의 `[paper] connection_approved = true`(사용자가 키움 모의 접속을 승인한 뒤 직접 설정)일 때만 접속한다.
  시작하면 항상 K8(재시작 복구 대기) — 대조 확인 후 `scripts/resume.py --mode <모드> K8`로 재개.
- 백테스트: `.venv\Scripts\python -m trading_system.app backtest --strategy 패키지.모듈:클래스 --params config/strategies/<전략>.toml --data <종목코드.csv 폴더> --start YYYY-MM-DD --end YYYY-MM-DD --run-id <ID>` → `var/backtest/<ID>/report.md`
- 필요한 설정: `config/limits.<mode>.toml`, `config/market.<mode>.toml`, `config/app.<mode>.toml`, `config/calendar/krx-<연도>.toml` (각 `*.example.toml` 참고, 값은 사용자가 정함)
- 운영 도구: `scripts/kill.py`, `resume.py`, `status.py`, `reconcile-resolve.py`, `backup.py`, `restore-test.py`, `archive-logs.py`, `migrate.py`, `watchdog.py`
