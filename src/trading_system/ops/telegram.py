"""텔레그램 알림·원격 명령 (R-07, R-13, R-21, 01 §2.9·§2.10·D-024·D-025, 03 D-052·§6.6, 04 V-30·V-57·V-107·V-119).

- 봇 API 롱 폴링(getUpdates)만 쓴다. 외부 포트를 열지 않는다. update_id로 중복을 없앤다(V-57).
- 허용된 채팅 ID만 받는다. `/kill`을 뺀 명령은 1회용 확인 코드(60초)를 받은 뒤에만 명령 테이블에 넣는다(V-107).
  `/kill`은 위험을 줄이는 명령이라 확인 없이 즉시 넣는다(결정 대기열 DP-9).
- 원격으로는 K1·K2만 재개할 수 있다(정지 관리자가 판정). 재개·상태 응답에는 KILL 파일 존재를 표시한다(V-119).
- 알림: 감사 이벤트 ALERT를 읽어 보낸다(전송 상태는 notification 표, ENT-26). 같은 알림은 최대 3회 시도.
  연속 3건 실패하면 K11(위험 확대 차단), 이후 전송이 성공하면 K11 해제 조건을 보고하고 밀린 알림을 보낸다(V-30).
- 토큰은 Secret으로만 다루고 URL·오류 메시지·로그에 남기지 않는다. HTTP 자동 재시도는 없다(03 D-044와 같은 원칙).
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType, IntentState
from trading_system.domain.sinks import HaltControl
from trading_system.storage.jsonlog import Masker
from trading_system.storage.repository import Repository, StorageFailure

CONFIRM_TTL = timedelta(seconds=60)
STALE_AFTER = timedelta(seconds=120)   # 시작 시각보다 이만큼 넘게 오래된 메시지는 실행하지 않는다 (꺼져 있던 동안의 명령)
K11_AFTER_FAILURES = 3     # 01 §2.7 알림 전송 연속 실패 3회 → K11 (D-024)
SEND_INTERVAL = 1.0        # T-017 M-3: 같은 채팅 연속 전송 간격(초)
MAX_PER_PUMP = 20          # T-017 M-3: 한 회차 최대 전송 수 (밀린 알림이 많아도 스레드를 오래 잡지 않음)
UPDATE_MAX_FAILURES = 3    # T-017 M-4: 같은 update 처리가 연속 실패하면 건너뛰고 경고
RETRY_AFTER_CAP = 300.0    # T-017 재검토 M-3: 429 대기 상한(초). 넘으면 상한만 기다리고 실패로 센다(K11 경로)
MAX_TEXT = 4000            # 텔레그램 메시지 길이 한도(4096자)보다 짧게 잘라 보낸다 (재검토 H-1)
# 메시지 내용 때문에만 나는 400 설명(일치하면 그 메시지만 버린다). 그 밖의 400(chat not found 등)은 채널 장애로 센다
_MESSAGE_FAULTS = ("message is too long", "can't parse entities", "message text is empty",
                   "text must be non-empty")
CONNECT_TIMEOUT, READ_TIMEOUT = 3, 10
POLL_SECONDS = 30
LEVEL_KO = {"EMERGENCY": "긴급", "WARNING": "경고", "INFO": "정보"}


class TelegramError(RuntimeError):
    """전송·수신 실패. 메시지에 토큰·URL을 담지 않는다.

    permanent: 메시지 자체가 거부됨(HTTP 400) — 다시 보내도 같다. retry_after: 429 전송 제한의 대기 초 (T-017)."""

    def __init__(self, message: str, permanent: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


class TelegramApi:
    def __init__(self, token_reveal: Callable[[], str], base_url: str = "https://api.telegram.org") -> None:
        self._token = token_reveal
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.mount("http://", HTTPAdapter(max_retries=0))
        self._session.mount("https://", HTTPAdapter(max_retries=0))

    def _call(self, method: str, payload: dict, read_timeout: float) -> object:
        url = f"{self._base}/bot{self._token()}/{method}"
        try:
            r = self._session.post(url, json=payload, timeout=(CONNECT_TIMEOUT, read_timeout))
        except requests.RequestException as exc:
            raise TelegramError(f"{method}: 연결 실패 ({type(exc).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(f"{method}: 응답 해석 불가 (HTTP {r.status_code})") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            desc = body.get("description", "") if isinstance(body, dict) else ""
            retry = None
            if r.status_code == 429:
                params = body.get("parameters") if isinstance(body, dict) else None
                raw = params.get("retry_after") if isinstance(params, dict) else None
                ok_num = isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0
                retry = float(raw) if ok_num else 5.0
            message_fault = r.status_code == 400 and any(k in str(desc).lower() for k in _MESSAGE_FAULTS)
            raise TelegramError(f"{method}: 거부 (HTTP {r.status_code}) {str(desc)[:80]}",
                                permanent=message_fault, retry_after=retry)
        return body.get("result")

    def send_message(self, chat_id: str, text: str) -> None:
        if len(text) > MAX_TEXT:   # 길이 초과로 거부되지 않게 자른다 (재검토 H-1)
            text = text[:MAX_TEXT - 20] + " …(이하 생략)"
        self._call("sendMessage", {"chat_id": chat_id, "text": text}, READ_TIMEOUT)

    def get_updates(self, offset: int | None, timeout: int = POLL_SECONDS) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout + READ_TIMEOUT)
        return result if isinstance(result, list) else []


class Notifier:
    """감사 ALERT → 텔레그램. 실패 누적 시 K11 (D-024)."""

    def __init__(self, repo: Repository, api: TelegramApi, chat_id: str, clock: Clock, halts: HaltControl | None,
                 masker: Masker | None = None, sleep: Callable[[float], object] = time.sleep,
                 should_stop: Callable[[], bool] = lambda: False) -> None:
        self._repo = repo
        self._sleep = sleep
        self._should_stop = should_stop   # 종료 신호 — 전송 간격 대기 중에도 빨리 끝낸다 (재검토 M-2)
        self._retry_until: datetime | None = None   # 429 retry_after가 끝나는 시각 (T-017 M-3)
        self._api = api
        self._chat = chat_id
        self._clock = clock
        self._halts = halts
        self._masker = masker or Masker()

    def pump(self) -> tuple[int, int]:
        """새 알림을 대기열에 넣고 대기 중인 것을 보낸다. (성공 수, 실패 수)."""
        with self._repo.tx():
            self._repo.enqueue_new_alerts()
        sent = failed = 0
        if self._retry_until is not None and self._clock.now() < self._retry_until:
            return 0, 0   # 429 대기 중 (T-017 M-3)
        self._retry_until = None
        attempts = 0
        # 긴급 알림을 먼저 보낸다 — 밀린 알림이 많아도 새 긴급 알림이 간격(1초)×건수만큼 늦지 않게 (T-017)
        pending = sorted(self._repo.pending_notifications(), key=lambda n: (n["level"] != "EMERGENCY", n["alert_seq"]))
        for n in pending[:MAX_PER_PUMP]:
            if self._should_stop():
                break
            if attempts:
                self._sleep(SEND_INTERVAL)
                if self._should_stop():
                    break
            attempts += 1
            text = self._masker.text(f"[{LEVEL_KO.get(n['level'], n['level'])}] {n['message']}")
            now = self._clock.now()
            try:
                self._api.send_message(self._chat, text)
            except TelegramError as exc:
                failed += 1
                if exc.retry_after is not None:
                    # 전송 제한: 채널은 살아 있다 → 기다린다 (T-017 M-3). 대기가 상한을 넘으면 상한만 기다리고
                    # 실패로 센다 — 오래 못 보내면 K11로 사람에게 알 길을 남긴다 (재검토 M-3)
                    long_wait = exc.retry_after > RETRY_AFTER_CAP
                    self._retry_until = now + timedelta(seconds=min(exc.retry_after, RETRY_AFTER_CAP))
                    with self._repo.tx():
                        self._repo.mark_notification(n["alert_seq"], ok=False, at=now, error=str(exc),
                                                     count_failure=long_wait)
                    if long_wait and self._repo.notification_fail_streak() >= K11_AFTER_FAILURES and self._halts:
                        self._halts.activate(HaltType.K11, "notifier", f"알림 연속 실패: {exc}", None)
                    break
                if exc.permanent:
                    # 메시지 자체 거부: 다시 보내도 같다 → 이 메시지만 버리고 다음으로 (T-017 M-2).
                    # 알림 원문은 감사 기록·로컬 로그에 남아 있다
                    with self._repo.tx():
                        self._repo.drop_notification(n["alert_seq"], now, str(exc))
                        self._repo.audit(now, "NOTIFY_DROPPED", "notifier", str(n["alert_seq"]),
                                         {"error": str(exc)[:200], "level": n["level"]})
                    continue
                with self._repo.tx():
                    self._repo.mark_notification(n["alert_seq"], ok=False, at=now, error=str(exc))
                if self._repo.notification_fail_streak() >= K11_AFTER_FAILURES and self._halts:
                    self._halts.activate(HaltType.K11, "notifier", f"알림 연속 실패: {exc}", None)
                break  # 채널이 실패하면 이번 회차는 멈춘다(순서 유지)
            sent += 1
            with self._repo.tx():
                self._repo.mark_notification(n["alert_seq"], ok=True, at=now)
            if self._halts is not None and any(h["halt_type"] == "K11" for h in self._repo.halt_rows(None)):
                self._halts.report_condition(HaltType.K11, None, "notify_ok", True, "알림 전송 성공")
        return sent, failed


@dataclass
class _Pending:
    command: str
    args: dict
    chat: str
    update_id: int
    expires: datetime


class CommandBot:
    """원격 명령 수신 (03 §6.6). 확인된 명령을 ops_command 표에 넣는다. 처리는 OpsProcessor."""

    def __init__(self, repo: Repository, api: TelegramApi, allowed_chat: str, account: str, clock: Clock,
                 kill_file: Path, alert: Callable[[str, str], None]) -> None:
        self._repo = repo
        self._api = api
        self._allowed = allowed_chat
        self._account = account
        self._clock = clock
        self._kill_file = kill_file
        self._alert = alert
        self._offset: int | None = None
        self._pending: dict[str, _Pending] = {}
        self._started = clock.now()
        self._fail_counts: dict[int, int] = {}   # update_id → 연속 처리 실패 수 (T-017 M-4)

    def poll_once(self, timeout: int = POLL_SECONDS) -> int:
        updates = self._api.get_updates(self._offset, timeout)
        handled = 0
        for u in updates:
            uid = u.get("update_id") if isinstance(u, dict) else None
            if not isinstance(uid, int) or isinstance(uid, bool):
                continue
            msg = u.get("message")
            msg = msg if isinstance(msg, dict) else {}
            chat_obj = msg.get("chat")
            chat = str(chat_obj.get("id", "")) if isinstance(chat_obj, dict) else ""
            text = str(msg.get("text") or "").strip()
            raw_date = msg.get("date")
            try:
                self._handle(uid, chat, text, raw_date if isinstance(raw_date, int) else None)
            except StorageFailure:
                # T-017 M-4: 저장 실패면 offset을 넘기지 않는다 → 다음 폴링에서 같은 update를 다시 받는다.
                # 같은 update가 UPDATE_MAX_FAILURES번 연속 실패하면 건너뛴다(무한 반복 방지). 경고는 저장소를
                # 거치므로 닿지 않을 수 있어, 허용 채팅에는 텔레그램으로 직접 알린다 (재검토 H-2)
                count = self._fail_counts.get(uid, 0) + 1
                self._fail_counts[uid] = count
                if count >= UPDATE_MAX_FAILURES:
                    self._fail_counts.pop(uid, None)
                    self._offset = max(self._offset or 0, uid + 1)
                    if chat == self._allowed:
                        self._reply(chat, "명령을 저장하지 못해 접수하지 않았습니다(저장소 오류). "
                                          "긴급 정지는 PC에서 kill.py로 하세요")
                    self._alert("WARNING", f"원격 명령을 {count}번 저장하지 못해 건너뜀 — 필요하면 다시 보내세요")
                raise
            except Exception:
                # 저장 실패가 아닌 오류(결함)는 다시 처리해도 같다 → 건너뛰고 기록만 (재검토 L-1)
                self._fail_counts.pop(uid, None)
                self._offset = max(self._offset or 0, uid + 1)
                raise
            self._fail_counts.pop(uid, None)
            self._offset = max(self._offset or 0, uid + 1)
            handled += 1
        return handled

    def _is_stale(self, date: int | None) -> bool:
        """메시지 시각(epoch 초)이 있고 시작보다 STALE_AFTER 넘게 오래됐으면 True. 시각이 없으면 오래된 것으로 보지 않는다."""
        if date is None:
            return False
        sent = datetime.fromtimestamp(date, tz=self._started.tzinfo)
        return sent < self._started - STALE_AFTER

    def _reply(self, chat: str, text: str) -> None:
        try:
            self._api.send_message(chat, text)
        except TelegramError:
            pass  # 회신 실패는 알림 경로(Notifier)의 실패 집계로 드러난다

    def _handle(self, uid: int, chat: str, text: str, date: int | None = None) -> None:
        if chat != self._allowed:
            with self._repo.tx():
                self._repo.audit(self._clock.now(), "REMOTE_REJECTED", "telegram", str(uid),
                                 {"reason": "허용되지 않은 채팅"})
            self._alert("WARNING", "허용되지 않은 채팅에서 명령 시도 (거부)")
            return
        parts = text.split()
        if not parts:
            return
        if self._is_stale(date):
            # 텔레그램은 미수신 메시지를 24시간 보관한다 → 꺼져 있던 동안 보낸 /kill 등이 시작 직후 실행되면
            # 그날 매매가 사라진다(2026-10-05 독립 검토). 실행하지 않고 다시 보내라고만 답한다.
            with self._repo.tx():
                self._repo.audit(self._clock.now(), "REMOTE_STALE", "telegram", str(uid),
                                 {"text_head": parts[0][:20], "message_date": date})
            if parts[0].startswith("/"):
                self._reply(chat, "꺼져 있던 동안 보낸 명령이라 실행하지 않았습니다 — 필요하면 다시 보내세요")
            return
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "/status":
            self._reply(chat, self._status())
        elif cmd == "/kill":
            self._submit(uid, chat, "KILL", {"account": None, "reason": " ".join(args) or "원격 킬 스위치"})
        elif cmd == "/confirm":
            self._confirm(uid, chat, args[0] if args else "")
        elif cmd in ("/resume", "/cancel", "/exit", "/reduce"):
            try:
                name, cargs = self._parse(cmd, args)
            except ValueError as exc:
                self._reply(chat, f"형식 오류: {exc}")
                return
            code = secrets.token_hex(3)
            self._pending[code] = _Pending(name, cargs, chat, uid, self._clock.now() + CONFIRM_TTL)
            self._reply(chat, f"{name} {json.dumps(cargs, ensure_ascii=False)} — 60초 안에 /confirm {code}")
        else:
            self._reply(chat, "명령: /kill [사유], /resume K1|K2, /cancel <주문키>, /exit <종목> <수량>, "
                              "/reduce <종목> <수량>, /status, /confirm <코드>")

    def _parse(self, cmd: str, args: list[str]) -> tuple[str, dict]:
        if cmd == "/resume":
            if len(args) != 1:
                raise ValueError("/resume K1|K2")
            return "RESUME", {"account": None, "halt_type": args[0].upper()}
        if cmd == "/cancel":
            if len(args) != 1:
                raise ValueError("/cancel <주문키>")
            return "CANCEL", {"order_key": args[0]}
        if len(args) != 2 or not args[1].isdigit():
            raise ValueError(f"{cmd} <종목> <수량(정수)>")
        return cmd[1:].upper(), {"account": self._account, "instrument": args[0], "quantity": args[1]}

    def _confirm(self, uid: int, chat: str, code: str) -> None:
        p = self._pending.get(code)   # 접수(저장) 성공 뒤에 지운다 — 저장 실패 후 재처리 때 코드가 남아 있게 (재검토 M-1)
        now = self._clock.now()
        if p is None or p.chat != chat or now > p.expires:
            with self._repo.tx():
                self._repo.audit(now, "REMOTE_REJECTED", "telegram", str(uid),
                                 {"reason": "확인 코드 오류·만료"})
            self._alert("WARNING", "원격 명령 확인 코드 오류·만료 (거부)")
            self._pending.pop(code, None)
            self._reply(chat, "확인 코드가 틀렸거나 만료됨 — 명령을 다시 보내세요")
            return
        self._submit(p.update_id, chat, p.command, p.args)
        self._pending.pop(code, None)

    def _submit(self, uid: int, chat: str, command: str, args: dict) -> None:
        cid = f"tg:{uid}"  # 채널 메시지 ID = 멱등키 (03 §6.6, V-57)
        with self._repo.tx():
            fresh = self._repo.insert_command(cid, "REMOTE", "telegram", command, args, self._clock.now())
        self._reply(chat, f"명령 접수: {command} ({cid})" if fresh else f"이미 받은 명령: {cid}")

    def _status(self) -> str:
        halts = self._repo.halt_rows(None)
        kill = self._kill_file.exists()
        lines = [f"정지 {len(halts)}건: " + (", ".join(f"{h['halt_type']}({h['state']})" for h in halts) or "없음"),
                 f"KILL 파일: {'있음 — 위험 확대 전송 차단 중 (로컬에서만 삭제)' if kill else '없음'}",
                 f"결과 불명: {len(self._repo.intents_in_state(IntentState.UNKNOWN))}건",
                 f"대조 불일치: {len(self._repo.open_recon_items())}건"]
        return "\n".join(lines)
