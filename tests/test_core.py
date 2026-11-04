import asyncio
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from desk import ledger, logs
from desk.book.positions import Bench, Book
from desk.broker.paper import PaperBroker
from desk.config import effective_mode, load_config, set_override
from desk.policy import rules
from desk.policy.calibration import Calibrator, fit_platt
from desk.questions import gambler as qg, house as qh, scan as qs
from desk.questions.base import choice, normalize, noul, schema_for, score, validate
from desk.risk.engine import Intent, RiskEngine
from desk.state.market import fair_yes
from desk.bots.house import compute_quotes
from desk.util import kalshi_fee
from desk.venues.kalshi import Book as KBook, Signer, parse_book


def test_batteries_valid():
    for b in (qg.BATTERY, qh.BATTERY, qs.battery("gambler", ["A", "B", "C"]), qs.battery("house", ["A"])):
        assert validate(b) == []
    sch = schema_for(qg.BATTERY)
    assert set(sch["required"]) == {q.id for q in qg.BATTERY}
    assert all(c["additionalProperties"] is False for c in sch["properties"].values())


def test_validate_catches_bad():
    bad = [noul("a", "x", []), noul("a", "y", [])]
    assert "duplicate question ids" in validate(bad)
    c = choice("c", "pick", {"only": "one"}, [])
    assert any("single real option" in e for e in validate([c]))
    s = score("s", "rate", {0: "a"}, [])
    assert any("score needs" in e for e in validate([s]))


def test_normalize():
    q = choice("r", "x", {"a": "", "b": ""}, [])
    n = normalize(q, {"a": 3, "b": 1, "other": 0})
    assert n["pick"] == "a" and abs(sum(n["probs"].values()) - 1) < 1e-3
    s = score("s", "x", {0: "", 1: "", 2: "", 3: ""}, [])
    n = normalize(s, {"L0": .1, "L1": .2, "L2": .3, "L3": .4})
    assert abs(n["p_ge"]["2"] - 0.7) < 1e-3 and abs(n["ev"] - 2.0) < 1e-3
    assert normalize(noul("n", "x", []), {"p_true": 1.7})["p"] == 1.0


def test_fee_and_fair():
    assert kalshi_fee(0.5, 1) == 0.02            # 0.0175 rounds up to a cent
    assert kalshi_fee(0.5, 100) == 1.75
    assert kalshi_fee(0.5, 10, maker=True) == 0.0
    assert kalshi_fee(0.5, 10, "quadratic_with_maker_fees", maker=True) == 0.05
    p = fair_yes("greater", 80000, None, 80000, 0.5, 60)
    assert 0.45 < p < 0.51
    assert fair_yes("greater", 81000, None, 80000, 0.5, 60) < 0.1
    b = fair_yes("between", 79900, 80100, 80000, 0.5, 60)
    assert 0 < b < 1


def test_parse_book():
    b = parse_book("T", {"orderbook_fp": {"yes_dollars": [["0.2000", "10"], ["0.2400", "44"]],
                                          "no_dollars": [["0.7100", "9"], ["0.7500", "18"]]}})
    assert b.best_bid == 0.24 and b.best_ask == 0.25 and b.asks[1][0] == 0.29


@pytest.mark.parametrize("kind", ["rsa", "ed"])
def test_signer(kind):
    if kind == "rsa":
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()).decode()
    else:
        k = ed25519.Ed25519PrivateKey.generate()
        pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode()
    s = Signer("kid", pem)
    h = s.headers("GET", "/trade-api/v2/portfolio/balance?x=1")
    import base64
    msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance".encode()
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    if kind == "rsa":
        k.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                    salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        k.public_key().verify(sig, msg)


def _ans(**kw):
    base = {
        "regime": {"probs": {"trending": .7, "mean_reverting": .2, "chaotic": .05, "other": .05}, "pick": "trending"},
        "pressure": {"p": .7},
        "setup": {"probs": {"0": .05, "1": .15, "2": .4, "3": .4}, "p_ge": {"0": 1, "1": .95, "2": .8, "3": .4}},
        "action": {"probs": {"buy_yes": .7, "buy_no": .05, "hold": .2, "other": .05}, "pick": "buy_yes"},
        "worth": {"p": .7}, "exec_degraded": {"p": .1}, "spot_up": {"p": .7}}
    base.update(kw)
    return base


def test_gambler_policy():
    cfg = load_config()
    pol = cfg["policy"]["gambler"]
    cal = Calibrator()
    up = {"yes_wins_if_spot": "rises"}
    assert rules.gambler(_ans(), {"model_edge_c": 5, **up}, pol, cal).action == "buy_yes"
    assert rules.gambler(_ans(), {"model_edge_c": 1, **up}, pol, cal).action == "hold"      # code edge too small
    assert rules.gambler(_ans(), {"model_edge_c": None, **up}, pol, cal).action == "hold"   # missing is not neutral
    assert rules.gambler(_ans(exec_degraded={"p": .8}), {"model_edge_c": 5, **up}, pol, cal).action == "hold"
    assert rules.gambler(_ans(spot_up={"p": .3}), {"model_edge_c": 5, **up}, pol, cal).action == "hold"   # flow against
    assert rules.gambler(_ans(), {"model_edge_c": 5, "yes_wins_if_spot": "range"}, pol, cal).action == "hold"
    a = _ans(action={"probs": {"buy_yes": .05, "buy_no": .75, "hold": .15, "other": .05}, "pick": "buy_no"},
             spot_up={"p": .3})
    assert rules.gambler(a, {"model_edge_c": -5, **up}, pol, cal).action == "buy_no"
    assert rules.gambler(a, {"model_edge_c": 5, **up}, pol, cal).action == "hold"           # model and code disagree


def test_house_policy_and_quotes():
    cfg = load_config()
    pol, cal = cfg["policy"]["house"], Calibrator()
    good = {"range_state": {"probs": {"inside": .8, "testing_edge": .1, "breaking_out": .05, "other": .05}},
            "toxic": {"p": .2}, "env": {"p_ge": {"2": .7}}, "volume_alive": {"p": .9}}
    assert rules.house(good, pol, cal).action == "quote"
    assert rules.house({**good, "toxic": {"p": .6}}, pol, cal).action == "widen"
    assert rules.house({**good, "toxic": {"p": .9}}, pol, cal).action == "pull"
    assert rules.house({**good, "volume_alive": {"p": .1}}, pol, cal).action == "kill"
    feat = {"bid": 0.40, "ask": 0.44, "mid": 0.42, "microprice": 0.42, "rv_30m_c": 1.0}
    c = cfg["bots"]["house"]
    qb, qa, _ = compute_quotes(feat, 0, c, widen=False)
    assert qb < 0.44 and qa > 0.40 and qb < qa
    qb2, qa2, _ = compute_quotes(feat, 10, c, widen=False)     # long inventory skews quotes down
    assert qb2 <= qb and qa2 <= qa
    qb3, qa3, _ = compute_quotes(feat, c["max_inventory"], c, widen=False)
    assert qb3 is None and qa3 is not None


def test_scan_pick():
    pol = {"worth_min": 0.5}
    ans = {"best": {"probs": {"A": .6, "B": .3, "other": .1}}, "worth_A": {"p": .4}, "worth_B": {"p": .7}}
    pick, _ = rules.scan_pick(ans, ["A", "B"], pol)
    assert pick == "B"          # A won the choice but failed the absolute check
    ans["best"]["probs"] = {"A": .2, "B": .2, "other": .6}
    assert rules.scan_pick(ans, ["A", "B"], pol)[0] is None


def test_risk_engine():
    cfg = load_config()
    r = RiskEngine(cfg, "America/Los_Angeles")
    it = Intent(bot="gambler", ticker="T", side="bid", count=5, price=0.40, expected_edge_c=5, state_ts=time.time(),
                model_version="m1", tag="entry")
    assert r.check(it, 0, 0, 100)[0]
    r.pin("m1")
    assert not r.check(Intent(**{**it.to_dict(), "model_version": "m2"}), 0, 0, 100)[0]
    assert not r.check(Intent(**{**it.to_dict(), "expected_edge_c": 1.0}), 0, 0, 100)[0]      # below fees + min
    assert not r.check(Intent(**{**it.to_dict(), "count": 600}), 0, 0, 1000)[0]
    assert not r.check(Intent(**{**it.to_dict(), "state_ts": time.time() - 999}), 0, 0, 100)[0]
    assert not r.check(it, 0, 99, 100)[0]                                                    # exposure cap
    r.halt("test")
    assert not r.check(it, 0, 0, 100)[0]
    ex = Intent(bot="gambler", ticker="T", side="ask", count=5, price=0.40, reduce_only=True, tif="immediate_or_cancel")
    assert r.check(ex, 5, 0, None)[0]                                                        # exits pass during halts
    q = Intent(bot="house", ticker="T", side="bid", count=5, price=0.30, post_only=True, expected_edge_c=2,
               band=(0.35, 0.50), best_ask=0.32, tag="quote", state_ts=time.time())
    r.clear_halt()
    assert not r.check(q, 0, 0, 100)[0]                                                      # range guard
    ok, why = r.check(Intent(**{**q.to_dict(), "price": 0.36, "best_ask": 0.36}), 0, 0, 100)
    assert not ok and "post_only_cross" in why


def test_paper_broker():
    b = PaperBroker("t", bankroll=100)
    book = KBook("T", time.time(), bids=[(0.40, 10)], asks=[(0.42, 10)])
    it = Intent(bot="g", ticker="T", side="bid", count=5, price=0.42, tif="immediate_or_cancel")
    ack = asyncio.run(b.place(it, book))
    assert ack.filled == 5 and abs(ack.avg_price - 0.42) < 1e-9
    assert abs(b.cash - (100 - 5 * 0.42 - ack.fee)) < 1e-9
    it2 = Intent(bot="g", ticker="T", side="ask", count=5, price=0.40, tif="immediate_or_cancel", reduce_only=True)
    ack2 = asyncio.run(b.place(it2, book))
    assert ack2.filled == 5 and asyncio.run(b.position("T")).qty == 0
    assert abs(b.cash - (100 - 5 * 0.02 - ack.fee - ack2.fee)) < 1e-6
    # resting maker bid filled by a trade-through
    q = Intent(bot="h", ticker="T", side="bid", count=5, price=0.40, post_only=True)
    ack3 = asyncio.run(b.place(q, book))
    assert ack3.status == "resting"
    b.on_market("T", book, [(time.time() + 1, 0.39, 20, "ask")])
    assert asyncio.run(b.position("T")).qty == 5
    b.settle("T", "yes")
    assert asyncio.run(b.position("T")).qty == 0


def test_book_and_bench():
    bk = Book()
    assert bk.acquire("gambler", "T", "lead1")
    assert not bk.acquire("gambler", "U", "lead2")      # one position at a time
    bk.release("gambler", "x", "wrong")                  # only the closing path frees the book
    assert bk.held("gambler")
    bk.release("gambler", "exit", "lead1")
    assert bk.held("gambler") is None
    be = Bench({"too_early": 20, "malformed": 100000})
    be.bench("g:T", "too_early")
    assert be.benched("g:T") == "too_early"
    be.bench("g:U", "too_early", until=time.time() - 1)
    assert be.benched("g:U") is None


def test_config_override_and_modes():
    cfg = load_config()
    assert effective_mode(cfg, "gambler") == "shadow"
    set_override("bots.gambler.mode", "live")
    set_override("desk.mode_ceiling", "paper")
    cfg = load_config()
    assert effective_mode(cfg, "gambler") == "paper"


def test_ledger_and_platt():
    ledger.record("judge", 0.01)
    ledger.record("seat:head", 0.005)
    assert abs(ledger.spent() - 0.015) < 1e-9
    a, b = fit_platt([0.9] * 50 + [0.1] * 50, [1] * 25 + [0] * 25 + [0] * 50)
    cal = Calibrator()
    cal.params["k"] = (a, b)
    assert cal("k", 0.9) < 0.9                           # overconfident 0.9s get pulled down


def test_implied_sigma_roundtrip():
    from desk.state.market import event_iv, implied_sigma
    p = fair_yes("greater", 79500, None, 80000, 0.42, 300)
    iv = implied_sigma(79500, 80000, 300, p)
    assert abs(iv - 0.42) < 1e-3
    close = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300 * 60))
    ladder = [{"event_ticker": "E", "strike_type": "greater", "floor_strike": 79500, "close_time": close,
               "yes_bid_dollars": f"{p - 0.005:.4f}", "yes_ask_dollars": f"{p + 0.005:.4f}"}]
    assert event_iv(ladder, "E", 80000) is None            # a lone market can't price its own vol
    ladder += [{"event_ticker": "E", "strike_type": "greater", "floor_strike": k, "close_time": close,
                "yes_bid_dollars": "0.0100", "yes_ask_dollars": "0.0300"} for k in (81000, 82000)]
    assert abs(event_iv(ladder, "E", 80000) - 0.42) < 0.03


def test_signer_accepts_bare_base64():
    k = ed25519.Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    body = "".join(l for l in pem.splitlines() if "-----" not in l)
    s = Signer("kid", body)
    assert s.pem.startswith("-----BEGIN PRIVATE KEY-----") and s.headers("GET", "/x")["KALSHI-ACCESS-KEY"] == "kid"



def test_gambler_tp_and_sizing():
    from desk.bots.gambler import Gambler
    from desk.engine.main import Desk
    d = Desk()
    g = Gambler(d)
    assert g.tp_price("bid", 0.40) == 0.42            # +5% on YES
    assert g.tp_price("ask", 0.60) == 0.58            # NO bought at 0.40 -> NO 0.42 -> YES 0.58
    assert g.tp_price("bid", 0.10) == 0.11            # never less than one tick
    n = asyncio.run(g.size("shadow", "bid", 0.40, "quadratic", 1.0))
    assert n == int((50 * 0.5) // (0.40 + 0.07 * 0.4 * 0.6))       # 50% of $50 per trade, fee included
    assert asyncio.run(g.size("shadow", "ask", 0.60, "quadratic", 1.0)) > 0


def test_risk_lets_closing_orders_through():
    cfg = load_config()
    r = RiskEngine(cfg, "America/Los_Angeles")
    tp = Intent(bot="gambler", ticker="T", side="ask", count=10, price=0.42, post_only=True, best_bid=0.40,
                best_ask=0.41, tag="take_profit")
    assert r.check(tp, 10, 4, None)[0]                     # sells YES we hold: no economics/version needed
    flip = Intent(**{**tp.to_dict(), "count": 30})
    assert not r.check(flip, 10, 4, None)[0]               # would flip short: treated as a new entry


def test_spot_flow_features():
    from desk.state.aggr import SpotFlow
    f = SpotFlow(venues=["coinbase", "kraken"])
    now = time.time()
    f.started = now - 4000
    f.last_ok = {"coinbase": now, "kraken": now}
    for i in range(300):
        f.trades.append((now - i * 10, 80000.0, 0.5, 1 if i % 3 else -1, "coinbase" if i % 2 else "kraken"))
    f.books["coinbase"] = (now, [(79999.0, 5.0)], [(80001.0, 1.0)])
    f.books["kraken"] = (now, [(79999.5, 3.0)], [(80000.5, 1.0)])
    x = f.features(now)
    assert x["flow_venues"] == 2 and x["spot_cvd_5m_btc"] > 0 and x["spot_book_imb_10bps"] > 0.5
    assert 0.6 < x["spot_buy_share_5m"] < 0.7
    f.started = now - 100
    assert f.features(now)["spot_cvd_5m_btc"] is None      # not yet covered: unknown, not zero


def test_gambler_take_profit_roundtrip_paper():
    from desk.bots.gambler import Gambler
    from desk.engine.main import Desk
    from desk.venues.kalshi import Book as KB
    d = Desk()
    d.cfg["bots"]["gambler"]["mode"] = "paper"
    g = Gambler(d)
    book = KB("T", time.time(), bids=[(0.39, 500)], asks=[(0.40, 500)])
    st = d.md.st("T")
    st.book, st.book_ts = book, time.time()

    async def go():
        n = await g.size("paper", "bid", 0.40, "quadratic", 1.0)
        it = Intent(bot="gambler", ticker="T", side="bid", count=float(n), price=0.40, tif="immediate_or_cancel",
                    expected_edge_c=5, state_ts=time.time(), best_bid=0.39, best_ask=0.40, decision_id="lead1", tag="entry")
        await g.place_entry(it, "paper", {}, {"lo_60m": 0.37, "hi_60m": 0.43})
        lock = d.book.held("gambler")
        assert lock and lock["tp"] == 0.42 and lock["qty"] == n
        g.mdx = d.md
        d.md.refresh_book = lambda *a, **k: _ret(book)
        await g.manage(lock, "paper")
        br = d.broker_for("gambler", "paper")
        opens = await br.open_orders("T")
        assert len(opens) == 1 and opens[0]["side"] == "ask" and opens[0]["price"] == 0.42
        br.on_market("T", book, [(time.time() + 1, 0.43, 10000, "bid")])     # market trades through the target
        assert (await br.position("T")).qty == 0
        await g.manage(d.book.held("gambler"), "paper")
        assert d.book.held("gambler") is None
        tr = [r for r in logs.read("trades") if r.get("bot") == "gambler"][-1]
        assert tr["rule"] == "take_profit" and tr["pnl_after_fees"] > 0
    asyncio.run(go())


async def _ret(x):
    return x


def test_backfill_seeds_history():
    from desk.state.market import MarketData

    class FakeClient:
        async def candles(self, series, ticker, a, b, period=1):
            assert series == "KXBTCD"
            now = int(time.time())
            return [{"end_period_ts": now - 60 * i, "yes_bid": {"close_dollars": f"{0.40 + 0.001 * i:.4f}"},
                     "yes_ask": {"close_dollars": f"{0.42 + 0.001 * i:.4f}"}} for i in range(60)] + \
                   [{"end_period_ts": now - 3700, "yes_bid": {"close_dollars": "0.0000"}, "yes_ask": {"close_dollars": "0.0100"}}]

    md = MarketData(FakeClient(), load_config())
    n = asyncio.run(md.backfill("KXBTCD-26NOV0417-T80000"))
    assert n == 60 and len(md.st("KXBTCD-26NOV0417-T80000").hist) == 60
    assert asyncio.run(md.backfill("KXBTCD-26NOV0417-T80000")) == 0        # once per ticker


def test_force_ride_stop_paper():
    from desk.bots.gambler import Gambler
    from desk.engine.main import Desk
    from desk.venues.kalshi import Book as KB
    d = Desk()
    d.cfg["bots"]["gambler"]["mode"] = "paper"
    g = d.bots["gambler"]
    book = KB("KXBTCD-X-T1", time.time(), bids=[(0.49, 500)], asks=[(0.50, 500)])

    async def fake_refresh(t, min_interval=0):
        st = d.md.st(t)
        st.book, st.book_ts = KB(t, time.time(), book.bids, book.asks), time.time()
        return st.book

    async def fake_fee(series):
        return "quadratic", 1.0
    d.md.refresh_book = fake_refresh
    d.md.series_fee = fake_fee

    async def go():
        ok, msg = await g.force("KXBTCD-X-T1", "yes")
        assert ok, msg
        lock = d.book.held("gambler")
        assert lock["side"] == "bid" and lock["entry"] == 0.50 and lock["tp"] == 0.53
        ok2, _ = await g.force("KXBTCD-X-T1", "no")
        assert not ok2                                         # one trade at a time
        g.mdx = d.md
        await g.manage(d.book.held("gambler"), "paper")
        br = d.broker_for("gambler", "paper")
        assert any(o["price"] == 0.53 for o in await br.open_orders("KXBTCD-X-T1"))
        assert g.control("ride")[0]
        await g.manage(d.book.held("gambler"), "paper")
        assert not await br.open_orders("KXBTCD-X-T1")        # take-profit pulled while riding
        book.bids[:] = [(0.60, 500)]                            # through the old target: rider keeps holding
        await g.manage(d.book.held("gambler"), "paper")
        assert d.book.held("gambler") is not None
        assert g.control("stop")[0]
        await g.manage(d.book.held("gambler"), "paper")
        assert d.book.held("gambler") is None and (await br.position("KXBTCD-X-T1")).qty == 0
        tr = [r for r in logs.read("trades") if r.get("bot") == "gambler"][-1]
        assert tr["rule"] == "manual_stop" and tr["pnl_after_fees"] > 0
    asyncio.run(go())


def test_force_respects_hard_limits():
    from desk.engine.main import Desk
    d = Desk()                                                   # gambler in shadow
    ok, msg = asyncio.run(d.bots["gambler"].force("KXBTCD-X-T1", "yes"))
    assert not ok and "shadow" in msg
    r = RiskEngine(load_config(), "America/Los_Angeles")
    r.halt("loss limit")
    it = Intent(bot="gambler", ticker="T", side="bid", count=5, price=0.5, forced=True, state_ts=time.time())
    assert not r.check(it, 0, 0, 100)[0]                         # a halt still blocks forced trades
    r.clear_halt()
    assert r.check(it, 0, 0, 100)[0]                             # no edge estimate needed when forced
    assert not r.check(Intent(**{**it.to_dict(), "count": 9999}), 0, 0, 100000)[0]


def test_force_refuses_if_price_moved():
    from desk.engine.main import Desk
    from desk.venues.kalshi import Book as KB
    d = Desk()
    d.cfg["bots"]["gambler"]["mode"] = "paper"

    async def fake_refresh(t, min_interval=0):
        st = d.md.st(t)
        st.book, st.book_ts = KB(t, time.time(), [(0.86, 500)], [(0.87, 500)]), time.time()
        return st.book

    async def fake_fee(series):
        return "quadratic", 1.0
    d.md.refresh_book, d.md.series_fee = fake_refresh, fake_fee
    ok, msg = asyncio.run(d.bots["gambler"].force("KXBTCD-X-T1", "yes", 0.78))
    assert not ok and "moved to 87" in msg and d.book.held("gambler") is None
    ok, msg = asyncio.run(d.bots["gambler"].force("KXBTCD-X-T1", "no", 0.14))       # NO costs 14c, saw 14c
    assert ok, msg



def test_fifteen_minute_profile():
    from desk.config import profile
    cfg = load_config()
    assert cfg["venue"]["universe_series"] == ["KXBTC15M"]
    f = profile(cfg, "funnel", "KXBTC15M")
    assert f["min_minutes_to_close"] == 3 and f["max_minutes_to_close"] == 16 and f["price_ceiling"] >= 0.85
    g = profile(cfg, "gambler", "KXBTC15M")
    assert g["exit_minutes_before_close"] == 1 and g["size_pct_equity"] == 50
    assert profile(cfg, "gambler", "KXBTCD")["exit_minutes_before_close"] == 20
