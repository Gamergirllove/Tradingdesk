"""Halftime rule for 15-minute "BTC up?" markets, as specified by the desk owner:
take a side that is 70c+ at halftime when BTC is $100+ past the target on that side and the move is speeding up.
Pure arithmetic on numbers code already has; it runs alongside the bot's other checks (both must pass)."""
from __future__ import annotations


def check(feat: dict, spot: float | None, vwaps: list, side: str, rule: dict) -> dict:
    """side: 'yes' or 'no'. Returns per-condition values and an overall ok."""
    mtc = feat.get("minutes_to_close")
    mid = feat.get("mid")
    k = feat.get("floor_strike")
    half, latest = float(rule.get("half_minutes", 7.5)), float(rule.get("latest_minutes", 2))
    pmin, dmin = float(rule.get("min_price", 0.70)), float(rule.get("min_distance_usd", 100))
    sign = 1 if side == "yes" else -1
    side_px = None if mid is None else (mid if side == "yes" else 1 - mid)
    dist = None if (spot is None or k is None) else (spot - k) * sign
    d1 = d2 = None
    if len(vwaps) >= 3 and None not in vwaps[:3]:
        d1, d2 = (vwaps[0] - vwaps[1]) * sign, (vwaps[1] - vwaps[2]) * sign
    out = {
        "time": {"v": mtc, "ok": mtc is not None and latest <= mtc <= half},
        "price": {"v": side_px, "ok": side_px is not None and side_px >= pmin},
        "distance": {"v": None if dist is None else round(dist, 2), "ok": dist is not None and dist >= dmin},
        "accel": {"v": None if d1 is None else round(d1 - d2, 2), "d1": None if d1 is None else round(d1, 2),
                  "ok": (d1 is not None and d1 > 0 and d1 > d2) or not rule.get("require_accel", True)},
    }
    out["ok"] = all(x["ok"] for x in out.values())
    out["side"] = side
    return out


def best_side(feat: dict) -> str:
    mid = feat.get("mid")
    return "yes" if (mid is None or mid >= 0.5) else "no"
