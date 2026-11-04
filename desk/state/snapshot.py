"""Lean per-bot snapshots: only the named fields the questions read. Nulls stay null."""
from __future__ import annotations

import json

from ..util import r
from .market import fair_yes

GAMBLER_FIELDS = [
    "mid", "spread_c", "spread_avg_10m_c", "imbalance3", "ret_5m_c", "ret_15m_c", "ret_60m_c", "rv_30m_c",
    "dist_hi_c", "dist_lo_c", "vol_30m_vs_pace", "taker_buy_share_15m", "minutes_to_close",
]
HOUSE_FIELDS = [
    "mid", "spread_c", "spread_avg_10m_c", "imbalance3", "ret_5m_c", "ret_15m_c", "rv_30m_c",
    "hi_60m", "lo_60m", "vol_5m", "vol_30m", "vol_30m_vs_pace", "taker_buy_share_15m", "minutes_to_close",
]


def with_fair(feat: dict, und: dict, sigma: float | None = None) -> dict:
    """sigma: market-implied vol from the event ladder when available, else realized spot vol."""
    sig = sigma or und.get("spot_rv_ann")
    fv = fair_yes(feat.get("strike_type"), feat.get("floor_strike"), feat.get("cap_strike"),
                  und.get("spot"), sig, feat.get("minutes_to_close"))
    out = dict(feat)
    out["sigma_used"] = sig
    out["fair_yes"] = r(fv, 4) if fv is not None else None
    out["model_edge_c"] = r((fv - feat["mid"]) * 100, 2) if (fv is not None and feat.get("mid") is not None) else None
    return out


FLOW_FIELDS = ["spot_cvd_1m_btc", "spot_cvd_5m_btc", "spot_cvd_15m_btc", "spot_buy_share_5m", "spot_vol_5m_vs_60m",
               "spot_big_net_5m", "spot_book_imb_10bps", "spot_book_imb_50bps", "flow_venues"]


def yes_direction(strike_type: str | None) -> int:
    """+1 if YES wins when spot rises, -1 if YES wins when spot falls, 0 if neither (range buckets)."""
    if strike_type in ("greater", "greater_or_equal"):
        return 1
    if strike_type in ("less", "less_or_equal"):
        return -1
    return 0


def gambler(feat: dict, und: dict, flow: dict | None = None) -> dict:
    s = {k: feat.get(k) for k in GAMBLER_FIELDS}
    flow = flow or {}
    for k in FLOW_FIELDS:
        s[k] = flow.get(k)
    s["yes_wins_if_spot"] = {1: "rises", -1: "falls"}.get(yes_direction(feat.get("strike_type")), "range")
    s["fair_yes"] = feat.get("fair_yes")
    s["model_edge_c"] = feat.get("model_edge_c")
    s["spot_ret_5m_bps"] = und.get("spot_ret_5m_bps")
    s["spot_ret_15m_bps"] = und.get("spot_ret_15m_bps")
    s["spot_rv_ann"] = und.get("spot_rv_ann")
    return s


def house(feat: dict, und: dict, inventory: float, band: tuple[float, float] | None, session: dict) -> dict:
    s = {k: feat.get(k) for k in HOUSE_FIELDS}
    s["fair_yes"] = feat.get("fair_yes")
    s["spot_ret_5m_bps"] = und.get("spot_ret_5m_bps")
    s["spot_rv_ann"] = und.get("spot_rv_ann")
    s["inventory"] = inventory
    s["band_lo"], s["band_hi"] = (band if band else (None, None))
    s["fills_30m"] = session.get("fills_30m")
    s["adverse_fills_30m"] = session.get("adverse_fills_30m")
    return s


def candidate_line(cid: str, feat: dict) -> str:
    """Two-line summary for comparing candidates (no dossiers)."""
    def g(k, fmt="{}"):
        v = feat.get(k)
        return "null" if v is None else fmt.format(v)
    return (f"{cid}: {feat.get('ticker')} | mid {g('mid')} spread {g('spread_c')}c | vol24h {g('vol_24h')} "
            f"| {g('minutes_to_close')}m to close\n"
            f"    fair {g('fair_yes')} edge {g('model_edge_c')}c | ret15 {g('ret_15m_c')}c | rv30 {g('rv_30m_c')}c "
            f"| imb {g('imbalance3')}")


def est_tokens(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"))) // 4 + 1
