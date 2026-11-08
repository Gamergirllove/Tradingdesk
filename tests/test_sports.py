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
                       answers={"worth": {"p": self.worth}, "exec_degraded": {"p": 0.1}})

    async def health(self):
        return {}


def sports_desk(monkeypatch, phase="pre", home_ml="-150", away_ml="+130", ask=0.50, wp=None):
    from desk.engine.main import Desk
    d = Desk(judge=FakeJudge())
    d.cfg["bots"]["sports"]["mode"] = "paper"
    d.cfg["bots"]["sports"].update({                     # these tests check the mechanics at the earlier settings
        "min_price": 0.15, "take_profit_pct": 10, "stop_loss_pct": 5, "live": True, "size_pct_equity": 50,
        "min_edge_c": {"pregame": 3, "live": 5}})
    sp = d.bots["sports"]
    now = time.time()
    start = dt.datetime.fromtimestamp(now + (45 if phase == "pre" else -3600), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date()
    ev = f"KXNBAGAME-{day:%y}{day.strftime('%b').upper()}{day:%d}SASPHX"
    state = {"ask": ask}
    mk = lambda: [kmkt(ev, "PHX", "Phoenix", state["ask"] - 0.01, state["ask"]),
                  kmkt(ev, "SAS", "San Antonio", 1 - state["ask"] - 0.01, 1 - state["ask"] + 0.01)]
    state["phase"] = "pre" if phase == "pre" else "in"
    game = lambda: espn_event("401", ("Phoenix", "Suns", "PHX"), ("San Antonio", "Spurs", "SA"), start,
                              state=state["phase"], home_ml=home_ml, away_ml=away_ml, wp=wp)

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
    run(sp.step())                                          # pregame: no take-profit resting yet
    assert not d.book.held(f"sports:{ev}-PHX").get("tp_order")
    state["phase"] = "in"; run(sp.scan("paper"))
    run(sp.step())                                          # game on: rests the take-profit as a maker order
    assert d.book.held(f"sports:{ev}-PHX").get("tp_order")
    state["phase"] = "in"; run(sp.scan("paper"))     # the game has started: exits are on
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
    state["phase"] = "in"; run(sp.scan("paper"))     # the game has started: exits are on
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
    state["phase"] = "in"; run(sp.scan("paper"))     # the game has started: exits are on
    state["ask"] = 0.47
    run(sp.step())
    sp.last_scan = 0
    run(sp.step())
    assert not sp.locks()
    row = next(x for x in sp.board["rows"] if x["team"] == "Phoenix")
    slot = next(g for g in row["gates"] if g["k"] == "slot")
    assert not slot["ok"] and slot["note"] == "stopped out of this game"


def test_live_exit_trusts_the_fill_and_logs_real_fills(monkeypatch):
    """Kalshi's positions can trail a fill: the bot must not sell again, and P&L comes from Kalshi's fills."""
    from desk.broker.kalshi_broker import KalshiBroker
    from desk import logs
    from desk.util import local_day

    class FakeClient:
        def __init__(self):
            self.pos, self.orders, self.fill_rows = [], [], []

        async def create_order(self, ticker, side, count, price, coid, post_only=False, tif="", reduce_only=False):
            self.orders.append((side, count, price, tif))
            if tif == "immediate_or_cancel":
                px = price + 0.01 if side == "ask" else price            # price improvement
                self.fill_rows.append({"action": "buy" if side == "bid" else "sell", "count_fp": str(count),
                                       "yes_price_dollars": f"{px:.4f}", "fee_cost": "0.4500"})
                return {"order_id": f"o{len(self.orders)}", "fill_count": count, "remaining_count": 0}
            return {"order_id": f"o{len(self.orders)}", "fill_count": 0, "remaining_count": count}

        async def positions(self):
            return self.pos                                               # stale: never shows the exit

        async def resting_orders(self, ticker=None):
            return []

        async def cancel_order(self, oid, t):
            return {}

        async def fills(self, min_ts=None, ticker=None):
            return self.fill_rows

        async def balance(self):
            return {"balance": 1700, "portfolio_value": 0}

    d, sp, state, ev = sports_desk(monkeypatch, ask=0.50)
    fc = FakeClient()
    kb = KalshiBroker(fc, "live")
    monkeypatch.setattr(d, "broker_for", lambda bot, mode: kb)
    run(sp.scan("live")); run(sp.scan("live"))
    t = f"{ev}-PHX"
    lk = d.book.held(f"sports:{t}")
    assert lk and lk["mode"] == "live"
    fc.pos = [{"ticker": t, "position_fp": str(lk["qty"]), "market_exposure_dollars": str(lk["qty"] * 0.5)}]
    kb._pos_cache, kb._hold_until = (0.0, {}), 0
    state["phase"] = "in"; run(sp.scan("live"))
    state["ask"] = 0.47                                                  # stop
    run(sp.step())
    sells = [o for o in fc.orders if o[0] == "ask" and o[3] == "immediate_or_cancel"]
    assert len(sells) == 1 and not d.book.held(f"sports:{t}")
    run(sp.step())
    assert len([o for o in fc.orders if o[0] == "ask" and o[3] == "immediate_or_cancel"]) == 1   # never sold twice
    tr = [x for x in logs.read("trades", local_day(d.tz)) if x["bot"] == "sports"][-1]
    q = lk["qty"]
    assert tr["from_fills"] and tr["exit"] == 0.47 and abs(tr["pnl_after_fees"] - (q * (0.47 - 0.50) - 0.90)) < 1e-6



def test_pregame_bet_waits_for_the_start(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch)
    run(sp.scan("paper")); run(sp.scan("paper"))
    k = f"sports:{ev}-PHX"
    assert d.book.held(k)
    for ask in (0.60, 0.40):                                # past the take-profit, then past the stop: still held
        state["ask"] = ask
        run(sp.step())
        assert d.book.held(k) and not d.book.held(k).get("tp_order")
    assert sp.view()["bets"][0]["waiting_for_start"]
    state["phase"] = "in"; state["ask"] = 0.47; run(sp.scan("paper"))
    run(sp.step())                                          # started and below the stop: sells
    assert not d.book.held(k)


def test_stop_now_works_before_the_start(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch)
    run(sp.scan("paper")); run(sp.scan("paper"))
    assert sp.control(f"{ev}-PHX", "stop")[0]
    run(sp.step())
    assert not d.book.held(f"sports:{ev}-PHX")



def test_pregame_bets_only_in_the_last_minute(monkeypatch):
    d, sp, state, ev = sports_desk(monkeypatch)
    sp.games.clear()
    import desk.bots.sports as S
    orig = S.parse_game
    monkeypatch.setattr(S, "parse_game", lambda e: dict(orig(e), start_ts=time.time() + 600))   # 10 min out
    run(sp.scan("paper")); run(sp.scan("paper"))
    row = next(x for x in sp.board["rows"] if x["team"] == "Phoenix")
    assert not sp.locks() and row["why"] == "Pregame" and row["gates"][0]["need"] == "1 min before start"
    monkeypatch.setattr(S, "parse_game", lambda e: dict(orig(e), start_ts=time.time() - 120))   # past the time, not started
    run(sp.scan("paper")); run(sp.scan("paper"))
    assert sp.locks()



def test_live_floor_is_80c():
    from desk.config import load_config
    c = load_config()["bots"]["sports"]
    assert c["min_price"] == 0.80 and c["max_price"] == 0.90


def test_realized_waits_for_trailing_fills():
    from desk.broker.kalshi_broker import KalshiBroker

    class C:
        n = 0

        async def fills(self, min_ts=None, ticker=None):
            C.n += 1
            rows = [{"action": "buy", "count_fp": "12", "yes_price_dollars": "0.64", "fee_cost": "0.1936"}]
            if C.n >= 3:                                   # the sell shows up on the third read
                rows += [{"action": "sell", "count_fp": "10", "yes_price_dollars": "0.60", "fee_cost": "0.168"},
                         {"action": "sell", "count_fp": "2", "yes_price_dollars": "0.60", "fee_cost": "0.0336"}]
            return rows

    kb = KalshiBroker(C(), "live")
    r = run(kb.realized("T", 0, "yes", wait_s=0))
    assert C.n == 3 and r["sell_qty"] == 12 and abs(r["proceeds"] - r["cost"] - r["fees"] - (-0.8752)) < 1e-6



def test_hold_to_final_settles(monkeypatch):
    """Live settings: pregame only, no take-profit, no stop, 20% per bet; the bet settles at the final."""
    from desk import logs
    from desk.util import local_day
    from desk.config import load_config
    c = load_config()["bots"]["sports"]
    assert c["take_profit_pct"] == 0 and c["stop_loss_pct"] == 0 and c["size_pct_equity"] == 20 and c["live"] is False
    d, sp, state, ev = sports_desk(monkeypatch, ask=0.50)
    d.cfg["bots"]["sports"].update({"take_profit_pct": 0, "stop_loss_pct": 0, "size_pct_equity": 20})
    run(sp.scan("paper")); run(sp.scan("paper"))
    k, t = f"sports:{ev}-PHX", f"{ev}-PHX"
    lk = d.book.held(k)
    assert lk and lk["tp"] is None and lk["stop"] is None
    assert lk["qty"] * 0.5 <= 50 * 0.2 + 0.5                     # about 20% of the $50 paper bankroll
    state["phase"] = "in"; run(sp.scan("paper"))
    for ask in (0.95, 0.20):                                     # big swings: no sale either way
        state["ask"] = ask
        run(sp.step())
        assert d.book.held(k) and not d.book.held(k).get("tp_order")
    d.md.st(t).meta = dict(d.md.st(t).meta, status="finalized", result="yes")
    d.brokers["paper_sports"].settle(t, "yes")
    run(sp.step())
    assert not d.book.held(k)
    tr = [x for x in logs.read("trades", local_day(d.tz)) if x["bot"] == "sports"][-1]
    assert tr["rule"] == "settled" and tr["exit"] == 1.0 and tr["pnl_after_fees"] > 0
