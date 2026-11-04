"""The Gambler: bets on direction. One position at a time; exits are arithmetic, never re-judged."""
from __future__ import annotations

import math
import time

from .. import logs
from ..config import effective_mode, profile, series_of
from ..policy import rules
from ..questions.gambler import BATTERY
from ..risk.engine import Intent
from ..state import snapshot as snap
from ..state.snapshot import with_fair
from ..util import kalshi_fee, r
from .common import handle_verdict_failure, maybe_pin

NAME = "gambler"


class Gambler:
    def __init__(self, desk):
        self.d = desk
        self.mdx, self.fx = desk.md, desk.funnel
        self.last_decision = 0.0
        self.stale_reads = 0
        self.exit_tries = 0
        self.status: dict = {"state": "idle"}

    @property
    def cfg(self):
        return self.d.cfg["bots"][NAME]

    async def step(self):
        if not self.cfg.get("enabled", True):
            self.status = {"state": "disabled"}
            return
        mode = effective_mode(self.d.cfg, NAME)
        lock = self.d.book.held(NAME)
        data_mode = lock.get("mode", mode) if lock else mode
        self.mdx, self.fx = self.d.md_for(data_mode), self.d.funnel_for(data_mode)
        if lock:
            await self.manage(lock, mode)
            return
        blocked = self.d.risk.blocked(NAME)
        if blocked:
            self.status = {"state": "blocked", "reason": blocked, "mode": mode}
            return
        if time.time() - self.last_decision < self.cfg["decision_every_s"]:
            return
        self.last_decision = time.time()
        await self.decide(mode)

    async def decide(self, mode: str):
        self.status = {"state": "scanning", "mode": mode, "ts": time.time()}
        cand = await self.fx.run(NAME)
        if not cand:
            self.status = {"state": "no_trade", "mode": mode, "ts": time.time(),
                           "funnel": self.fx.last_stats.get(NAME)}
            return
        t = cand.ticker
        await self.mdx.refresh_book(t, min_interval=2)
        await self.mdx.refresh_trades(t, min_interval=10)
        feat = with_fair(self.mdx.features(t), cand.und, self.mdx.implied_vol(t, cand.und.get("spot")))
        s = snap.gambler(feat, cand.und, self.d.flow.features() if getattr(self.d, "flow", None) else None)
        did = cand.lead_id
        v = await self.d.judge.ask(NAME, BATTERY, s, did)
        rec = {"bot": NAME, "decision_id": did, "lead_id": cand.lead_id, "ticker": t, "mode": mode, "state": s,
               "battery_id": v.battery_id, "verdict": v.kind, "answers": v.answers, "model_version": v.model_version,
               "backend": v.backend, "latency_ms": v.latency_ms, "book": {"bid": feat.get("bid"), "ask": feat.get("ask")}}
        if not v.ok:
            rec["policy"] = {"action": "hold", "reasons": [f"judge {v.kind}"]}
            logs.write("decisions", rec)
            await handle_verdict_failure(self.d, NAME, v)
            self.status = {"state": "hold", "mode": mode, "reason": f"judge {v.kind}", "ticker": t}
            return
        maybe_pin(self.d, v)
        dec = rules.gambler(v.answers, s, self.d.cfg["policy"]["gambler"], self.d.cal)
        rec["policy"] = {"action": dec.action, "reasons": dec.reasons, "scores": dec.scores}
        self._board_judged(t, s, v, dec)
        if dec.action == "hold":
            logs.write("decisions", rec)
            if any(x.startswith("worth") for x in dec.reasons):
                self.d.bench.bench(f"{NAME}:{t}", "judge_no", scale=self.fx._bscale(t))
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": dec.reasons}
            return
        side = "bid" if dec.action == "buy_yes" else "ask"
        px = round(feat["ask"] if side == "bid" else feat["bid"], 2)
        fair = feat.get("fair_yes")
        edge_c = ((fair - px) * 100) if side == "bid" else ((px - fair) * 100)
        tp = self.tp_price(side, px)
        gain_c = round(abs(tp - px) * 100, 2)
        fee_c = kalshi_fee(px, 100, cand.fee_type, cand.fee_mult) / 100 * 100     # per-contract taker fee, cents
        count = await self.size(mode, side, px, cand.fee_type, cand.fee_mult)
        rec["sizing"] = {"pct_equity": self.cfg.get("size_pct_equity"), "count": count, "tp": tp,
                         "tp_gain_c": gain_c, "entry_fee_c": round(fee_c, 3)}
        if gain_c <= fee_c:
            rec["policy"]["action"] = "hold"
            rec["policy"]["reasons"] = [f"{self.cfg['take_profit_pct']}% target is {gain_c}c, entry fee is {fee_c:.2f}c"]
            logs.write("decisions", rec)
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": rec["policy"]["reasons"]}
            return
        if count < 1:
            rec["policy"]["action"] = "hold"
            rec["policy"]["reasons"] = ["stake too small for one contract"]
            logs.write("decisions", rec)
            self.status = {"state": "hold", "mode": mode, "ticker": t, "reasons": rec["policy"]["reasons"]}
            return
        it = Intent(bot=NAME, ticker=t, side=side, count=float(count), price=px,
                    tif="immediate_or_cancel", expected_edge_c=r(edge_c, 2), fee_type=cand.fee_type,
                    fee_mult=cand.fee_mult, model_version=v.model_version, state_ts=self.mdx.st(t).book_ts,
                    best_bid=feat["bid"], best_ask=feat["ask"], decision_id=did, tag="entry")
        rec["intent"] = it.to_dict()
        await self.place_entry(it, mode, rec, feat)

    def _board_judged(self, ticker, snapshot, v, dec):
        """Attach the judged logic trace to the board row the dashboard shows."""
        pol = self.d.cfg["policy"]["gambler"]
        sc = dec.scores
        act = sc.get("action")
        flow = sc.get("flow_favors_yes")
        flow_side = None if flow is None else (flow if act != "buy_no" else 1 - flow)
        gates = [
            {"k": "edge", "label": "Fair value edge", "v": sc.get("model_edge_c"), "unit": "c",
             "ok": sc.get("model_edge_c") is not None and abs(sc["model_edge_c"]) >= pol["min_model_edge_c"],
             "need": f"±{pol['min_model_edge_c']}c"},
            {"k": "flow", "label": "Spot order flow", "v": flow_side, "ok": flow_side is not None and flow_side >= pol.get("flow_min", 0.55),
             "need": f"{pol.get('flow_min', 0.55):.2f}"},
            {"k": "regime", "label": "Trending", "v": sc.get("trend"), "ok": (sc.get("trend") or 0) >= pol["trend_min"],
             "need": f"{pol['trend_min']:.2f}"},
            {"k": "setup", "label": "Setup ≥ 2", "v": sc.get("setup_ge2"), "ok": (sc.get("setup_ge2") or 0) >= pol["setup_ge2_min"],
             "need": f"{pol['setup_ge2_min']:.2f}"},
            {"k": "action", "label": f"Action {act or '?'}", "v": sc.get("action_conf"),
             "ok": act in ("buy_yes", "buy_no") and (sc.get("action_conf") or 0) >= pol["action_conf_min"],
             "need": f"{pol['action_conf_min']:.2f}"},
            {"k": "worth", "label": "Worth it", "v": sc.get("worth"), "ok": (sc.get("worth") or 0) >= pol["worth_min"],
             "need": f"{pol['worth_min']:.2f}"},
            {"k": "exec", "label": "Clean fills", "v": None if sc.get("exec_degraded") is None else 1 - sc["exec_degraded"],
             "ok": (sc.get("exec_degraded") if sc.get("exec_degraded") is not None else 1) <= pol["exec_degraded_max"],
             "need": f"{1 - pol['exec_degraded_max']:.2f}"},
        ]
        b = self.fx.board.get(NAME)
        if not b:
            return
        for row in b["rows"]:
            if row["ticker"] == ticker:
                row.update({"judged": True, "judge": v.backend, "action": dec.action, "reasons": dec.reasons,
                            "gates": gates, "ts_judged": time.time()})

    async def force(self, ticker: str, side_word: str, seen_price: float | None = None,
                    tolerance: float = 0.02) -> tuple[bool, str]:
        """Human override: open a trade the judge didn't. Hard limits still apply."""
        if side_word not in ("yes", "no"):
            return False, "side must be yes or no"
        mode = effective_mode(self.d.cfg, NAME)
        if self.d.book.held(NAME):
            return False, "Close the open trade first (one trade at a time)"
        if self.d.broker_for(NAME, mode) is None:
            return False, f"The Gambler is in {mode}. Move it to paper or higher to place trades"
        md = self.d.md_for(mode)
        book = await md.refresh_book(ticker, min_interval=1)
        if book is None or book.mid is None:
            return False, "No two-sided price on that market right now"
        meta = md.st(ticker).meta
        ft, fm = await md.series_fee(meta.get("_series") or ticker.split("-")[0])
        side = "bid" if side_word == "yes" else "ask"
        px = round(book.best_ask if side == "bid" else book.best_bid, 2)
        held_px = px if side == "bid" else round(1 - px, 2)          # what one contract of your side costs
        if seen_price is not None and held_px > seen_price + tolerance + 1e-9:
            return False, f"Price moved to {round(held_px * 100)}¢ (you saw {round(seen_price * 100)}¢). Nothing bought"
        count = await self.size(mode, side, px, ft, fm)
        if count < 1:
            return False, "Stake is too small for one contract"
        feat = md.features(ticker)
        did = f"force_{int(time.time())}"
        it = Intent(bot=NAME, ticker=ticker, side=side, count=float(count), price=px, tif="immediate_or_cancel",
                    fee_type=ft, fee_mult=fm, state_ts=md.st(ticker).book_ts, best_bid=book.best_bid,
                    best_ask=book.best_ask, decision_id=did, tag="entry", forced=True)
        rec = {"bot": NAME, "decision_id": did, "lead_id": did, "ticker": ticker, "mode": mode, "verdict": "forced",
               "policy": {"action": f"buy_{side_word}", "reasons": ["forced from dashboard"]},
               "sizing": {"pct_equity": self.cfg.get("size_pct_equity"), "count": count, "tp": self.tp_price(side, px)},
               "intent": it.to_dict(), "backend": "human"}
        self.mdx = md
        await self.place_entry(it, mode, rec, feat)
        lock = self.d.book.held(NAME)
        if lock:
            self.d.book.update(NAME, manual=True)
            return True, f"Bought {'YES' if side == 'bid' else 'NO'} x{lock['qty']:.0f} at {lock['entry']:.2f}"
        return False, (rec.get("risk") or {}).get("why") or (rec.get("ack") or {}).get("message") or "not filled"

    def control(self, action: str) -> tuple[bool, str]:
        lock = self.d.book.held(NAME)
        if not lock:
            return False, "No open trade"
        if action == "stop":
            self.d.book.update(NAME, manual_stop=True)
            return True, "Closing the trade now"
        if action in ("ride", "unride"):
            self.d.book.update(NAME, ride=action == "ride")
            return True, "Riding: take-profit off, stop-loss stays" if action == "ride" else "Take-profit back on"
        return False, "unknown action"

    def tp_price(self, side: str, entry: float) -> float:
        """+take_profit_pct on the contract you hold. YES at 0.40 -> 0.42. NO bought at YES 0.60 (NO 0.40) -> YES 0.58."""
        pct = float(self.cfg.get("take_profit_pct", 5)) / 100
        if side == "bid":
            return min(0.99, math.ceil(round(entry * (1 + pct) * 100, 6)) / 100)
        no = 1 - entry
        no_tp = math.ceil(round(no * (1 + pct) * 100, 6)) / 100
        return max(0.01, round(1 - no_tp, 2))

    async def size(self, mode: str, side: str, px: float, fee_type: str, fee_mult: float) -> int:
        """size_pct_equity of the account per trade, whole contracts, fee included. Never from model confidence."""
        broker = self.d.broker_for(NAME, mode)
        if broker is not None:
            eq = await broker.equity()
            avail = await broker.available()
        else:
            eq = avail = float(self.d.cfg["desk"].get("paper_bankroll", 50))
        stake = eq * float(self.cfg.get("size_pct_equity", 50)) / 100
        if avail is not None:
            stake = min(stake, max(0.0, avail - float(self.d.cfg["risk"]["balance_reserve_usd"])))
        unit = (px if side == "bid" else 1 - px) + 0.07 * px * (1 - px) * fee_mult
        return max(0, int(stake // unit)) if unit > 0 else 0

    async def place_entry(self, it: Intent, mode: str, rec: dict, feat: dict):
        broker = self.d.broker_for(NAME, mode)
        pos = await broker.position(it.ticker) if broker else None
        ok, why = self.d.risk.check(it, pos.qty if pos else 0.0, await self.d.total_exposure(),
                                    await broker.available() if broker else None)
        rec["risk"] = {"ok": ok, "why": why}
        if not ok:
            logs.write("decisions", rec)
            self.status = {"state": "vetoed", "mode": mode, "ticker": it.ticker, "why": why}
            return
        if broker is None:
            rec["execution"] = "shadow: would place"
            logs.write("decisions", rec)
            logs.write("orders", {"kind": "would_place", "mode": mode, "intent": it.to_dict()})
            self.status = {"state": "shadow_signal", "mode": mode, "ticker": it.ticker, "action": it.side}
            return
        ack = await broker.place(it, self.mdx.st(it.ticker).book)
        rec["ack"] = ack.__dict__
        logs.write("decisions", rec)
        logs.write("orders", {"kind": "placed" if ack.ok else "rejected", "mode": mode, "intent": it.to_dict(),
                              "ack": ack.__dict__})
        if ack.ok and ack.filled > 0:
            entry = ack.avg_price or it.price
            lo, hi = feat.get("lo_60m"), feat.get("hi_60m")
            pc = profile(self.d.cfg, "gambler", series_of(it.ticker))
            sc, buf = pc["stop_c"] / 100, pc["stop_buffer_c"] / 100
            if it.side == "bid":
                swing = (lo - buf) if lo is not None else entry - sc
                stop = max(entry - 2 * sc, min(entry - sc, swing))
            else:
                swing = (hi + buf) if hi is not None else entry + sc
                stop = min(entry + 2 * sc, max(entry + sc, swing))
            tp = self.tp_price(it.side, round(entry, 2))
            self.d.book.acquire(NAME, it.ticker, it.decision_id, {
                "side": it.side, "qty": ack.filled, "entry": entry, "stop": round(stop, 2), "tp": round(tp, 2),
                "opened": time.time(), "mode": mode, "fee_type": it.fee_type, "fee_mult": it.fee_mult,
                "entry_fee": ack.fee})
            self.status = {"state": "in_position", "mode": mode, "ticker": it.ticker}
            await self.d.alert("Gambler entered", f"{mode}: {'YES' if it.side == 'bid' else 'NO'} "
                                                  f"{ack.filled:.0f} @ {entry:.2f} on {it.ticker}")

    async def manage(self, lock: dict, mode_now: str):
        t = lock["ticker"]
        mode = lock.get("mode", mode_now)
        broker = self.d.broker_for(NAME, mode)
        if broker is None:
            self.d.book.release(NAME, "no_broker", lock["lead_id"])
            return
        book = await self.mdx.refresh_book(t, min_interval=2)
        pos = await broker.position(t)
        opens = await broker.open_orders(t)
        if pos.qty == 0:
            for o in opens:
                await broker.cancel(o["order_id"], t)
            if lock.get("tp_order"):
                self._log_trade(lock, None, "take_profit", exit_px=lock["tp"], maker_exit=True)
                await self.d.alert("Gambler hit take profit", f"{mode}: {t} @ {lock['tp']:.2f}")
            else:
                self._log_trade(lock, None, "flat")
            self.d.book.release(NAME, "take_profit" if lock.get("tp_order") else "flat", lock["lead_id"])
            self.status = {"state": "idle", "mode": mode}
            return
        await self._ensure_tp(lock, pos, broker, book, opens, mode)
        if lock.get("manual_stop"):
            rule = "manual_stop"
        elif book is None or book.mid is None:
            self.stale_reads += 1
            if self.stale_reads <= 2:
                return
            rule = "data_stale"
        else:
            self.stale_reads = 0
            rule = self._exit_rule(lock, book)
        self.status = {"state": "riding" if lock.get("ride") else "in_position", "mode": mode, "ticker": t,
                       "qty": pos.qty, "entry": lock.get("entry"), "stop": lock.get("stop"), "tp": lock.get("tp"),
                       "mark": book.mid if book else None}
        if not rule:
            return
        await self._exit(lock, pos, book, rule, broker, mode)

    async def _ensure_tp(self, lock, pos, broker, book, opens, mode):
        """Rest the take-profit as a post-only maker order (0 maker fee on these series) for the size held."""
        long_yes = pos.qty > 0
        side = "ask" if long_yes else "bid"
        mine = [o for o in opens if o.get("order_id") == lock.get("tp_order")]
        if lock.get("ride"):
            for o in mine:
                await broker.cancel(o["order_id"], lock["ticker"])
            if lock.get("tp_order"):
                self.d.book.update(NAME, tp_order=None)
                lock["tp_order"] = None
            return
        if mine and abs(mine[0].get("remaining", 0) - abs(pos.qty)) < 1e-9:
            return
        for o in mine:
            await broker.cancel(o["order_id"], lock["ticker"])
        if book is None or book.mid is None:
            return
        tp = lock["tp"]
        if (long_yes and book.best_bid is not None and book.best_bid >= tp) or \
                (not long_yes and book.best_ask is not None and book.best_ask <= tp):
            return      # market is already through the target; _exit_rule takes it this cycle
        it = Intent(bot=NAME, ticker=lock["ticker"], side=side, count=abs(pos.qty), price=tp, post_only=True,
                    fee_type=lock.get("fee_type", "quadratic"), fee_mult=lock.get("fee_mult", 1.0),
                    best_bid=book.best_bid, best_ask=book.best_ask, decision_id=lock["lead_id"], tag="take_profit")
        ok, why = self.d.risk.check(it, pos.qty, await self.d.total_exposure(), None)
        if not ok:
            return
        ack = await broker.place(it, book)
        logs.write("orders", {"kind": "take_profit_rest", "mode": mode, "intent": it.to_dict(), "ack": ack.__dict__})
        if ack.ok and ack.order_id:
            self.d.book.update(NAME, tp_order=ack.order_id)
            lock["tp_order"] = ack.order_id

    def _exit_rule(self, lock: dict, book) -> str | None:
        c = profile(self.d.cfg, "gambler", series_of(lock["ticker"]))
        f = self.mdx.features(lock["ticker"])
        long_yes = lock["side"] == "bid"
        exit_px = book.best_bid if long_yes else book.best_ask
        if exit_px is None:
            return "no_exit_price"
        ride = bool(lock.get("ride"))
        if not ride and (long_yes and exit_px >= lock["tp"] or (not long_yes and exit_px <= lock["tp"])):
            return "take_profit"
        if long_yes and exit_px <= lock["stop"] or (not long_yes and exit_px >= lock["stop"]):
            return "stop"
        mtc = f.get("minutes_to_close")
        if mtc is not None and mtc <= c["exit_minutes_before_close"]:
            return "time_to_close"
        if ride:
            return None
        if time.time() - lock["opened"] > c["max_hold_minutes"] * 60:
            return "max_hold"
        ratio = f.get("vol_30m_vs_pace")
        if ratio is not None and ratio < c["volume_exit_ratio"] and time.time() - lock["opened"] > 1800:
            return "volume_died"
        return None

    async def _exit(self, lock, pos, book, rule, broker, mode):
        for o in await broker.open_orders(lock["ticker"]):
            await broker.cancel(o["order_id"], lock["ticker"])
        lock.pop("tp_order", None)
        self.d.book.update(NAME, tp_order=None)
        pos = await broker.position(lock["ticker"])
        if pos.qty == 0:
            return
        long_yes = pos.qty > 0
        slip = min(self.exit_tries, 5) / 100
        if book is not None and book.mid is not None:
            px = (book.best_bid - slip) if long_yes else (book.best_ask + slip)
        else:
            last = self.mdx.st(lock["ticker"]).hist
            m = last[-1][1] if last else lock["entry"]
            px = (m - 0.05) if long_yes else (m + 0.05)
        px = round(min(0.99, max(0.01, px)), 2)
        it = Intent(bot=NAME, ticker=lock["ticker"], side="ask" if long_yes else "bid", count=abs(pos.qty),
                    price=px, tif="immediate_or_cancel", reduce_only=True, fee_type=lock.get("fee_type", "quadratic"),
                    fee_mult=lock.get("fee_mult", 1.0), decision_id=lock["lead_id"], tag="exit")
        ok, why = self.d.risk.check(it, pos.qty, await self.d.total_exposure(), None)
        if not ok:
            return
        ack = await broker.place(it, book)
        logs.write("orders", {"kind": "exit", "rule": rule, "mode": mode, "intent": it.to_dict(), "ack": ack.__dict__})
        after = await broker.position(lock["ticker"])
        if after.qty == 0:
            self.exit_tries = 0
            self._log_trade(lock, ack, rule)
            self.d.book.release(NAME, f"exit:{rule}", lock["lead_id"])
            self.status = {"state": "idle", "mode": mode, "last_exit": rule}
            await self.d.alert("Gambler exited", f"{mode}: {rule} on {lock['ticker']} @ {px:.2f}")
        else:
            self.exit_tries += 1

    def _log_trade(self, lock, ack, rule, exit_px=None, maker_exit=False):
        entry, qty = lock.get("entry"), lock.get("qty", 0)
        if exit_px is None:
            exit_px = ack.avg_price if ack and ack.avg_price else None
        pnl = None
        if exit_px is not None and entry is not None:
            pnl = (exit_px - entry) * qty if lock["side"] == "bid" else (entry - exit_px) * qty
            ft, fm = lock.get("fee_type", "quadratic"), lock.get("fee_mult", 1.0)
            exit_fee = kalshi_fee(exit_px, qty, ft, fm, maker=maker_exit)
            if lock.get("mode") in ("demo", "live"):
                fees = kalshi_fee(entry, qty, ft, fm) + exit_fee
            else:
                fees = (lock.get("entry_fee") or 0) + ((ack.fee or 0) if ack else exit_fee)
            pnl -= fees
        logs.write("trades", {"bot": NAME, "lead_id": lock["lead_id"], "manual": bool(lock.get("manual")),
                              "ticker": lock["ticker"], "side": lock["side"],
                              "qty": qty, "entry": entry, "exit": exit_px, "rule": rule, "pnl_after_fees": r(pnl, 4),
                              "mode": lock.get("mode"), "held_s": round(time.time() - lock.get("opened", time.time()))})
