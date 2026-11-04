"""The judge: the only process that holds the Anthropic key and talks to Claude for decisions.

Bots call it with the desk secret. It validates the battery, enforces the daily budget and the deadline,
and returns answers raw. It makes no trading decision of any kind; the caller owns every threshold.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .. import ledger, logs, secrets
from ..config import get_path, load_config
from ..questions.base import Q, battery_id, normalize, validate
from ..state.snapshot import est_tokens
from .backend_claude import BackendError, ClaudeBackend
from .backend_mock import MockBackend


def _err(status: int, kind: str, msg: str = "", **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": kind, "message": msg[:300], **extra}, status_code=status)


class Judge:
    def __init__(self, cfg: dict, backend=None):
        self.cfg = cfg
        self.backend = backend
        self.injected = backend is not None
        self.backend_kind = os.environ.get("DESK_JUDGE_BACKEND", "claude")
        self.tz = get_path(cfg, "desk.timezone", "America/Los_Angeles")

    def ensure_backend(self):
        if self.injected:
            return self.backend
        if self.backend_kind == "mock":
            self.backend = self.backend or MockBackend()
            return self.backend
        secrets.collect_inbox("anthropic_api_key")
        key = os.environ.get("ANTHROPIC_API_KEY_JUDGE") or secrets.read("anthropic_api_key")
        if not key:
            self.backend = None
            return None
        if self.backend is None or getattr(self.backend, "_key", None) != key:
            j = self.cfg["judge"]
            b = ClaudeBackend(key, j["model"], int(j.get("max_tokens", 600)), j.get("pricing_per_mtok", {}),
                              j.get("mode", "batched"))
            b._key = key
            self.backend = b
        return self.backend

    def est_cost(self, qs: list[Q], state: dict) -> float:
        p = self.cfg["judge"].get("pricing_per_mtok", {})
        tin = 650 + est_tokens(state) + 60 * len(qs)
        tout = 40 * len(qs) + 30
        mult = len(qs) if self.cfg["judge"].get("mode") == "isolated" else 1
        return mult * (tin * p.get("input", 0.1) + tout * p.get("output", 0.5)) / 1e6

    def budget_left(self) -> float:
        cap = float(self.cfg["judge"].get("daily_budget_usd", 0.10))
        return round(cap - ledger.spent(tz=self.tz), 6)


def create_app(cfg: dict | None = None, backend=None) -> FastAPI:
    cfg = cfg or load_config()
    judge = Judge(cfg, backend)
    app = FastAPI(title="desk-judge", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.judge = judge

    def authed(x_desk_secret: str | None) -> bool:
        return bool(x_desk_secret) and hmac.compare_digest(x_desk_secret, secrets.desk_secret())

    @app.get("/health")
    async def health():
        b = judge.ensure_backend()
        return {"ok": True, "backend": getattr(b, "name", None), "has_key": b is not None,
                "model": cfg["judge"]["model"], "spent_today": ledger.spent(tz=judge.tz),
                "cap": cfg["judge"].get("daily_budget_usd")}

    @app.post("/v1/battery")
    async def battery(request: Request, x_desk_secret: str | None = Header(default=None)):
        if not authed(x_desk_secret):
            return _err(401, "unauthorized")
        try:
            body = await request.json()
            qs = [Q.from_dict(q) for q in body["questions"]]
            state = body["state"]
            bot = str(body.get("bot", "?"))
            deadline_ms = int(body.get("deadline_ms", cfg["judge"].get("deadline_ms", 9000)))
            decision_id = str(body.get("decision_id", ""))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            return _err(422, "malformed_question", f"bad request body: {e}")
        errs = validate(qs)
        if errs:
            logs.write("judge", {"kind": "malformed", "bot": bot, "errors": errs})
            return _err(422, "malformed_question", "; ".join(errs))
        b = judge.ensure_backend()
        if b is None:
            return _err(503, "no_key", "Anthropic API key not set")
        est = judge.est_cost(qs, state)
        if judge.budget_left() < est:
            return _err(402, "budget_exhausted", "daily AI budget reached", spent=ledger.spent(tz=judge.tz))
        t0 = time.time()
        try:
            res = await asyncio.wait_for(b.ask(qs, state), timeout=max(0.5, deadline_ms / 1000))
        except asyncio.TimeoutError:
            logs.write("judge", {"kind": "deadline", "bot": bot, "decision_id": decision_id, "deadline_ms": deadline_ms})
            return _err(504, "deadline", f"no answer within {deadline_ms} ms")
        except BackendError as e:
            logs.write("judge", {"kind": e.kind, "bot": bot, "decision_id": decision_id, "message": str(e)[:300]})
            status = {"malformed": 422, "rate_limited": 429, "overloaded": 503, "down": 502,
                      "auth": 401, "bad_output": 502, "no_credit": 402}.get(e.kind, 502)
            kind = "malformed_question" if e.kind == "malformed" else e.kind
            if e.kind == "auth":
                kind = "upstream_auth"
            return _err(status, kind, str(e))
        if res.cost_usd:
            ledger.record("judge", res.cost_usd, {"bot": bot, "model": res.model_version}, tz=judge.tz)
        answers = {q.id: normalize(q, res.raw.get(q.id)) for q in qs}
        out = {"ok": True, "decision_id": decision_id, "battery_id": battery_id(qs), "answers": answers,
               "model_version": res.model_version, "backend": getattr(b, "name", "?"),
               "latency_ms": res.latency_ms, "wall_ms": int((time.time() - t0) * 1000),
               "usage": res.usage, "cost_usd": res.cost_usd}
        logs.write("judge", {"kind": "ok", "bot": bot, "decision_id": decision_id, "model": res.model_version,
                             "latency_ms": res.latency_ms, "cost_usd": res.cost_usd})
        return out

    return app


def main():
    import uvicorn
    cfg = load_config()
    url = cfg["judge"]["url"]
    port = int(url.rsplit(":", 1)[-1].split("/")[0])
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
