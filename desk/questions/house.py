"""House battery: paid by activity, not direction. Chop is the meal, a trend is the risk."""
from .base import Q, choice, noul, score

BATTERY: list[Q] = [
    choice("range_state", "Where is the YES price relative to its recent range and the quoting band?",
           {"inside": "well inside the range", "testing_edge": "pressing against a range edge",
            "breaking_out": "leaving the range with follow-through"},
           ["mid", "hi_60m", "lo_60m", "band_lo", "band_hi", "ret_5m_c", "ret_15m_c"]),
    noul("toxic", "Incoming flow is toxic: informed traders are likely to pick off resting quotes "
                  "(the underlying is moving fast and YES is lagging, or fills keep going against us).",
         ["spot_ret_5m_bps", "ret_5m_c", "adverse_fills_30m", "fills_30m", "taker_buy_share_15m"]),
    score("env", "Is this an environment worth quoting into at all?",
          {0: "no: pull everything", 1: "marginal", 2: "decent: normal quotes", 3: "ideal two-way chop"},
          ["spread_c", "rv_30m_c", "vol_30m_vs_pace", "minutes_to_close"]),
    noul("volume_alive", "Volume is still flowing through this market.",
         ["vol_5m", "vol_30m", "vol_30m_vs_pace"]),
]
