# 전략 m2-verify v1

<!-- 양식: docs/templates/strategy-template.md (A-015). M1 paper0-verify v2(같은 날 왕복, split_time 분리)를 M2(바이낸스 Spot 테스트넷)에 맞춘 PAPER-0 검증 전략. 기준: CP-024 v0.5(A-066), CP-036 v0.1(A-106) M2 실행 경로 = 브랜치 feat/T-049-m2-ops 406ab6a, 04-validation 2-M2.6, 테스트넷 실측 broker-specs/binance-spot.md §12(2026-10-14) -->

- 전략 ID / 이름: `m2-verify` / M2 PAPER-0 인프라 검증
- 버전: v1 (규칙·파라미터 변경 시 +1 — D-011. 승인 전 값 조정은 v1 안에서 한다)
- 설명: 바이낸스 Spot 테스트넷에서 주문·체결·사용자 스트림·대조·dust·정지가 CP-024·CP-036 설계대로 동작하는지 관찰한다. **수익 목적 아님.** 하루 1회 UTC 01:00에 고정 금액(10 USDT) 매수 신호, 같은 UTC 날짜 04:00 판정에서 전량 매도 신호. 매수 수수료가 기초 자산(BTC)에서 빠지므로 매도 뒤 **dust가 남는 경로(D-102)를 매일 지나간다** — 의도된 관찰 대상.
- 상태(시장별): M2 **확정안 (draft)** — 2026-10-17 에이전트 갱신(A-110 ① 작업 지시: 초안을 새 M2 실행 경로 기준으로 갱신). **미승인. 배포 안 함.** 필요한 승인(서로 별개): ① 이 전략(종목·시각·금액) 승인 — 04 2-M2.6 PAPER-0 시작 조건, 배포 설정의 `verification_approval` A-ID ② T-M2-15 테스트넷 주문 전송 승인 — `orders_approval` A-ID(CP-036 D-204), 지급 BTC 매도(A-106 Q-1 (가)) 포함. 실행 명령은 사용자가 직접(05 D-086). 실전 주문 코드·LIVE 승인과 무관. 전환 이력 `status.md`는 등록 때 만든다.
- 이전 초안: `docs/strategies/m2-paper0-verify/strategy.md`(2026-10-05, 미승인)는 **superseded** 표시(2026-10-17, A-110 ① — 보존, 삭제 안 함). 이 문서의 2026-10-15 초안(브랜치 feat/T-M2-15-verify-strategy 36e4b2d)도 이 판으로 대체.
- 대상 시장: M2
- 유니버스 방식: U-A 고정 목록 — `BTCUSDT` 1개. 출처: 사용자 결정 A-096(2026-10-12, `config/m2/universe.paper.toml`), 테스트넷 존재·규칙값 확인 PV-M2-03(2026-10-14, §12)
- 보유 기간: 예상 약 3시간(같은 UTC 날짜) / 최대 보유: 청산 실패 시 다음 청산 판정(다음 날 UTC 04:00)까지. 보유 규모 상한 = 1회 진입분(≤ 50 USDT)
- 코드: `src/trading_system/strategy/m2_verify.py` (`M2Verify`) / 파라미터: `config/strategies/m2-verify-v1.toml`
- 시험: `tests/scenario/test_tm2_15_m2_verify.py` (오프라인, 네트워크 없음 — 아래 "오프라인 검증")

## 새 M2 실행 경로와의 연결 (CP-036, A-106)
| 항목 | 이 전략이 기대하는 것 | 시스템 쪽 근거 (브랜치 406ab6a) |
|---|---|---|
| 조립 | 실운영·시험이 같은 함수 `build_engine(market="M2")` | `app/wiring.py` `_build_spot_engine` (D-202) |
| 판정 시각 | 배포 설정 `eval_times = ["01:00", "04:00"]`을 **UTC**로 해석, 판정 ID 시장 M2, 주말 포함, 재시작 뒤 같은 시각 재판정 없음 | `SpotProfile.last_at_or_before` (D-201·D-207, V-280) |
| 판정 입력 | 보유 = 청산 가능 수량(LOT_SIZE 내림 — MARKET_LOT_SIZE step 0이므로 D-208), dust·0이면 보유 없음, 수동 보유는 `owned_by_me = False`, 확정 1d 봉만 | `SpotProfile._view`·`_position` (D-207·D-208) |
| 주문 유형·가격 | 전략은 신호만. 의도 생성 시 매수 = IOC 지정가(최근가 × 1.005, tick 내림), 매도 = 시세 정상 시장가 / 지연 IOC 지정가(× 0.995, tick 올림). 신호의 LIMIT/MARKET·가격은 명목값 | `SpotProfile.prepare` → `apply_spot_terms` (D-104·D-120, V-281) |
| 시작 규칙 검사 | `validate_rules(params, LOT_SIZE stepSize, minNotional)` — qty_step = stepSize, notional ≥ 2 × minNotional. 불일치·규칙 없음이면 시작 거부(주문 0) | `app/wiring.spot_rule_problems`, `main._run_m2` (D-209, DP-54) |
| 배포 검사 | `validate_deploy` — split_time 전 판정 정확히 1개, 이후 1개 이상 | `main._strategies` |
| 등록 | `scripts/strategy.py register --mode PAPER --market M2` + M2 PAPER-0 검증 예외(markets M2, S-1, 다섯 능력, l7, 승인 A-ID) | `strategy_tool._open_m2`, `strategy_registry._paper0_m2_problems` (D-212) |
| 주문 전송 | `orders_approved = true` + `orders_approval = "A-숫자"`일 때만. 꺼져 있으면 전략 배포 run은 시작 거부 | `app/startup` 주문 스위치 (D-204) |
| 운영 요청 | `/exit BTCUSDT 1`(지급 BTC) — 소유자 무관 위험 축소, 시세 정상 시장가, LOT 배수 아니면 거부(자동 반올림 없음), L7 미적용 | `SpotProfile.prepare_ops` (D-211, V-290), gate G7 위험 축소 제외(01 D-019) |

## 필요 능력 (D-004)
| CAP-ID | 용도 | M2 상태 (capabilities.md, 브랜치 406ab6a) |
|---|---|---|
| CAP-TRG-01 | 정해진 시각 판정 (UTC 01:00, 04:00) | 검증 중 |
| CAP-DAT-01 | UTC 1d 확정 봉 — 데이터 품질 확인, 최근가 없을 때 청산 기준가 | 검증 중 |
| CAP-ORD-02 | IOC 지정가 매수(가격은 시스템 규칙 D-104), 시세 지연 중 IOC 매도(D-120) | 검증 중 |
| CAP-ORD-01 | 시장가 매도(청산) — M2는 시장가 매도만 | 검증 중 |
| CAP-INS-05 | 크립토 현물(00 v0.5 §2.3) | 검증 중 |

→ "검증 중"이라 BACKTEST 등록은 통과, PAPER 등록은 M2 PAPER-0 검증 예외(D-212, 사용자 승인 A-ID)로만 통과한다(시험 `test_registration_with_real_capabilities_and_m2_paper0_exception`). 다섯 능력은 예외 허용 범위(`PAPER0_M2_CAPABILITIES`)와 같다.
※ capabilities.md M2 열은 브랜치 반영 상태다 — main 병합(A-109 ①) 뒤 M2 체크아웃 갱신 때 적용된다.

## 필요 데이터
| 종류 | 주기 | 워밍업(과거 구간) | 수정주가 여부 |
|---|---|---|---|
| 최근가(테스트넷, D-116, 공급 스레드 D-206) | 판정 시 | — | — (신선도 60초는 게이트 G11, A-064) |
| 1d klines(테스트넷) | 일(UTC 00:00 경계, 확정 봉만 — V-142) | 없음(어제 확정 봉 1개 — 없으면 data_ok False) | 해당 없음(크립토) |

## 판정 시점
- 트리거 CAP-TRG-01, **UTC 01:00(KST 10:00) = 진입 판정**, **UTC 04:00(KST 13:00) = 청산 판정**. `split_time` = UTC 02:30(두 시각 사이). 배포 설정 `eval_times`는 M2에서 UTC로 해석된다(D-207 — `config/app_file.py` 설명의 "KST"는 M1 기준).

| 고려 | 01:00 / 04:00 UTC가 맞는 이유 |
|---|---|
| 주문 허용 창 UTC 00:10~23:50 (L8, A-064) | 둘 다 창 안 |
| 일 경계 UTC 00:00 대조·00:10 일일 보고 (D-108·D-109) | 경계 처리 50분 뒤 진입. 진입·청산이 **같은 UTC 날짜** → L4·L5·기준 금액이 한 날짜에 묶이고 왕복이 경계를 넘지 않음(V-140) |
| M1 paper0-verify 판정 KST 09:30·14:30 | 30분·90분 떨어짐 — 두 시장 알림이 섞이지 않음 |
| 사용자 부재(평일 07:15~저녁, 텔레그램만) | 사람 조작이 필요 없는 규칙. 이상 시 시스템 정지는 저녁에 확인해도 위험 확대 없음. KST 13:00은 점심 시간이라 결과 알림 확인 가능성 |
| 실행 창 | M2 프로세스가 **KST 10:00 전부터 13:10 이후까지** 떠 있어야 한다(A-106 Q-2 (가) 수동 run: 출발 전 시작). 꺼져 있으면 그 판정은 없다(주문 0) |
| 보유 3시간 | 사용자 스트림 체결 통보·60초 주기 대조를 보유 상태에서 여러 번 관찰. 밤샘 보유 없음 |

## 규칙 표 (§2.2)
우선순위 필드는 같은 종류 규칙끼리의 순서만 정한다. 종류 간 순서는 청산 > 감액 > 증액 > 진입으로 고정(D-006).

| 규칙 ID | 종류 | 입력 | 판정 시점 | 조건 | 행동 | 무효 조건 | 우선순위 |
|---|---|---|---|---|---|---|---|
| EXIT-1 | EXIT | 상태 입력 | CAP-TRG-01, 판정 시각 ≥ split_time (04:00) | 이 전략 소유 보유(청산 가능 수량 > 0, dust 아님) AND 미체결 없음 | 청산 가능 수량 전량 매도. 유형·가격은 시스템: 시세 정상 = 수량 지정 시장가, 지연 = IOC 지정가 | 데이터 품질 실패, 미체결 있음, 기준가(최근가·확정 봉 종가) 없음 | 1 |
| ENTRY-1 | ENTRY | CAP-DAT-01, 상태 입력 | CAP-TRG-01, 판정 시각 < split_time (01:00) | 보유 없음(dust는 없음으로 봄) AND 미체결 없음 AND 심볼 상태 이상 없음 AND 최근가 있음 | S-1 `notional` USDT 상당 매수. 유형·가격은 시스템: IOC 지정가(최근가 × 1.005, tick 내림) | 데이터 품질 실패, 미체결 있음, 최근가 없음, 심볼 상태 이상, **수동(manual)·다른 전략 보유 있음**(INV-16), 내림 수량 0 | 1 |
- 진입 판정에서는 청산하지 않고, 청산 판정에서는 진입하지 않는다(M1 v2 교훈). 청산 실패 다음 날: 01:00 아무것도 안 함 → 04:00 청산 → 그다음 날 정상.
- 진입 판정은 하루 1번뿐(`validate_deploy`) → IOC가 체결 0으로 만료돼도 **그날 재매수 없음**. 같은 판정 재실행은 결정적 판정 ID·멱등키·재판정 방지(T-044)로 차단(시스템).
- 수동 보유·다른 전략 보유가 있으면 두 규칙 모두 아무것도 하지 않는다(게이트 G6과 같은 판단을 전략 단계에서 미리). **테스트넷 지급 BTC 1개가 남아 있으면 이 전략은 매일 아무것도 하지 않는다** → 아래 절차 3단계에서 사람이 `/exit`로 판다(A-106 Q-1 (가)).

## 규칙 ↔ PV 대응 (04 2-M2.6)
| PV | 확인 | 이 전략에서 | 규칙 |
|---|---|---|---|
| PV-M2-01 | rateLimits·응답 헤더 사용량 | 매일 주문 2건 + 조회·대조 호출 | ENTRY-1·EXIT-1 |
| PV-M2-02 | 접수 직후 고객 주문 ID 조회(-2013 지연) | 매일 매수·매도 각 1건 | ENTRY-1·EXIT-1 |
| PV-M2-03 | 심볼·규칙값·호가 유동성 | 규칙값 확인 완료(§12). **유동성(IOC 체결률·체결가 − 주문가)** 은 매일 매수와 지급 BTC 1 시장가 매도로 관찰 | ENTRY-1, 운영 /exit |
| PV-M2-04 | 키 권한·IP 제한 화면 | 해당 없음(사용자 화면 확인) | — |
| PV-M2-05 | 사용자 스트림 재연결 누락 | 24시간 전 교체(D-114) 전후의 체결 통보 누락 여부를 대조로 관찰 | (간접) |
| PV-M2-06 | HMAC 키 `userDataStream.subscribe.signature` | 체결 통보(executionReport) 수신 자체(T-050에서 먼저 관찰) | ENTRY-1·EXIT-1 |
| PV-M2-07 | 테스트넷 수수료 적용·수수료 자산 | 매수 = BTC 차감?, 매도 = USDT 차감? 매일 2건 | ENTRY-1·EXIT-1 |
| PV-M2-08 | 소수 문자열 표기·**MARKET_LOT_SIZE step 0의 의미** | 매수 수량 5자리(0.00016)·가격 2자리, 시장가 매도 수량(LOT_SIZE 내림 0.00015) 수용 여부 | ENTRY-1·EXIT-1 |
| PV-M2-09 | 고객 주문 ID 길이·문자 | 매 주문 | ENTRY-1·EXIT-1 |
| PV-M2-10 | 리셋 실제 증상 | 기간 중 발생할 때만(미발생이면 미실행 기록) | — |
| PV-M2-11 | 체결 0 EXPIRED IOC 조회·allOrders 지연 | 발생 시 관찰. **전략이 일부러 만들지 않음** | ENTRY-1 (발생 시) |
| (V 관찰) | V-129 순 수령·V-131/V-160 dust·V-136 IOC 부분 체결·V-143 USDT 대조·V-153 일 경계 | 매일 왕복 + dust 잔량 + 다음 날 재진입 | ENTRY-1·EXIT-1 |

## 파라미터 (R-19, 값은 config/strategies/m2-verify-v1.toml)
| 이름 | 타입 | 단위 | 허용 범위 | 설명 |
|---|---|---|---|---|
| notional | decimal | USDT | 10 ~ 50 | 1회 진입 목표 금액. v1 = **10** (제안). 하한 10 = minNotional 5의 2배(아래 증명) |
| qty_step | decimal | BTC | 0.00000001 ~ 1 | 수량 내림 단위 = LOT_SIZE stepSize. v1 = **0.00001** (§12 실측). 시작 때 거래소 값과 다르면 시작 거부(`validate_rules`) |
| split_time | str | HH:MM (UTC) | 두 판정 시각 사이 | v1 = **02:30**. 이 시각 전 판정은 진입만, 이후는 청산만 |

배포 설정(사용자가 M2 체크아웃 `config/m2/app.paper.toml`에 직접 추가 — 추적 파일을 에이전트가 바꾸지 않음, D-216):
```toml
[[strategies]]
module = "trading_system.strategy.m2_verify:M2Verify"
params = "config/strategies/m2-verify-v1.toml"
eval_times = ["01:00", "04:00"]          # UTC (KST 10:00·13:00)
verification_approval = "A-<전략 승인 ID>"
```

## 신호 유효 기한 (§2.6)
- 기한 산정 규칙: 판정 기준 시각 + 10분 / 절대 상한: 10분

## 미체결 정책
- CAP-ORD-03 미선언. 매수(IOC)·지연 중 매도(IOC)는 미체결이 남지 않는다(미체결분 EXPIRED, D-104). 정정 없음(M2 미지원). 1분 넘게 열린 주문은 시스템 알림(D-109).

## 포지션 크기 (§2.8)
- 방식: **S-1 고정 금액**, 파라미터 `notional`. 기준가 = 판정 시 최근가 `P`.
- **반올림 규칙**: 수량 = floor(notional ÷ P ÷ qty_step) × qty_step (`domain.sizing.size_fixed_amount`, ROUND_FLOOR, Decimal). 결과 0이면 신호 없음. 주문 가격은 시스템이 최근가 × 1.005를 tickSize 0.01로 **내림**(D-104). 매도 수량은 판정 입력이 LOT_SIZE stepSize로 **내림**(D-208, MARKET_LOT_SIZE step 0).
- 최소 주문 금액: 수량 = k × step(k ≥ 1)이면 k > (notional ÷ P ÷ step) ÷ 2 이므로 **k × step × P > notional ÷ 2 ≥ 5** (notional ≥ 10). 매수 명목가는 주문가 기준이라 더 크다 → G10 매수 경계(명목가 ≤ 5 거부)에 걸리지 않음. 시험 `test_floored_order_value_stays_above_min_notional`.
- 예(가격은 **가상**): P = 60,000 → 0.00016 BTC, 주문가 60,300.00, 수수료 0.1%(가상)면 순 수령 0.00015984 → 매도 0.00015 → dust 0.00000984 / P = 100,000 → 0.0001 / P > 1,000,000 → 0(신호 없음).

## 전략별 리스크 한도 (계좌 전체 한도 이하, R-20)
| 항목 | 값 | 계좌 한도(config/m2/limits.paper.toml, A-064)와 확인 |
|---|---|---|
| 전략 L7 1회 주문 최대 | **60 USDT** (notional 상한 50 × 1.005 = 50.25 위 여유) | 계좌 L7 100 이하 ✓ |
| L1 | 손절가 없음 → L1 = 주문 금액 전체(01 D-020). 계좌 L1 0.5% → 기준 금액 약 2,010 USDT 이상이면 통과. 테스트넷 USDT 10,000(§12) ✓ | — |
| L2·L3 | 계좌 L2 10%·L3 50% — 지급 BTC 1개를 팔기 전에는 초과 가능하나, 그때는 전략이 진입하지 않는다(수동 보유) | — |
| L5 | 하루 신규 주문 2건(매수 1·매도 1) ≤ 5 ✓ | — |

## 하지 않는 것
- 가격 지정·지정가 대기·정정·손절·익절·분할 매매·증액·감액 — 없음.
- 수동 보유(지급 BTC 포함)·dust의 정리나 청산 — 하지 않음(dust는 원장·대조에 남김, D-102 — 다음 매수분과 합쳐져 다음 매도 때 단위 이상분이 같이 팔린다. 지급 BTC 매도는 사람의 운영 요청).
- 같은 날 재매수, 체결 0 IOC의 재전송, 실패한 매도의 자동 재시도 — 없음.
- 시세·지표 기반 판단, 수익 추구, PAPER-1·LIVE 투입, 일부러 장애 만들기 — 없음.

## 백테스트 통과 기준 (04-validation §2.7) — 미결
| 항목 | 값 |
|---|---|
| A-029 증거(이 시스템 백테스트 1회 오류 없는 실행 기록) | **미결 Q-1**: `app/backtest_m2.py`는 하루 판정 시각 1개(`eval_time`)만 지원해 01:00 진입·04:00 청산 왕복을 실행하지 못한다. 상태 전이 BACKTEST_PASSED에는 증거 파일이 필요(`strategy.py transition --evidence`) |
| 최소 거래 수 / 성과 기준 / 벤치마크 | 해당 없음(검증 전략, 실행 기록만) |
| 체결 가정·구간 분리 변경 | 없음 (04 2-M2.2 M2 기본 가정) |

## 오프라인 검증 (2026-10-17, 네트워크 없음 — `tests/scenario/test_tm2_15_m2_verify.py`)
| 항목 | 내용 | 결과 |
|---|---|---|
| 규칙·수량·배포 검사 | 가상 스냅샷, §12 실측 규칙값 | 통과 |
| validate_rules (DP-54) | 실측값 통과 / stepSize 0.0001·minNotional 6이면 사유, 시작 호출부 `spot_rule_problems` 경유 (규칙 없음 → 거부) | 통과 |
| 등록 | 실제 capabilities.md: BACKTEST 통과, PAPER 승인 A-ID 없으면 거부·있으면 통과, `strategy_main register --mode PAPER --market M2` → M2 DB에 m2-verify v1 DRAFT(시장 M2) | 통과 |
| 시작 (`run --market M2` BACKTEST fake) | 실측 형태 규칙 → 시작·K8·주문 0 / qty_step 불일치 → 시작 거부·DB 없음 | 통과 |
| 하루 왕복 (`build_engine(market="M2")`, fake 현물 브로커, 실제 M2 PAPER 한도 파일) | UTC 01:00 IOC 매수 0.00016 @ 60,300.00 → 순 수령 0.00015984 → UTC 04:00 시장가 매도 0.00015 → dust 0.00000984·소유권 해제 → 다음 날 01:00 재매수, 불변 조건 위반 0 | 통과 |
| IOC 체결 0 | EXPIRED, 예약 해제, 그날 청산·재매수 없음 | 통과 |
| 지급 BTC (A-106 Q-1) | 기준선(수동 보유)만 하면 주문 0 / 시작·K8 재개 → `EXIT BTCUSDT 1`(원격) → 시장가 매도 1 → 보유 0 → 01:00 전략 매수 | 통과 |
- 가격·잔고·수수료율(0.1%)은 가상값이다. 테스트넷 실제 체결·수수료·유동성은 검증하지 않았다(PAPER-0 관찰 대상).

## PV-M2 실행 절차 (T-M2-15 — 사용자 실행, 승인 뒤에만)
전제: (a) 이 전략 승인 A-ID와 T-M2-15 주문 전송 승인 A-ID (b) M2 실행 경로 main 병합(A-109 ①)과 `m2/run` 갱신(사용자 지시) (c) T-050 관찰 run 결과 기록(PV-M2-12) (d) M2 텔레그램 봇 준비(A-106 Q-3, A-109 ③) — `/exit`는 텔레그램으로만 낼 수 있다(로컬 EXIT 도구 없음) (e) V-157 잔여 4개 처리(CP-036 §8 차단 행) (f) 아래 Q-1 결정. 모든 명령은 M2 체크아웃에서 PowerShell로, 파이썬은 M1 체크아웃의 가상환경을 쓴다(`scripts/run-m2.py` 설명과 같음).

**1. 설정 (사용자가 파일을 직접 편집, 한 번)** — `C:\AI\trading_system\m2-binance\config\m2\app.paper.toml`
- `[paper]`에 `connection_approved = true`(T-050 때 켰으면 그대로), `orders_approved = true`, `orders_approval = "A-<T-M2-15 주문 승인 ID>"` 추가.
- 파일 끝에 위 "배포 설정" `[[strategies]]` 블록 추가(`verification_approval` = 전략 승인 ID).
- 기대: 다음 단계 명령에서 설정 오류 거부가 없음. 주문 스위치가 꺼진 채 전략을 배포하면 run이 시작을 거부한다(D-204).
- `orders_approval`의 A-ID는 **M2 체크아웃의 `docs/approvals.md`에 행으로 있어야** 시작된다(DP-66) — 승인 기록이 main에 커밋된 뒤 `m2/run`을 갱신해야 한다(사용자 지시).

**2. 등록·상태 전이 (엔진 정지 중)**
```powershell
cd C:\AI\trading_system\m2-binance
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py register --mode PAPER --market M2
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py transition --mode PAPER --market M2 m2-verify 1 BACKTEST_PASSED --approval A-<전략 승인 ID> --evidence docs/strategies/m2-verify/backtest-v1.md
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py transition --mode PAPER --market M2 m2-verify 1 PAPER --approval A-<전략 승인 ID>
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py show --mode PAPER --market M2
```
- 기대: `등록 통과: m2-verify:1 (PAPER 대상, PAPER-0 검증 승인 A-…)`, 마지막 `show`에 `m2-verify v1 M2 PAPER`. 다른 시장 DB면 `거부(아무것도 쓰지 않음)`(종료 코드 2).
- 두 번째 줄의 증거 파일 `backtest-v1.md`는 **아직 없다** — Q-1 결정 뒤 만든다. 없으면 `전이 거부: 증거 파일 없음`으로 끝나고 아무것도 바뀌지 않는다.

**3. 기준선 확정 (엔진 정지 중, 처음 한 번 — T-050에서 이미 했으면 건너뜀)**
```powershell
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\reconcile-resolve.py initial-baseline --mode PAPER --market M2
```
- 기대: `initial-baseline 완료: USDT 기준선 <테스트넷 USDT free+locked>, 유니버스 보유 {'BTCUSDT': '1'} (수동 보유로 기록)`. 이미 기준선이 있으면 거부 문구에 `reset-confirm`이 나온다 → 정상(건너뜀).
- run 시작 출력·대조에 `QUOTE_BASELINE_MISSING`(기대 USDT 모름)이 보이면: run을 정지(아래 7단계) → 이 명령 → 다시 4단계.
- 테스트넷 리셋(약 월 1회, K4·잔고 재지급) 뒤에는 `initial-baseline` 대신 `reset-confirm`(같은 형식) → 지급 BTC가 다시 수동 보유가 되므로 5단계 `/exit`를 반복한다.

**4. run 시작 (출발 전, KST 07:15 전)**
```powershell
cd C:\AI\trading_system\m2-binance
$env:TRADE_MODE='PAPER'
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\run-m2.py
```
- 기대: `주문 전송 스위치: 주문 전송 허용 (승인 A-…, D-204)`, `바이낸스 서버 시각 오차 …ms`, `시작: 시장 M2, 모드 PAPER, 계좌 BN-PAPER-01, 복구 판정 통과 — K8 재개: …`. 이 창은 닫지 않는다. 시작 직후는 항상 K8(주문 없음).

**5. K8 재개 → 지급 BTC 매도 (시작 직후, KST 10:00 전에)**
- 텔레그램(M2 봇): `/resume K8` → 회신의 `/confirm <코드>`를 60초 안에 보냄. 또는 PC에서 다른 PowerShell 창:
```powershell
cd C:\AI\trading_system\m2-binance
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\resume.py --mode PAPER --market M2 K8
```
- 텔레그램: `/status`로 BTCUSDT 1 수동 보유 확인 → `/exit BTCUSDT 1` → `/confirm <코드>`. 기대: 시장가 매도 1건 체결 알림, 이후 보유 0. KST 10:00 전에 못 하면 그날 전략은 아무것도 하지 않는다(안전 — 다음 날 전에 하면 됨).
- 부분 체결로 BTC가 남으면 남은 수량(LOT 0.00001 배수)으로 `/exit`를 다시 낸다(배수가 아니면 거부 — 자동 반올림 없음).

**6. 매일 확인**
- KST 10:00 무렵 매수 체결 알림(IOC — 체결 0이면 EXPIRED 알림, 그날 매수 없음), KST 13:00 무렵 매도 체결 알림. 텔레그램 `/status`, `/review`.
- 저녁(PC): `C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\status.py --mode PAPER --market M2` — 활성 정지 없음, 결과 불명 없음, 대조 MATCH, BTCUSDT 잔량이 dust(0.00001 미만)인지.
- 기록할 것: 위 "규칙 ↔ PV 대응"의 관찰값(체결가 − 주문가, 수수료 자산, 시장가 매도 수량 수용 여부(PV-M2-08), 스트림 교체 전후 누락), 정지·알림·운영 개입.

**7. 정지**
- 가능하면 보유가 없는 시간(KST 13:10 이후)에: run 창에서 Ctrl+C, 또는 다른 창에서
```powershell
New-Item -ItemType File C:\AI\trading_system\m2-binance\var\paper\STOP
```
- 긴급: 텔레그램 `/kill [사유]`(K1 — 신규 진입 차단, **자동 청산 아님**). 보유 중 정지하면 다음 run의 04:00 판정까지 보유가 남는다. 다시 시작하면 4·5단계(K8 재개, 지급 BTC는 이미 팔았으면 생략).

## 실패 국면 가설 (§2.12)
| 국면 | 판별 지표 | 예상 실패 양상 | 운영 대응 |
|---|---|---|---|
| 테스트넷 유동성 부족 | IOC 체결 0·부분 체결 | 매수 EXPIRED 반복, 지급 BTC 1 매도 부분 체결 | 정상 동작으로 기록. 계속되면 심볼 교체는 사용자 |
| 시장가 매도 수량 규칙 | 매도 -1013 | D-208(LOT_SIZE 내림)이 거래소 의미와 다르면 매도 거부 → 보유 지속(dust 확정 경로 D-102 (나)) | 첫날 매도 결과로 PV-M2-08 판정, 다르면 CP |
| 테스트넷 리셋 | 대조 불일치 → K4 | 지급 BTC 재등장 → 수동 보유 → 진입 없음 | `reset-confirm` → PAPER 기간 재시작 → `/exit` 반복 |
| 시세 지연 | 시세 수신 > 60초 | 매수 없음(G11), 매도는 IOC 지정가 → 미체결 시 다음 날 | 기록, 지속 시 K10 알림 확인 |
| 프로세스 미가동 | 판정 기록 없음 | 그 시각 판정 없음(주문 0) | 사용자 재시작, K8 재개 |

## 가정 · 미확인 · 착수 차단 (T-M2-15를 실제로 돌리기 전)
| ID | 구분 | 내용 | 차단 여부 | 확인 주체 |
|---|---|---|---|---|
| B-1 | 결정됨 | 지급 BTC 1개 → A-106 Q-1 (가): 시작 직후 `/exit`(절차 5단계), 리셋 뒤 반복. 이 매도는 T-M2-15 주문 전송 승인 범위에 넣는다 | 해소(승인 범위 명시 필요 — Q-3) | 사용자 |
| B-2 | 해소(브랜치) | M2 실행 경로 — CP-036 T-045~T-049(406ab6a). main 병합·`m2/run` 갱신은 A-109 ① 순서대로 | 병합 전 **차단** | 사용자 지시 |
| B-3·B-4·B-7 | 해소(브랜치) | capabilities M2 열·CAP-INS-05, M2 PAPER-0 예외, `--market M2` 등록(D-212) | 병합 전 **차단** | — |
| B-5 | 승인 | 이 전략 승인 + T-M2-15 주문 전송 승인(별개). 실전 주문 코드·LIVE 승인 아님 | **차단** | 사용자 |
| B-6 | 결정됨 + 미확인 | D-208(A-106 Q-4 (가)): step 0이면 LOT_SIZE 내림. **MARKET_LOT_SIZE stepSize 0의 거래소 의미는 미확인 — PV-M2-08** 실측이 다르면 CP | 비차단 | PV-M2-08 |
| B-8 | 해소(브랜치) | 시작 시 `validate_rules` 호출(D-209, DP-54) | — | — |
| B-11 | 미결 | A-029 백테스트 실행 기록(BACKTEST_PASSED 증거) — backtest_m2 판정 시각 1개 한계(Q-1) | **차단**(상태 PAPER 전이) | 사용자 |
| B-12 | 선행 | T-050 관찰 run 결과(PV-M2-12), V-157 잔여 4개(V-02·V-76 부분, V-112·V-127) | **차단** | 사용자 + 구현 |
| B-9 | 가정 | KST 10:00·13:10 사이 PC·M2 프로세스 가동, 주말 포함 | 비차단 | 사용자 |
| B-10 | 미확인 | **테스트넷 수수료 적용·수수료 자산(B-U10)** — 미승인 수수료 자산이면 K4(D-101) | 비차단 — PV-M2-07 | PAPER-0 관찰 |
| B-13 | 미확인 | **테스트넷 BTCUSDT 시세 수준·호가 유동성(B-U4 잔여)** — IOC 체결률, 1 BTC 시장가 매도 체결 수준 | 비차단(테스트넷 가상 자산) | PV-M2-03 |
| B-14 | 미확인 | HMAC 키 사용자 스트림 구독(B-U9, PV-M2-06) — T-050에서 먼저 관찰 | T-050 결과로 판단 | T-050 |

## 사용자 결정 (제안 기본값 — 승인 때 확정)
| 항목 | 제안 기본값 | 허용 범위·조건 |
|---|---|---|
| ① 매수 시각 | **UTC 01:00 (KST 10:00)** | UTC 00:10 이후, 하루 1개 |
| ② 매도 시각 / 보유 | **UTC 04:00 (KST 13:00) / 3시간** (`split_time` 02:30) | 매수 뒤 같은 UTC 날짜, 23:50 전 |
| ③ 1회 금액 | **10 USDT** | 10 ~ 50 USDT (minNotional 5의 2배 이상, 전략 L7 60 이하) |

## 다음 단계 입력
- 사용자: 위 3가지 확정, 질문 Q-1~Q-3, 전략 승인 A-ID, T-M2-15 주문 전송 승인 A-ID(별개).
  - Q-1 (차단) BACKTEST_PASSED 증거: (가) backtest_m2에 판정 시각 복수화(오프라인 작은 작업) 후 A-110 ② 공개 일봉(최종 확인 구간 2026-01-01 전)으로 1회 실행 → `backtest-v1.md` [권장] / (나) 이 문서의 오프라인 엔진 왕복 시험 기록을 이 검증 전략의 A-029 증거로 인정(규칙 해석 — 사용자 명시 결정 필요)
  - Q-2 (비차단) 제안 기본값(01:00/04:00 UTC, 10 USDT, split 02:30) 그대로인지
  - Q-3 (차단) T-M2-15 주문 전송 승인 범위에 "지급 BTC `/exit`(부분 체결 시 남은 수량 재요청, 리셋 뒤 반복 포함)"를 명시할지
- 구현(별도 지시): Q-1 (가)를 고르면 backtest_m2 복수 판정 시각 작업.
- 관련 ID: T-M2-15, A-110 ①, A-106(CP-036 D-201~D-212·Q-1·Q-4), A-109, 04 2-M2.6 PV-M2-01~12, CP-024 D-099·D-101~D-105·D-108·D-109·D-116·D-118·D-120, DP-54, A-062~A-066·A-096, V-129·V-131·V-136·V-140·V-142·V-143·V-153·V-160·V-280~V-283·V-289·V-290, INV-16

## 변경 이력
| 버전 | 일자 | 내용 | 상태 |
|---|---|---|---|
| v1 초안 | 2026-10-15 | 최초 작성(에이전트, 브랜치 feat/T-M2-15-verify-strategy 36e4b2d). 코드·파라미터·오프라인 시험 포함, 배포 안 함 | draft (미승인) |
| v1 확정안 | 2026-10-17 | A-110 ①: 새 M2 실행 경로(CP-036, 406ab6a) 기준 갱신 — CAP-INS-05 선언, 실행 경로 연결 표, 등록(D-212)·시작 규칙 검사(D-209)·지급 BTC(A-106 Q-1 (가)) 반영, 하루 왕복 시험을 `build_engine(market="M2")`로 교체, PV-M2 실행 절차, 미확인·차단 갱신. 이전 초안 m2-paper0-verify superseded 표시. 규칙·파라미터 값 변경 없음 | draft (미승인) |
