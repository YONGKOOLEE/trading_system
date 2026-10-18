# T-050 테스트넷 관찰 run 결과 (PV-M2-12) — M2 PAPER-0 전제 (c)

- 기록: 2026-10-18 (세션 50). 근거: 정지 뒤 `C:\AI\trading_system\m2-binance\var\paper\trading.db`(23:53:02 KST 마지막 기록)를 작업용 임시 폴더로 **복사해** 읽기 전용 URI로 조회, 사용자가 붙여 넣은 run 창 경고 4줄. 실행 DB 원본에는 쓰지 않음.
- 실행: m2-binance `m2/run2`(406ab6a), 사용자 시작 2026-10-17 20:27 KST → 사용자 Ctrl+C 2026-10-18 23:53 KST(약 27시간). 주문 스위치 꺼짐(`ORDER_SWITCH orders_enabled false`, D-204), 배포 전략 없음 → **주문 0**.

| 관찰 | 결과 | 근거 |
|---|---|---|
| 사용자 스트림(HMAC, PV-M2-06) | 시작 직후 구독, **23시간 뒤 1회 교체**(10-18 19:27 KST) — 두 번 모두 STREAM_RECONNECT 대조 실행, K5 없음 | `audit_event` RECONCILE STREAM_RECONNECT 2건(11:27:06Z·10-18 10:27:07Z), `halt`에 K5 행 없음 |
| 주기 대조 | 1분마다 1,636회 + 시작 1 + 날짜 경계 1 — 모두 MISMATCH(항목 2) | RECONCILE 1,640건 |
| 대조 불일치 2건 | ① QTY_MISMATCH BTCUSDT 내부 0·거래소 1(테스트넷 지급 BTC) ② QUOTE_BASELINE_MISSING USDT 10000(기준선 없음) — **`initial-baseline` 미실행이 원인, 설계대로**(D-099·D-210) | `recon_item` OPEN 2 |
| 정지 | K8(시작 복구 대기)·K4(시작 대조 불일치 2건) 유지 — 해제 시도 없음 | `halt` |
| 시계 | 165회 모두 OK, 최대 오차 0.041초 | `clock_check` |
| 일일 보고 | 10-17·10-18 UTC 일 경계에 생성(기준 금액 없음 → 비율 생략) | ALERT INFO 2 |
| 알림 | 긴급 3(시작 K8·K4·불일치), 경고 1 + 매시간 "불일치 미해소" 경고 | ALERT 33 |

- 판정(에이전트 제안 — 확인은 사용자): PAPER-0 전제 (c) **충족** — 연결·사용자 스트림 구독·교체·주기 대조·시계·일일 보고가 27시간 동안 오류 없이 동작. 불일치 2건은 기준선 미설정에 따른 예상 결과이고 가이드 §4-5(`initial-baseline`)로 해소된다.
- 다음: `initial-baseline`은 열린 항목을 BASELINE_CONFIRMED로 닫지만 **K4는 풀지 않는다**(`app/m2_baseline.py:12`) → run 시작 뒤 대조 일치 확인 → `/resume K4` → `/resume K8`(가이드 §4-7 갱신).
