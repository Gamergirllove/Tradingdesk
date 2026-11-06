"""Sports battery: one call per finalist. Code already found the gap between Kalshi and the fair price."""
from .base import Q, noul

BATTERY: list[Q] = [
    noul("worth", "This bet is worth taking now: the gap between the Kalshi price and the fair price is real "
                  "(not stale data, a lineup or injury change the fair price hasn't caught, or a thin book), "
                  "after the spread and fees.",
         ["model_edge_c", "spread_c", "fair_source", "phase", "minutes_to_start", "game_clock", "score_diff",
          "kalshi_move_5m_c", "volume_24h", "price_c"]),
    noul("exec_degraded", "Execution quality has degraded in the last minutes (spread widening, thin top of "
                          "book, erratic quotes).",
         ["spread_c", "spread_avg_10m_c", "imbalance3"]),
]
