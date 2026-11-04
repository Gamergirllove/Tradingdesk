"""Code applies policy. Weights and thresholds live in config; the model never sizes or triggers."""
from __future__ import annotations

from dataclasses import dataclass, field

from .calibration import Calibrator


@dataclass
class Decision:
    action: str                    # buy_yes | buy_no | hold | quote | widen | pause | pull | kill
    reasons: list[str] = field(default_factory=list)
    scores: dict = field(default_factory=dict)


def _p(ans: dict, qid: str, opt: str | None = None) -> float | None:
    a = ans.get(qid)
    if not a:
        return None
    if opt is None:
        return a.get("p")
    return (a.get("probs") or {}).get(opt)


def gambler(ans: dict, snap: dict, pol: dict, cal: Calibrator) -> Decision:
    c = lambda key, p: None if p is None else cal(f"gambler.{key}", p)
    trend = c("regime.trending", _p(ans, "regime", "trending"))
    regime_pick = (ans.get("regime") or {}).get("pick")
    setup_ge2 = (ans.get("setup") or {}).get("p_ge", {}).get("2")
    setup_ge2 = c("setup.ge2", setup_ge2)
    act = (ans.get("action") or {}).get("pick")
    conf = c(f"action.{act}", _p(ans, "action", act)) if act else None
    worth = c("worth", _p(ans, "worth"))
    degraded = c("exec_degraded", _p(ans, "exec_degraded"))
    edge = snap.get("model_edge_c")
    p_up = c("spot_up", _p(ans, "spot_up"))
    ydir = {"rises": 1, "falls": -1}.get(snap.get("yes_wins_if_spot"), 0)
    flow_yes = None if (p_up is None or ydir == 0) else (p_up if ydir > 0 else 1 - p_up)
    sc = {"trend": trend, "setup_ge2": setup_ge2, "action": act, "action_conf": conf, "worth": worth,
          "exec_degraded": degraded, "model_edge_c": edge, "spot_up": p_up, "flow_favors_yes": flow_yes}
    why = []
    if None in (trend, setup_ge2, conf, worth, degraded):
        return Decision("hold", ["missing answer"], sc)
    if regime_pick != "trending" or trend < pol["trend_min"]:
        why.append(f"regime {regime_pick} ({trend:.2f})")
    if setup_ge2 < pol["setup_ge2_min"]:
        why.append(f"setup>=2 {setup_ge2:.2f}")
    if act not in ("buy_yes", "buy_no"):
        why.append(f"action {act}")
    elif conf < pol["action_conf_min"]:
        why.append(f"action conf {conf:.2f}")
    if worth < pol["worth_min"]:
        why.append(f"worth {worth:.2f}")
    if degraded > pol["exec_degraded_max"]:
        why.append(f"exec degraded {degraded:.2f}")
    fmin = pol.get("flow_min", 0.55)
    if flow_yes is None:
        why.append("no spot flow read")
    elif act == "buy_yes" and flow_yes < fmin:
        why.append(f"spot flow favors YES only {flow_yes:.2f}")
    elif act == "buy_no" and flow_yes > 1 - fmin:
        why.append(f"spot flow favors NO only {1 - flow_yes:.2f}")
    if edge is None:
        why.append("no fair value")
    elif act == "buy_yes" and edge < pol["min_model_edge_c"]:
        why.append(f"code edge {edge}c < {pol['min_model_edge_c']}c for YES")
    elif act == "buy_no" and edge > -pol["min_model_edge_c"]:
        why.append(f"code edge {edge}c > -{pol['min_model_edge_c']}c for NO")
    if why:
        return Decision("hold", why, sc)
    return Decision(act, ["all gates passed"], sc)


def house(ans: dict, pol: dict, cal: Calibrator) -> Decision:
    c = lambda key, p: None if p is None else cal(f"house.{key}", p)
    brk = c("range_state.breaking_out", _p(ans, "range_state", "breaking_out"))
    toxic = c("toxic", _p(ans, "toxic"))
    env_ge2 = c("env.ge2", (ans.get("env") or {}).get("p_ge", {}).get("2"))
    alive = c("volume_alive", _p(ans, "volume_alive"))
    sc = {"breakout": brk, "toxic": toxic, "env_ge2": env_ge2, "volume_alive": alive}
    if None in (brk, toxic, env_ge2, alive):
        return Decision("pull", ["missing answer"], sc)
    if alive < pol["volume_alive_min"]:
        return Decision("kill", [f"volume dying {alive:.2f}"], sc)
    if brk >= pol["breakout_pause"]:
        return Decision("pause", [f"breakout {brk:.2f}"], sc)
    if toxic >= pol["toxic_pull"]:
        return Decision("pull", [f"toxic {toxic:.2f}"], sc)
    if env_ge2 < pol["env_ge2_min"]:
        return Decision("pull", [f"env>=2 {env_ge2:.2f}"], sc)
    if toxic >= pol["toxic_widen"]:
        return Decision("widen", [f"toxic {toxic:.2f}"], sc)
    return Decision("quote", ["all gates passed"], sc)


def scan_pick(ans: dict, cids: list[str], pol: dict) -> tuple[str | None, dict]:
    worth = {c: _p(ans, f"worth_{c}") for c in cids}
    if len(cids) == 1:
        c = cids[0]
        ok = (worth.get(c) or 0) >= pol["worth_min"]
        return (c if ok else None), {"worth": worth}
    probs = (ans.get("best") or {}).get("probs") or {}
    if (probs.get("other") or 0) >= max((probs.get(c) or 0) for c in cids):
        return None, {"worth": worth, "best": probs, "note": "other won the choice"}
    ranked = sorted((c for c in cids), key=lambda c: -(probs.get(c) or 0))
    for c in ranked:
        if (worth.get(c) or 0) >= pol["worth_min"]:
            return c, {"worth": worth, "best": probs}
    return None, {"worth": worth, "best": probs}
