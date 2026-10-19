# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: **2026-10-19 16시 (세션 50 계속)**. 마지막 승인 **A-128**(다음 A-129). main d3f0a9a(통합 integ/2026-10-19c 병합). push는 사용자.
- 실전 주문 코드·LIVE 승인 **없음**(M1·M2 모두). A-125는 **테스트넷 주문만**.
- 방향: **M2 우선**(CP-037). 매매 대상 BTC·ETH 고정(A-120), **배포 후보 = 60분 판정 crypto-trend**(A-123 ⑤ — 공식 판정 전). 실전까지 가장 긴 것은 crypto-trend **PAPER-1 최소 90 UTC일 + 왕복 1건**(CP-038·CP-041).

## 지금 돌고 있는 것
| 항목 | 상태 |
|---|---|
| **M2 PAPER-0** (m2-verify v1, 테스트넷) | 시작 2026-10-19 00:06 KST, m2-binance `m2/run3`(수동 run). **첫날 왕복 정상**: 10:00 IOC 매수 0.00012 @ 82,302.44, 13:00 시장가 매도 @ 82,533.16, 수수료 0(테스트넷 — PV-M2-07), 정지 0·불일치 0. 확인 = M2 봇 `/review` |
| M1 | 월요일 06:45 무인 자동 운영 정상 예정(PV-11 끝, K8·K9만, 미체결 0 — 0056724 정정 완료). 보유 005930 1주, 14:30 매도 예정 |
| T-050 관찰 run | 종료(10-18 23:53). 결과 [t050-observation.md](strategies/m2-verify/t050-observation.md) |

## 다음 세션 첫 행동
1. **crypto-trend v3 공식 검증 결과 확인**(10-19 16시 main에서 실행 — `varacktest\crypto-trend-v3-official-1alidation.md`, 로그 scratchpad). 판정 문구(일반 → 저빈도 예외 LF-0~LF-8)를 사용자에게 권장안과 함께 질문(A-127 (2)). 통과면 BACKTEST_PASSED·PAPER 전환 승인 → PAPER-1 준비(CP-039 T-060~063 실서버 시세 신호, T-071 ETH 유니버스, T-072, T-059, T-056 무인 관찰·D-231 등록 승인).
2. M2 PAPER-0 관찰 계속(PV-M2-01~11 기록 문서 정하기 — 07 M2 절 권장). 종료·판정 시점은 사용자와(M1은 5거래일이었음).
3. CP-041 다음 draft: D-276 (2)·(4) 문구(일봉 = m2_daily_input, 결측 = DP-82 (b)) 정정. CP-038 D-244 (5)에 [CP-042] 표시.
4. 비차단 정리: 재검토 지적(LF-8 blocked_entries 이중 계산, 일봉 백테스트 창 길이, 감독 STOP 파일 지움 경쟁, 예약 작업 해제 안내).

## 사용자 결정 대기
1. crypto-trend 공식 판정(결과 나오면 질문).
2. D-231 M2 예약 작업 등록 승인(T-056 무인 관찰 전 — PAPER-0과 같은 체크아웃이라 PAPER-0 뒤). 정지 뒤 다음 날 자동 재시작됨(DP-98) 안내 필요.
3. DP-89 (6) 손절 감시 주문 창 밖 20분 공백 — CAP-HLD-02 선언 전략 PAPER 전.
4. DP-83~100 확인(비차단), DP-85 (6) 저빈도 원장 위치.
5. [M2 LIVE 전] L-1 바이낸스 실계정 자동매매 가능 여부(안 되면 업비트 — PAPER-1 시작 전에 결정), L-2 전용 계정, L-3 고정 IP·키.

## 10-18~19 병합된 것 (main d3f0a9a)
- 10-18: M2 실행 경로 T-045~T-051 등, T-052·T-053, crypto-trend v3 재검토, T-073, V-157 잔여(m2_daily_input), T-070.
- 10-19(integ/2026-10-19c, 전체 3817 passed / 1 skipped): CP-040 T-065 수수료·T-066 분봉 확장·T-067 손절 감시·T-068 장중 실행기(여러 심볼·확정 일봉·DP-82·최종 확인 경로), CP-037 T-054·T-055 M2 감독·예약 스크립트(등록 안 함), CP-038 T-058 저빈도 판정, 경계·isMaker·불완전 1분봉(--skip-incomplete-minutes) 수정, A-128·CP-042·판정 전용 한도 config/m2/limits.verdict.toml, crypto-trend 저빈도 선언.
- crypto-trend 재현: 축소판 2020~2025 75건이 전략실 재생과 진입·청산 분 단위 일치. 2017~ 104건(전략실 105) — BTC 2018-03-03 1건은 2018-02-08 불완전 일봉 제외 + DP-82 (b)로 설명, ETH 2017-10 2건 1시간 차이는 미확인(일봉 출처 차이 추정).

## LF-8 / crypto-trend
- 공식 LF-8 목록 `docs/strategies/lf8/lf8-list.csv`(SHA-256 432cf0cf…) = 수집용 목록과 101행 일치 → 보충 수집 불필요. 해석 H-1~H-5·D-244 (4) 전환 4종목(EOS·BTT·MATIC·FTM) ×0.9 청산 여부는 T-058 때 사용자 확인.
- crypto-trend v3: 운영 재검토 완료(review-v3.md), BACKTEST 등록만. 재현은 T-068 뒤. 전략실 미리 보기 LF-8 4항목 충족(공식 아님).
- 승인 증거 재현 영향(DP-82): m2-verify backtest-v1(A-112)·btc-trend validation-v1은 재실행 시 결과가 달라짐 — 승인본 보존.

## 전략실 연락
- 전략실 세션 `strategy-lab-b7`. 10-18에 T-073 결과·handoff 수정 회신. A-122~A-125·PAPER-0 시작은 아직 알리지 않음.
- 전략실 무거운 실행 시간 금지 **없음**(사용자 해제 — 항상 BelowNormal, 평일 06:40~16:00 병렬 최대 8). 시험 실패 시 부하 원인 여부를 단독 재실행으로 가린다.

## 데이터 (모두 `var\backtest\data\`, git 제외)
- 국내 `kiwoom-krall-adj-20261017\`·`kiwoom-usertrades-adj-20261016\`, 바이낸스 `binance-public-20261017\`(BTC·ETH 1d·1m)·`binance-public-usdt-1d-20261018\`(763심볼 일봉)·`binance-public-lf8-1m-20261018\`(LF-8 29심볼 1분봉, 9.8GB). 2026-01-01 이후 행은 검증에 쓰지 않음.

## 도구 (사람 실행)
- M1: `scripts\autopilot.py`·`ops-stop.py`·`status.py`·`resume.py`·`reconcile-resolve.py`·`paper-review.py`·`backup.py`·`strategy.py`(`--mode PAPER`).
- M2(m2-binance에서, 파이썬은 M1 폴더 `.venv`): `scripts\run-m2.py`, `status.py --mode PAPER --market M2`, `resume.py --mode PAPER --market M2 K4|K8`, `reconcile-resolve.py initial-baseline|reset-confirm --mode PAPER --market M2`. 정지 = run 창 Ctrl+C 또는 `var\paper\STOP` 파일.
- LF-8: `scripts\lf8-list.py`.

## 주의·위험
- 서브에이전트 지시에는 프로세스 종료·네트워크·비밀 파일·main 체크아웃·var 쓰기·push 금지를 항상 넣는다. 서브에이전트가 남긴 백그라운드 작업은 TaskStop으로 정리(10-18 1건).
- 비밀 파일 이름이 든 셸 명령은 권한 차단 → Grep/Glob 도구나 사용자 실행 명령으로.
- 경로 역슬래시는 Edit/Write 도구로. 수정 뒤 제어 문자 검사.
- `docs/capabilities.md`는 실행 코드가 읽음 — M1 run 중 변경·병합 금지.
- M2 run은 수동·자동 재시작 없음 → 재부팅되면 그날 거래 없음(위험 아님). 테스트넷 리셋(약 월 1회) → K4 → `reset-confirm` → `/exit` 반복.
- 모의 서버(M1)는 평일 07:53 WS 종료 → K5 → 대조로 해제. 주말은 토 07:53 종료, 일 05:00·07:2x 조회 실패(07 §12H).

## LIVE 전 필수 (요약 — 04 §5.1, 07 §13, CP-037 D-233 L-1~L-10)
- M1: 달력 원문 대조, 시계 기준, 실전 호출 제한·수수료·호가 단위 원문, 전용 실계좌, PV-13·PV-04, L1 근거, 모의 데이터와 KRX 대조.
- M2: B-U6 계정·약관, 실전 키 IP 제한, 실서버 수수료·호출 제한, 테스트넷 체결 품질 한계, 저빈도 예외 전략 첫 규모 50% 이하, DP-82 결정, 실전 주문 코드 승인·LIVE 실행 승인(각각 별개).

## 작업 방식
- 시험 먼저 → 통과 → 로컬 커밋(push·main 병합은 사용자) → 이 파일·worklog(15줄 이내) 갱신.
- 수정 뒤 독립 재검토 1회 이상. 서브에이전트 수치는 원본 1~2곳 직접 대조(CLAUDE.md §12).
- 승인은 사용자가 대화에서 한 것만 approvals.md에 전사.
- 사용자: 터미널 비숙련 — 복사해 붙일 전체 명령과 기대 결과. 안내 전 **코드에서 키 이름·출력 문구를 확인**(10-18 가이드 오류 3건의 교훈). "실전"은 LIVE에만.
