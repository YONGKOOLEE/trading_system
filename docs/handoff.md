# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-04 오후 / 결정표 전부 처리(A-046~A-053), main 병합(71f4438까지) / **다음: (지시 시) main 재병합 → PAPER-0 실행(사용자)**
- 브랜치(로컬만, push 없음): main = 71f4438 (A-047 병합). **feat/T-017-verify는 그 뒤 문서 커밋 4개 더 있음**(264340b·6609ee8·53b5294·승인 반영) — 병합은 지시 시
- 테스트 `.venv\Scripts\python -m pytest -q` → **983 passed** (2026-10-04 21:32 실행, 757초 — 예수금 관찰 기록 A-054 포함). 빠른 확인은 `--ignore=tests/scenario/test_run_rehearsal.py --ignore=tests/scenario/test_run_rehearsal2.py` (969, 약 5.5분)
- 권한: settings.local.json의 git merge 차단 규칙은 사용자가 삭제(2026-10-04)

## 현재 상태
- 6단계 구현, 인도 차수 M1(국내주식, 키움 모의). 완료 T-000~T-015·T-020·T-021, T-016(PAPER-0) **시작 조건 충족**(Q-1~3 해결)·실행 대기.
- 승인본: 업무정의서 v0.6, 00 v0.4, **01 v0.7**, 02 v0.7, **03 v0.8**, **04 v0.7**, 05 v0.5. **마지막 승인 A-053**.
- 결정 완료: Q-1 예수금·Q-2 U-06(A-046, 조회 제한 조건 kiwoom-kr §2), DP-28~38·40~43 확정(A-048·A-049), CP-021(A-050), CP-022(=DP-39, A-052), 01/03/04 개정(A-053).
- LIVE 계좌: 모의는 현 계좌 유지, LIVE는 자동매매 전용 실계좌 추가(A-051).
- PAPER-0 운영 방식(A-054): **10/6(화)부터 출근 전 시작**(06:45 preflight → run → K8 재개, 07:15 출발, 퇴근 후 확인) — pv-procedure §3.2. 예수금은 CASH_OBSERVED 자동 기록.
- M2: 바이낸스 현물 유지(사용자 2026-10-04, 승인 범위 그대로). A-055: USDT 기준·환율 없음, 일일 기준 UTC 00:00, 한국 거주자 이용 가능(사용자 보고). 미결: PAPER 환경(테스트넷 권장/Demo), API 키 유형·리셋 처리(나중), 자동매매 전용 계정 분리(사용자 수동 매매 중 — A-051과 같은 문제). 숏·선물은 범위 밖 — 나중에 백테스트 근거 후 CP. 공식 문서 조사 초안 `docs/broker-specs/binance-spot.md`(작성 중/검토 필요).
- 텔레그램: 모듈(ops/telegram.py)은 구현·시험됨, **run에 미연결**. PAPER-0 1일차 뒤 별도 브랜치에서 연결(봇 생성·토큰 입력은 사용자, 외부 연결 승인 필요).
- 전략 paper0-verify v1 = PAPER (A-040·A-041), SNTP 연결 A-042. **실전 주문 코드·LIVE 승인 없음**.
- PAPER 설정 완비(.env.paper는 사용자 작성·에이전트 읽기 금지, connection_approved=true, 005930, 시장가만).

## 다음 세션 첫 행동 (순서)
1. `git status`·이 파일·`docs/.status` 확인. 남은 결정: S-Q1~4(전략 후보, T-018 — 지금 비차단), main 재병합 여부.
2. 병합 지시 시 **feat/T-017-verify → main** (fast-forward, D-085). push는 지시 시만.
3. PAPER-0 실행 안내(사용자 실행): `docs/strategies/paper0-verify/pv-procedure.md` — cd → preflight → run(창 하나) → **09:25까지 K8 재개** → status 확인. 3.1절 운영 주의(14:20 정지 확인, Ctrl+C 종료, 절전 끄기, 실행 중 preflight 금지).
4. 1일차 기록 요청: entr·d2_entra 세 시점(DP-31), 첫 체결 직후 K4 여부(K-U16), 시작 대조 조회 실패 여부(K-U21), 계좌번호 형식(K-U20).
5. 사용자가 "n일차 끝"이라 하면: `scripts\paper-review.py --mode PAPER` 결과 + `var/paper/logs/ops-*.jsonl` 분석 → kiwoom-kr K-U 갱신, T-017 기록.

## 앞으로의 로드맵 (승인 단위)
| 순서 | 작업 | 내용 | 선행·승인 |
|---|---|---|---|
| 1 | T-016 PAPER-0 실행 | 사용자 실행, 최소 기간·거래 수는 04 §4.1 | 위 결정 Q-1~3 |
| 2 | T-017 PAPER-0 결과 반영 | 07-paper-review 초안, K-U·PV 판정, DP-31·40 재결정(entr, 키움 마감 소멸 PV-03), 필요 시 CP | PAPER-0 데이터 |
| 3 | T-018 실제 전략 결정 | 후보 3종 비교(`docs/strategies/candidates-2026-10-03.md`), 추가 일봉 조회 승인(S-Q3), 운용 규모 L7·L2·L3 재결정(S-Q4) | 사용자 결정 |
| 4 | T-019 PAPER-1 | 실제 전략 등록 → 백테스트(`--eval-time 09:30,14:30` 가능, DP-42) → PAPER 전이(A-ID) → 04 §4.2·4.3 기준 | 전략 승인 |
| 5 | LIVE 준비 | 아래 "LIVE 전 필수" 전부 + 실전 주문 코드 승인(별도) + LIVE 실행 승인(별도, 사용자 실행) | 각각 별도 승인 |

## 세션 시작 시 읽을 것
1. CLAUDE.md, 이 파일, `git status`, `docs/.status`, `docs/approvals.md` 끝 5행
2. `docs/decision-sheet.md`, `docs/decisions-pending.md`(DP-28~43 대기)
3. `docs/changes/review-2026-10-03-pre-paper0.md`(검토 1~8차 요약), pv-procedure, kiwoom-kr §7b·K-U 표

## 도구 (사람 실행, `.venv\Scripts\python scripts\<x>.py --mode PAPER`)
- `preflight`(엔진 실행 중이면 거부), `status`, `kill`, `resume <K>`, `reconcile-resolve`, `backup`, `restore-test`, `strategy show|register|transition`, `paper-review`(읽기 전용 집계)
- 실행: `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run` (사용자만)
- 백테스트: `... -m trading_system.app backtest --strategy <모듈:클래스> --params <toml> --data var/backtest/data/kiwoom-ka10081-adj-20261002 --start --end --run-id <새 ID> --cash 100000000 --eval-time 09:30[,14:30] --slippage-tick 100` (교란 검사 포함 약 20분)
- 권한 `.claude/settings.local.json`: PowerShell·Bash·Edit·Write 자동, push·reset·rebase·merge·clean·pip install·.env 차단

## 남은 위험·관찰 (PAPER-0에서 집중)
- 정지 → 매수 취소 재시도, KILL/RESUME 순서: 검토 3~7차마다 결함(일부 회귀) → 운영 중 K1·K2·K5 발생 시 status와 키움 앱 미체결을 반드시 대조.
- 키움은 마감 소멸을 EXPIRED로 표시하지 않음 → PV-03 전에는 미체결이 남으면 K4(사람 정리). K5 중에는 청산도 막힘(01 정책).
- 미처리(낮음): 알림 롤백 유실(운영 로그엔 남음), CLOSE 대조 지연(5차 F-5), 같은 tick 확정+FOUND 이중 판정(8차 F-2), rt.start 예외 시 WS 감시 공백.
- 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거, 텔레그램 연결.
- 정리 필요(사용자): `C:\Users\user\AppData\Local\Temp\v91-p8kqynon` 임시 폴더(권한 거부로 남음), var/backtest의 중단·거부 실행 폴더(git 제외).

## LIVE 전 필수 (미뤄 둔 항목)
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 KRX 공지(현재 예상값 10:00~16:30)
- 실전 API 호출 제한·수수료·호가 단위(X-U1) 공식 원문 확인(`docs/broker-specs/live-prep-2026-10-03.md` — 현재 2차 출처뿐)
- **자동매매 전용 실계좌 추가(A-051)**: 키움 추가 계좌 개설 가능 여부·앱키/IP 등록 단위·계좌 분리 시 실전 호출 제한 적용 확인 → 계좌 별칭(예: KR-LIVE-01)·.env.live
- 백업 외부 저장소(A-039), 텔레그램 연결, 노출된 모의 키 재발급, 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자)

## 작업 방식
- 작업 하나 = 세션 하나 권장. 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내.
- 병렬 에이전트는 파일 소유를 나눠 같은 작업 트리에서, 커밋은 리드만. 수정 뒤에는 독립 재검토 1회 이상(회귀가 잦았음).
- 무인 작업 지시 시: 시작 시각 기록·경과 확인·마감 뒤 새 작업 금지, 끝나면 지시대로 PC 종료.
