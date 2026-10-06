# PAPER-0 2일차(2026-10-07) 결과 분석 — 읽기 전용 조회 목록

- 버전: v0.1
- 상태: draft (작업 도구 문서 — 승인 대상 아님. 분석 결과는 이 문서가 아니라 07-paper-review·kiwoom-kr에 기록)
- 작성일: 2026-10-07
- 인도 차수: M1 (국내주식, 키움 모의, 계좌 별칭 `KR-PAPER-01`)
- 선행 문서: `docs/handoff.md` "다음 세션 첫 행동" 2번, `docs/07-paper-review.md` v0.1, `docs/broker-specs/kiwoom-kr.md` §7c, `docs/strategies/paper0-verify/pv-procedure.md` v0.4, worklog (31)~(34)
- 관련 ID: PV-01~16(04 §4.1), T-017(F-1~F-5), K-U14, D-034·D-045(대조), DP-31(예수금)
- 표기: 시각은 KST. 저장값은 UTC ISO 8601(`...Z` 또는 `+00:00`)이므로 조회 코드가 +09:00으로 바꿔 출력한다

## 1. 목적과 입력
2일차 저녁에 T-017 수정(F-1~F-5)이 PAPER에서 동작했는지와 2일차 운영 사건을 **쓰기 없이** 확인한다. 판정(합격·불합격)은 하지 않는다. 사실만 모아 07-paper-review v0.2에 옮긴다.

| 입력 | 위치 | 쓰는 방법 |
|---|---|---|
| PAPER 저장소 | `var\paper\trading.db` | **읽기 전용 URI `file:var/paper/trading.db?mode=ro`로만 연다.** 쓰기·`Repository` 객체 생성 금지(열 때 스키마 생성 SQL을 실행하므로) |
| 운영 로그 | `var\paper\logs\ops-20261007.jsonl` | 알림 등급별 건수만 집계(§3 j). 메시지 원문은 문서에 옮기지 않는다 |
| 집계 결과 | `var\paper\review\review-20261007-*.md` (저녁 `paper-review` 실행분) | 누적 집계(1일차 포함)다 — 2일차 값은 아래 조회로 따로 본다 |
| `status` 출력 | 사용자가 저녁에 실행한 창 | 해제되지 않은 정지·열린 대조 불일치·보유 확인 |
| 텔레그램 기록 | 사용자 휴대폰 (사용자 제공) | `/status`·`/kill` 보낸 시각과 회신 내용. DB에 없는 항목(§6) 보완 |

- 실행 위치: `C:\AI\trading_system\trading_system` (저장소 루트, PAPER 실행 체크아웃)
- 실행 시점: 매매 프로그램(`run`)을 Ctrl+C로 끈 뒤 권장. 실행 중이어도 읽기 전용이라 쓰지 않지만, 장중 기록이 계속 늘어난다
- 금지: `.env*` 파일 열기, 키움·텔레그램 접속, DB 쓰기. 출력에는 계좌 별칭만 나온다(계좌번호는 DB에 저장되지 않음)

## 2. 실행 방법
1. §4의 파이썬 코드를 파일로 저장한다.
   - 에이전트: 세션 scratchpad에 `day2_query.py`로 저장 (저장소 안에 두지 않는다)
   - 사용자가 직접 할 때: 메모장에 붙여 `%TEMP%\day2_query.py`로 저장 (인코딩 UTF-8)
2. PowerShell에서 실행한다.
   ```
   cd C:\AI\trading_system\trading_system
   .venv\Scripts\python $env:TEMP\day2_query.py 2026-10-07
   ```
   - 일부 항목만: `.venv\Scripts\python $env:TEMP\day2_query.py 2026-10-07 a b f` (항목 키 a~k)
   - 다른 거래일: 날짜만 바꾼다(예: `2026-10-06`). 거래일 거르기는 KST 0시~24시 = UTC 전날 15:00~당일 15:00
3. 기대 결과: 첫 줄 `DB var\paper\trading.db (읽기 전용), 거래일 2026-10-07, ...` 뒤에 `=== (a) ...` ~ `=== (k) ...` 절이 나온다. DB가 없으면 `DB 없음: ...`으로 끝난다(빈 DB를 만들지 않음).

## 3. 확인 항목별 판정 기준
"정상/이상"은 T-017 수정 의도·2일차 절차(pv-procedure v0.4 §3.2·§3.4) 대비 관찰 판정이다. PV 합격 판정은 사용자가 PAPER-0 종료 시 한다.

| 키 | 확인 항목 (handoff) | 볼 것 (§4 출력) | 정상 | 이상 → 할 일 |
|---|---|---|---|---|
| a | 시작 대조가 1일차 열린 항목을 AUTO_MATCHED로 종결 | PREFLIGHT·STARTUP 대조 행의 `자동종결`, 대조 항목 표, 감사 `RECON_ITEM_AUTO_RESOLVED` | 1일차 `MISSING_FILL 005930` 항목이 `RESOLVED AUTO_MATCHED reconciler`, 종결 시각이 그날 **첫 대조**(06:4x PREFLIGHT일 가능성이 큼 — preflight도 같은 대조기를 쓴다) 이후·09:30 전. 감사 `basis`=`주문별 체결 수량 일치 (K-U14)`, 브로커합=내부=1. 이후 STARTUP `MATCH`, `이전열린=0` | 항목 `OPEN` 유지, 또는 method가 사람 해소(`reconcile-resolve` 경로) → "자동 종결 미동작"으로 기록, 실행 코드 버전(브랜치·HEAD) 확인. STARTUP `MISMATCH` |
| b | 09:30 v1 매도 체결 뒤 K4 재발 없음 (K-U14 수정) | `K4 발동` 줄, `이날 새로 생긴 대조 항목`, 대조 결과 집계 | 2일차 K4 발동 0, 새 대조 항목 0, PERIODIC `MISMATCH` 0. 매도 체결의 체결번호 ≠ 주문번호여도 MATCH ((g)에서 두 값 확인) | K4 발동·`MISSING_FILL`/`EXCESS_FILL` 새 항목 → 항목 `kind`·시각·(g)의 체결 행과 대조, F-1 재발로 기록(T-017 main 병합 보류 근거) |
| c | VI `_AL`/`_NX` 해석 실패 경고 사라짐 | `ALERT '통보 해석 불가' n건, 그중 9001 n건`, 반복 억제 요약 합 | 9001 해석 불가 **0건** (1일차 85건). 다른 TR의 해석 불가는 따로 기록 | 9001 경고 계속 → 실행 코드가 수정본인지 확인. 새 접미사·형식이면 kiwoom-kr §7c에 형식만 기록 |
| d | 불일치 알림 반복 없음 (긴급 1회·경고 60분 간격) | `대조 불일치` 알림 등급별 수, `WARNING 중 직전 알림과 59분 미만 간격` | 불일치가 없으면 0건. 불일치가 있었다면 새 항목(또는 재시작 첫 회)마다 EMERGENCY 1회, 이후 WARNING만, 59분 미만 간격 **0건** | 매분 EMERGENCY, 59분 미만 WARNING → F-2 미동작으로 기록 |
| e | 07:50~09:00 서버 구간 (K5 발동·자동 해제) | `K5` 줄(발동~해제·사유), 07:30~09:30 경고 알림, 그 구간 대조 결과 | 관찰 기록 항목. 1일차(07:53:24 발동, 09:00:01 자동 해제, ka10001 코드 7·kt00001/kt00018 코드 20)와 같은 시각·코드 재현 여부를 적는다. **09:30 판정 전 자동 해제**면 정상 | 09:30에 K5 유지 → (f)에서 09:30 `EVALUATION_SKIPPED`("전역 정지 중") 확인, 매도 미실행 기록. 해제가 사람 재개였으면 경위 확인 |
| f | 사용자 `/kill` 여부와 14:30 판정 | `KILL 명령 n건`(채널·처리 상태), `REMOTE_STALE`/`REMOTE_REJECTED`, K1 행, 09:30·14:30 `EVALUATION`/`EVALUATION_SKIPPED`, 게이트 판정 | 경우 A(보유 0 → `/kill`): KILL `REMOTE telegram DONE` 09:35 전후, K1 `ACTIVE`(저녁까지 유지가 정상 — 3일차 시작 전 재개 필요), 14:30 `EVALUATION_SKIPPED` "전역 정지 중", 14:30 게이트 판정 없음. 경우 B(`/kill` 없음): 14:30 `EVALUATION` + `ENTRY BUY` 게이트 판정 → 승인·체결이면 보유 이월(매매 순서 밀림 지속) | `REMOTE_STALE`(꺼져 있던 동안 보낸 명령으로 무시) 또는 KILL `REJECTED`/`FAILED`/`PENDING`, K1이 있는데 14:30 의도 생성 → 경위 기록, 텔레그램 기록과 대조 |
| g | 의도·전송·주문·체결 상태와 주문번호 | 의도 줄(상태·전송 결과·응답 지연·주문번호), 체결 줄(체결번호), 의도 없는/외부 주문, 현재 포지션 | 09:30 `EXIT SELL 005930 1` 의도 `FILLED`, 전송 `RESPONDED/ACCEPTED`, 브로커 주문 `FILLED`, 체결 1건. 결과 불명 0, 외부 주문 0. 경우 A면 포지션 수량 0. 주문번호는 1일차(0050626)와 같은 번호가 다시 나와도 거래일 범위라 허용(PV-02 관찰) | 의도 `UNKNOWN`·결과 불명, `origin`이 외부, 포지션이 앱 잔고와 다름 → 07 §5에 기록, 사용자에게 키움 앱 잔고 확인 요청 |
| h | 정지 이력 타임라인 (KST) | `HALT_ACTIVATED`·`HALT_CONDITION`(상태 바뀐 것만)·`HALT_RESUME`·`HALT_VARIANT`, 정지 행 표 | 예상 순서: 1일차 K4 → 첫 대조 뒤 `AWAITING_RESUME` → 로컬 `resume K4` / 시작 K8 → `resume K8` → (K5 07:5x~09:00) → (경우 A: K1 09:35). 저녁 미해제는 경우 A의 K1뿐 | 예상 밖 정지(K3·K7·K9·K10 등) 또는 해제 안 된 K4·K8 → 원인 기록 |
| i | 운영 명령 결과 | 명령 줄(채널·명령·인자·상태·결과·처리 시각), PENDING 수 | `RESUME K8`·`RESUME K4`(LOCAL) `DONE`, 경우 A의 `KILL`(REMOTE) `DONE`, PENDING 0 | `REJECTED`·`FAILED` → 결과 문구 기록. PENDING 남음 → 프로세스 종료 전 미처리 |
| j | 알림 전송 상태 (실패·드롭·K11) | 등급×상태 집계, SENT 아님·시도 2회 이상 행, `NOTIFY_DROPPED`, K11 행, 연속 실패 수, 로그 등급별 수 | 모두 `SENT`, `DROPPED` 0, K11 없음(또는 자동 해제), 연속 실패 0, 로그 등급별 수 = DB 수 | `PENDING` 남음·`DROPPED`·K11 `ACTIVE` → 시각·오류 요약 기록(07 §7.2). 로그와 DB 수 차이 → 기록 |
| k | 예수금 관찰 CASH_OBSERVED | `CASH_OBSERVED`(entr·d1_entra·d2_entra·ord_alow_amt·pymn_alow_amt), `DAILY_BASE`, 일일 보고 | 시작 시 1건 이상, 매도 체결 뒤 값 변화 기록(값이 바뀔 때만 기록). 관찰 항목: 매도 대금이 어느 필드에 언제 반영되는지(PV-16), 매도 비용·세금 차감 크기(PV-07, K-U9) | 기록 없음 → 예수금 조회 실패 경고((e)·(j))와 같이 확인. 일일 보고 없음 → 16:00 전 종료 여부 확인 |

## 4. 조회 코드 (읽기 전용)
2026-10-07에 1일차 날짜로 실행 검증함(§5). 출력에는 계좌 별칭·주문번호·체결번호가 나온다(민감정보 아님 — 07-paper-review 1일차 기록과 같은 수준).

```python
# PAPER-0 거래일 분석 조회 (읽기 전용). 사용: python day2_query.py [YYYY-MM-DD] [항목 a~k ...]
# 실행 위치: 저장소 루트 (var\paper\trading.db 가 보이는 곳). DB는 mode=ro URI로만 연다.
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-10-07"
ONLY = set(sys.argv[2:])            # 비우면 전 항목
DB = Path("var/paper/trading.db")
LOG = Path(f"var/paper/logs/ops-{DAY.replace('-', '')}.jsonl")

if not DB.is_file():
    sys.exit(f"DB 없음: {DB}")
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

# KST 거래일 DAY = UTC (DAY-1)T15:00 ~ DAY T15:00. 저장 시각은 UTC ISO('...Z' 또는 '+00:00')라 앞 19자 문자열 비교가 성립
_d = date.fromisoformat(DAY)
LO = f"{(_d - timedelta(days=1)).isoformat()}T15:00:00"
HI = f"{DAY}T15:00:00"


def kst(text):
    if not text:
        return "-"
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(KST).strftime("%m-%d %H:%M:%S.%f")[:-3]


def q(sql, params=()):
    return con.execute(sql, params).fetchall()


def ev(kinds, extra="", params=()):
    """그날 감사 이벤트 (kind 목록)."""
    marks = ",".join("?" * len(kinds))
    return q(f"SELECT seq, at, kind, actor, ref, summary FROM audit_event WHERE kind IN ({marks}) "
             f"AND at >= ? AND at < ? {extra} ORDER BY seq", (*kinds, LO, HI, *params))


def alerts(like):
    out = []
    for r in ev(["ALERT"]):
        s = json.loads(r["summary"])
        if like in s.get("message", ""):
            out.append((r, s))
    return out


def head(key, title):
    if ONLY and key not in ONLY:
        return False
    print(f"\n=== ({key}) {title} — {DAY} KST ===")
    return True


print(f"DB {DB} (읽기 전용), 거래일 {DAY}, UTC 범위 [{LO}, {HI})")

if head("a", "시작 대조와 1일차 열린 항목 종결"):
    for r in q("SELECT recon_id, trigger, result, summary, created_at FROM reconciliation "
               "WHERE trigger IN ('PREFLIGHT','STARTUP') AND created_at >= ? AND created_at < ? ORDER BY created_seq",
               (LO, HI)):
        s = json.loads(r["summary"])
        print(f"  {kst(r['created_at'])} {r['trigger']} {r['result']} 이전열린={s.get('open_items_before')} "
              f"새항목={s.get('items')} 자동종결={s.get('auto_closed_items')}")
    print("  -- 대조 항목 (열림 또는 이날 종결)")
    for r in q("SELECT i.item_id, i.kind, i.instrument, i.ref, i.state, i.method, i.resolver, i.resolved_at, "
               "c.created_at made FROM recon_item i JOIN reconciliation c ON c.recon_id=i.recon_id "
               "WHERE i.state='OPEN' OR (i.resolved_at >= ? AND i.resolved_at < ?) ORDER BY c.created_seq", (LO, HI)):
        print(f"  {r['item_id'][:14]} {r['kind']} {r['instrument']} 생성 {kst(r['made'])} "
              f"{r['state']} {r['method'] or '-'} {r['resolver'] or '-'} 종결 {kst(r['resolved_at'])}")
    for r in ev(["RECON_ITEM_AUTO_RESOLVED"]):
        s = json.loads(r["summary"])
        print(f"  감사 {kst(r['at'])} RECON_ITEM_AUTO_RESOLVED {r['ref'][:14]} {s.get('basis', '')} "
              f"브로커합={s.get('broker_total')} 내부={s.get('internal_filled')}")

if head("b", "09:30 매도 체결 뒤 K4 재발 여부 (K-U14 수정 확인)"):
    for r in ev(["HALT_ACTIVATED"]):
        s = json.loads(r["summary"])
        if s.get("type") == "K4":
            print(f"  K4 발동 {kst(r['at'])} {r['actor']} {s.get('reason')}")
    n_new = q("SELECT COUNT(*) n FROM recon_item i JOIN reconciliation c ON c.recon_id=i.recon_id "
              "WHERE c.created_at >= ? AND c.created_at < ?", (LO, HI))[0]["n"]
    print(f"  이날 새로 생긴 대조 항목: {n_new}건")
    for r in q("SELECT trigger, result, COUNT(*) n, MIN(created_at) a, MAX(created_at) b FROM reconciliation "
               "WHERE created_at >= ? AND created_at < ? GROUP BY trigger, result ORDER BY a", (LO, HI)):
        print(f"  대조 {r['trigger']} {r['result']} {r['n']}회 ({kst(r['a'])} ~ {kst(r['b'])})")

if head("c", "VI 1h 통보 해석 실패 경고 (_AL/_NX)"):
    hits = alerts("통보 해석 불가")
    print(f"  ALERT '통보 해석 불가' {len(hits)}건, 그중 9001 {sum('9001' in s['message'] for _, s in hits)}건")
    for r, s in hits[:5]:
        print(f"  {kst(r['at'])} {s['level']} {s['message'][:90]}")
    sup = [(r, s) for r, s in alerts("반복 억제 요약") if "해석 불가" in s["message"]]
    print(f"  반복 억제 요약 중 해석 불가 관련 {len(sup)}건, 추가 억제 합 {sum(s.get('suppressed', 0) for _, s in sup)}건")

if head("d", "대조 불일치 알림 반복"):
    hits = [(r, s) for r, s in alerts("대조 불일치") if not s["message"].startswith("정지 ")]  # K4 발동 알림은 (h)에서
    print(f"  {Counter(s['level'] for _, s in hits)}")
    prev, lines, short = None, [], 0
    for r, s in hits:
        t = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
        gap = (t - prev).total_seconds() / 60 if prev else None
        prev = t
        if s["level"] == "WARNING" and gap is not None and gap < 59:
            short += 1
        lines.append(f"  {kst(r['at'])} {s['level']} 직전과 간격 {'-' if gap is None else f'{gap:.1f}분'} {s['message'][:50]}")
    print(f"  WARNING 중 직전 알림과 59분 미만 간격: {short}건")
    print("\n".join(lines if len(lines) <= 30 else lines[:10] + [f"  ... ({len(lines) - 13}건 생략)"] + lines[-3:]))

if head("e", "07:30~09:30 서버 구간 (K5·조회 실패)"):
    w_lo, w_hi = f"{(_d - timedelta(days=1)).isoformat()}T22:30:00", f"{DAY}T00:30:00"   # KST 07:30 ~ 09:30
    for r in q("SELECT halt_type, actor, reason, created_at, state, released_at, released_by FROM halt "
               "WHERE halt_type='K5' AND created_at >= ? AND created_at < ? ORDER BY created_at", (LO, HI)):
        print(f"  K5 {kst(r['created_at'])} ~ {kst(r['released_at'])} {r['state']} {r['released_by'] or '-'} "
              f"{r['actor']} {(r['reason'] or '')[:70]}")
    for r in q("SELECT seq, at, summary FROM audit_event WHERE kind='ALERT' AND at >= ? AND at < ? ORDER BY seq",
               (w_lo, w_hi)):
        s = json.loads(r["summary"])
        if s.get("level") != "INFO" and "해석 불가" not in s.get("message", ""):
            print(f"  {kst(r['at'])} {s['level']} {s['message'][:90]}")
    for r in q("SELECT result, COUNT(*) n FROM reconciliation WHERE created_at >= ? AND created_at < ? GROUP BY result",
               (w_lo, w_hi)):
        print(f"  07:30~09:30 대조 {r['result']} {r['n']}회")

if head("f", "/kill 여부와 14:30 판정"):
    kills = q("SELECT channel, actor, command, args, status, result, created_at, done_at FROM ops_command "
              "WHERE command='KILL' AND created_at >= ? AND created_at < ? ORDER BY created_seq", (LO, HI))
    print(f"  KILL 명령: {len(kills)}건")
    for r in kills:
        print(f"  KILL {kst(r['created_at'])} {r['channel']} {r['actor']} {r['status']} 처리 {kst(r['done_at'])} "
              f"{(r['result'] or '')[:60]}")
    for r in ev(["REMOTE_STALE", "REMOTE_REJECTED"]):
        print(f"  {kst(r['at'])} {r['kind']} {r['summary'][:80]}")
    for r in q("SELECT halt_type, actor, state, created_at, released_at FROM halt WHERE halt_type='K1' "
               "AND (created_at >= ? AND created_at < ? OR state != 'RELEASED')", (LO, HI)):
        print(f"  K1 {kst(r['created_at'])} {r['actor']} {r['state']} 해제 {kst(r['released_at'])}")
    for r in ev(["EVALUATION", "EVALUATION_SKIPPED"]):
        s = json.loads(r["summary"])
        print(f"  {kst(r['at'])} {r['kind']} {r['ref'][-25:]} {json.dumps(s, ensure_ascii=False)[:90]}")
    for r in q("SELECT created_at, request_type, side, quantity, instrument, result, failed_check, reason, owner "
               "FROM gate_decision WHERE created_at >= ? AND created_at < ? ORDER BY seq", (LO, HI)):
        print(f"  게이트 {kst(r['created_at'])} {r['request_type']} {r['side']} {r['instrument']} {r['quantity']} "
              f"{r['result']} {r['failed_check'] or '-'} {(r['reason'] or '')[:50]}")

if head("g", "의도·전송·브로커 주문·체결"):
    for r in q("SELECT i.intent_id, i.created_at, i.request_type, i.side, i.instrument, i.quantity, i.state, "
               "i.filled_quantity, i.closed_reason, a.state ast, a.outcome, a.created_at ac, a.result_at ar, "
               "b.order_no, b.state bst, b.filled_quantity bfq, b.origin FROM intent i "
               "LEFT JOIN attempt a ON a.intent_id=i.intent_id LEFT JOIN broker_order b ON b.order_key=i.broker_order_key "
               "WHERE i.created_at >= ? AND i.created_at < ? ORDER BY i.created_seq", (LO, HI)):
        lat = "-"
        if r["ar"]:
            lat = f"{(datetime.fromisoformat(r['ar'].replace('Z', '+00:00')) - datetime.fromisoformat(r['ac'].replace('Z', '+00:00'))).total_seconds():.3f}초"
        print(f"  의도 {kst(r['created_at'])} {r['request_type']} {r['side']} {r['instrument']} {r['quantity']} "
              f"{r['state']} 체결 {r['filled_quantity']} 사유 {r['closed_reason'] or '-'} | 전송 {r['ast'] or '-'}/"
              f"{r['outcome'] or '-'} 응답지연 {lat} | 주문번호 {r['order_no'] or '-'} {r['bst'] or '-'} "
              f"{r['bfq'] or '-'} {r['origin'] or '-'}")
    for r in q("SELECT f.at, f.fill_id, f.quantity, f.price, b.order_no, b.side, b.instrument FROM fill f "
               "JOIN broker_order b ON b.order_key=f.order_key WHERE f.at >= ? AND f.at < ? ORDER BY f.at", (LO, HI)):
        print(f"  체결 {kst(r['at'])} {r['side']} {r['instrument']} {r['quantity']}@{r['price']} 주문번호 {r['order_no']} "
              f"체결번호 {r['fill_id']}")
    for r in q("SELECT order_no, side, instrument, quantity, filled_quantity, state, origin FROM broker_order "
               "WHERE trade_date = ? AND (intent_id IS NULL OR origin != 'INTERNAL_NEW')", (DAY,)):
        print(f"  의도 없는/외부 주문 {r['order_no']} {r['side']} {r['instrument']} {r['state']} {r['origin']}")
    for r in q("SELECT account, instrument, quantity, avg_price FROM position"):
        print(f"  현재 포지션 {r['account']} {r['instrument']} {r['quantity']} @{r['avg_price']}")

if head("h", "정지 이력 타임라인"):
    for r in ev(["HALT_ACTIVATED", "HALT_RESUME", "HALT_VARIANT", "HALT_CONDITION"]):
        s = json.loads(r["summary"])
        if r["kind"] == "HALT_CONDITION" and s.get("state") == "ACTIVE":
            continue   # 상태가 바뀌지 않은 조건 보고는 생략
        print(f"  {kst(r['at'])} {r['kind']} {s.get('type', r['ref'])} {r['actor']} "
              f"{s.get('reason') or s.get('condition') or ''} {s.get('state', '')} "
              f"{'ok=' + str(s.get('ok')) if 'ok' in s else ''}"[:140])
    print("  -- 정지 행 (이날 발동·해제 또는 미해제)")
    for r in q("SELECT halt_type, actor, state, created_at, released_at, released_by, reason FROM halt "
               "WHERE (created_at >= ? AND created_at < ?) OR (released_at >= ? AND released_at < ?) "
               "OR state != 'RELEASED' ORDER BY created_at", (LO, HI, LO, HI)):
        print(f"  {r['halt_type']} {kst(r['created_at'])} {r['state']} "
              f"해제 {kst(r['released_at'])} {r['released_by'] or '-'} {(r['reason'] or '')[:50]}")

if head("i", "운영 명령"):
    for r in q("SELECT created_at, channel, actor, command, args, status, result, done_at FROM ops_command "
               "WHERE created_at >= ? AND created_at < ? ORDER BY created_seq", (LO, HI)):
        print(f"  {kst(r['created_at'])} {r['channel']} {r['actor']} {r['command']} {r['args']} → {r['status']} "
              f"{(r['result'] or '')[:60]} (처리 {kst(r['done_at'])})")
    print(f"  처리 대기(PENDING) 명령: {q('SELECT COUNT(*) n FROM ops_command WHERE status=?', ('PENDING',))[0]['n']}건")

if head("j", "알림 전송 상태 (실패·드롭·K11)"):
    rows = q("SELECT n.alert_seq, n.level, n.status, n.attempts, n.last_attempt_at, n.last_error, a.at FROM notification n "
             "JOIN audit_event a ON a.seq=n.alert_seq WHERE a.at >= ? AND a.at < ? ORDER BY n.alert_seq", (LO, HI))
    print(f"  알림 {len(rows)}건 상태별 {Counter((r['level'], r['status']) for r in rows)}")
    for r in rows:
        if r["status"] != "SENT" or r["attempts"] > 1:
            print(f"  {kst(r['at'])} {r['level']} {r['status']} 시도 {r['attempts']} 마지막 {kst(r['last_attempt_at'])} "
                  f"{(r['last_error'] or '')[:60]}")
    for r in ev(["NOTIFY_DROPPED"]):
        print(f"  {kst(r['at'])} NOTIFY_DROPPED {r['ref']} {r['summary'][:80]}")
    for r in q("SELECT created_at, released_at, state, reason FROM halt WHERE halt_type='K11' "
               "AND created_at >= ? AND created_at < ?", (LO, HI)):
        print(f"  K11 {kst(r['created_at'])} ~ {kst(r['released_at'])} {r['state']} {(r['reason'] or '')[:60]}")
    fs = q("SELECT value FROM meta WHERE key='notify_fail_streak'")
    print(f"  현재 연속 실패 수: {fs[0]['value'] if fs else '기록 없음'}")
    if LOG.is_file():
        lv = Counter(json.loads(x).get("level") for x in LOG.open(encoding="utf-8") if x.strip())
        print(f"  운영 로그 {LOG.name}: {dict(lv)} (DB 알림 수와 비교)")
    else:
        print(f"  운영 로그 없음: {LOG}")

if head("k", "예수금 관찰 CASH_OBSERVED·일일 기준·일일 보고"):
    for r in ev(["CASH_OBSERVED", "DAILY_BASE"]):
        s = json.loads(r["summary"])
        s.pop("account", None)
        print(f"  {kst(r['at'])} {r['kind']} {json.dumps(s, ensure_ascii=False)}")
    for r in q("SELECT trade_day, version, status, content, created_at FROM daily_report WHERE trade_day = ? "
               "ORDER BY seq", (DAY,)):
        c = json.loads(r["content"])
        for k in ("account", "settings_hash"):
            c.pop(k, None)
        print(f"  일일 보고 {r['trade_day']} v{r['version']} {r['status']} {kst(r['created_at'])} "
              f"{json.dumps(c, ensure_ascii=False)[:600]}")

con.close()
```

## 5. 검증 기록
| 일자 | 실행 | 결과 |
|---|---|---|
| 2026-10-07 | 위 코드를 에이전트 scratchpad에 저장, 저장소 루트에서 `.venv\Scripts\python <scratchpad>\day2_query.py 2026-10-06` (전 항목 a~k) — 2일차 데이터가 아직 없어 1일차 날짜로 실행. 읽기 전용 URI | 종료 코드 0. 모든 SQL 문법·표·열 이름·감사 이벤트 요약 키(`RECONCILE`·`HALT_*`·`EVALUATION`·`CASH_OBSERVED`·`DAILY_BASE`·`ALERT`) 정상 해석, KST 변환 출력 확인. 출력 수치는 07-paper-review 1일차 기록과 맞았다(이 문서에는 옮기지 않음) |
| 2026-10-07 | 같은 코드 `2026-10-07` (데이터 없는 날) | 종료 코드 0, 빈 결과로 오류 없이 끝남 |

- 실행 중 확인되지 않은 경로: `RECON_ITEM_AUTO_RESOLVED`·`EVALUATION_SKIPPED`·`REMOTE_STALE`·`REMOTE_REJECTED`·`NOTIFY_DROPPED`·KILL 명령·K1 행은 1일차 DB에 없어 **출력 형식은 미검증**(조회 자체는 실행됨, 0행). 요약 키는 코드(`reconciler.py`·`engine.py`·`telegram.py`)에서 확인.
- 읽기 전용으로 열어도 SQLite WAL 공유 메모리 파일(`trading.db-shm`)의 수정 시각은 바뀔 수 있다. `trading.db` 본문은 바뀌지 않았다(수정 시각 그대로).

## 6. 이 조회로 볼 수 없는 것 (데이터 모델 한계)
| 항목 | 이유 | 대신 볼 것 |
|---|---|---|
| 텔레그램 `/status` 요청·회신 | `/status`는 운영 명령으로 기록하지 않고 바로 회신만 한다 | 사용자 텔레그램 기록 |
| `/kill` 메시지를 보낸 시각 | `ops_command.created_at`은 프로그램이 **받은** 시각. 보낸 시각은 `REMOTE_STALE`일 때만 남는다 | 텔레그램 메시지 시각 |
| 원격 명령 회신(“명령 접수”) 전달 여부 | 회신 실패는 기록하지 않는다 | 텔레그램 기록 |
| 알림이 실제로 휴대폰에 도착한 시각 | `notification`은 전송 시도 시각까지만 | 텔레그램 기록 |
| 알림 생성 시각 | `notification`에 생성 시각 열이 없다 — `audit_event.seq = alert_seq`로 이어 붙여 본다(코드에 반영) | — |
| 억제된 알림 각각 | 반복 억제 창(60초) 안의 같은 알림은 1회만 기록, 나머지는 "반복 억제 요약"의 건수로만 남는다 | (c)의 억제 합 |
| 체결가 대비 같은 시각 시세(PV-10), 시세 조회 주기(PV-15) | 시세 이력 미저장 | 키움 앱 |
| 키움 원문 응답(코드 7·20의 원문, 9001 원문) | 원문 미저장(설계 정책) | 알림 문구 요약만 |
| 실행 코드 버전 | DB에 코드 버전 기록 없음 | `git log -1`, `git status`(실행 체크아웃) |
| 키움 앱의 잔고·수수료·세금 | 브로커 앱 화면 | 사용자 확인 (07 §12 N-7·N-8) |

## 7. 결과를 옮기는 곳
- **`docs/07-paper-review.md` → v0.2** (승인본 아님 — draft 갱신)
  - §2 기간·거래 수: 거래일 2일, 2일차 실행 구간, 판정·의도·전송·체결 수, 왕복 완료 여부(09:30 매도로 1일차 열린 거래 종결 → 왕복 1건), 실행 코드 버전(`feat/T-017-paper0-day1-fixes` HEAD).
  - §3 다음에 "2일차 타임라인" 표 추가 — (a)(e)(f)(g)(h)(i) 출력의 시각 순.
  - §4 PV 표: PV-02(두 거래일 주문번호), PV-05(응답 지연 n=2), PV-07·PV-16((k) 매도 뒤 예수금), PV-04(서버 구간 재현 여부) 상태 갱신.
  - §5·§7 표에 "2일차" 열 추가 — (b)(c)(d)(j) 집계. §7.2 알림은 등급별 수와 59분 미만 간격 수.
  - §9 F-1~F-5 "PAPER 확인" 열: F-1 ← (a)(b), F-2 ← (d), F-3 ← (c), F-4·F-5 ← (g)의 체결·포지션 수량·평균가. "확인됨/미확인/재발"만 적는다.
  - §11·§12 N-1~N-10 갱신, §17 변경 이력 v0.2 행.
- **`docs/broker-specs/kiwoom-kr.md` §7c 보강** — 행 머리에 `[2026-10-07]`을 붙여 추가(1일차 행은 고치지 않음)
  - K-U14: 매도 체결의 실시간 909 vs kt00009 `cntr_no` 관계(1일차와 같은 패턴인지), 행 단위는 부분 체결이 없으면 계속 미확인.
  - 이른 아침 서버: (e)의 K5 발동·해제 시각과 오류 코드 — 재현이면 "2거래일 연속" 기록, 코드 정의는 미확인 유지.
  - VI 1h: 수정 뒤 해석 불가 0건이면 "접미사 허용 처리로 경고 소멸", 새 형식이면 형식만.
  - 예수금: 매도 체결 뒤 필드별 변화(매도 대금·비용 반영 위치).
  - §8 미확인 목록 표의 해당 행(K-U14 등) 상태 갱신.
- 판단이 필요한 것(T-017 main 병합 여부, 경우 A/B에 따른 v2 전환 가능 여부)은 문서에 결론을 쓰지 않고 사용자 확인 항목으로 남긴다(handoff "다음 세션 첫 행동" 3번).

## 8. 변경 이력
| 버전 | 일자 | 변경 | 승인 |
|---|---|---|---|
| v0.1 | 2026-10-07 | 최초 작성 (확인 항목 a~k, 조회 코드, 1일차 데이터로 실행 검증) | 해당 없음 (작업 도구 문서) |
