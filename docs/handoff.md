# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-06 / **PAPER-0 1일차 완료 — K-U14로 K4, 매도 거부, 모의 1주 보유 중. T-017 수정 커밋(feat/T-017-paper0-day1-fixes, 체크아웃됨)** / **다음: ① 2일차 시작 대조가 어제 항목을 자동 종결하는지 확인 → K8·K4 재개 ② 2일차 결과 분석 ③ 개정 draft 문서별 승인**
- 브랜치(로컬만, push 없음): 현재 체크아웃 **`feat/T-017-paper0-day1-fixes`** = main(4e502a5) + T-017 수정 + docs/m2-binance-design 병합. **PAPER-0 2일차는 이 브랜치로 실행.** main 병합은 사용자 지시 시
- 테스트 `.venv\Scripts\python -m pytest -q` → 1006 passed(2026-10-05, 807초). 빠른 확인은 `--ignore=tests/scenario/test_run_rehearsal.py --ignore=tests/scenario/test_run_rehearsal2.py`
- 승인본: 업무정의서 v0.6, 00 v0.4, 01 v0.8, 02 v0.7, 03 v0.9, 04 v0.8, 05 v0.5. **마지막 기록 A-065**(A-062~A-065는 M2 설계 결정 — 문서 승인 아님). 실전 주문 코드·LIVE 승인 없음.

## 다음 세션 첫 행동
1. `git status`·이 파일·`docs/.status`·`docs/approvals.md` 끝 5행·`docs/worklog.md` 마지막 항목 확인. 사용자가 어느 작업(트랙 A/B)을 원하는지 확인.
2. **트랙 A — M1 PAPER-0 (사용자 실행, D-086)**: 사용자가 "n일차 끝"·결과를 주면 `scripts\paper-review.py --mode PAPER` 출력 + `var/paper/logs/ops-*.jsonl` + 텔레그램 메시지를 분석 → kiwoom-kr K-U 갱신, T-017 기록. 1일차 관찰 항목: 첫 체결 직후 K4/K5 여부(K-U14·16·20), 06:45 서버 응답, CASH_OBSERVED(DP-31 재결정), 09:30 누락 시 14:30 역매수 여부, ka10054 경고 재발 여부(값이 메시지에 표시됨).
3. **트랙 B — M2 설계 (문서만, 코드 금지)**: CP-024 v0.5 승인(A-066). 승인본은 `docs/archive/*-v(이전).md`에 보존, 개정 draft 7종(업무정의서 v0.7, 00 v0.5, 01 v0.9, 02 v0.8, 03 v1.0, 04 v0.9, 05 v0.6)을 사용자에게 문서별 승인 요청. capabilities.md 개정안은 **`docs/changes/CP-024-capabilities-draft.md`에 따로** 있다 — 실제 `docs/capabilities.md`는 M1 실행 코드(main.py:130, strategy_tool.py:105)가 읽으므로 승인 뒤 M1 회귀 시험과 함께 반영. M2 코드는 A-065 Q1대로 M1 T-017 뒤 T-M2-01부터.
   - **개정 draft 미결(사용자 판단)**: (a) V-157 재실행 목록의 V-41·V-48·V-125·V-126 기대값(04 §6) (b) 03 §16 미결 3건 — dust 판정 시점, PENDING_NEW·PENDING_CANCEL 처리, CANCEL_ONLY 보유 알림 (c) 승인된 CP-024 본문 정오: §7 dust "min(...)"→ D-102 "max(...)", §3 04 행 V/PV 범위 낡음, §4 V-131 "보수 근사" — 개정본은 D-102를 따름 (d) "M2 브랜치" 정의 없음(03 §8.2.1·05 2-M2.0) (e) CAP-ORD-03을 묶음+03a·03b로 둔 것(코드가 "CAP-ORD-03" 문자열 사용), 업무정의서에 §12 변경 이력 신설 (f) 02 §2.1·ENT-22 "KST 날짜 혼용을 KRX 거래일로 정리"는 M1 일일 리스크·보고 키를 바꿀 수 있음 — 구현 시 M1 회귀로 확인 (g) 기존부터 낡은 선행 문서 버전 표기(02·04·05 머리).
4. 사용자는 터미널 명령에 익숙하지 않다 — 실행 안내는 **복사해 붙일 전체 명령**으로, 용어는 풀어서 쓴다(예: "자리표시자" 설명 필요했음).

## PAPER-0 2일차 주의 (2026-10-07)
- 어제 K4가 걸린 채 끝났고 005930 1주 보유. 아침 run 시작 대조에서 어제 MISSING_FILL 항목이 자동 종결(AUTO_MATCHED)되어야 한다 → `status`에서 열린 대조 불일치 0건 확인 → `resume.py --mode PAPER K8`, `resume.py --mode PAPER K4`.
- 보유 1주라 09:30에는 EXIT(매도)가 나가고 ENTRY는 안 나감, 14:30에는 ENTRY(매수)가 나가 밤새 보유 — 매매 시각이 하루 밀린 상태. 그대로 두어도 인프라 검증에는 문제없음(익일 보유·재시작 대조도 검증됨). 원래 순서로 돌리려면 14:30 전 `/kill`(K1) → 다음 날 아침 `resume.py K1`.
- 관찰: 매도 체결 뒤 K4가 다시 안 걸리는지(수정 확인), VI 경고가 사라졌는지, 07:50~09:00 서버 구간.

## M1 현재 상태
- 6단계 구현, M1(국내주식, 키움 모의). 완료 T-000~T-015·T-020·T-021, T-016(PAPER-0) 시작 조건 충족. 전략 paper0-verify v1 = PAPER(005930 1주, 09:30 매수·14:30 매도, 시장가).
- 운영 방식(A-054): 출근 전 시작·무인 운영 — `docs/strategies/paper0-verify/pv-procedure.md` §3.2(06:43 시간 동기화 → preflight → run → K8 재개, 휴대폰 09:35·14:35 `/status`, 저녁 Ctrl+C). 텔레그램 켜짐(A-056, 봇·키는 사용자 설정).
- 10-05 반영(A-059·A-060, CP-023): 실시간 재접속 한도 뒤에도 계속, 꺼져 있던 동안의 텔레그램 명령 미실행, VI 목록 우리 종목만 해석, 실시간 통보 해석 실패 경고, 종목코드 오류에 값 표시.
- 예행연습(10-04 밤·10-05 새벽, 휴장 시간): 시작·K8 재개·텔레그램 정상. **주문·체결·대조 경로는 실서버 미검증 — 1일차에 처음 돈다.**
- 무인 중 수용 위험: K3·K4·K5(스레드)·K7·K8은 원격 재개 불가 → 그날 매매 없음 또는 모의 1주 익일 보유.

## M2 설계 (바이낸스 현물, 롱 온리)
- 확정: 바이낸스 현물 유지, USDT 기준·환율 없음, 일일 기준 UTC 00:00, 한국 거주자 이용 가능(사용자 보고) — A-055. **구조 = 코드 공유 + 실행 분리(시장별 프로그램·DB·폴더, 같은 저장소의 별도 체크아웃), 별도 프로젝트 포크 아님 — A-061.**
- 근거 문서: `docs/changes/M2-impact-survey-2026-10-05.md`(코드·문서 영향, 선택지 4a~4h, 작업 분해 T-M2 후보, 결정 10건), `docs/broker-specs/binance-spot.md` v0.2.
- 조사 핵심: "어댑터만 추가"가 아님 — 공통 코드 30파일 일반화 필요(수수료가 받은 자산에서 차감 → 보유 수량 대조, 거래일·달력 개념, 주문 요청·체결 필드). **공통 코드 일반화는 M1 PAPER-0 결과 반영(T-017) 뒤 착수 권장**, 그 전에는 문서와 M1 코드를 건드리지 않는 신규 부분만.
- **결정 완료**: A-062(테스트넷·정지 시장별·순 수령·dust·대조 범위), A-063(IOC 지정가 매수·시장가 매도·시장별 봇·HMAC·테스트넷 시세), A-064(L1 0.5%·L2 10%·L3 50%·L4 1%·L5 5·L6 3·L7 100 USDT, 창 UTC 00:10~23:50, 신선도 60초, 서버 시각 경고 0.5초), A-065(위임: 착수 = T-017 뒤 T-M2-01, 체크아웃 `C:\AI\trading_system\m2-binance`, 시세 지연 청산 = IOC 지정가, `[M1]` 표시는 T-017 뒤).
- 설계 초안 CP-024 v0.5(미승인): D-096~D-120, V-129~V-161, PV-M2-01~11. 오류 분류는 원문 근거 있는 -1013·-1021·-2010(중복 외)만 명시 거부, 나머지 결과 불명. 결과 불명 5회 미해소 → K3(자동 미접수 종결 없음). 사양 기록 `binance-spot.md` v0.3(원문 대조 2026-10-05). 검증 전략 초안 `docs/strategies/m2-paper0-verify/strategy.md`(미승인, L1 때문에 테스트넷 USDT 기준 금액 4,000 이상 필요 — 미확인).
- 절차: 계획 가이드 "다중 시장 처리" — M2 착수 시 0~5단계 문서 전체 영향 검토, 공통 본문이 바뀌면 M1 승인 재검토. 구현은 M2 계획 승인 + "구현 시작" 지시 뒤. 숏·선물은 범위 밖(나중에 백테스트 근거 후 CP).
- LIVE 전 과제: 자동매매 전용 계정 분리(사용자가 바이낸스에서 수동 매매 중 — A-051과 같은 문제), 약관·법규 판단은 사용자(U-17).

## 로드맵 (승인 단위)
| 순서 | 작업 | 선행·승인 |
|---|---|---|
| 1 | T-016 PAPER-0 실행(사용자, 04 §4.1) | 충족 |
| 2 | T-017 PAPER-0 결과 반영 — 07-paper-review 초안, K-U·PV 판정, DP-31·40 재결정 | PAPER-0 데이터 |
| 3 | 문서 정리 CP — 02 열거값(SKIPPED·AUTO_ADOPTED·CASH_OBSERVED), 03 §13 목록, 04·05 추적표, DP-40·41·43 문구 | T-017과 함께 |
| 4 | T-018 실제 전략 결정(`docs/strategies/candidates-2026-10-03.md`, S-Q1~4) → T-019 PAPER-1 | 사용자 결정 |
| 5 | CP-024 승인 → 문서 개정 → M2 구현(T-M2-01부터, T-017 뒤) | CP-024 사용자 승인, M2 "구현 시작" |
| 6 | LIVE 준비(시장별) — 아래 필수 항목 + 실전 주문 코드 승인 + LIVE 실행 승인(각각 별도, 실행은 사용자) | 별도 승인 |

## 도구 (사람 실행, 먼저 `cd C:\AI\trading_system\trading_system`)
- 점검 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` (run 실행 중에는 거부)
- 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run` (사용자만)
- `.venv\Scripts\python scripts\<도구>.py --mode PAPER`: `status`, `resume K8`, `kill`, `reconcile-resolve`, `paper-review`, `telegram-test`, `backup`, `restore-test`, `strategy show|register|transition`
- 권한 `.claude/settings.local.json`: push·reset·rebase·clean·pip install·비밀 파일 접근 차단(merge 차단은 사용자가 삭제). **명령문에 비밀 파일 이름이 들어가면 거부되므로** 그런 문자열이 필요한 문서 수정은 스크립트 파일로 한다.

## 남은 위험·미처리
- 텔레그램 검토 M-2~4(영구 실패 알림의 K11 고착, 전송 간격·429, 명령 처리 중 저장 오류 시 유실) — PAPER-0 후.
- 낮음: 알림 롤백 유실, CLOSE 대조 지연(5차 F-5), 같은 tick 확정+FOUND 이중 판정(8차 F-2), 텔레그램 스레드 생존 감시 밖, status 정지 시각 UTC 표시.
- 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거.
- 문서 경로를 heredoc으로 쓸 때 역슬래시가 제어 문자로 깨진 적 있음 → 문서 수정 뒤 제어 문자 검사.
- 정리 필요(사용자): `C:\Users\user\AppData\Local\Temp\v91-p8kqynon` 임시 폴더, var/backtest의 중단·거부 실행 폴더.

## LIVE 전 필수
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 KRX 공지
- 실전 API 호출 제한·수수료·호가 단위(X-U1) 공식 원문(`docs/broker-specs/live-prep-2026-10-03.md` — 2차 출처뿐)
- 자동매매 전용 실계좌 추가(A-051), 백업 외부 저장소(A-039), 노출된 모의 키 재발급, 밤샘 실행 검증(PV-11)
- 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자)

## 작업 방식
- 작업 하나 = 세션 하나 권장. 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내.
- 수정 뒤에는 독립 재검토 1회 이상(회귀가 잦았음). 병렬 에이전트는 읽기 전용 또는 파일 소유 분리, 커밋은 리드만.
- 승인된 문서를 고칠 때: 승인본 archive → draft → 사용자 승인 → approvals 전사. 병합·push는 지시 시만.
