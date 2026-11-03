"""The funnel: kill in order of cost, cheapest check first. Every rejection is logged with the check that fired.

1 free cut on data already in hand  ->  2 one-call checks  ->  3 hard facts in code  ->  4 judge  ->  5 pick
No trade is a result.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from .. import logs
from ..policy.rules import scan_pick
from ..questions import scan as scanq
from ..state.market import MarketData
from ..state.snapshot import candidate_line, with_fair
from ..state.underlying import Underlying
from ..util import fnum, kalshi_fee, new_id, parse_ts


@dataclass
class Candidate:
    ticker: str
    series: str
    feat: dict
    und: dict
    lead_id: str
    fee_type: str = "quadratic"
    fee_mult: float = 1.0
    scan_answers: dict = field(default_factory=dict)


class Funnel:
    def __init__(self, md: MarketData, und: Underlying, bench, judge, cfg: dict, held_tickers):
        self.md, self.und, self.bench, self.judge, self.cfg = md, und, bench, judge, cfg
        self.held_tickers = held_tickers          # callable -> set of tickers held by any bot
        self.last_stats: dict[str, dict] = {}
        self.watch: dict[str, list[str]] = {}

    def _free_cut(self, bot: str, m: dict, fc: dict) -> str | None:
        if m.get("status") not in (None, "active", "open"):
            return "not_active"
        bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        if not bid or not ask or ask <= bid:
            return "no_two_sided_quote"
        if (ask - bid) * 100 > fc["max_spread_c"] + 1e-9:
            return "spread_wide"
        mid = (bid + ask) / 2
        if mid < fc["price_floor"] or mid > fc["price_ceiling"]:
            return "price_tail"
        if (fnum(m.get("volume_24h_fp"), 0) or 0) < fc["min_volume_24h"]:
            return "low_volume"
        if (fnum(m.get("open_interest_fp"), 0) or 0) < fc["min_open_interest"]:
            return "low_open_interest"
        close = parse_ts(m.get("close_time"))
        mins = (close - time.time()) / 60 if close else None
        if mins is None or mins < fc["min_minutes_to_close"]:
            return "closing_soon"
        if mins > fc["max_minutes_to_close"]:
            return "too_far"
        if bot == "house" and (ask - bid) * 100 < fc.get("house_min_spread_c", 1) - 1e-9:
            return "spread_tight"
        return None

    async def run(self, bot: str) -> Candidate | None:
        fc = self.cfg["funnel"]
        series = self.cfg["venue"]["universe_series"]
        lead = new_id("lead_")
        uni = await self.md.universe(series, self.cfg["venue"].get("universe_refresh_seconds", 120))
        held = self.held_tickers()
        rej: list[tuple[str, int, str]] = []
        counts = Counter()
        s1: list[dict] = []
        for m in uni:
            t = m["ticker"]
            key = f"{bot}:{t}"
            if t in held:
                counts["held_by_desk"] += 1
                continue
            b = self.bench.benched(key)
            if b:
                counts[f"benched:{b}"] += 1
                continue
            why = self._free_cut(bot, m, fc)
            if why:
                rej.append((t, 1, why))
                counts[why] += 1
                if why == "closing_soon":
                    self.bench.bench(key, why, until=parse_ts(m.get("close_time")) or None)
                elif why in self.cfg["bench_ttl_minutes"]:
                    self.bench.bench(key, why)
                continue
            s1.append(m)
        s1.sort(key=lambda m: -(fnum(m.get("volume_24h_fp"), 0) or 0))
        top = s1[: fc["depth_check_top_k"]]
        self.watch[bot] = [m["ticker"] for m in top]

        # stage 2: one call each (orderbook depth)
        s2 = []
        for m in top:
            t = m["ticker"]
            try:
                book = await self.md.refresh_book(t, min_interval=5)
            except Exception:
                book = None
            if book is None or book.mid is None:
                rej.append((t, 2, "no_book"))
                counts["no_book"] += 1
                continue
            d_b = sum(s for _, s in book.bids[:3])
            d_a = sum(s for _, s in book.asks[:3])
            if min(d_b, d_a) < fc["min_top3_depth"]:
                rej.append((t, 2, "thin_book"))
                counts["thin_book"] += 1
                self.bench.bench(f"{bot}:{t}", "thin_book")
                continue
            s2.append(m)

        # stage 3: hard facts in code
        s3: list[Candidate] = []
        for m in s2:
            t = m["ticker"]
            prod = self.und.product_for(m.get("_series"))
            if prod:
                await self.und.refresh(prod)
            und = self.und.features(prod)
            feat = with_fair(self.md.features(t), und, self.md.implied_vol(t, und.get("spot")))
            ft, fm = await self.md.series_fee(m.get("_series", ""))
            hist_len = len(self.md.st(t).hist)
            if feat.get("ret_15m_c") is None and hist_len < 20 and bot == "gambler":
                rej.append((t, 3, "too_early"))
                counts["too_early"] += 1
                continue
            if bot == "gambler":
                edge = feat.get("model_edge_c")
                if edge is None:
                    rej.append((t, 3, "no_fair_value"))
                    counts["no_fair_value"] += 1
                    self.bench.bench(f"{bot}:{t}", "no_fair_value")
                    continue
                px = feat["ask"] if edge > 0 else feat["bid"]
                fee_c = kalshi_fee(px, 1, ft, fm) * 100
                net = abs(edge) - (feat["spread_c"] / 2) - fee_c
                if abs(edge) < self.cfg["policy"]["gambler"]["min_model_edge_c"] or net < self.cfg["risk"]["min_edge_over_fees_c"]:
                    rej.append((t, 3, "no_edge"))
                    counts["no_edge"] += 1
                    self.bench.bench(f"{bot}:{t}", "no_edge")
                    continue
            s3.append(Candidate(t, m.get("_series", ""), feat, und, lead, ft, fm))

        if bot == "gambler":
            s3.sort(key=lambda c: -abs(c.feat.get("model_edge_c") or 0))
        else:
            s3.sort(key=lambda c: -((c.feat.get("vol_24h") or 0) * (c.feat.get("spread_c") or 0)))
        finalists = s3[: fc["judge_top_n"]]
        pick: Candidate | None = None
        jinfo: dict = {}
        if len(finalists) == 1:
            # No Choice over a single option; the bot's own battery carries the "worth it at all" check.
            pick = finalists[0]
            jinfo = {"verdict": "skipped_single_candidate"}
        elif finalists:
            cids = [chr(65 + i) for i in range(len(finalists))]
            state = {"bot": bot, "candidates": "\n".join(candidate_line(c, f.feat) for c, f in zip(cids, finalists))}
            qs = scanq.battery(bot, cids)
            v = await self.judge.ask(f"{bot}.scan", qs, state, lead)
            jinfo = {"verdict": v.kind, "model": v.model_version}
            if v.ok:
                chosen, detail = scan_pick(v.answers, cids, self.cfg["policy"]["scan"])
                jinfo.update(detail)
                logs.write("decisions", {"bot": f"{bot}.scan", "decision_id": lead, "battery_id": v.battery_id,
                                         "state": state, "answers": v.answers, "model_version": v.model_version,
                                         "backend": v.backend, "latency_ms": v.latency_ms,
                                         "tickers": dict(zip(cids, [f.ticker for f in finalists]))})
                for c, f in zip(cids, finalists):
                    if c != chosen:
                        rej.append((f.ticker, 4, "judge_no" if (v.answers.get(f"worth_{c}", {}).get("p", 0) <
                                                                self.cfg["policy"]["scan"]["worth_min"]) else "not_best"))
                        if rej[-1][2] == "judge_no":
                            self.bench.bench(f"{bot}:{f.ticker}", "judge_no")
                if chosen:
                    pick = finalists[cids.index(chosen)]
                    pick.scan_answers = v.answers
            else:
                for f in finalists:
                    rej.append((f.ticker, 4, f"judge_{v.kind}"))
        stats = {"bot": bot, "lead_id": lead, "universe": len(uni), "after_free_cut": len(s1), "examined": len(top),
                 "after_depth": len(s2), "after_facts": len(s3), "finalists": len(finalists),
                 "pick": pick.ticker if pick else None, "counts": dict(counts), "judge": jinfo,
                 "rejections": rej}
        self.last_stats[bot] = {k: v for k, v in stats.items() if k != "rejections"}
        logs.write("funnel", stats)
        return pick
