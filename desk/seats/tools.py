"""Seat tools: thin wrappers over the engine's /internal API. No keys, no shell, no file access."""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import secrets
from ..config import data_dir

ENGINE = os.environ.get("DESK_ENGINE_URL", "http://127.0.0.1:8080")


def _txt(obj: Any, limit: int = 12000) -> dict:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return {"content": [{"type": "text", "text": s[:limit]}]}


async def _get(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(ENGINE + path, params=params, headers={"X-Desk-Secret": secrets.desk_secret()})
        return r.json()


async def _post(path: str, body: dict) -> Any:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(ENGINE + path, json=body, headers={"X-Desk-Secret": secrets.desk_secret()})
        return r.json()


def build_server(seat: str, allowed: list[str]):
    out: dict[str, Any] = {}

    @tool("desk_status", "Current desk state: bots, modes, locks, positions, equity, AI spend, approvals, funnel.", {})
    async def desk_status(args):
        s = await _get("/internal/status")
        return _txt(s)

    @tool("daily_summary", "Today's summary: decisions, orders, PnL after fees, vetoes, funnel, AI spend.", {})
    async def daily_summary(args):
        return _txt((await _get("/internal/report/daily")).get("text", ""))

    @tool("recent_logs", "Recent log rows. stream: decisions|vetoes|trades|funnel|events|orders|fills|judge|outcomes|alerts",
          {"stream": str, "n": int})
    async def recent_logs(args):
        n = max(1, min(int(args.get("n", 20)), 60))
        return _txt(await _get(f"/internal/logs/{args['stream']}", {"n": n}))

    @tool("calibration_report", "Calibration per question key: n, Brier, base-rate Brier, reliability bins, Platt fit.", {})
    async def calibration_report(args):
        rep = await _get("/internal/report/calibration")
        slim = {k: {kk: v[kk] for kk in ("n", "brier", "brier_climatology", "base_rate", "bins") if kk in v}
                for k, v in (rep.get("keys") or {}).items()}
        return _txt({"n_labelled": rep.get("n_labelled"), "keys": slim})

    @tool("pause_bot", "Pause a bot (reversible). bot: gambler|house.", {"bot": str, "reason": str})
    async def pause_bot(args):
        return _txt(await _post("/internal/pause", {"bot": args["bot"], "reason": args.get("reason", ""), "seat": seat}))

    @tool("propose_change", "Propose a config change for the human to APPROVE. path like policy.gambler.action_conf_min; "
                            "value as JSON (number, string, list).", {"path": str, "value": str, "title": str, "reason": str})
    async def propose_change(args):
        try:
            val = json.loads(args["value"])
        except (ValueError, TypeError):
            val = args["value"]
        return _txt(await _post("/internal/propose", {"path": args["path"], "value": val, "title": args.get("title", ""),
                                                      "reason": args.get("reason", ""), "seat": seat}))

    @tool("set_universe", "Replace the scan universe with these Kalshi series tickers (max 4). Reversible.",
          {"series": list, "reason": str})
    async def set_universe(args):
        return _txt(await _post("/internal/universe", {"series": args["series"][:4], "reason": args.get("reason", ""),
                                                       "seat": seat}))

    @tool("series_volume", "Open market count and total 24h contract volume for Kalshi series tickers (public data).",
          {"series": list})
    async def series_volume(args):
        res = {}
        async with httpx.AsyncClient(timeout=15) as c:
            for s in list(args["series"])[:8]:
                try:
                    r = await c.get("https://external-api.kalshi.com/trade-api/v2/markets",
                                    params={"series_ticker": s, "status": "open", "limit": 500})
                    ms = r.json().get("markets") or []
                    res[s] = {"open_markets": len(ms),
                              "vol_24h": round(sum(float(m.get("volume_24h_fp") or 0) for m in ms))}
                except (httpx.HTTPError, ValueError):
                    res[s] = "unavailable"
        return _txt(res)

    @tool("notify_human", "Push a short message to the human's phone.", {"title": str, "text": str})
    async def notify_human(args):
        return _txt(await _post("/internal/notify", {"title": args["title"], "text": args["text"]}))

    @tool("post_report", "Save your report (plain text, phone-sized). Call once at the end.", {"text": str})
    async def post_report(args):
        d = data_dir() / "reports"
        d.mkdir(exist_ok=True)
        p = d / f"{time.strftime('%Y-%m-%d_%H%M')}-{seat}.md"
        p.write_text(str(args["text"])[:8000])
        out["report"] = str(args["text"])
        return _txt("saved")

    every = {f.name: f for f in (desk_status, daily_summary, recent_logs, calibration_report, pause_bot,
                                 propose_change, set_universe, series_volume, notify_human, post_report)}
    tools = [every[n] for n in allowed if n in every]
    out["_tools"] = {t.name: t for t in tools}
    return create_sdk_mcp_server(name="desk", version="1.0.0", tools=tools), out
