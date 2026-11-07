"""Sports: NFL, NBA, NHL and college football game winners on Kalshi, priced off ESPN.

Fair price: before the game, the sportsbook moneyline ESPN shows (DraftKings) with its margin removed; during
the game, ESPN's live win probability (NFL, NBA, college football; NHL has none, so NHL is pregame only).
Code finds Kalshi asks below fair after fees, the judge answers a short battery on the best one, policy and the
risk engine decide, and exits are arithmetic: +take_profit_pct / -stop_loss_pct on the contract.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import time
from collections import defaultdict

from .. import logs
from ..config import effective_mode
from ..questions.sports import BATTERY
from ..risk.engine import Intent
from ..sports.espn import Espn, parse_game
from ..sports.leagues import LEAGUES, LIVE_WP
from ..sports.match import ET, event_date, match_event
from ..util import fnum, kalshi_fee, r
from .common import handle_verdict_failure, maybe_pin

NAME = "sports"


def lock_key(ticker: str) -> str:
    return f"{NAME}:{ticker}"


class Sports:
    def __init__(self, desk):
        self.d = desk
        self.espn = Espn()
        self.status: dict = {"state": "idle"}
        self.board: dict = {"rows": [], "ts": 0, "stats": {}}
        self.last_scan = 0.0
        self.scan_n = 0
        self.seen: dict[str, tuple[int, int]] = {}            # ticker -> (last scan id, consecutive passes)
        self.matches: dict[str, tuple[float, str | None, dict | str]] = {}   # event -> (ts, espn id, side map | reason)
        self.exit_tries: dict[str, int] = {}
        self.games: dict[str, dict] = {}                      # espn id -> parsed game, latest scan

    @property
    def cfg(self):
        return self.d.cfg["bots"][NAME]

    def locks(self) -> dict[str, dict]:
        return {k: v for k, v in self.d.book.locks.items() if k.startswith(NAME + ":")}

    # ---------- loop ----------
    async def step(self):
        if not self.cfg.get("enabled", True):
            self.status = {"state": "disabled"}
            return
        mode = effective_mode(self.d.cfg, NAME)
        for k, lk in list(self.locks().items()):
            await self.manage(k, lk, mode)
        if time.time() - self.last_scan < float(self.cfg.get("scan_every_s", 20)):
            return
        self.last_scan = time.time()
        await self.scan(mode)

    # ---------- scan ----------
    async def scan(self, mode: str):
        c, md = self.cfg, self.d.md
        self.scan_n += 1
        now = time.time()
        series = [s for s in c.get("series", list(LEAGUES)) if s in LEAGUES]
        mkts = await md.universe(series, max_age=max(10.0, float(c.get("scan_every_s", 20)) - 2))
        by_ev: dict[str, list[dict]] = defaultdict(list)
        for m in mkts:
            by_ev[m["event_ticker"]].append(m)
        today = dt.datetime.now(ET).date()
        want: dict[tuple[str, dt.date], list[str]] = defaultdict(list)
        for et, ms in by_ev.items():
            d = event_date(et)
            if d and today - dt.timedelta(days=1) <= d <= today + dt.timedelta(days=1):
                want[(ms[0]["_series"], d)].append(et)
        raw: dict[tuple[str, dt.date], list[dict]] = {}

        async def fetch(key):
            s, d = key
            path, ps, _ = LEAGUES[s]
            raw[key] = await self.espn.games(path, ps, d)

        await asyncio.gather(*(fetch(k) for k in want))
        fees = {s: await md.series_fee(s) for s in series}
        held = {lk["ticker"] for lk in self.locks().values()}
        held_events = {lk.get("event_ticker") for lk in self.locks().values()}
        rows, stats = [], {"events": 0, "matched": 0, "live": 0, "pregame": 0, "no_match": 0}
        keep = {lk.get("espn_id") for lk in self.locks().values()}
        self.games = {k: v for k, v in self.games.items() if k in keep}
        for (s, d), ets in want.items():
            path, _, label = LEAGUES[s]
            evs = raw.get((s, d)) or []
            by_id = {e["id"]: e for e in evs}
            for et in ets:
                stats["events"] += 1
                ms = by_ev[et]
                hit = self.matches.get(et)
                if not hit or (hit[1] is None and now - hit[0] > 600) or (hit[1] and hit[1] not in by_id):
                    e, res = match_event(ms, evs)
                    hit = (now, e["id"] if e else None, res)
                    self.matches[et] = hit
                if not hit[1] or hit[1] not in by_id:
                    stats["no_match"] += 1
                    continue
                stats["matched"] += 1
                g = parse_game(by_id[hit[1]])
                self.games[g["id"]] = g
                phase = {"pre": "pregame", "in": "live"}.get(g["state"])
                if phase:
                    stats[phase] += 1
                for m in ms:
                    side = hit[2].get(m["ticker"])
                    if side not in ("home", "away"):
                        continue
                    rows.append(self._row(m, s, label, path, g, side, phase, fees[s], held, held_events, now))
        # Live fair prices: only for live rows that pass every price gate, the busiest first (ESPN reads are capped).
        live = [x for x in rows if x["phase"] == "live" and x["fair"] is None and x["_pre_ok"]]
        live.sort(key=lambda x: -(x["vol_24h"] or 0))
        for x in live[: int(c.get("max_live_wp_reads", 8))]:
            wp = await self.espn.live_wp(x["path"], x["espn_id"])
            if wp is not None:
                self._set_fair(x, wp if x["side"] == "home" else 1 - wp, "ESPN live win probability")
        for x in rows:
            self._gates(x)
        rows.sort(key=lambda x: (not x["ready"], -sum(g["ok"] for g in x["gates"]),
                                 -(x["edge_c"] if x["edge_c"] is not None else -99)))
        shown = [x for x in rows if x["fair"] is not None][:12] or rows[:12]
        self.board = {"rows": [{k: v for k, v in x.items() if not k.startswith("_")} for x in shown],
                      "ts": now, "stats": stats}
        ready = [x for x in rows if x["ready"]]
        if not ready:
            self.status = {"state": "no_trade", "mode": mode, "ts": now, "stats": stats,
                           "open": len(self.locks())}
            return
        await self.try_entry(ready[0], mode)

    def _row(self, m, s, label, path, g, side, phase, fee, held, held_events, now) -> dict:
        c = self.cfg
        bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        team, opp = g["teams"].get(side, {}), g["teams"].get("away" if side == "home" else "home", {})
        try:
            score_diff = float(team.get("score")) - float(opp.get("score"))
        except (TypeError, ValueError):
            score_diff = None
        x = {"ticker": m["ticker"], "event_ticker": m["event_ticker"], "series": s, "league": label, "path": path,
             "espn_id": g["id"], "game": g["name"], "team": m.get("yes_sub_title"), "side": side, "phase": phase,
             "state": g["state"], "clock": g["detail"], "start_ts": g["start_ts"],
             "minutes_to_start": r((g["start_ts"] - now) / 60, 1) if g["start_ts"] else None,
             "bid": bid, "ask": ask, "spread_c": r((ask - bid) * 100, 1) if (bid and ask) else None,
             "vol_24h": fnum(m.get("volume_24h_fp") or m.get("volume_24h")), "score_diff": score_diff,
             "fee_type": fee[0], "fee_mult": fee[1], "fair": None, "fair_source": None, "edge_c": None,
             "held": m["ticker"] in held, "event_held": m["event_ticker"] in held_events}
        x["fee_c"] = r(kalshi_fee(ask, 100, fee[0], fee[1]), 4) if ask else None   # $ per 100 = cents per contract
        if phase == "pregame" and g.get("odds"):
            o = g["odds"]
            self._set_fair(x, o["p_home"] if side == "home" else o["p_away"],
                           f"{o.get('provider') or 'Sportsbook'} moneyline, margin removed")
        elif phase == "live" and path in LIVE_WP and g.get("wp_home") is not None and c.get("live", True):
            self._set_fair(x, g["wp_home"] if side == "home" else 1 - g["wp_home"], "ESPN live win probability")
        x["_pre_ok"] = bool(ask and bid and x["spread_c"] is not None and x["spread_c"] <= c["max_spread_c"]
                            and c["min_price"] <= ask <= c["max_price"])
        return x

    @staticmethod
    def _set_fair(x: dict, p: float, src: str):
        x["fair"], x["fair_source"] = round(p, 4), src
        if x["ask"] is not None:
            x["edge_gross_c"] = r((p - x["ask"]) * 100, 2)
            x["edge_c"] = r(x["edge_gross_c"] - (x["fee_c"] or 0), 2)

    def _gates(self, x: dict):
        c = self.cfg
        ph = x["phase"] or "pregame"
        mn = float((c.get("min_edge_c") or {}).get(ph, 3))
        mx = float((c.get("max_edge_c") or {}).get(ph, 15))
        when_ok = (x["phase"] == "pregame" and x["minutes_to_start"] is not None
                   and 0 <= x["minutes_to_start"] <= float(c["pregame_hours"]) * 60) or \
                  (x["phase"] == "live" and c.get("live", True) and x["path"] in LIVE_WP)
        if x["phase"] == "pregame":
            when_need = f"starts within {c['pregame_hours']}h"
        elif x["phase"] == "live":
            when_need = "live, ESPN win prob" if x["path"] in LIVE_WP else "no live price for NHL"
        else:
            when_need = "game over"
        open_n = len(self.locks())
        g = [
            {"k": "when", "label": "Pregame" if x["phase"] == "pregame" else ("Live" if x["phase"] == "live" else "Final"),
             "v": x["minutes_to_start"] if x["phase"] == "pregame" else None, "unit": "min", "ok": when_ok,
             "need": when_need, "note": x["clock"] if x["phase"] == "live" else None},
            {"k": "fair", "label": "Fair price", "v": x["fair"], "unit": "p", "ok": x["fair"] is not None,
             "need": "line or win prob", "note": None if x["fair"] is not None else "no line yet"},
            {"k": "spread", "label": "Tight spread", "v": x["spread_c"], "unit": "c",
             "ok": x["spread_c"] is not None and x["spread_c"] <= c["max_spread_c"], "need": f"≤{c['max_spread_c']}c"},
            {"k": "price", "label": "Price in range", "v": x["ask"], "unit": "p",
             "ok": x["ask"] is not None and c["min_price"] <= x["ask"] <= c["max_price"],
             "need": f"{round(c['min_price'] * 100)}–{round(c['max_price'] * 100)}¢"},
            {"k": "volume", "label": "Traded today", "v": x["vol_24h"], "unit": "n",
             "ok": (x["vol_24h"] or 0) >= c["min_volume_24h"], "need": f"{c['min_volume_24h']}+"},
            {"k": "edge", "label": "Below fair after fees", "v": x["edge_c"], "unit": "c",
             "ok": x["edge_c"] is not None and x["edge_c"] >= mn, "need": f"{mn:g}c+"},
            {"k": "cap", "label": "Gap believable", "v": x["edge_c"], "unit": "c",
             "ok": x["edge_c"] is not None and x["edge_c"] <= mx, "need": f"≤{mx:g}c"},
        ]
        base_ok = all(i["ok"] for i in g)
        last, n = self.seen.get(x["ticker"], (0, 0))
        n = (n + 1 if last == self.scan_n - 1 else 1) if base_ok else 0
        self.seen[x["ticker"]] = (self.scan_n, n)
        need_n = int(c.get("confirm_scans", 2))
        g.append({"k": "confirm", "label": "Held for 2 checks" if need_n == 2 else f"Held for {need_n} checks",
                  "v": n, "unit": "n", "ok": n >= need_n, "need": f"{need_n}"})
        benched = self.d.bench.benched(f"{NAME}:{x['event_ticker']}")
        g.append({"k": "slot", "label": "Bet slot free", "v": open_n, "unit": "n",
                  "ok": not x["held"] and not x["event_held"] and not benched and open_n < int(c["max_open_bets"]),
                  "need": f"<{c['max_open_bets']} open",
                  "note": "already in this game" if x["event_held"] else ("stopped out of this game" if benched else None)})
        x["gates"] = g
        x["ready"] = all(i["ok"] for i in g)
        x["why"] = next((i["label"] for i in g if not i["ok"]), "ready")

    # ---------- entry ----------
    def snapshot(self, x: dict, feat: dict) -> dict:
        return {"league": x["league"], "game": x["game"], "team": x["team"], "phase": x["phase"],
                "fair_yes": x["fair"], "fair_source": x["fair_source"], "price_c": r((x["ask"] or 0) * 100, 1),
                "model_edge_c": x.get("edge_gross_c"), "fee_c": x["fee_c"], "spread_c": x["spread_c"],
                "spread_avg_10m_c": feat.get("spread_avg_10m_c"), "imbalance3": feat.get("imbalance3"),
                "kalshi_move_5m_c": feat.get("ret_5m_c"), "minutes_to_start": x["minutes_to_start"],
                "game_clock": x["clock"] if x["phase"] == "live" else None, "score_diff": x["score_diff"],
                "volume_24h": x["vol_24h"]}

    async def try_entry(self, x: dict, mode: str):
        t, md = x["ticker"], self.d.md
        book = await md.refresh_book(t, min_interval=1)
        if book is None or book.best_ask is None or book.best_bid is None:
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": ["no two-sided book"]}
            return
        x = dict(x, bid=book.best_bid, ask=book.best_ask, spread_c=r((book.best_ask - book.best_bid) * 100, 1))
        x["fee_c"] = r(kalshi_fee(x["ask"], 100, x["fee_type"], x["fee_mult"]), 4)
        self._set_fair(x, x["fair"], x["fair_source"])
        c = self.cfg
        mn = float((c.get("min_edge_c") or {}).get(x["phase"], 3))
        if x["edge_c"] is None or x["edge_c"] < mn or x["spread_c"] > c["max_spread_c"]:
            self.status = {"state": "hold", "mode": mode, "ticker": t,
                           "reasons": [f"book moved: edge {x['edge_c']}c, spread {x['spread_c']}c"]}
            return
        feat = md.features(t)
        s = self.snapshot(x, feat)
        did = f"sp_{int(time.time() * 1000)}"
        v = await self.d.judge.ask(NAME, BATTERY, s, did)
        rec = {"bot": NAME, "decision_id": did, "lead_id": did, "ticker": t, "mode": mode, "state": s,
               "battery_id": v.battery_id, "verdict": v.kind, "answers": v.answers, "model_version": v.model_version,
               "backend": v.backend, "book": {"bid": x["bid"], "ask": x["ask"]}}
        if not v.ok:
            rec["policy"] = {"action": "hold", "reasons": [f"judge {v.kind}"]}
            logs.write("decisions", rec)
            await handle_verdict_failure(self.d, NAME, v)
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": [f"judge {v.kind}"]}
            return
        maybe_pin(self.d, v)
        pol = self.d.cfg["policy"][NAME]
        worth = (v.answers.get("worth") or {}).get("p_true")
        degr = (v.answers.get("exec_degraded") or {}).get("p_true")
        reasons = []
        if worth is None or worth < pol["worth_min"]:
            reasons.append(f"worth {worth} < {pol['worth_min']}")
        if degr is not None and degr > pol["exec_degraded_max"]:
            reasons.append(f"execution degraded {degr} > {pol['exec_degraded_max']}")
        self._mark_judged(t, v, worth, degr, reasons)
        if reasons:
            rec["policy"] = {"action": "hold", "reasons": reasons}
            logs.write("decisions", rec)
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": reasons}
            return
        rec["policy"] = {"action": "buy_yes", "reasons": ["all gates passed"], "worth": worth, "exec_degraded": degr}
        await self.open_bet(x, mode, rec, v.model_version, did, forced=False)

    def _mark_judged(self, t, v, worth, degr, reasons):
        pol = self.d.cfg["policy"][NAME]
        for row in self.board.get("rows", []):
            if row["ticker"] == t:
                row["gates"] = row["gates"] + [
                    {"k": "worth", "label": f"Judge: worth it ({v.backend})", "v": worth, "ok": worth is not None and worth >= pol["worth_min"],
                     "need": f"{pol['worth_min']:.2f}"},
                    {"k": "exec", "label": "Clean fills", "v": None if degr is None else round(1 - degr, 3),
                     "ok": degr is None or degr <= pol["exec_degraded_max"], "need": f"{1 - pol['exec_degraded_max']:.2f}"}]
                row["judged"], row["reasons"] = True, reasons

    async def size(self, mode: str, px: float, fee_type: str, fee_mult: float) -> int:
        broker = self.d.broker_for(NAME, mode)
        if broker is not None:
            eq, avail = await broker.equity(), await broker.available()
        else:
            eq = avail = float(self.d.cfg["desk"].get("paper_bankroll", 50))
        stake = eq * float(self.cfg.get("size_pct_equity", 50)) / 100
        if avail is not None:
            stake = min(stake, max(0.0, avail - float(self.d.cfg["risk"]["balance_reserve_usd"])))
        unit = px + 0.07 * px * (1 - px) * fee_mult
        return max(0, int(stake // unit)) if unit > 0 else 0

    def exits_for(self, entry: float) -> tuple[float, float]:
        tp_pct = float(self.cfg.get("take_profit_pct", 10)) / 100
        sl_pct = float(self.cfg.get("stop_loss_pct", 5)) / 100
        tp = min(0.99, max(entry + 0.01, math.ceil(round(entry * (1 + tp_pct) * 100, 6)) / 100))
        sl = max(0.01, min(entry - 0.01, math.floor(round(entry * (1 - sl_pct) * 100, 6)) / 100))
        return round(tp, 2), round(sl, 2)

    async def open_bet(self, x: dict, mode: str, rec: dict, model_version: str | None, did: str, forced: bool):
        t, md = x["ticker"], self.d.md
        count = await self.size(mode, x["ask"], x["fee_type"], x["fee_mult"])
        rec["sizing"] = {"pct_equity": self.cfg.get("size_pct_equity"), "count": count}
        if count < 1:
            rec["policy"] = {"action": "hold", "reasons": ["stake too small for one contract"]}
            logs.write("decisions", rec)
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": rec["policy"]["reasons"]}
            return rec
        it = Intent(bot=NAME, ticker=t, side="bid", count=float(count), price=round(x["ask"], 2),
                    tif="immediate_or_cancel", expected_edge_c=x.get("edge_gross_c"), fee_type=x["fee_type"],
                    fee_mult=x["fee_mult"], model_version=model_version, state_ts=md.st(t).book_ts,
                    best_bid=x["bid"], best_ask=x["ask"], decision_id=did, tag="entry", forced=forced)
        rec["intent"] = it.to_dict()
        broker = self.d.broker_for(NAME, mode)
        pos = await broker.position(t) if broker else None
        ok, why = self.d.risk.check(it, pos.qty if pos else 0.0, await self.d.total_exposure(),
                                    await broker.available() if broker else None)
        rec["risk"] = {"ok": ok, "why": why}
        if not ok:
            logs.write("decisions", rec)
            self.status = {"state": "vetoed", "mode": mode, "ticker": t, "why": why}
            return rec
        if broker is None:
            rec["execution"] = "shadow: would place"
            logs.write("decisions", rec)
            logs.write("orders", {"kind": "would_place", "mode": mode, "intent": it.to_dict()})
            self.status = {"state": "shadow_signal", "mode": mode, "ticker": t}
            return rec
        ack = await broker.place(it, md.st(t).book)
        rec["ack"] = ack.__dict__
        logs.write("decisions", rec)
        logs.write("orders", {"kind": "placed" if ack.ok else "rejected", "mode": mode, "intent": it.to_dict(),
                              "ack": ack.__dict__})
        if ack.ok and ack.filled > 0:
            entry = ack.avg_price or it.price
            tp, sl = self.exits_for(round(entry, 2))
            self.d.book.acquire(lock_key(t), t, did, {
                "bot": NAME, "side": "bid", "qty": ack.filled, "entry": entry, "tp": tp, "stop": sl,
                "opened": time.time(), "mode": mode, "fee_type": it.fee_type, "fee_mult": it.fee_mult,
                "entry_fee": ack.fee, "event_ticker": x["event_ticker"], "espn_id": x["espn_id"], "path": x["path"],
                "game": x["game"], "team": x["team"], "league": x["league"], "fair_at_entry": x["fair"],
                "fair_source": x["fair_source"], "phase_at_entry": x["phase"], "manual": forced})
            self.status = {"state": "in_position", "mode": mode, "ticker": t}
            await self.d.alert("Sports bet placed", f"{mode}: {x['team']} ({x['game']}) x{ack.filled:.0f} @ "
                                                    f"{entry:.2f}, fair {x['fair']:.2f}")
        return rec

    async def force(self, ticker: str, seen_price: float | None = None, tolerance: float = 0.02) -> tuple[bool, str]:
        """Human override: skip the gates and the judge; the risk engine's hard limits still apply."""
        mode = effective_mode(self.d.cfg, NAME)
        if self.d.broker_for(NAME, mode) is None:
            return False, f"Sports is in {mode}. Move it to paper or higher to bet"
        if lock_key(ticker) in self.d.book.locks:
            return False, "Already holding that team"
        row = next((x for x in self.board.get("rows", []) if x["ticker"] == ticker), None)
        if row is None:
            return False, "That market isn't on the board right now"
        book = await self.d.md.refresh_book(ticker, min_interval=1)
        if book is None or book.best_ask is None:
            return False, "No price on that market right now"
        if seen_price is not None and book.best_ask > seen_price + tolerance + 1e-9:
            return False, f"Price moved to {round(book.best_ask * 100)}¢ (you saw {round(seen_price * 100)}¢). Nothing bought"
        x = dict(row, bid=book.best_bid, ask=book.best_ask)
        x["fee_c"] = r(kalshi_fee(x["ask"], 100, x["fee_type"], x["fee_mult"]), 4)
        if x.get("fair") is not None:
            self._set_fair(x, x["fair"], x["fair_source"])
        did = f"force_{int(time.time())}"
        rec = {"bot": NAME, "decision_id": did, "lead_id": did, "ticker": ticker, "mode": mode, "verdict": "forced",
               "backend": "human", "policy": {"action": "buy_yes", "reasons": ["forced from dashboard"]}}
        rec = await self.open_bet(x, mode, rec, None, did, forced=True)
        lk = self.d.book.held(lock_key(ticker))
        if lk:
            return True, f"Bought {x['team']} x{lk['qty']:.0f} at {lk['entry']:.2f}"
        return False, (rec.get("risk") or {}).get("why") or (rec.get("ack") or {}).get("message") or \
            ((rec.get("policy") or {}).get("reasons") or ["not filled"])[0]

    def control(self, ticker: str, action: str) -> tuple[bool, str]:
        k = lock_key(ticker)
        if k not in self.d.book.locks:
            return False, "No open bet on that team"
        if action == "stop":
            self.d.book.update(k, manual_stop=True)
            return True, "Closing the bet now"
        if action in ("ride", "unride"):
            self.d.book.update(k, ride=action == "ride")
            return True, "Riding: take-profit off, stop-loss stays" if action == "ride" else "Take-profit back on"
        return False, "unknown action"

    # ---------- open bets ----------
    async def manage(self, key: str, lk: dict, mode_now: str):
        t, md = lk["ticker"], self.d.md
        mode = lk.get("mode", mode_now)
        broker = self.d.broker_for(NAME, mode)
        if broker is None:
            self.d.book.release(key, "no_broker", lk["lead_id"])
            return
        book = await md.refresh_book(t, min_interval=3)
        pos = await broker.position(t)
        opens = await broker.open_orders(t)
        meta = md.st(t).meta
        if pos.qty == 0:
            for o in opens:
                await broker.cancel(o["order_id"], t)
            res = meta.get("result")
            if res in ("yes", "no"):
                rule, px, maker = "settled", 1.0 if res == "yes" else 0.0, False
            elif lk.get("tp_order"):
                rule, px, maker = "take_profit", lk["tp"], True
            else:
                rule, px, maker = "flat", None, False
            self._log_trade(lk, rule, px, maker_exit=maker, settled=rule == "settled", real=await self._real(lk, broker))
            self.d.book.release(key, rule, lk["lead_id"])
            if rule in ("take_profit", "settled"):
                await self.d.alert("Sports bet closed", f"{mode}: {lk.get('team')} {rule} @ {px:.2f}")
            return
        g = self.games.get(lk.get("espn_id"))
        game_over = (g and g.get("state") == "post") or meta.get("status") not in (None, "active", "open")
        if game_over:
            if time.time() - lk.get("meta_ts", 0) > 60:
                await md.refresh_meta(t)
                self.d.book.update(key, meta_ts=time.time())
            return                                       # trading has stopped; it settles on its own
        await self._ensure_tp(key, lk, pos, broker, book, opens, mode)
        rule = None
        if lk.get("manual_stop"):
            rule = "manual_stop"
        elif book is not None and book.best_bid is not None:
            if not lk.get("ride") and book.best_bid >= lk["tp"]:
                rule = "take_profit"
            elif book.best_bid <= lk["stop"]:
                rule = "stop"
        if rule:
            await self._exit(key, lk, pos, book, rule, broker, mode)

    async def _ensure_tp(self, key, lk, pos, broker, book, opens, mode):
        mine = [o for o in opens if o.get("order_id") == lk.get("tp_order")]
        if lk.get("ride"):
            for o in mine:
                await broker.cancel(o["order_id"], lk["ticker"])
            if lk.get("tp_order"):
                self.d.book.update(key, tp_order=None)
            return
        if mine and abs(mine[0].get("remaining", 0) - abs(pos.qty)) < 1e-9:
            return
        for o in mine:
            await broker.cancel(o["order_id"], lk["ticker"])
        if book is None or book.best_bid is None or book.best_bid >= lk["tp"]:
            return
        it = Intent(bot=NAME, ticker=lk["ticker"], side="ask", count=abs(pos.qty), price=lk["tp"], post_only=True,
                    fee_type=lk.get("fee_type", "quadratic"), fee_mult=lk.get("fee_mult", 1.0),
                    best_bid=book.best_bid, best_ask=book.best_ask, decision_id=lk["lead_id"], tag="take_profit")
        ok, _ = self.d.risk.check(it, pos.qty, await self.d.total_exposure(), None)
        if not ok:
            return
        ack = await broker.place(it, book)
        logs.write("orders", {"kind": "take_profit_rest", "mode": mode, "intent": it.to_dict(), "ack": ack.__dict__})
        if ack.ok and ack.order_id:
            self.d.book.update(key, tp_order=ack.order_id)

    async def _exit(self, key, lk, pos, book, rule, broker, mode):
        t = lk["ticker"]
        for o in await broker.open_orders(t):
            await broker.cancel(o["order_id"], t)
        self.d.book.update(key, tp_order=None)
        pos = await broker.position(t)
        if pos.qty == 0:
            return
        tries = self.exit_tries.get(t, 0)
        px = round(min(0.99, max(0.01, (book.best_bid if book and book.best_bid else lk["stop"]) - min(tries, 5) / 100)), 2)
        it = Intent(bot=NAME, ticker=t, side="ask", count=abs(pos.qty), price=px, tif="immediate_or_cancel",
                    reduce_only=True, fee_type=lk.get("fee_type", "quadratic"), fee_mult=lk.get("fee_mult", 1.0),
                    decision_id=lk["lead_id"], tag="exit")
        ok, _ = self.d.risk.check(it, pos.qty, await self.d.total_exposure(), None)
        if not ok:
            return
        ack = await broker.place(it, book)
        logs.write("orders", {"kind": "exit", "rule": rule, "mode": mode, "intent": it.to_dict(), "ack": ack.__dict__})
        after = await broker.position(t)
        if after.qty == 0 or (ack.ok and ack.filled >= abs(pos.qty) - 1e-9):
            self.exit_tries.pop(t, None)
            self._log_trade(lk, rule, ack.avg_price or px, ack_fee=ack.fee, real=await self._real(lk, broker))
            self.d.book.release(key, f"exit:{rule}", lk["lead_id"])
            if rule in ("stop", "manual_stop"):          # no buying the same game back right after a stop
                self.d.bench.bench(f"{NAME}:{lk.get('event_ticker')}", "sports_stopped",
                                   until=time.time() + float(self.cfg.get("reentry_after_stop_min", 120)) * 60)
            await self.d.alert("Sports bet closed", f"{mode}: {lk.get('team')} {rule} @ {px:.2f}")
        else:
            self.exit_tries[t] = tries + 1

    async def _real(self, lk, broker) -> dict | None:
        if lk.get("mode") not in ("live", "demo") or not hasattr(broker, "realized"):
            return None
        return await broker.realized(lk["ticker"], lk.get("opened", time.time() - 86400), "yes")

    def _log_trade(self, lk, rule, exit_px, maker_exit=False, settled=False, ack_fee=None, real=None):
        entry, qty = lk.get("entry"), lk.get("qty", 0)
        pnl = None
        if real and real.get("buy_qty"):
            # Real money: Kalshi's own fills are the record (price improvement and actual fees included).
            entry = real["avg_buy"]
            payout = real["proceeds"]
            if settled and exit_px is not None:
                payout += max(0.0, real["buy_qty"] - real["sell_qty"]) * exit_px
            elif real["avg_sell"] is not None:
                exit_px = real["avg_sell"]
            pnl = payout - real["cost"] - real["fees"]
        elif exit_px is not None and entry is not None:
            ft, fm = lk.get("fee_type", "quadratic"), lk.get("fee_mult", 1.0)
            exit_fee = 0.0 if settled else (ack_fee if ack_fee is not None and lk.get("mode") == "paper"
                                            else kalshi_fee(exit_px, qty, ft, fm, maker=maker_exit))
            entry_fee = lk.get("entry_fee") if lk.get("mode") == "paper" else kalshi_fee(entry, qty, ft, fm)
            pnl = (exit_px - entry) * qty - (entry_fee or 0) - exit_fee
        logs.write("trades", {"bot": NAME, "lead_id": lk["lead_id"], "manual": bool(lk.get("manual")), "from_fills": bool(real),
                              "ticker": lk["ticker"], "side": "bid", "team": lk.get("team"), "game": lk.get("game"),
                              "qty": qty, "entry": entry, "exit": exit_px, "rule": rule, "pnl_after_fees": r(pnl, 4),
                              "mode": lk.get("mode"), "fair_at_entry": lk.get("fair_at_entry"),
                              "held_s": round(time.time() - lk.get("opened", time.time()))})

    # ---------- dashboard ----------
    def view(self) -> dict:
        bets = []
        for k, lk in self.locks().items():
            b = dict(lk)
            st = self.d.md.st(lk["ticker"])
            bk = st.book
            if bk and bk.best_bid is not None:
                b["mark"] = bk.best_bid
                b["upnl"] = round((bk.best_bid - lk["entry"]) * lk.get("qty", 0) - (lk.get("entry_fee") or 0), 4)
            g = self.games.get(lk.get("espn_id"))
            if g:
                b["clock"], b["state"] = g.get("detail"), g.get("state")
            bets.append(b)
        rows = []
        for x in self.board.get("rows", []):
            st = self.d.md.st(x["ticker"])
            if st.book and st.book.best_ask is not None and time.time() - st.book_ts < 120:
                x = dict(x, bid=st.book.best_bid, ask=st.book.best_ask)
            rows.append(x)
        return {"bets": bets, "board": dict(self.board, rows=rows),
                "cfg": {k: self.cfg.get(k) for k in ("size_pct_equity", "take_profit_pct", "stop_loss_pct",
                                                      "max_open_bets", "pregame_hours")}}
