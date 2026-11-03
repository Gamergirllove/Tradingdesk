"""Daily summary: decisions made, trades taken, PnL after fees, every risk veto that fired."""
from __future__ import annotations

from collections import Counter

from .. import logs
from ..util import local_day


def build_text(desk) -> str:
    day = local_day(desk.tz)
    dec = logs.read("decisions", day)
    vet = logs.read("vetoes", day)
    trades = logs.read("trades", day)
    orders = logs.read("orders", day)
    funnel = logs.read("funnel", day)
    events = logs.read("events", day)
    lines = [f"Desk summary {day}"]
    by_bot = Counter(d.get("bot") for d in dec)
    lines.append("Decisions: " + (", ".join(f"{k} {v}" for k, v in sorted(by_bot.items())) or "none"))
    acts = Counter((d.get("bot"), (d.get("policy") or {}).get("action")) for d in dec if d.get("policy"))
    if acts:
        lines.append("Policy: " + ", ".join(f"{b}:{a} {n}" for (b, a), n in sorted(acts.items(), key=str)))
    verdicts = Counter(d.get("verdict") for d in dec if d.get("verdict") and d.get("verdict") != "ok")
    if verdicts:
        lines.append("Judge problems: " + ", ".join(f"{k} {v}" for k, v in verdicts.items()))
    kinds = Counter(o.get("kind") for o in orders)
    lines.append("Orders: " + (", ".join(f"{k} {v}" for k, v in kinds.items()) or "none"))
    closed = [t for t in trades if t.get("pnl_after_fees") is not None]
    pnl = sum(t["pnl_after_fees"] for t in closed)
    lines.append(f"Closed trades: {len(closed)}, PnL after fees {pnl:+.2f} USD")
    sessions = [t for t in trades if t.get("session_end")]
    if sessions:
        lines.append("House sessions: " + ", ".join(f"{t['ticker']} ({t['session_end']}, {t.get('fills', 0)} fills)"
                                                    for t in sessions[-5:]))
    for k, e in (desk.equity or {}).items():
        lines.append(f"Equity {k}: {e:.2f} (day {desk.risk.day_pnl(k, e):+.2f})")
    vc = Counter(v.get("check") for v in vet)
    lines.append("Risk vetoes: " + (", ".join(f"{k} {v}" for k, v in vc.most_common()) or "none"))
    if funnel:
        uni = sum(f.get("universe", 0) for f in funnel)
        free = sum(f.get("after_free_cut", 0) for f in funnel)
        picks = sum(1 for f in funnel if f.get("pick"))
        rate = 1 - (free / uni) if uni else 0
        lines.append(f"Funnel: {len(funnel)} runs, {picks} picks, stage-1 rejection {rate:.0%}")
        if uni and rate < 0.05:
            lines.append("WARNING: the filter rejected almost nothing today. The filter is broken, not the market.")
    halts = [e for e in events if e.get("kind") == "halt"]
    if halts:
        lines.append("Halts: " + "; ".join(f"{h.get('bot') or 'desk'}: {h.get('reason')}" for h in halts[-3:]))
    ai = desk.ai_spend()
    lines.append(f"AI spend: ${ai['spent']:.4f} of ${ai['cap']:.2f} cap")
    pin = desk.risk.state.pinned_version
    lines.append(f"Judge version pinned: {pin or 'not yet'}")
    return "\n".join(lines)
