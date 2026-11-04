"""Gambler battery: bets on direction. One call per decision."""
from .base import Q, choice, noul, score

BATTERY: list[Q] = [
    choice("regime", "What regime is this market's YES price in right now?",
           {"trending": "moving persistently one way",
            "mean_reverting": "oscillating around a level, moves fade",
            "chaotic": "large erratic moves with no persistence"},
           ["ret_5m_c", "ret_15m_c", "ret_60m_c", "rv_30m_c", "spot_ret_15m_bps"]),
    noul("pressure", "Buying pressure on YES is building right now.",
         ["imbalance3", "taker_buy_share_15m", "ret_5m_c", "spot_cvd_5m_btc", "spot_book_imb_10bps"]),
    score("setup", "How well does the state match this rulebook setup: the code's fair-value edge and the "
                   "recent momentum point the same way, the spread is tight, and there is enough time before close?",
          {0: "no match", 1: "one element present", 2: "most elements present", 3: "every element present and strong"},
          ["model_edge_c", "ret_15m_c", "spread_c", "minutes_to_close"]),
    choice("action", "Best action on this market right now, after costs?",
           {"buy_yes": "open a long YES position", "buy_no": "open a long NO position", "hold": "do nothing"},
           ["model_edge_c", "ret_15m_c", "imbalance3", "spread_c"]),
    noul("spot_up", "Aggregated BTC spot order flow across Coinbase, Kraken, Bitstamp and Gemini (taker buy-minus-sell "
                    "volume and resting book depth) points to BTC trading higher 15 minutes from now.",
         ["spot_cvd_1m_btc", "spot_cvd_5m_btc", "spot_cvd_15m_btc", "spot_buy_share_5m", "spot_big_net_5m",
          "spot_book_imb_10bps", "spot_book_imb_50bps", "spot_ret_5m_bps"]),
    noul("worth", "Any trade in this market is worth taking right now at all, after the spread and fees.",
         ["model_edge_c", "spread_c", "rv_30m_c", "minutes_to_close"]),
    noul("exec_degraded", "Execution quality has degraded in the last minutes (spread widening, thin top of "
                          "book, erratic quotes).",
         ["spread_c", "spread_avg_10m_c", "imbalance3"]),
]
