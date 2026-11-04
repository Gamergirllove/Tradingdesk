"""The House: quotes both sides and gets paid by activity. Quotes are arithmetic; the judge only says
whether to quote, widen, pause, pull or kill."""
from __future__ import annotations

import math
import time

from .. import logs
from ..config import effective_mode, profile, series_of
from ..policy import rules
from ..questions.house import BATTERY
from ..risk.engine import Intent
from ..state import snapshot as snap
from ..state.snapshot import with_fair
from ..util import r
from .common import handle_verdict_failure, maybe_pin

NAME = "house"


def compute_quotes(feat: dict, inv: float, c: dict, widen: bool) -> tuple[float | None, float | None, dict]:
    """Avellaneda-Stoikov flavoured quotes in YES-price cents. Pure code."""
    bid_c, ask_c = round(feat["bid"] * 100), round(feat["ask"] * 100)
    ref = (feat.get("microprice") or feat["mid"]) * 100
    sigma = feat.get("rv_30m_c") or 1.0
    half = max(c["min_half_spread_c"], c["vol_mult"] * sigma) * (2 if widen else 1)
    skew = c["inventory_skew_c"] * inv
    res = ref - skew
    qb, qa = math.floor(res - half), math.ceil(res + half)
    room = ask_c - bid_c >= 3
    qb = min(qb, bid_c + (1 if room else 0), ask_c - 1)
    qa = max(qa, ask_c - (1 if room else 0), bid_c + 1)
    if qa <= qb:
        qa = qb + 1
    if inv >= c["max_inventory"]:
        qb = None
    if inv <= -c["max_inventory"]:
        qa = None
    info = {"ref_c": r(ref, 2), "half_c": r(half, 2), "skew_c": r(skew, 2), "sigma_c": sigma}
    return (qb / 100 if qb is not None else None), (qa / 100 if qa is not None else None), info


class House:
    def __init__(self, desk):
        self.d = desk
        self.mdx, self.fx = desk.md, desk.funnel
        self.last_scan = 0.0
        self.answers: dict | None = None
        self.answers_ts = 0.0
        self.answers_version: str | None = None
        self.last_judge_try = 0.0
        self.last_decision: rules.Decision | None = None
        self.prev_inv: float | None = None
        self.fill_events: list[tuple[float, int, float]] = []   # (ts, +1 bought / -1 sold, mid)
        self.outside_since: float | None = None
        self.shadow_quotes: dict[str, float | None] = {}
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
            await self.cycle(lock, mode)
            return
        blocked = self.d.risk.blocked(NAME)
        if blocked:
            self.status = {"state": "blocked", "reason": blocked, "mode": mode}
            return
        if time.time() - self.last_scan < self.cfg["scan_every_s"]:
            return
        self.last_scan = time.time()
        self.status = {"state": "scanning", "mode": mode}
        cand = await self.fx.run(NAME)
        if not cand:
            self.status = {"state": "no_market", "mode": mode, "funnel": self.fx.last_stats.get(NAME)}
            return
        mid = cand.feat["mid"]
        band = (round(max(0.02, mid - self.cfg["band_c"] / 100), 2), round(min(0.98, mid + self.cfg["band_c"] / 100), 2))
        self.answers, self.answers_ts, self.prev_inv, self.fill_events = None, 0.0, None, []
        br = self.d.broker_for(NAME, mode)
        start_eq = await br.equity() if br else None
        self.d.book.acquire(NAME, cand.ticker, cand.lead_id, {"mode": mode, "band": band, "start_mid": mid,
                                                             "start_equity": start_eq,
                                                             "opened": time.time(), "series": cand.series,
                                                             "fee_type": cand.fee_type, "fee_mult": cand.fee_mult})
        await self.d.alert("House opened a session", f"{mode}: quoting {cand.ticker} band {band}")

    async def ask_judge(self, lock, s: dict, mode: str):
        self.last_judge_try = time.time()
        v = await self.d.judge.ask(NAME, BATTERY, s, f"{lock['lead_id']}:{int(time.time())}")
        rec = {"bot": NAME, "decision_id": f"{lock['lead_id']}:{int(self.last_judge_try)}", "lead_id": lock["lead_id"],
               "ticker": lock["ticker"], "mode": mode, "state": s, "battery_id": v.battery_id, "verdict": v.kind,
               "answers": v.answers, "model_version": v.model_version, "backend": v.backend, "latency_ms": v.latency_ms}
        if v.ok:
            maybe_pin(self.d, v)
            self.answers, self.answers_ts, self.answers_version = v.answers, time.time(), v.model_version
            dec = rules.house(v.answers, self.d.cfg["policy"]["house"], self.d.cal)
            rec["policy"] = {"action": dec.action, "reasons": dec.reasons, "scores": dec.scores}
        else:
            rec["policy"] = {"action": "hold", "reasons": [f"judge {v.kind}"]}
            await handle_verdict_failure(self.d, NAME, v)
        logs.write("decisions", rec)
        return v

    async def cycle(self, lock: dict, mode_now: str):
        t, mode = lock["ticker"], lock.get("mode", mode_now)
        broker = self.d.broker_for(NAME, mode)
        book = await self.mdx.refresh_book(t, min_interval=2)
        await self.mdx.refresh_trades(t, min_interval=self.d.cfg["venue"]["trades_poll_seconds"])
        if book is None or book.mid is None:
            await self._pull(broker, t, "no book")
            return
        prod = self.d.und.product_for(lock.get("series"))
        if prod:
            await self.d.und.refresh(prod)
        und = self.d.und.features(prod)
        feat = with_fair(self.mdx.features(t), und, self.mdx.implied_vol(t, und.get("spot")))
        pos = await broker.position(t) if broker else None
        inv = pos.qty if pos else 0.0
        self._track_fills(inv, feat["mid"])
        if lock.get("manual_stop"):
            await self.end(lock, broker, mode, "stopped from dashboard", bench_reason=None)
            return
        blocked = self.d.risk.blocked(NAME)
        if blocked:
            await self.end(lock, broker, mode, f"blocked: {blocked}", bench_reason=None)
            return
        mtc = feat.get("minutes_to_close")
        if mtc is not None and mtc <= profile(self.d.cfg, "house", series_of(t))["stop_minutes_before_close"]:
            await self.end(lock, broker, mode, "closing", bench_reason="closing_soon")
            return
        lo, hi = lock["band"]
        if not (lo <= feat["mid"] <= hi):
            self.outside_since = self.outside_since or time.time()
            await self._pull(broker, t, "range_guard")
            self.status = {"state": "range_guard", "mode": mode, "ticker": t, "mid": feat["mid"], "band": lock["band"]}
            if time.time() - self.outside_since > 600:
                await self.end(lock, broker, mode, "range_break", bench_reason="breakout")
            return
        self.outside_since = None
        now = time.time()
        recent = [e for e in self.fill_events if e[0] >= now - 1800]
        adverse = sum(1 for ts, d, m in recent if (feat["mid"] - m) * d <= -0.02)
        s = snap.house(feat, und, inv, lock["band"], {"fills_30m": len(recent), "adverse_fills_30m": adverse})
        due = (now - self.answers_ts >= self.cfg["judge_every_s"]) and (now - self.last_judge_try >= 60)
        if self.answers is None or due:
            await self.ask_judge(lock, s, mode)
        if self.answers is None or now - self.answers_ts > self.cfg["judge_ttl_s"]:
            await self._pull(broker, t, "no fresh judgment")
            self.status = {"state": "holding", "mode": mode, "ticker": t, "reason": "no fresh judgment", "inv": inv}
            return
        dec = rules.house(self.answers, self.d.cfg["policy"]["house"], self.d.cal)
        self.last_decision = dec
        if dec.action == "kill":
            await self.end(lock, broker, mode, "volume_dead", bench_reason="volume_dead")
            return
        if dec.action in ("pause", "pull"):
            await self._pull(broker, t, dec.action)
            self.status = {"state": dec.action, "mode": mode, "ticker": t, "reasons": dec.reasons, "inv": inv}
            return
        qb, qa, info = compute_quotes(feat, inv, self.cfg, widen=dec.action == "widen")
        self.status = {"state": "quoting", "mode": mode, "ticker": t, "bid": qb, "ask": qa, "inv": inv,
                       "decision": dec.action, "mid": feat["mid"], **info}
        await self._sync_quotes(lock, broker, mode, feat, qb, qa, info)

    async def _sync_quotes(self, lock, broker, mode, feat, qb, qa, info):
        t = lock["ticker"]
        want = {"bid": qb, "ask": qa}
        have: dict[str, dict] = {}
        for o in (await broker.open_orders(t) if broker else []):
            if o["side"] in have:                      # never let stale duplicates pile up
                await broker.cancel(o["order_id"], t)
            else:
                have[o["side"]] = o
        if broker is None:
            have = {k: {"price": v, "order_id": None} for k, v in self.shadow_quotes.items() if v is not None}
        mid_c = feat["mid"] * 100
        adverse_c = 0.5 * (feat.get("rv_30m_c") or 1.0)
        for side, px in want.items():
            cur = have.get(side)
            if cur and px is not None and abs(cur["price"] - px) * 100 < self.cfg["requote_min_change_c"] - 1e-9:
                continue
            if cur and broker:
                await broker.cancel(cur["order_id"], t)
            self.shadow_quotes[side] = px
            if px is None:
                continue
            edge = (mid_c - px * 100 if side == "bid" else px * 100 - mid_c) - adverse_c
            it = Intent(bot=NAME, ticker=t, side=side, count=float(self.cfg["quote_contracts"]), price=px,
                        post_only=True, expected_edge_c=r(edge, 2), fee_type=lock.get("fee_type", "quadratic"),
                        fee_mult=lock.get("fee_mult", 1.0), model_version=self.answers_version,
                        state_ts=self.mdx.st(t).book_ts, best_bid=feat["bid"], best_ask=feat["ask"],
                        band=tuple(lock["band"]), decision_id=lock["lead_id"], tag="quote")
            pos = await broker.position(t) if broker else None
            ok, why = self.d.risk.check(it, pos.qty if pos else 0.0, await self.d.total_exposure(),
                                        await broker.available() if broker else None)
            if not ok:
                continue
            if broker is None:
                logs.write("orders", {"kind": "would_quote", "mode": mode, "intent": it.to_dict(), "quote": info})
                continue
            ack = await broker.place(it, self.mdx.st(t).book)
            logs.write("orders", {"kind": "quote", "mode": mode, "intent": it.to_dict(), "ack": ack.__dict__})

    def _track_fills(self, inv: float, mid: float):
        if self.prev_inv is not None and inv != self.prev_inv:
            self.fill_events.append((time.time(), 1 if inv > self.prev_inv else -1, mid))
            self.fill_events = self.fill_events[-200:]
        self.prev_inv = inv

    async def _pull(self, broker, ticker: str, why: str):
        self.shadow_quotes = {}
        if broker:
            n = await broker.cancel_all(ticker)
            if n:
                logs.write("orders", {"kind": "pull", "ticker": ticker, "why": why, "n": n})

    async def end(self, lock, broker, mode, reason: str, bench_reason: str | None):
        t = lock["ticker"]
        await self._pull(broker, t, f"end:{reason}")
        if broker:
            pos = await broker.position(t)
            if pos.qty:
                book = self.mdx.st(t).book
                if book and book.mid is not None:
                    px = book.best_bid if pos.qty > 0 else book.best_ask
                    it = Intent(bot=NAME, ticker=t, side="ask" if pos.qty > 0 else "bid", count=abs(pos.qty),
                                price=round(px, 2), tif="immediate_or_cancel", reduce_only=True,
                                fee_type=lock.get("fee_type", "quadratic"), fee_mult=lock.get("fee_mult", 1.0),
                                decision_id=lock["lead_id"], tag="exit")
                    ok, _ = self.d.risk.check(it, pos.qty, await self.d.total_exposure(), None)
                    if ok:
                        ack = await broker.place(it, book)
                        logs.write("orders", {"kind": "flatten", "reason": reason, "mode": mode,
                                              "intent": it.to_dict(), "ack": ack.__dict__})
                if (await broker.position(t)).qty:
                    self.status = {"state": "flattening", "mode": mode, "ticker": t, "reason": reason}
                    return          # keep the lock until flat
        pnl = None
        if broker and lock.get("start_equity") is not None:
            pnl = round(await broker.equity() - lock["start_equity"], 4)
        logs.write("trades", {"bot": NAME, "lead_id": lock["lead_id"], "ticker": t, "session_end": reason,
                              "pnl_after_fees": pnl,
                              "mode": mode, "held_s": round(time.time() - lock.get("opened", time.time())),
                              "fills": len(self.fill_events)})
        if bench_reason:
            self.d.bench.bench(f"{NAME}:{t}", bench_reason, scale=self.fx._bscale(t))
        self.d.book.release(NAME, f"end:{reason}", lock["lead_id"])
        self.answers = None
        self.status = {"state": "idle", "mode": mode, "last_end": reason}
        await self.d.alert("House closed a session", f"{mode}: {t} ended ({reason})")
