"""Desk seats on the Claude Agent SDK: HEAD, RISK, SEARCH, EXIT, REVIEW.

Runs as the judge user (it needs the Anthropic key, never the Kalshi key). No built-in tools: no shell,
no file reads, no web. Each run has a turn cap and a dollar cap, and all spend goes on the shared ledger.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import ledger, logs, secrets
from ..config import data_dir, get_path, load_config
from .charters import CHARTERS, DEFAULT_PROMPTS, TOOLS
from .tools import build_server

BUILTINS = ["Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch",
            "WebSearch", "NotebookEdit", "Task", "Agent", "TodoWrite", "Skill", "SlashCommand", "ExitPlanMode"]


async def run_seat(seat: str, prompt: str | None = None, cfg: dict | None = None) -> dict:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query
    cfg = cfg or load_config()
    sc = cfg["seats"]
    if cfg["judge"].get("backend", "claude") != "claude":
        return {"ok": False, "error": "seats run on Claude; the judge is on rules (no AI credit)"}
    tz = get_path(cfg, "desk.timezone", "America/Los_Angeles")
    secrets.collect_inbox("anthropic_api_key")
    key = os.environ.get("ANTHROPIC_API_KEY_SEATS") or secrets.read("anthropic_api_key")
    if not key:
        return {"ok": False, "error": "no Anthropic key"}
    budget = float(sc["review_budget_usd"] if seat == "review" else sc["budget_per_run_usd"])
    left = float(cfg["judge"]["daily_budget_usd"]) - ledger.spent(tz=tz)
    if left < budget:
        logs.write("seats", {"seat": seat, "skipped": "budget", "left": left})
        return {"ok": False, "error": "daily AI budget too low for a seat run"}
    server, out = build_server(seat, TOOLS[seat])
    model = sc["review_model"] if seat == "review" else sc["model"]
    work = tempfile.mkdtemp(prefix=f"seat_{seat}_")
    opts = ClaudeAgentOptions(
        system_prompt=CHARTERS[seat], model=model, mcp_servers={"desk": server},
        tools=[], allowed_tools=[f"mcp__desk__{t}" for t in TOOLS[seat]], disallowed_tools=BUILTINS,
        permission_mode="dontAsk", max_turns=int(sc["max_turns"]), max_budget_usd=budget,
        setting_sources=[], cwd=work,
        env={"ANTHROPIC_API_KEY": key, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
             "CLAUDE_AGENT_SDK_CLIENT_APP": "trading-desk-seats"},
    )
    texts, cost, result, err = [], 0.0, None, None
    t0 = time.time()
    try:
        async for msg in query(prompt=prompt or DEFAULT_PROMPTS[seat], options=opts):
            if isinstance(msg, AssistantMessage):
                texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                cost = float(msg.total_cost_usd or 0)
                result = msg.result
    except Exception as e:      # a seat failing never touches trading
        err = repr(e)[:300]
    if cost:
        ledger.record(f"seat:{seat}", cost, {"model": model}, tz=tz)
    report = out.get("report") or result or "\n".join(texts)[-4000:]
    if not out.get("report") and report:
        d = data_dir() / "reports"
        d.mkdir(exist_ok=True)
        (d / f"{time.strftime('%Y-%m-%d_%H%M')}-{seat}.md").write_text(report[:8000])
    logs.write("seats", {"seat": seat, "cost_usd": cost, "secs": round(time.time() - t0, 1), "error": err,
                         "chars": len(report or "")})
    return {"ok": err is None, "cost_usd": cost, "report": report, "error": err}


def _due(seat: str, cfg: dict, state: dict, now: datetime) -> bool:
    sch = cfg["seats"]["schedule"]
    hm = sch.get(seat)
    if not hm:
        return False
    if seat in ("search", "review") and now.weekday() != int(sch.get(f"{seat}_weekday", 0)):
        return False
    h, m = (int(x) for x in hm.split(":"))
    if (now.hour, now.minute) < (h, m):
        return False
    return state.get(seat) != now.strftime("%Y-%m-%d")


async def loop():
    st_p = data_dir() / "seats_state.json"
    state = json.loads(st_p.read_text()) if st_p.exists() else {}
    req_dir = data_dir() / "seat_requests"
    req_dir.mkdir(exist_ok=True)
    while True:
        cfg = load_config()
        if cfg["seats"].get("enabled", True):
            now = datetime.now(ZoneInfo(get_path(cfg, "desk.timezone", "America/Los_Angeles")))
            for seat in ("risk", "exit", "head", "search", "review"):
                if _due(seat, cfg, state, now):
                    state[seat] = now.strftime("%Y-%m-%d")
                    st_p.write_text(json.dumps(state))
                    await run_seat(seat, cfg=cfg)
            for p in sorted(req_dir.glob("*.json"))[:3]:
                try:
                    req = json.loads(p.read_text())
                except ValueError:
                    req = {}
                p.unlink(missing_ok=True)
                seat = req.get("seat") if req.get("seat") in CHARTERS else "head"
                await run_seat(seat, prompt=str(req.get("prompt") or DEFAULT_PROMPTS[seat])[:600], cfg=cfg)
        await asyncio.sleep(30)


def main():
    asyncio.run(loop())


if __name__ == "__main__":
    main()
