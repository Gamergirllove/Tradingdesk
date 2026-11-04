"""Shared seat preamble and helpers for bots."""
from __future__ import annotations

from .. import logs

SEAT_RULE = ("You don't form opinions about trades. You build a state, call the judge once and act on the numbers. "
             "If the judge is unreachable, nothing passes on your say-so.")


async def handle_verdict_failure(desk, bot: str, v) -> None:
    """Fallback ladder side effects that are the same for every bot."""
    if v.action == "disable":
        desk.risk.halt(f"malformed battery {v.battery_id}: {v.message[:120]}", bot=bot)
        await desk.alert(f"{bot} halted", f"Judge rejected the question battery as malformed. It will not be "
                                          f"retried. {v.message[:200]}", priority="high")
    elif v.kind == "no_credit":
        await desk.alert("Desk is waiting for Claude credit",
                         "Your Anthropic account has no credit, so the bots are holding. Add $5 at "
                         "platform.claude.com/settings/billing; the desk resumes by itself.", dedupe_s=6 * 3600)
    elif v.action == "halt":
        desk.risk.halt(f"judge errors: {v.kind} {v.message[:120]}", bot=bot)
        await desk.alert(f"{bot} halted", f"Repeated judge errors ({v.kind}). Resume needs APPROVE.",
                         priority="high")
    logs.write("events", {"kind": "judge_fail", "bot": bot, "verdict": v.kind, "action": v.action,
                          "message": v.message[:200]})


def maybe_pin(desk, v) -> None:
    if v.ok and v.backend == "claude" and v.model_version and not desk.risk.state.pinned_version:
        desk.risk.pin(v.model_version)
