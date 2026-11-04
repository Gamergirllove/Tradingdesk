"""Rules judge: free, deterministic answers to the same batteries, computed from the state code already built.

It stands in for Claude when there is no API credit. It never calibrates (only backend=claude answers are
labelled), and switching judges clears the version pin so thresholds re-pin on the next Claude answer.
"""
from __future__ import annotations

import math
import time

from ..questions.base import Q
from .backend_claude import BackendResult

VERSION = "rules-v1"


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def _num(state: dict, k: str):
    v = state.get(k)
    return v if isinstance(v, (int, float)) else None


def _peaked(options: list[str], pick: str, p: float) -> dict:
    rest = (1 - p) / max(1, len(options) - 1)
    return {o: (p if o == pick else rest) for o in options}


class RulesBackend:
    name = "rules"
    version = VERSION

    async def ask(self, qs: list[Q], state: dict) -> BackendResult:
        t0 = time.time()
        raw = {q.id: self.answer(q, state) for q in qs}
        return BackendResult(raw=raw, model_version=VERSION, usage={"input_tokens": 0, "output_tokens": 0},
                             cost_usd=0.0, latency_ms=int((time.time() - t0) * 1000))

    def answer(self, q: Q, s: dict) -> dict:
        g = lambda k: _num(s, k)
        edge, sp = g("model_edge_c"), g("spread_c")
        r5, r15, rv = g("ret_5m_c"), g("ret_15m_c"), g("rv_30m_c")

        # ---------- scan (candidates already ranked by code; A is the funnel's best) ----------
        if q.id == "best":
            return _peaked(q.options, q.options[0], 0.6)
        if q.id.startswith("worth_"):
            return {"p_true": 0.6}

        # ---------- gambler ----------
        if q.id == "regime":
            s15, s5 = g("spot_ret_15m_bps"), g("spot_ret_5m_bps")
            if r15 is None or rv is None:
                if s15 is None:
                    return {o: 1 / len(q.options) for o in q.options}
                trending = abs(s15) >= 8 and (s5 is None or s5 * s15 >= 0)      # BTC itself is moving one way
                return _peaked(q.options, "trending" if trending else "mean_reverting", 0.65)
            trending = abs(r15) >= max(2.0, 2 * rv) and (r5 is None or r5 * r15 >= 0)
            pick = "trending" if trending else ("chaotic" if rv >= 3 else "mean_reverting")
            return _peaked(q.options, pick, 0.7)
        if q.id == "pressure":
            x = 2 * (g("imbalance3") or 0) + 6 * ((g("taker_buy_share_15m") or 0.5) - 0.5) + 0.4 * (r5 or 0) \
                + 0.1 * (g("spot_cvd_5m_btc") or 0)
            return {"p_true": round(_sig(x), 4)}
        if q.id == "setup":
            if edge is None:
                return {f"L{k}": (0.85 if k == "0" else 0.05) for k in q.levels}
            n = 0
            ydir = {"rises": 1, "falls": -1}.get(s.get("yes_wins_if_spot"), 0)
            mom = r15 if r15 is not None else ((g("spot_ret_15m_bps") or 0) * ydir)
            n += 1 if (abs(edge) >= 3 and mom * edge > 0) else 0
            n += 1 if (sp is not None and sp <= 2) else 0
            n += 1 if ((g("minutes_to_close") or 0) >= 60) else 0
            return {f"L{k}": (0.75 if int(k) == n else 0.25 / (len(q.levels) - 1)) for k in q.levels}
        if q.id == "action":
            pick = "hold" if edge is None else ("buy_yes" if edge >= 3 else ("buy_no" if edge <= -3 else "hold"))
            return _peaked(q.options, pick, 0.7)
        if q.id == "spot_up":
            cvd, imb, sr = g("spot_cvd_5m_btc"), g("spot_book_imb_10bps"), g("spot_ret_5m_bps")
            if cvd is None and imb is None:
                return {"p_true": 0.5}
            return {"p_true": round(_sig((cvd or 0) / 8 + 1.5 * (imb or 0) + (sr or 0) / 25), 4)}
        if q.id == "worth":
            if edge is None or sp is None:
                return {"p_true": 0.2}
            return {"p_true": round(_sig((abs(edge) - sp / 2 - 1.5 - 1) / 1.5), 4)}
        if q.id == "exec_degraded":
            avg = g("spread_avg_10m_c")
            if sp is None:
                return {"p_true": 0.7}
            return {"p_true": 0.8 if (avg is not None and sp > avg + 1) or sp > 4 else 0.15}

        # ---------- house ----------
        if q.id == "range_state":
            mid, lo, hi = g("mid"), g("band_lo"), g("band_hi")
            h60, l60 = g("hi_60m"), g("lo_60m")
            if mid is None:
                return {o: 1 / len(q.options) for o in q.options}
            if (lo is not None and hi is not None and not lo <= mid <= hi) or abs(r15 or 0) >= 4:
                pick = "breaking_out"
            elif h60 is not None and l60 is not None and (h60 - mid <= 0.01 or mid - l60 <= 0.01) and h60 - l60 >= 0.03:
                pick = "testing_edge"
            else:
                pick = "inside"
            return _peaked(q.options, pick, 0.7)
        if q.id == "toxic":
            fills, adv = g("fills_30m") or 0, g("adverse_fills_30m") or 0
            fast = abs(g("spot_ret_5m_bps") or 0) >= 15
            bad = fills >= 2 and adv / fills >= 0.5
            return {"p_true": 0.8 if (fast or bad) else 0.2}
        if q.id == "env":
            mtc = g("minutes_to_close") or 0
            n = 0
            n += 1 if (sp is not None and sp >= 1) else 0
            n += 1 if (rv is not None and rv <= 1.5) else 0
            n += 1 if mtc >= 60 else 0
            return {f"L{k}": (0.75 if int(k) == n else 0.25 / (len(q.levels) - 1)) for k in q.levels}
        if q.id == "volume_alive":
            ratio = g("vol_30m_vs_pace")
            if ratio is None:
                return {"p_true": 0.5}
            return {"p_true": 0.85 if ratio >= 0.3 else 0.15}

        # ---------- anything else: an honest "don't know" ----------
        if q.kind == "noul":
            return {"p_true": 0.5}
        if q.kind == "choice":
            return {o: 1 / len(q.options) for o in q.options}
        return {f"L{k}": 1 / len(q.levels) for k in q.levels}
