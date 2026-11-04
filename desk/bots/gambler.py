"""The Gambler: bets on direction. One position at a time; exits are arithmetic, never re-judged."""
from __future__ import annotations

import math
import time

from .. import logs
from ..config import effective_mode
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
        if dec.action == "hold":
            logs.write("decisions", rec)
            if any(x.startswith("worth") for x in dec.reasons):
                self.d.bench.bench(f"{NAME}:{t}", "judge_no")
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
            sc, buf = self.cfg["stop_c"] / 100, self.cfg["stop_buffer_c"] / 100
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
        if book is None or book.mid is None:
            self.stale_reads += 1
            if self.stale_reads <= 2:
                return
            rule = "data_stale"
        else:
            self.stale_reads = 0
            rule = self._exit_rule(lock, book)
        self.status = {"state": "in_position", "mode": mode, "ticker": t, "qty": pos.qty,
                       "entry": lock.get("entry"), "stop": lock.get("stop"), "tp": lock.get("tp"),
                       "mark": book.mid if book else None}
        if not rule:
            return
        await self._exit(lock, pos, book, rule, broker, mode)

    async def _ensure_tp(self, lock, pos, broker, book, opens, mode):
        """Rest the take-profit as a post-only maker order (0 maker fee on these series) for the size held."""
        long_yes = pos.qty > 0
        side = "ask" if long_yes else "bid"
        mine = [o for o in opens if o.get("order_id") == lock.get("tp_order")]
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
        c = self.cfg
        f = self.mdx.features(lock["ticker"])
        long_yes = lock["side"] == "bid"
        exit_px = book.best_bid if long_yes else book.best_ask
        if exit_px is None:
            return "no_exit_price"
        if long_yes and exit_px >= lock["tp"] or (not long_yes and exit_px <= lock["tp"]):
            return "take_profit"
        if long_yes and exit_px <= lock["stop"] or (not long_yes and exit_px >= lock["stop"]):
            return "stop"
        mtc = f.get("minutes_to_close")
        if mtc is not None and mtc <= c["exit_minutes_before_close"]:
            return "time_to_close"
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
        logs.write("trades", {"bot": NAME, "lead_id": lock["lead_id"], "ticker": lock["ticker"], "side": lock["side"],
                              "qty": qty, "entry": entry, "exit": exit_px, "rule": rule, "pnl_after_fees": r(pnl, 4),
                              "mode": lock.get("mode"), "held_s": round(time.time() - lock.get("opened", time.time()))})
