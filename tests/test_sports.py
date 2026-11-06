import asyncio
import datetime as dt
import time

from desk.judge.client import Verdict
from desk.sports.espn import american_to_prob, parse_game
from desk.sports.match import match_event, norm
from desk.venues.kalshi import Book


def espn_event(eid, home, away, start, state="pre", home_ml="-150", away_ml="+130", wp=None, score=("0", "0")):
    def team(t, sc):
        loc, nick, ab = t
        return {"abbreviation": ab, "displayName": f"{loc} {nick}", "shortDisplayName": nick, "name": nick, "location": loc}
    comp = {"status": {"type": {"state": state, "shortDetail": "Q3 4:12" if state == "in" else "7:00 PM"}},
            "competitors": [{"homeAway": "home", "team": team(home, score[0]), "score": score[0]},
                            {"homeAway": "away", "team": team(away, score[1]), "score": score[1]}],
            "odds": [{"provider": {"name": "Draft Kings"},
                      "moneyline": {"home": {"close": {"odds": home_ml}}, "away": {"close": {"odds": away_ml}}}}]}
    if wp is not None:
        comp["situation"] = {"lastPlay": {"probability": {"homeWinPercentage": wp}}}
    return {"id": eid, "date": start, "shortName": f"{away[2]} @ {home[2]}", "competitions": [comp]}


def kmkt(event, code, name, bid, ask, vol=5000, series="KXNBAGAME"):
    return {"ticker": f"{event}-{code}", "event_ticker": event, "yes_sub_title": name, "title": f"{name} wins",
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "volume_24h_fp": str(vol),
            "status": "active", "_series": series}


def test_odds_and_devig():
    assert abs(american_to_prob("-200") - 2 / 3) < 1e-9 and abs(american_to_prob("+150") - 0.4) < 1e-9
    assert american_to_prob("EVEN") == 0.5 and american_to_prob("+50") is None and american_to_prob(None) is None
    g = parse_game(espn_event("1", ("Phoenix", "Suns", "PHX"), ("San Antonio", "Spurs", "SA"), "2026-11-10T03:00Z"))
    o = g["odds"]
    assert abs(o["p_home"] + o["p_away"] - 1) < 1e-9 and o["p_home"] > 0.55 and o["vig"] > 0


def test_names_and_matching():
    assert norm("Nicholls St.") == ["nicholls", "state"] and norm("St. Louis") == ["saint", "louis"]
    evs = [espn_event("10", ("New York", "Islanders", "NYI"), ("San Jose", "Sharks", "SJ"), "2026-11-10T00:00Z"),
           espn_event("11", ("Edmonton", "Oilers", "EDM"), ("New York", "Rangers", "NYR"), "2026-11-10T02:00Z")]
    ms = [kmkt("KXNHLGAME-26NOV09NYREDM", "NYR", "New York R", .4, .42), kmkt("KXNHLGAME-26NOV09NYREDM", "EDM", "Edmonton", .58, .6)]
    e, sides = match_event(ms, evs)
    assert e["id"] == "11" and sides == {"KXNHLGAME-26NOV09NYREDM-NYR": "away", "KXNHLGAME-26NOV09NYREDM-EDM": "home"}
    e2, why = match_event([kmkt("KXNHLGAME-26NOV09ABCXYZ", "ABC", "Nowhere", .4, .42),
                           kmkt("KXNHLGAME-26NOV09ABCXYZ", "XYZ", "Atlantis", .5, .52)], evs)
    assert e2 is None and "match" in why


class FakeJudge:
    def __init__(self, worth=0.8):
        self.worth, self.calls = worth, 0

    async def ask(self, bot, qs, state, did, deadline_ms=None):
        self.calls += 1
        return Verdict(ok=True, kind="ok", action="proceed", model_version="rules-v1", backend="rules",
                       answers={"worth": {"p_true": self.worth}, "exec_degraded": {"p_true": 0.1}})

    async def health(self):
        return {}


def sports_desk(monkeypatch, phase="pre", home_ml="-150", away_ml="+130", ask=0.50, wp=None):
    from desk.engine.main import Desk
    d = Desk(judge=FakeJudge())
    d.cfg["bots"]["sports"]["mode"] = "paper"
    sp = d.bots["sports"]
    now = time.time()
    start = dt.datetime.fromtimestamp(now + (3600 if phase == "pre" else -3600), dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    day = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date()
    ev = f"KXNBAGAME-{day:%y}{day.strftime('%b').upper()}{day:%d}SASPHX"
    state = {"ask": ask}
    mk = lambda: [kmkt(ev, "PHX", "Phoenix", state["ask"] - 0.01, state["ask"]),
                  kmkt(ev, "SAS", "San Antonio", 1 - state["ask"] - 0.01, 1 - state["ask"] + 0.01)]
    game = lambda: espn_event("401", ("Phoenix", "Suns", "PHX"), ("San Antonio", "Spurs", "SA"), start,
                              state="pre" if phase == "pre" else "in", home_ml=home_ml, away_ml=away_ml, wp=wp)

    async def universe(series, max_age=0):
        out = mk()
        for m in out:
            d.md.st(m["ticker"]).meta = m
        return out if "KXNBAGAME" in series else []

    async def games(path, ps, day_):
        return [game()] if path == "basketball/nba" else []

    async def fee(series):
        return "quadratic_with_maker_fees", 1.0

    async def refresh_book(t, min_interval=0):
        m = next(x for x in mk() if x["ticker"] == t)
        b = Book(t, time.time(), [(float(m["yes_bid_dollars"]), 500)], [(float(m["yes_ask_dollars"]), 500)])
        st = d.md.st(t)
        st.book, st.book_ts, st.backfilled = b, b.ts, True
        return b

    monkeypatch.setattr(d.md, "universe", universe)
    monkeypatch.setattr(d.md, "series_fee", fee)
    monkeypatch.setattr(d.md, "refresh_book", refresh_book)
    monkeypatch.setattr(sp.espn, "games", games)
    return d, sp, state, ev


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


def test_pregame_edge_bets_after_two_checks_then_takes_profit(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch)            # Phoenix fair ~0.58, ask 0.50
    run(sp.scan("paper"))
    row = next(x for x in sp.board["rows"] if x["team"] == "Phoenix")
    assert row["fair"] > 0.55 and row["edge_c"] > 3 and row["why"] == "Held for 2 checks" and not sp.locks()
    run(sp.scan("paper"))
    lk = d.book.held(f"sports:{ev}-PHX")
    assert lk and lk["entry"] == 0.50 and lk["tp"] == 0.55 and lk["stop"] == 0.47 and lk["qty"] > 0
    assert d.judge.calls == 1
    run(sp.step())                                          # rests the take-profit as a maker order
    assert d.book.held(f"sports:{ev}-PHX").get("tp_order")
    state["ask"] = 0.57                                     # bid 0.56 >= 0.55 target
    run(sp.step())
    assert not d.book.held(f"sports:{ev}-PHX")
    from desk import logs
    tr = [x for x in logs.read("trades", __import__("desk.util", fromlist=["local_day"]).local_day(d.tz)) if x["bot"] == "sports"]
    assert tr and tr[-1]["rule"] == "take_profit" and tr[-1]["pnl_after_fees"] > 0


def test_stop_loss_and_no_daily_stop(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch)
    run(sp.scan("paper")); run(sp.scan("paper"))
    assert d.book.held(f"sports:{ev}-PHX")
    state["ask"] = 0.47                                     # bid 0.46 <= stop 0.47
    run(sp.step())
    assert not d.book.held(f"sports:{ev}-PHX")
    assert d.daily_stop_on("gambler") and not d.daily_stop_on("sports")


def test_no_bet_when_gap_too_big_or_small(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch, home_ml="-400", away_ml="+320")   # fair ~0.79 vs 0.50: not believable
    run(sp.scan("paper")); run(sp.scan("paper"))
    assert not sp.locks()
    assert next(x for x in sp.board["rows"] if x["team"] == "Phoenix")["why"] == "Gap believable"
    d2, sp2, _, _ = sports_desk(monkeypatch, home_ml="-102", away_ml="-118")    # fair ~0.49: no edge
    run(sp2.scan("paper")); run(sp2.scan("paper"))
    assert not sp2.locks()


def test_live_uses_espn_win_probability(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch, phase="live", wp=0.62, ask=0.52)
    run(sp.scan("paper"))
    row = next(x for x in sp.board["rows"] if x["team"] == "Phoenix")
    assert row["phase"] == "live" and row["fair"] == 0.62 and "live win" in row["fair_source"]
    run(sp.scan("paper"))
    assert d.book.held(f"sports:{ev}-PHX")


def test_force_and_controls(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch, home_ml="-102", away_ml="-118")   # no edge: bot won't bet
    run(sp.scan("paper"))
    ok, msg = run(sp.force(f"{ev}-PHX", seen_price=0.50))
    assert ok, msg
    lk = d.book.held(f"sports:{ev}-PHX")
    assert lk["manual"] and d.judge.calls == 0
    assert sp.control(f"{ev}-PHX", "ride")[0] and d.book.held(f"sports:{ev}-PHX")["ride"]
    assert sp.control(f"{ev}-PHX", "stop")[0]
    run(sp.step())
    assert not d.book.held(f"sports:{ev}-PHX")
    ok, msg = run(sp.force(f"{ev}-PHX", seen_price=0.40))
    assert not ok and "moved" in msg


def test_stop_benches_the_game(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch)
    run(sp.scan("paper")); run(sp.scan("paper"))
    state["ask"] = 0.47
    run(sp.step())
    sp.last_scan = 0
    run(sp.step())
    assert not sp.locks()
    row = next(x for x in sp.board["rows"] if x["team"] == "Phoenix")
    assert row["why"] == "Bet slot free" and any(g.get("note") == "stopped out of this game" for g in row["gates"])
