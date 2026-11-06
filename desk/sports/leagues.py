"""Kalshi game series -> ESPN league. Only series with an ESPN feed can be priced, so only those are traded."""
from __future__ import annotations

# series: (espn sport/league path, scoreboard param sets (merged), label)
LEAGUES: dict[str, tuple[str, list[dict], str]] = {
    "KXNFLGAME": ("football/nfl", [{}], "NFL"),
    "KXNCAAFGAME": ("football/college-football", [{"groups": 80}, {"groups": 81}], "College FB"),
    "KXNBAGAME": ("basketball/nba", [{}], "NBA"),
    "KXNHLGAME": ("hockey/nhl", [{}], "NHL"),
}

# ESPN publishes a live win probability for these; NHL has none, so NHL is pregame only.
LIVE_WP = {"football/nfl", "football/college-football", "basketball/nba"}


def league(series: str):
    return LEAGUES.get(series)
