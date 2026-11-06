"""Match Kalshi game events to ESPN events by date and both team names (code only, no guessing by the AI)."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
SYN = {"la": "los angeles", "ny": "new york", "utd": "united", "intl": "international", "univ": "university",
       "mt": "mount", "ft": "fort", "n": "north", "s": "south", "e": "east", "w": "west"}
DROP = {"fc", "cf", "sc", "afc", "ac", "the", "club", "de", "cd", "sk", "fk", "if", "bk", "and"}


def norm(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"^st\.?\s", "saint ", s)
    s = re.sub(r"\bst\.?$", "state", s.strip())
    s = re.sub(r"\bst\.\s", "saint ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = []
    for t in s.split():
        toks.extend(SYN.get(t, t).split())
    return [t for t in toks if t not in DROP] or toks


def event_date(event_ticker: str) -> dt.date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return dt.date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def espn_date(e: dict) -> dt.date | None:
    try:
        return dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET).date()
    except Exception:
        return None


def _tok_match(a: str, b: str) -> bool:
    return a == b or (len(a) <= 2 and b.startswith(a)) or (len(a) >= 4 and b.startswith(a)) or (len(b) >= 4 and a.startswith(b))


def name_score(kname: str, kcode: str, team: dict) -> float:
    K = norm(kname)
    if not K:
        return 0.0
    best = 0.0
    for f in ("displayName", "location", "shortDisplayName", "name"):
        C = norm(team.get(f) or "")
        if not C:
            continue
        if K == C:
            return 1.0
        mk = sum(1 for k in K if any(_tok_match(k, c) for c in C))
        mc = sum(1 for c in C if any(_tok_match(k, c) for k in K))
        best = max(best, 0.8 * mk / len(K) + 0.2 * mc / len(C) if mk else 0.0)
    if kcode and (team.get("abbreviation") or "").upper() == kcode.upper():
        best = max(best, 0.9)
    return round(best, 3)


def teams_of(e: dict) -> dict:
    """{'home': team, 'away': team} from an ESPN event."""
    c = e["competitions"][0]
    return {x["homeAway"]: x["team"] for x in c["competitors"]}


def match_event(kmarkets: list[dict], espn_events: list[dict], min_team: float = 0.5, min_margin: float = 0.2):
    """kmarkets: the Kalshi markets of one event (2 teams, or 2 teams + Tie). Returns (espn_event, {ticker: side})
    where side is 'home' | 'away' | 'draw', or (None, reason)."""
    d = event_date(kmarkets[0]["event_ticker"])
    sides = [m for m in kmarkets if (m["ticker"].split("-")[-1].upper() != "TIE")]
    tie = [m for m in kmarkets if m["ticker"].split("-")[-1].upper() == "TIE"]
    if len(sides) != 2:
        return None, f"{len(sides)} team markets"
    scored = []
    for e in espn_events:
        ed = espn_date(e)
        if d and ed and abs((ed - d).days) > 1:
            continue
        tm = teams_of(e)
        if "home" not in tm or "away" not in tm:
            continue
        a, b = sides
        ca, cb = a["ticker"].split("-")[-1], b["ticker"].split("-")[-1]
        s1 = (name_score(a["yes_sub_title"], ca, tm["home"]), name_score(b["yes_sub_title"], cb, tm["away"]))
        s2 = (name_score(a["yes_sub_title"], ca, tm["away"]), name_score(b["yes_sub_title"], cb, tm["home"]))
        if sum(s1) >= sum(s2):
            sc, amap = s1, {a["ticker"]: "home", b["ticker"]: "away"}
        else:
            sc, amap = s2, {a["ticker"]: "away", b["ticker"]: "home"}
        bonus = 0.05 if (d and ed == d) else 0.0
        scored.append((sum(sc) + bonus, min(sc), e, amap))
    if not scored:
        return None, "no ESPN game that day"
    scored.sort(key=lambda x: -x[0])
    tot, low, e, amap = scored[0]
    if low < min_team:
        return None, f"names don't match well ({low:.2f})"
    if len(scored) > 1 and tot - scored[1][0] < min_margin:
        return None, "two ESPN games match"
    for m in tie:
        amap[m["ticker"]] = "draw"
    return e, amap
