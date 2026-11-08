"""ESPN's public scoreboard and game summaries: pregame sportsbook moneylines and live win probability.

Free and unofficial: no key, no SLA. Every read is cached, and a failed read only means that game has no fair
price this cycle (no trade), never a guess.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import httpx

from .. import logs
from ..util import parse_ts

BASE = "https://site.api.espn.com/apis/site/v2/sports"


def american_to_prob(x) -> float | None:
    if x is None:
        return None
    s = str(x).strip().upper().replace("+", "")
    if s in ("EVEN", "EV", "PK", "PICK"):
        s = "100"
    try:
        v = float(s)
    except ValueError:
        return None
    if -100 < v < 100:
        return None
    return (-v) / (-v + 100) if v < 0 else 100 / (v + 100)


def _ml(o: dict, side: str):
    """Moneyline for 'home'/'away' from an ESPN odds block: the closing (latest pregame) line, else the open."""
    m = (o.get("moneyline") or {}).get(side) or {}
    for k in ("current", "close", "open"):
        v = (m.get(k) or {}).get("odds")
        if v not in (None, "", "OFF"):
            return v
    return (o.get(f"{side}TeamOdds") or {}).get("moneyLine")


def parse_game(e: dict) -> dict:
    c = e["competitions"][0]
    st = c.get("status", {}).get("type", {})
    teams = {}
    for x in c.get("competitors", []):
        t = x.get("team", {})
        teams[x.get("homeAway")] = {"abbreviation": t.get("abbreviation"), "displayName": t.get("displayName"),
                                    "shortDisplayName": t.get("shortDisplayName"), "name": t.get("name"),
                                    "location": t.get("location"), "score": x.get("score")}
    g = {"id": e.get("id"), "name": e.get("shortName") or e.get("name"), "start_ts": parse_ts(e.get("date")),
         "state": st.get("state"), "detail": st.get("shortDetail") or st.get("detail"), "teams": teams,
         "odds": None, "wp_home": None}
    odds = (c.get("odds") or [None])[0]
    if odds:
        ph, pa = american_to_prob(_ml(odds, "home")), american_to_prob(_ml(odds, "away"))
        if ph and pa:
            g["odds"] = {"provider": (odds.get("provider") or {}).get("name"), "p_home_raw": ph, "p_away_raw": pa,
                         "p_home": ph / (ph + pa), "p_away": pa / (ph + pa), "vig": round(ph + pa - 1, 4)}
    prob = ((c.get("situation") or {}).get("lastPlay") or {}).get("probability") or {}
    if prob.get("homeWinPercentage") is not None:
        g["wp_home"] = float(prob["homeWinPercentage"])
        g["wp_src"] = "scoreboard"
    return g


class Espn:
    def __init__(self, ttl_pre: float = 45, ttl_live: float = 20, ttl_summary: float = 15, timeout: float = 10):
        self.ttl_pre, self.ttl_live, self.ttl_summary = ttl_pre, ttl_live, ttl_summary
        self._sb: dict[tuple, tuple[float, list, bool]] = {}
        self._sum: dict[tuple, tuple[float, float | None]] = {}
        self._http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (trading-desk)"})
        self.sem = asyncio.Semaphore(6)
        self.errors = 0
        self.last_ok = 0.0

    async def close(self):
        await self._http.aclose()

    async def _get(self, url: str, params: dict) -> dict | None:
        async with self.sem:
            try:
                r = await self._http.get(url, params=params)
                if r.status_code != 200:
                    self.errors += 1
                    return None
                self.last_ok = time.time()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                self.errors += 1
                logs.write("events", {"kind": "espn_error", "url": url[-60:], "error": repr(e)[:160]})
                return None

    async def games(self, path: str, param_sets: list[dict], day: dt.date) -> list[dict]:
        """Raw ESPN events for one league and one US date (merged across param sets, e.g. FBS + FCS)."""
        key = (path, repr(param_sets), day)
        hit = self._sb.get(key)
        if hit and time.time() - hit[0] < (self.ttl_live if hit[2] else self.ttl_pre):
            return hit[1]
        evs: dict[str, dict] = {}
        ok = False
        for p in param_sets:
            j = await self._get(f"{BASE}/{path}/scoreboard", {"dates": f"{day:%Y%m%d}", "limit": 500, **p})
            if j is None:
                continue
            ok = True
            for e in j.get("events", []):
                evs[e["id"]] = e
        if not ok and hit:
            return hit[1]
        out = list(evs.values())
        live = any(e["competitions"][0].get("status", {}).get("type", {}).get("state") == "in" for e in out)
        self._sb[key] = (time.time(), out, live)
        return out

    async def live_wp(self, path: str, event_id: str) -> float | None:
        """Latest home win probability from the game summary (ESPN's in-game model)."""
        key = (path, event_id)
        hit = self._sum.get(key)
        if hit and time.time() - hit[0] < self.ttl_summary:
            return hit[1]
        j = await self._get(f"{BASE}/{path}/summary", {"event": event_id})
        wp = None
        if j:
            seq = j.get("winprobability") or []
            if seq and seq[-1].get("homeWinPercentage") is not None:
                wp = float(seq[-1]["homeWinPercentage"])
        self._sum[key] = (time.time(), wp)
        return wp
