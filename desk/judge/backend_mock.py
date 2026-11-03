"""Deterministic offline judge for tests only. Answers are tagged backend=mock and never calibrate."""
from __future__ import annotations

import math
import time

from ..questions.base import Q
from .backend_claude import BackendResult


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


class MockBackend:
    name = "mock"
    version = "mock-judge-1"

    def __init__(self, delay_s: float = 0.0, fail: str | None = None):
        self.delay_s = delay_s
        self.fail = fail
        self.calls = 0

    async def ask(self, qs: list[Q], state: dict) -> BackendResult:
        import asyncio
        from .backend_claude import BackendError
        self.calls += 1
        if self.fail:
            raise BackendError(self.fail, "mock failure")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        t0 = time.time()
        g = lambda k: float(state.get(k) or 0.0)
        mom = g("ret_15m_c") + 0.5 * g("ret_5m_c")
        edge = g("model_edge_c")
        raw = {}
        for q in qs:
            if q.kind == "noul":
                if q.id == "pressure":
                    p = _sig(0.4 * mom + 2 * g("imbalance3"))
                elif q.id == "exec_degraded":
                    p = _sig(g("spread_c") - g("spread_avg_10m_c") - 1)
                elif q.id == "toxic":
                    p = _sig(abs(g("spot_ret_5m_bps")) / 10 - 1.5)
                elif q.id == "volume_alive":
                    p = _sig(3 * (g("vol_30m_vs_pace") - 0.3)) if state.get("vol_30m_vs_pace") is not None else 0.5
                elif q.id.startswith("worth_"):
                    p = 0.65 if q.id == "worth_A" else 0.45
                else:
                    p = _sig(abs(edge) / 3 - 1)
                raw[q.id] = {"p_true": round(p, 3)}
            elif q.kind == "choice":
                if q.id == "regime":
                    t = _sig(abs(mom) / 2 - 1)
                    raw[q.id] = {"trending": t, "mean_reverting": (1 - t) * 0.7, "chaotic": (1 - t) * 0.25,
                                 "other": (1 - t) * 0.05}
                elif q.id == "action":
                    up, dn = _sig((edge + mom) / 3 - 1), _sig((-edge - mom) / 3 - 1)
                    raw[q.id] = {"buy_yes": up, "buy_no": dn, "hold": 1.0, "other": 0.02}
                elif q.id == "range_state":
                    br = _sig(abs(g("ret_15m_c")) / 3 - 2)
                    raw[q.id] = {"inside": 1 - br, "testing_edge": 0.2, "breaking_out": br, "other": 0.02}
                else:
                    raw[q.id] = {o: (1.0 if i == 0 else 0.5) for i, o in enumerate(q.options)}
            else:
                lv = list(q.levels)
                s = _sig(1.5 - g("rv_30m_c")) if q.id == "env" else _sig(abs(edge) / 3 - 0.5)
                raw[q.id] = {f"L{k}": (s if int(k) >= 2 else 1 - s) / max(1, len(lv) / 2) for k in lv}
        return BackendResult(raw=raw, model_version=self.version, usage={"input_tokens": 0, "output_tokens": 0},
                             cost_usd=0.0, latency_ms=int((time.time() - t0) * 1000) + int(self.delay_s * 1000))
