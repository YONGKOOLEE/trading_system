"""CP-034 T-041·T-042 — M1 PAPER 동작 불변 고정 (U-A 전략 paper0-verify v2).

U-B 목록·배분 규칙을 추가해도 U-A 전략의 명세 해시(등록 일치 검사 D-011), 판정 스냅샷, 판정 기록, 의도·전송이
바뀌지 않아야 한다. 아래 기대값은 변경 전 코드(main 783ca75)에서 얻은 값이다.
"""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

from tests.support.engine_world import ACCOUNT, EngineWorld
from trading_system.domain.strategy import EvaluationSnapshot
from trading_system.strategy.paper0_verify import Paper0Verify, Paper0VerifyV2

V2_PARAMS = {"qty": 1, "hold_days": 0, "split_time": "12:00"}


def test_spec_hashes_unchanged() -> None:
    # 등록부는 같은 ID·버전의 명세 해시가 다르면 거부한다(D-011) — PAPER DB의 기존 등록과 맞아야 한다
    assert Paper0VerifyV2().spec.spec_hash() == "6fec68af962f9dd3eca10b59ab4b8efff6ea44c0551680a709d9836d3644374c"
    assert Paper0Verify().spec.spec_hash() == "69ca8273940d58f02383493702e5a292528ff96168797532bb1ea7651a0e390d"


class _Spy(Paper0VerifyV2):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
        self.snapshots.append(snapshot)
        return super().evaluate(snapshot, params)


def test_paper0_v2_engine_round_trip_unchanged(tmp_path: Path) -> None:
    spy = _Spy()
    w = EngineWorld(tmp_path, [(spy, V2_PARAMS, (time(9, 30), time(14, 30)))], ["005930"])
    try:
        w.start_and_resume()
        w.at(9, 30)
        w.engine.tick()
        w.at(14, 30)
        w.engine.tick()
        w.at(14, 31)
        w.engine.tick()   # 체결 통보 반영
        rows = [(r["request_type"], r["instrument"], r["quantity"], r["state"], r["owner"]) for r in w.intents()]
        assert rows == [("ENTRY", "005930", "1", "FILLED", "paper0-verify:2"),
                        ("EXIT", "005930", "1", "FILLED", "paper0-verify:2")]
        assert len(w.broker.sent) == 2
        # 신호 ID(= 멱등키)는 판정 ID에서 결정적으로 나온다 — 변경 전 값과 같아야 한다
        assert [r["idem_key"] for r in w.intents()] == ["sig-27e2472374e80e6378f3b7735c7a654a",
                                                        "sig-fc610914e121ac3e85fdd4279e1e4bd4"]
        # 판정 기록(EVALUATION) 요약 키는 그대로 — U-B·배분 규칙 항목은 U-A 전략에 붙지 않는다
        evs = [json.loads(e["summary"]) for e in w.audits("EVALUATION")]
        assert [sorted(e) for e in evs] == [["dropped", "error", "signals", "strategy"]] * 2
        assert [(e["signals"], e["dropped"], e["error"]) for e in evs] == [(1, 0, None), (1, 0, None)]
        assert not w.audits("ALLOCATION_EVALUATION")
        # 스냅샷: 유니버스 종목만, 등재 시각 없음(U-A)
        for snap in spy.snapshots:
            assert [v.instrument for v in snap.instruments] == ["005930"]
            assert all(getattr(v, "listed_at", None) is None for v in snap.instruments)
        assert w.repo.position(ACCOUNT, "005930").quantity == 0
    finally:
        w.close()
