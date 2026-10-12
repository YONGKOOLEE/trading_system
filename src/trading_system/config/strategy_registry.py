"""전략 등록 검증·상태 관리 (R-17, R-19, R-20, R-28, 00 §2.4·§2.11·§2.13, 02 ENT-05·06, INV-13).

- 등록 검증: 명세 누락, 형식, 능력 미지원, 규칙 우선순위 모호, 전략 한도 > 계좌 한도 → 거부 (E-8~E-10, V-85).
  능력 지원 기준: BACKTEST 대상은 "지원"·"검증 중", PAPER·LIVE 대상은 "지원"만 (결정 대기열 DP-1).
  예외(DP-24 해법 a, A-037): PAPER-0 검증 전략은 사용자 승인 A-ID가 있으면 PAPER 대상에서 "검증 중"을 허용한다.
  범위 강제: M1 한정, 고정 수량(S-4), 종목 목록(U-A), 시장가 능력만(PAPER0_CAPABILITIES), 전략 L7 필수.
- 상태 전이는 00 §2.11 표만 허용하고 **승인 A-ID**가 있어야 한다 (INV-13). 시스템은 상태를 스스로 바꾸지 않는다.
- 초안 → 백테스트 통과: 이 시스템 백테스트 실행 기록(증거)이 있어야 함 (A-029).
- PAPER 이후 → 중지: K9 정지 발동(사유별 변형). 중지 → 이전 상태: K9 재개. 폐기: K9(사용자 판단) 유지.
- 게이트 입력(StrategyFacts): 모드별 허용 상태가 아니면 신호 거부 (V-83, V-113). 전략별 한도 제공.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal

from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType, Mode, RequestType
from trading_system.domain.limits import AccountLimits
from trading_system.domain.sinks import HaltControl
from trading_system.domain.strategy import (
    ALLOWED_TRANSITIONS, MARKETS, RULE_KINDS, SIZING_METHODS, UNIVERSE_METHODS, StopReason, StrategyLimits,
    StrategySpec, StrategyState,
)
from trading_system.storage.repository import Repository

from .capabilities import NOT_IMPLEMENTED, CapabilityTable, capability_row, required_parts, uses_unfilled_cancel
from .strategy_params import PARAM_TYPES, params_hash, validate_params
from .errors import ConfigError

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_APPROVAL = re.compile(r"A-\d{3,}")
_SIGNAL_STATES = {
    Mode.BACKTEST: frozenset(StrategyState),  # 백테스트는 초안부터 돌린다
    Mode.PAPER: frozenset({StrategyState.PAPER, StrategyState.LIVE_CANDIDATE}),
    Mode.LIVE: frozenset({StrategyState.LIVE}),
}
_RISK_UP = (RequestType.ENTRY, RequestType.INCREASE)
# PAPER-0 검증 전략이 쓸 수 있는 능력: 정해진 시각 판정·일봉·시장가·국내 상장주식 (04 §4.1 "1주 단위 시장가")
PAPER0_CAPABILITIES = frozenset({"CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"})


@dataclass(frozen=True)
class RegistrationResult:
    ok: bool
    problems: tuple[str, ...]
    reg_id: str | None = None


def _paper0_problems(spec: StrategySpec, target_mode: Mode, approval: str) -> list[str]:
    """PAPER-0 검증 전략 예외의 범위 (DP-24 a). 하나라도 어긋나면 예외를 쓰지 않고 거부한다."""
    p: list[str] = []
    if target_mode is not Mode.PAPER:
        p.append(f"PAPER-0 검증 승인은 PAPER 대상에만 쓴다 ({target_mode.value} 대상 불가)")
    if not _APPROVAL.fullmatch(approval or ""):
        p.append("PAPER-0 검증 승인: 사용자 승인 A-ID(A-숫자)가 필요하다")
    if tuple(spec.markets) != ("M1",):
        p.append("PAPER-0 검증 전략: markets는 M1만")
    if spec.sizing != "S-4":
        p.append("PAPER-0 검증 전략: sizing은 고정 수량 S-4만 (04 §4.1 1주 단위)")
    if spec.universe_method != "U-A" or not spec.universe:
        p.append("PAPER-0 검증 전략: universe_method는 종목 목록 U-A만")
    extra = sorted(set(spec.capabilities) - PAPER0_CAPABILITIES)
    if extra:
        p.append(f"PAPER-0 검증 전략: 허용되지 않는 능력 {', '.join(extra)} (시장가 능력만)")
    if spec.limits.l7_max_order_amount is None:
        p.append("PAPER-0 검증 전략: limits.l7_max_order_amount(1회 주문 금액 상한) 필수")
    return p


def validate_spec(spec: StrategySpec, caps: CapabilityTable, account_limits: AccountLimits | None,
                  target_mode: Mode, verification_approval: str | None = None) -> list[str]:
    """등록 명세 검증 (00 §2.4·§2.13). 문제 목록을 돌려준다(없으면 통과).

    verification_approval: PAPER-0 검증 전략 승인 A-ID (DP-24 a). 주면 범위 검사를 통과해야 하고, 통과하면
    PAPER 대상에서 "검증 중" 능력을 허용한다."""
    p: list[str] = []
    paper0 = verification_approval is not None
    if paper0:
        p.extend(_paper0_problems(spec, target_mode, verification_approval or ""))
    if not _ID.fullmatch(spec.strategy_id or ""):
        p.append("strategy_id: 영문 소문자·숫자·하이픈")
    if not isinstance(spec.version, int) or isinstance(spec.version, bool) or spec.version < 1:
        p.append("version: 1 이상 정수 (D-011)")
    for name in ("name", "description", "data_requirements", "trigger", "signal_format", "validity_rule",
                 "unfilled_policy", "holding"):
        if not str(getattr(spec, name) or "").strip():
            p.append(f"{name}: 필수 항목 누락 (E-8)")
    if not spec.markets or not set(spec.markets) <= MARKETS:
        p.append("markets: M1/M2/M3 중 하나 이상")
    if spec.universe_method not in UNIVERSE_METHODS:
        p.append("universe_method: U-A/U-B/U-C")
    elif spec.universe_method in ("U-A", "U-B") and not spec.universe:
        p.append("universe: 종목 목록(U-A) 또는 목록 ID(U-B) 필요")
    if spec.sizing not in SIZING_METHODS:
        p.append("sizing: S-1~S-4 중 하나")
    if not isinstance(spec.validity_max_minutes, int) or spec.validity_max_minutes <= 0:
        p.append("validity_max_minutes: 0보다 큰 정수 (절대 상한, D-007)")

    if not spec.capabilities:
        p.append("capabilities: 필요 능력 누락")
    allowed = {"지원", "검증 중"} if target_mode is Mode.BACKTEST or (paper0 and target_mode is Mode.PAPER) \
        else {"지원"}
    for cap in spec.capabilities:
        for part in required_parts(cap):          # [CP-032 D-175] "CAP-ORD-03" = 03a·03b 모두 요구
            if part in NOT_IMPLEMENTED:
                p.append(f"{part}: {NOT_IMPLEMENTED[part]}" + (f" ({cap} 선언)" if part != cap else ""))
                continue
            row = capability_row(caps, part)
            if row is None:
                p.append(f"{part}: 정의되지 않은 능력")
                continue
            for m in spec.markets:
                status = row.get(m)
                if status not in allowed:
                    p.append(f"{part}: {m}에서 {status} — {target_mode.value} 등록 불가 (D-004, CP로 기능 추가)")
    if uses_unfilled_cancel(spec.capabilities) and spec.unfilled_policy.strip() in ("", "시스템 기본"):
        p.append("unfilled_policy: CAP-ORD-03·03b를 선언하면 미체결 정책 필수")

    if not spec.rules:
        p.append("rules: 규칙 표 누락")
    seen_ids: set[str] = set()
    seen_prio: dict[str, set[int]] = {}
    for r in spec.rules:
        if r.rule_id in seen_ids:
            p.append(f"rule {r.rule_id}: 규칙 ID 중복")
        seen_ids.add(r.rule_id)
        if r.kind not in RULE_KINDS:
            p.append(f"rule {r.rule_id}: 알 수 없는 규칙 종류 {r.kind}")
        if r.priority in seen_prio.setdefault(r.kind, set()):
            p.append(f"rule {r.rule_id}: 같은 종류({r.kind}) 안에서 우선순위 중복 — 순서가 모호함 (D-006)")
        seen_prio[r.kind].add(r.priority)
        for field_name in ("condition", "action", "invalid_when", "trigger"):
            if not str(getattr(r, field_name) or "").strip():
                p.append(f"rule {r.rule_id}: {field_name} 누락 (판정 가능해야 함, D-001)")
        for ref in (r.trigger, *r.inputs):
            if ref.startswith("CAP-") and ref not in spec.capabilities:
                p.append(f"rule {r.rule_id}: 선언하지 않은 능력 사용 {ref}")

    names: set[str] = set()
    for prm in spec.params:
        if prm.name in names:
            p.append(f"param {prm.name}: 이름 중복")
        names.add(prm.name)
        if prm.type not in PARAM_TYPES:
            p.append(f"param {prm.name}: 타입은 {sorted(PARAM_TYPES)}")
        if prm.minimum is not None and prm.maximum is not None and prm.minimum > prm.maximum:
            p.append(f"param {prm.name}: 최소 > 최대")
        if not prm.unit.strip() or not prm.description.strip():
            p.append(f"param {prm.name}: 단위·설명 필수")

    p.extend(_limit_problems(spec.limits, account_limits))
    return p


def _limit_problems(sl: StrategyLimits, acct: AccountLimits | None) -> list[str]:
    pairs = (("l1_max_loss_ratio", sl.l1_max_loss_ratio), ("l2_max_symbol_ratio", sl.l2_max_symbol_ratio),
             ("l3_max_total_ratio", sl.l3_max_total_ratio), ("l7_max_order_amount", sl.l7_max_order_amount))
    out: list[str] = []
    for name, v in pairs:
        if v is None:
            continue
        if not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
            out.append(f"limits.{name}: 0보다 큰 Decimal")
            continue
        if acct is None:
            out.append(f"limits.{name}: 계좌 한도 설정이 없어 비교 불가")
        elif getattr(acct, name) is None:
            # [CP-034 D-196] 계좌 L7이 비율 형식이면 기준 금액 없이 비교할 수 없다 — 실행 중 게이트가
            # min(전략 L7, 계좌 L7)을 판정마다 적용한다(01 §2.3.1-8)
            continue
        elif v > getattr(acct, name):
            out.append(f"limits.{name}: 계좌 한도보다 큼 (E-10)")
    return out


def _limits_json(sl: StrategyLimits) -> dict[str, str | None]:
    return {k: (None if v is None else str(v)) for k, v in asdict(sl).items()}


class StrategyRegistry:
    """전략 등록·상태의 원본 (저장소). 게이트 입력(StrategyFacts)을 겸한다."""

    def __init__(self, repo: Repository, clock: Clock, halts: HaltControl | None, market: str = "M1") -> None:
        self._repo = repo
        self._clock = clock
        self._halts = halts
        self._market = market

    # --- 등록 -------------------------------------------------------------------------
    def attach_halts(self, halts: HaltControl) -> None:
        """조립 지점에서 정지 관리자를 연결한다 (정지 관리자가 게이트를, 게이트가 이 등록부를 쓰므로 나중에 연결)."""
        self._halts = halts

    def register(self, spec: StrategySpec, params: dict[str, object], caps: CapabilityTable,
                 account_limits: AccountLimits | None, target_mode: Mode | None = None,
                 verification_approval: str | None = None) -> RegistrationResult:
        target = target_mode or self._repo.mode
        problems = validate_spec(spec, caps, account_limits, target, verification_approval)
        parsed: dict[str, object] = {}
        if not problems:
            try:
                parsed = validate_params(spec, params)
            except ConfigError as exc:
                problems = list(exc.problems)
        if not problems:
            prior = self._repo.latest_registration(spec.strategy_id, spec.version)
            if prior is not None and (prior["spec_hash"] != spec.spec_hash() or
                                      prior["params_hash"] != params_hash(parsed)):
                # 같은 ID·버전은 같은 명세·파라미터여야 한다. 바꾸려면 버전을 올려 새로 등록·승인 (D-011)
                problems = [f"버전 {spec.version}은 다른 명세·파라미터로 이미 등록됨 — 버전을 올려 새로 등록 (D-011)"]
        with self._repo.tx():
            now = self._clock.now()
            reg_id = self._repo.insert_strategy_registration({
                "strategy_id": spec.strategy_id, "version": spec.version, "spec_hash": spec.spec_hash(),
                "spec_json": json.dumps({"limits": _limits_json(spec.limits), "markets": list(spec.markets),
                                         **({"verification_approval": verification_approval}
                                            if verification_approval is not None else {})},
                                        ensure_ascii=False),
                "params_hash": params_hash(parsed) if parsed else None,
                "params_json": json.dumps({k: str(v) for k, v in parsed.items()}, ensure_ascii=False) if parsed else None,
                "target_mode": target.value, "result": "REJECTED" if problems else "PASSED",
                "reason": "; ".join(problems) if problems else None, "created_at": now.isoformat(),
            })
            if not problems and self._repo.strategy_state(spec.strategy_id, spec.version, self._market) is None:
                self._repo.upsert_strategy_state(spec.strategy_id, spec.version, self._market,
                                                 StrategyState.DRAFT.value, None, None, spec.spec_hash(), now)
            self._repo.audit(now, "STRATEGY_REGISTRATION", "registry", reg_id, {
                "strategy": spec.owner, "result": "REJECTED" if problems else "PASSED", "problems": problems,
                "verification_approval": verification_approval})
        return RegistrationResult(not problems, tuple(problems), reg_id)

    # --- 상태 전이 ----------------------------------------------------------------------
    def transition(self, strategy_id: str, version: int, to_state: StrategyState, approval_id: str, *,
                   evidence: str | None = None, stop_reason: StopReason | None = None, actor: str = "user") -> None:
        """사용자 승인 상태 전이 (00 §2.11). 잘못된 전이·승인 없음·증거 없음이면 ValueError."""
        if not _APPROVAL.fullmatch(approval_id or ""):
            raise ValueError("전략 상태 전이에는 승인 A-ID(A-숫자)가 필요하다 (INV-13)")
        row = self._repo.strategy_state(strategy_id, version, self._market)
        if row is None:
            raise ValueError("등록되지 않은 전략")
        current = StrategyState(row["state"])
        if to_state not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"허용되지 않는 전이 {current.value} → {to_state.value} (00 §2.11)")
        if current is StrategyState.STOPPED and to_state is not StrategyState.RETIRED and \
                to_state.value != row["prev_state"]:
            raise ValueError(f"중지 해제는 중지 전 상태({row['prev_state']})로만")
        if to_state is StrategyState.BACKTEST_PASSED and not (evidence or "").strip():
            raise ValueError("백테스트 통과에는 이 시스템 백테스트 실행 기록(보고서 경로)이 필요 (A-029)")
        if to_state is StrategyState.STOPPED and stop_reason is None:
            raise ValueError("중지에는 사유(교체/사용자 판단)가 필요 (01 K9)")
        if to_state in (StrategyState.PAPER, StrategyState.LIVE_CANDIDATE, StrategyState.LIVE) and \
                current is not StrategyState.STOPPED:
            reg = self._repo.latest_registration(strategy_id, version)
            if reg is None or reg["target_mode"] == Mode.BACKTEST.value:
                raise ValueError("PAPER·LIVE 대상 등록 검증(능력 '지원')을 통과한 기록이 없다 (D-004)")

        owner = f"{strategy_id}:{version}"
        if to_state is StrategyState.RETIRED and self._repo.active_ownerships_of(owner):
            # 폐기된 전략은 신호를 못 내므로 보유가 고아가 된다. 청산(또는 사람 정리) 후 폐기 (D-011 소유권 미승계)
            raise ValueError("이 전략 소유 보유·예약이 남아 있음 — 청산 후 폐기")
        prev = row["prev_state"]
        if to_state is StrategyState.STOPPED:
            prev = current.value
        with self._repo.tx():
            now = self._clock.now()
            self._repo.insert_strategy_transition(strategy_id, version, self._market, current.value, to_state.value,
                                                  approval_id, evidence, stop_reason.value if stop_reason else None,
                                                  now)
            reason = stop_reason.value if stop_reason else row["stop_reason"]
            if to_state is StrategyState.RETIRED:
                reason = StopReason.USER.value
            self._repo.upsert_strategy_state(strategy_id, version, self._market, to_state.value,
                                             reason if to_state in (StrategyState.STOPPED, StrategyState.RETIRED)
                                             else None, prev, row["spec_hash"], now)
            self._repo.audit(now, "STRATEGY_STATE", actor, owner, {
                "from": current.value, "to": to_state.value, "approval": approval_id, "evidence": evidence})
        self._sync_k9(owner, current, to_state, stop_reason, actor)

    def _sync_k9(self, owner: str, frm: StrategyState, to: StrategyState, reason: StopReason | None,
                 actor: str) -> None:
        if self._halts is None:
            return
        if to is StrategyState.STOPPED:
            self._halts.activate(HaltType.K9, actor, f"전략 중지 ({reason.value if reason else ''})", None,
                                 owner=owner, variant=(reason or StopReason.USER).value)
        elif to is StrategyState.RETIRED:  # 교체 중지였어도 폐기는 사용자 판단 행으로 바꾼다
            self._halts.activate(HaltType.K9, actor, "전략 폐기", None, owner=owner, variant=StopReason.USER.value)
        elif frm is StrategyState.STOPPED and to is not StrategyState.RETIRED:
            self._halts.report_condition(HaltType.K9, None, "strategy_resumed", True, f"상태 전이 → {to.value}",
                                         owner=owner)
            self._halts.resume(HaltType.K9, None, "LOCAL", actor, owner=owner)

    def sync_k9_from_state(self, owner: str, actor: str) -> str:
        """저장소의 전략 상태에 K9를 맞춘다 (RV-20). 엔진 밖 도구(scripts/strategy.py)가 전이를 기록하고 운영 명령
        STRATEGY_SYNC를 남기면, 엔진이 이 함수로 K9를 발동·해제한다. 상태는 바꾸지 않는다(승인 A-ID 전이는 이미 기록됨,
        INV-13). 같은 상태로 여러 번 불러도 결과가 같다(이미 있는 K9는 다시 발동해도 같은 행, 취소 재전송 없음 RV-07)."""
        if self._halts is None:
            raise ValueError("정지 관리자가 연결되지 않음")
        state = self.state_of(owner)
        if state is None:
            raise ValueError(f"등록되지 않은 전략 {owner}")
        sid, _, ver = owner.rpartition(":")
        row = self._repo.strategy_state(sid, int(ver), self._market)
        assert row is not None
        if state is StrategyState.STOPPED:
            reason = StopReason(row["stop_reason"]) if row["stop_reason"] else StopReason.USER
            self._halts.activate(HaltType.K9, actor, f"전략 중지 ({reason.value})", None, owner=owner,
                                 variant=reason.value)
            return f"K9 발동·유지 ({reason.value})"
        if state is StrategyState.RETIRED:
            self._halts.activate(HaltType.K9, actor, "전략 폐기", None, owner=owner, variant=StopReason.USER.value)
            return "K9 발동·유지 (폐기)"
        if not any(r["halt_type"] == HaltType.K9.value and r["scope_owner"] == owner
                   for r in self._repo.halt_rows(None)):
            return "K9 없음"
        self._halts.report_condition(HaltType.K9, None, "strategy_resumed", True, f"상태 {state.value}", owner=owner)
        self._halts.resume(HaltType.K9, None, "LOCAL", actor, owner=owner)
        return f"K9 해제 (상태 {state.value})"

    # --- 게이트 입력 (StrategyFacts) ---------------------------------------------------
    def state_of(self, owner: str) -> StrategyState | None:
        sid, _, ver = owner.rpartition(":")
        if not sid or not ver.isdigit():
            return None
        row = self._repo.strategy_state(sid, int(ver), self._market)
        return StrategyState(row["state"]) if row else None

    def signal_rejection(self, owner: str, request_type: RequestType) -> str | None:
        state = self.state_of(owner)
        if state is None:
            return "등록되지 않은 전략 (R-17)"
        if state in (StrategyState.STOPPED, StrategyState.RETIRED):
            # 위험 축소는 K9 정지표(사유별)가 판정한다. 위험 확대는 여기서 거부
            return f"전략 상태 {state.value}: 위험 확대 불가" if request_type in _RISK_UP else None
        if state not in _SIGNAL_STATES[self._repo.mode]:
            return f"전략 상태 {state.value}에서는 {self._repo.mode.value} 신호 불가 (00 §2.11, V-113)"
        return None

    def strategy_limits(self, owner: str) -> StrategyLimits | None:
        sid, _, ver = owner.rpartition(":")
        if not sid or not ver.isdigit():
            return None
        reg = self._repo.latest_registration(sid, int(ver))
        if reg is None:
            return None
        lim = json.loads(reg["spec_json"]).get("limits", {})
        values = {k: (None if v is None else Decimal(v)) for k, v in lim.items()}
        return None if all(v is None for v in values.values()) else StrategyLimits(**values)
