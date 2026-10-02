"""판정 실행기 (00 §2.5~§2.7, D-006·D-007). 전략 출력을 검사해 게이트에 넣을 신호만 남긴다.

- 전략 코드가 예외를 내면 그 판정의 신호는 없다(불확실하면 주문하지 않음). 사유를 돌려준다.
- 형식 검사: 전략 ID·버전·판정 ID·계좌 일치, 유니버스 안 종목, 데이터 품질 실패 종목 제외, 수량 단위.
- 종목당 1개 (D-006): 같은 종목에 여러 신호면 청산 > 감액 > 증액 > 진입, 같은 종류는 규칙 우선순위.
- 기한 (D-007): 기한 ≤ 판정 기준 시각 + 절대 상한, 시각·주기 트리거면 ≤ 다음 예정 판정 시각. 넘으면 버린다
  (크기·기한을 고쳐서 대신 내지 않는다 — 게이트와 같은 원칙, 00 D-009).
- 결과 순서 (00 §2.7): 위험 축소 먼저, 그다음 종목코드 순서. 매번 같은 결과.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from trading_system.domain.enums import RequestType
from trading_system.domain.models import Signal
from trading_system.domain.strategy import KIND_ORDER, EvaluationSnapshot
from trading_system.strategy.base import ParamValues, Strategy


@dataclass(frozen=True)
class Dropped:
    instrument: str
    rule_id: str
    reason: str


@dataclass
class EvaluationResult:
    signals: list[Signal] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    error: str | None = None


def _rank(spec_rules: dict[str, int], s: Signal) -> tuple[int, int]:
    return KIND_ORDER.index(s.kind), spec_rules.get(s.rule_id, 1_000_000)


def run_evaluation(strategy: Strategy, snapshot: EvaluationSnapshot, params: ParamValues,
                   lot_of: dict[str, Decimal] | None = None) -> EvaluationResult:
    spec = strategy.spec
    result = EvaluationResult()
    try:
        raw = list(strategy.evaluate(snapshot, params))
    except Exception as exc:  # noqa: BLE001 — 전략 오류는 판정 실패로 기록, 신호 없음
        result.error = f"전략 실행 오류: {type(exc).__name__}: {exc}"
        return result

    rules = {r.rule_id: r for r in spec.rules}
    priorities = {r.rule_id: r.priority for r in spec.rules}
    universe = set(spec.universe) if spec.universe_method in ("U-A",) else {v.instrument for v in snapshot.instruments}
    hard_cap = snapshot.as_of + timedelta(minutes=spec.validity_max_minutes)

    valid: list[Signal] = []
    for s in raw:
        reason = _check(s, spec, snapshot, rules, universe, hard_cap, lot_of or {})
        if reason:
            result.dropped.append(Dropped(getattr(s, "instrument", "?"), getattr(s, "rule_id", "?"), reason))
        else:
            valid.append(s)

    by_inst: dict[str, list[Signal]] = {}
    for s in valid:
        by_inst.setdefault(s.instrument, []).append(s)
    chosen: list[Signal] = []
    for inst, group in by_inst.items():
        group.sort(key=lambda s: _rank(priorities, s))
        chosen.append(group[0])
        for loser in group[1:]:
            result.dropped.append(Dropped(inst, loser.rule_id, f"종목당 1개 (D-006): {group[0].rule_id} 우선"))
    reducing = {RequestType.EXIT, RequestType.REDUCE}
    chosen.sort(key=lambda s: (0 if s.kind in reducing else 1, s.instrument))
    result.signals = chosen
    return result


def _check(s: object, spec, snapshot: EvaluationSnapshot, rules: dict, universe: set[str],  # type: ignore[no-untyped-def]
           hard_cap, lot_of: dict[str, Decimal]) -> str | None:
    if not isinstance(s, Signal):
        return "신호 형식이 아님 (00 §2.5)"
    if (s.strategy_id, s.strategy_version) != (spec.strategy_id, spec.version):
        return "다른 전략·버전의 신호"
    if s.evaluation_id != snapshot.evaluation_id or s.as_of != snapshot.as_of or s.account != snapshot.account:
        return "판정 ID·기준 시각·계좌 불일치"
    rule = rules.get(s.rule_id)
    if rule is None:
        return f"규칙 표에 없는 규칙 ID {s.rule_id}"
    if rule.kind != s.kind.value:
        return f"규칙 종류({rule.kind})와 신호 종류({s.kind.value}) 불일치"
    if s.instrument not in universe:
        return "유니버스 밖 종목 (00 D-012·D-014)"
    view = snapshot.view(s.instrument)
    if view is None or not view.data_ok:
        return "입력 데이터 없음·품질 실패 종목 (R-22)"
    lot = lot_of.get(s.instrument, Decimal("1"))
    if s.quantity % lot != 0:
        return f"수량 단위({lot}) 배수가 아님"
    if s.valid_until <= s.as_of:
        return "기한이 판정 기준 시각 이전"
    if s.valid_until > hard_cap:
        return f"기한이 절대 상한({spec.validity_max_minutes}분) 초과 (D-007)"
    if snapshot.next_evaluation_at is not None and s.valid_until > snapshot.next_evaluation_at:
        return "기한이 다음 예정 판정 시각 이후 (00 §2.6)"
    if s.stop_price is not None and s.kind not in (RequestType.ENTRY, RequestType.INCREASE):
        return "손절가는 진입·증액에만"
    if s.stop_price is not None and not (Decimal("0") < s.stop_price < s.price):
        return "손절가가 진입 기준가보다 낮은 양수가 아님"
    if s.kind in (RequestType.REDUCE, RequestType.EXIT):
        pos = view.position
        if pos is None or not pos.owned_by_me or s.quantity > pos.quantity:
            return "보유·소유하지 않았거나 보유 수량 초과 매도 (00 §2.8)"
        if s.kind is RequestType.EXIT and s.quantity != pos.quantity:
            return "청산 수량은 보유 전량이어야 함 (00 §2.8)"
    return None
