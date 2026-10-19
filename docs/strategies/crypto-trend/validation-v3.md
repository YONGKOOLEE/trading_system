# crypto-trend v3 — 운영 공식 검증 기록 (M2, CP-030 M2판 + CP-038 저빈도 예외)

- 실행: 2026-10-19 16~17시 KST, 리드(에이전트), 사용자 실행 승인 A-128 ④. **판정 제안 — 판정·상태 전환은 사용자.**
- 코드: 브랜치 `fix/T-068-zero-length-minute` **dfd3d79**(main d3f0a9a + 길이 0 1분봉 결측 처리). 결과 폴더 `var/backtest/crypto-trend-v3-official-2/`(git 제외)
  - `validation.md` SHA-256 `20179492657bdcb24086c2102677329f2825fd6430aaabaa122959fd0df597bb`
  - `validation.json` SHA-256 `b2eee25a1271bac2abfc64390827bae47ad8dec082aa36f0db8fc7e9c686c5d3`
- 명령(저장소 루트 = 위 코드 체크아웃, 데이터·출력은 main 폴더 `var/`):
  `backtest --market M2 --interval 1m --strategy trading_system.strategy.crypto_trend:CryptoTrendV3 --params config/strategies/crypto-trend-v3-paper.toml --minute BTCUSDT=… --minute ETHUSDT=… --daily BTCUSDT=… --daily ETHUSDT=… --run-id crypto-trend-v3-official-2 --lookback 60 --slippage 0.001 --skip-incomplete-minutes --validate --limits config/m2/limits.verdict.toml --lf8-minute-dir …/binance-public-lf8-1m-20261018 --lf8-daily-dir …/binance-public-usdt-1d-20261018 --out …/var/backtest`
- 조건: PAPER 축소판 + A-064 한도(프로필 m2-paper, CP-042), 60분 판정, 분봉 창 60, 슬리피지 0.1%(민감도 0.2%), 수수료 0.1% 자리값, 2026 이후 행 미사용. 불완전·길이 0 1분봉 결측 처리(BTC·ETH 각 17개, LF-8 표본 심볼별 0~9개 — `incomplete-minutes.json`).
- 앞선 시도 `crypto-trend-v3-official-1`: 전체 이력 실행이 2017-09-06 길이 0 봉에서 거부되어 중단 — 판정표·저빈도 원장 미생성, 부분 결과 미열람.

## 결과 요약
| 창(검증 해) | 거래 | 거래당 기대값(USDT) | 2배 슬리피지 | 수익률 | MDD |
|---|---|---|---|---|---|
| 2020 | 9 | 16.74 | 16.53 | +3.87% | 0.60% |
| 2021 | 7 | 41.58 | 41.35 | +2.91% | 0.99% |
| 2022 | 18 | **−2.88** | −3.07 | −0.52% | 0.58% |
| 2023 | 9 | 5.52 | 5.33 | +1.28% | 0.36% |
| 2024 | 18 | 4.35 | 4.16 | +0.79% | 0.70% |
| 2025 | 15 | 5.25 | 5.05 | +0.79% | 0.41% |

- 일반 판정(04 §2.9): C-2 합산 76건 < 100 → **판단 불가**(C-3 충족 7.65 USDT, C-4 ±20% 변형 모두 > 0).
- 저빈도 적격(D-235): E-1~E-5 충족, 전체 이력 연평균 12.41건(< 16.67).
- LF-0~LF-8 모두 충족(LF-1 76 ≥ 30, LF-2 104 ≥ 50, LF-3 0건 창 0·기대값 양수 창 5/6, LF-4 상위 6건 제외 뒤 합 +3.38, LF-5 하락 창(2022·2025) 방어, LF-6 2배 슬리피지 평균 > 0, LF-7 변형 모두 > 0, LF-8 교차 자산 29심볼 필수 항목 충족 — 배포÷표본 거래당 로그 수익 4.07).
- **종합: 통과(저빈도 예외, CP-038 — 과대 추정 가능)**.

## 주의 (판정 시 고려)
- 과대 추정 가능: BTC·ETH는 사후 선택(C-7), 파라미터는 전략실 탐색 633조합의 고원 가운데. LF-8 표본 대비 배포 심볼 거래당 수익이 약 4배.
- 2022(하락장) 창은 거래당 기대값 음수 — LF-3·LF-5 기준은 충족.
- 변형 p-order_capplus(1회 상한 +20% = 114 USDT)는 L7 100 USDT를 넘어 거래 4건뿐 — 변형 결과가 한도에 막힌 것(전략 민감도 아님).
- 축소판 규모(1회 ~95 USDT)라 수익률 절대값은 작다. 일반 규모 성과는 운영 엔진으로 재현하지 않음(CP-042 D-283).
- 보완 통제(D-238~D-240): PAPER-1 최소 90 UTC일 + 왕복 1건(상한 180일), LIVE 첫 규모 일반의 50% 이하. 이 기록은 PAPER·LIVE 승인이 아니다.
