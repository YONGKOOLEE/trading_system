# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: **2026-10-19 00:1x KST (세션 50 끝)**. 마지막 승인 **A-126**(다음 A-127). main은 origin보다 50커밋 앞섬 — **push는 사용자가 실행**(`git push origin main`).
- 실전 주문 코드·LIVE 승인 **없음**(M1·M2 모두). A-125는 **테스트넷 주문만**.
- 방향: **M2 우선**(CP-037). 매매 대상 BTC·ETH 고정(A-120), **배포 후보 = 60분 판정 crypto-trend**(A-123 ⑤ — 공식 판정 전). 실전까지 가장 긴 것은 crypto-trend **PAPER-1 최소 90 UTC일 + 왕복 1건**(CP-038·CP-041).

## 지금 돌고 있는 것
| 항목 | 상태 |
|---|---|
| **M2 PAPER-0** (m2-verify v1, 테스트넷) | **시작 2026-10-19 00:06 KST**. `C:\AI\trading_system\m2-binance` 브랜치 `m2/run3`(main 41e2948 기준 + 사용자 설정), run 창은 사용자 PC(수동 run, 자동 재시작 없음). 기준선 USDT 10000·BTC 1 수동 보유 → `/exit` **1 BTC @ 81,213.12 체결**(00:06:00). 정지 0. 매일 UTC 01:00(KST 10:00) 매수 10 USDT·04:00(13:00) 매도. 확인 = 텔레그램 M2 봇 `/review`(체결 알림은 없음) |
| M1 | 월요일 06:45 무인 자동 운영 정상 예정(PV-11 끝, K8·K9만, 미체결 0 — 0056724 정정 완료). 보유 005930 1주, 14:30 매도 예정 |
| T-050 관찰 run | 종료(10-18 23:53). 결과 [t050-observation.md](strategies/m2-verify/t050-observation.md) |

## 다음 세션 첫 행동
1. **M2 PAPER-0 첫날 확인**(사용자 보고 또는 M2 DB 읽기 전용 — 실행 중 DB는 `mode=ro`): 10:00 매수·13:00 매도, IOC 체결 여부, 수수료 자산(PV-M2-07), 대조 MATCH, 정지 없음. PV-M2-01~11 관찰을 기록할 곳: 새 `docs/07-paper-review.md` M2 절 또는 별도 문서(사용자와 정함).
2. **CP-040 T-065(수수료 필드)·T-066(분봉 확장) 착수**(A-123 ④, 선행 T-053 병합 완료). 그 뒤 T-067 → T-068(+CP-041 D-275~D-277 — **일봉은 `domain/daily_input.m2_daily_input` 사용**, D-276 문구 정정 필요). CP-039와 동시 진행 금지.
3. 가이드 [paper0-start-guide.md](strategies/m2-verify/paper0-start-guide.md)는 오늘 3번 정정(텔레그램 키 `M2_` 접두, K4 재개 단계, /status·체결 알림). 리셋(K4) 시 `reset-confirm` 안내 준비.

## 사용자 결정 대기
1. (결정됨 A-126) PV-11 **통과**. DP-82 = 결측 시 진입만 막고 청산 계속 + 텔레그램 경고 → **T-068 때 구현**, 그 전 추세 전략 PAPER 차단.
2. (비어 있음)
3. DP-83(M2 이상치 = OHLC 모순까지만, 급변 기준 없음), DP-84(M2 첫 끊김 K5 — M1보다 보수적). 비차단.
4. CP-041 T-071(universe-add)은 보류(A-123 ⑥). T-072·T-058(M2)·T-059는 T-068 뒤.
5. [M2 LIVE 전] B-U6·U-17 계정·약관·세금, 실전 키 IP 제한, 전용 계정 — 사용자 확인.
6. DP-46~DP-77 기존 항목(`docs/decisions-pending.md`).

## 오늘 병합된 것 (main 2a81b73)
- 통합 10-18: T-045~T-051 M2 실행 경로·btc-trend v1 검토·외부 주문 정정 도구·klines 도구 (3444 passed)
- 통합 10-18b: T-052·T-053(A-124 재검토 반영)·crypto-trend v3 재검토·T-073 LF-8 목록 (3544 passed)
- 통합 10-19: V-157 잔여(공용 `m2_daily_input`, V-76·V-112·V-127)·T-070 종목별 시작 규칙 검사 (3580 passed, 1 skipped)
- 사용자가 `git merge --ff-only`로 실행(에이전트의 main 체크아웃·병합은 권한 차단).

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
