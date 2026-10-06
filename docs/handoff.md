# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-06 밤 / PAPER-0 1일차 끝·분석·T-017 수정 완료. 2일차(10-07) 준비됨 / **다음: ① 2일차 결과 분석 ② "원격 운영" 방법 1(A-068 착수 지시, CP-026 결정 질문 답변 대기, 아래) ③ 저녁 v2 전환 ④ 문서 승인 묶음**
- 브랜치(로컬만, push 없음): 체크아웃 **`feat/T-017-paper0-day1-fixes`** (HEAD f73baa8) = main(4e502a5) + T-017 수정 + docs/m2-binance-design 병합. **PAPER-0는 이 브랜치로 실행.** main 병합은 2일차에서 수정 확인 뒤 사용자 지시로(A-067 B-7).
- 테스트: 2026-10-06 빠른 회귀 1052 passed(324초, 예행연습 2파일 제외) + 예행연습 9 passed(447초). 전체 `.venv\Scripts\python -m pytest -q`(약 13분).
- 승인본(게이트): 업무정의서 v0.6, 00 v0.4, 01 v0.8, 02 v0.7, 03 v0.9, 04 v0.8, 05 v0.5 (archive에 보존, 현 파일은 draft). **마지막 기록 A-068.** 실전 주문 코드·LIVE 승인 없음.

## 다음 세션 첫 행동
1. `git status`·이 파일·`docs/.status`·`docs/approvals.md` 끝 3행·`docs/worklog.md` 마지막 항목 확인.
2. 사용자가 "2일차 끝"·결과를 주면: `status` 출력·텔레그램·`var/paper/review/`·`var/paper/logs/ops-20261007.jsonl`·DB(읽기 전용 `file:...?mode=ro`)로 분석 → `docs/07-paper-review.md` 2일차 추가, `docs/broker-specs/kiwoom-kr.md` §7c 보강. **확인할 것**: 시작 대조가 1일차 항목을 AUTO_MATCHED로 종결했는지, 09:30 v1 매도 체결 뒤 K4 재발 없음(K-U14 수정 확인), VI `_AL`/`_NX` 경고 사라짐, 불일치 알림 반복 없음, 07:50~09:00 서버 구간, 사용자가 `/kill` 보냈는지.
3. 수정이 확인되면 사용자에게 main 병합 여부를 묻는다.
4. 사용자는 터미널 명령에 익숙하지 않다 — 실행 안내는 **복사해 붙일 전체 명령**, 용어는 풀어서.

## 2일차(2026-10-07) 사용자 절차 — 안내 완료
- 06:43 시간 동기화 → `preflight` → `run` → 두 번째 창 `status`("열린 대조 불일치: 0건") → `resume.py --mode PAPER K8`, `resume.py --mode PAPER K4` → `status`("해제되지 않은 정지: 0건").
- 09:35 `/status`: 보유 0건이면 **`/kill`**(14:30 매수 차단, 매매 순서 복구용). 14:35 `/status`.
- 저녁: `status` → `paper-review` → `backup` → Ctrl+C → "2일차 끝".
- (선택) 방법 2: run 뒤 VS Code 대화창 `/remote-control`(안 되면 터미널 `claude remote-control`) → 휴대폰 Claude 앱 Code 탭에서 연결.
- 상세: `docs/strategies/paper0-verify/pv-procedure.md` v0.4 §3.2·§3.4.

## 진행 중 결정·작업
### 원격 운영 (사용자 요청 2026-10-06 — 회사에서 장중 조치 불가)
- **방법 1 (착수 지시 A-068, 2026-10-06. CP-026 미작성 — CP-026 승인 전 PAPER 실행본에 넣지 않음)**: 구현은 **git worktree**(`C:\AI\trading_system\remote-ops`, 브랜치 `feat/T-022-remote-ops` 제안)에서 — PAPER 실행 중인 체크아웃의 브랜치를 바꾸지 않는다. 매매 시스템 텔레그램 봇 확장. `/resume K8`(복구 조건 충족 시만), `/resume K4`(열린 대조 불일치 0건 + 최근 대조 MATCH 시만) 원격 허용, `/reconcile`(즉시 대조·결과 회신), `/review`(오늘 체결·정지·불일치 요약). K3·K5·K7은 로컬 유지. 확인 코드(`/confirm`) 유지. 대상 규칙: 01 §2.9(원격 재개 범위), 03 §6.6, 04 V-32. **별도 브랜치에서 구현·시험 → CP-026 → 승인 뒤 병합**. 2일차 실행본에는 넣지 않는다.
- **방법 2 (설치 불필요)**: Claude Code Remote Control(https://code.claude.com/docs/en/remote-control.md). 도구 기능이라 정책 변경 없음. 원격 지시를 받아도 승인 구조 그대로 — `run`·정지 재개는 사용자(D-086), 에이전트는 분석·개발·조회. 텔레그램 채널 플러그인(https://code.claude.com/docs/en/channels.md)은 시험판·Bun 설치 필요라 보류.

### 검증 전략 v2 전환 (보유 0건인 날 저녁)
- Ctrl+C → 에이전트가 `config/app.paper.toml` 전략 3줄 변경(module `trading_system.strategy.paper0_verify:Paper0VerifyV2`, params `config/strategies/paper0-verify-v2.toml`, verification_approval `A-067`) → 사용자:
  ```
  .venv\Scripts\python scripts\strategy.py register --mode PAPER
  .venv\Scripts\python scripts\strategy.py transition --mode PAPER paper0-verify 2 BACKTEST_PASSED --approval A-067 --evidence docs/strategies/paper0-verify/backtest-v2.md
  .venv\Scripts\python scripts\strategy.py transition --mode PAPER paper0-verify 2 PAPER --approval A-067
  .venv\Scripts\python scripts\strategy.py transition --mode PAPER paper0-verify 1 STOPPED --stop-reason REPLACE --approval A-067
  ```
- 사본 저장소로 사전 시험 성공(2026-10-06). 전환 뒤 `status`에 K9(paper0-verify:1) 1건 남는 것은 정상. v1 보유는 v2로 넘어가지 않으므로(D-011) 보유가 있으면 전환하지 않는다. `docs/strategies/paper0-verify/status.md`에 전환 행 추가.

### 승인 대기 묶음 (사용자)
- CP-025(D-121~D-135, V-162~V-180, 결정 Q-1~Q-9) — T-017 코드가 이미 구현됨. 01 v0.9·02 v0.8·03 v1.0·04 v0.9 draft에 반영.
- CP-024 반영 개정 draft 7종(업무정의서 v0.7, 00 v0.5, 01 v0.9, 02 v0.8, 03 v1.0, 04 v0.9, 05 v0.6) + `docs/changes/CP-024-capabilities-draft.md`(실제 capabilities.md는 M1 실행 코드가 읽으므로 승인·회귀 시험 뒤 반영).
- 개정 draft 미결: V-157 재실행 목록 V-41·V-48·V-125·V-126 기대값, 03 §16 미결 3건(dust 판정 시점, PENDING_NEW·PENDING_CANCEL, CANCEL_ONLY 알림), CP-024 본문 정오(§7 min→max 등), "M2 브랜치" 정의, CAP-ORD-03a/03b, 02 KST 날짜 혼용 정리의 M1 영향.
- 사용자 확인: 1일차 매수 수수료(실측 추정 약 970원 = 0.35%, 설정 0.015% — 키움 앱 체결 내역), NXT VI(`_NX`)를 우리 종목 VI로 볼지(현재 보수적), 07-paper-review 판단 필요 1~5.

## 현재 상태 요약
- **M1**: 6단계, PAPER-0 진행(1일차 10-06 완료). 전략 paper0-verify v1 PAPER, 005930 1주 보유(v1 소유) 중 — 2일차 09:30 매도 예정. 1일차 결함·수정: worklog (30)(31), kiwoom-kr §7c, 07-paper-review v0.1.
- **M2(바이낸스)**: 설계 CP-024 v0.5 승인(A-066). 구현은 M1 T-017 뒤 T-M2-01부터(A-065 Q1) + M2 "구현 시작" 지시 필요. 근거: CP-024, binance-spot.md v0.3, 검증 전략 초안 `docs/strategies/m2-paper0-verify/strategy.md`(미승인).

## 로드맵 (승인 단위)
| 순서 | 작업 | 선행·승인 |
|---|---|---|
| 1 | PAPER-0 2일차 이후(3~5거래일 권장) | 진행 중 |
| 2 | 원격 운영 방법 1 (CP-026) | 착수 A-068. CP-026 결정 Q 답변 → CP-026 승인 → 병합 |
| 3 | T-017 마무리: main 병합, v2 전환, CP-025·개정 draft 승인, DP-31·40 재결정 | 2일차 확인 |
| 4 | T-018 실제 전략 결정 → T-019 PAPER-1 | 사용자 결정 |
| 5 | M2 구현(T-M2-01부터) | T-017 완료 + M2 구현 시작 |
| 6 | LIVE 준비(시장별) | 별도 승인 |

## 도구 (사람 실행, 먼저 `cd C:\AI\trading_system\trading_system`)
- 점검 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` (run 실행 중에는 거부)
- 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run` (사용자만)
- `.venv\Scripts\python scripts\<도구>.py --mode PAPER`: `status`, `resume K8|K4|K1`, `kill`, `reconcile-resolve`, `paper-review`, `telegram-test`, `backup`, `restore-test`, `strategy show|register|transition`
- 권한 `.claude/settings.local.json`: push·reset·rebase·clean·pip install·비밀 파일 접근·`rm -rf` 차단. **명령문에 비밀 파일 이름이 들어가면 거부**.

## 남은 위험·주의
- 텔레그램: 내용 거부 긴급 알림은 로컬에만 남음, 429 상한 이내 지연은 K11 없음(CP-025 R-4~R-8).
- K-U14 행 단위 미확인(부분 체결 여러 번인 날), 대조 합산이 많게 셀 수 있음 → K4(보수적).
- 낮음: 알림 롤백 유실, CLOSE 대조 지연, 같은 tick 확정+FOUND 이중 판정, 텔레그램 스레드 생존 감시 밖, status 정지 시각 UTC 표시.
- 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거.
- **문서 편집 주의**: heredoc·Python 문자열에 `scripts\resume.py` 같은 경로를 쓰면 `\r`이 줄바꿈으로 깨진다(10-06에도 발생). 경로가 든 문서 수정은 Edit 도구 또는 `chr(92)`로, 수정 뒤 `grep -n "^esume\|scripts$"`와 제어 문자 검사.
- 정리 필요(사용자): `C:\Users\user\AppData\Local\Temp\v91-p8kqynon` 임시 폴더.

## LIVE 전 필수
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 KRX 공지
- 실전 API 호출 제한·수수료·호가 단위(X-U1) 공식 원문
- 자동매매 전용 실계좌(A-051), 백업 외부 저장소(A-039), 노출된 모의 키 재발급, 밤샘 실행 검증(PV-11)
- 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자)

## 작업 방식
- 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내.
- 수정 뒤 독립 재검토 1회 이상(10-06에도 높음 등급 4건 발견). 재검토 지적은 코드로 사실 확인 후 반영(V-3처럼 틀린 지적 있음).
- 병렬 에이전트는 읽기 전용 또는 파일 소유 분리, 커밋은 리드만. `docs/capabilities.md`는 실행 코드가 읽으므로 PAPER 중 직접 수정 금지.
