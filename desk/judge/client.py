"""Bot-side judge client with the fallback ladder.

slow -> hold. down -> pull quotes / safe state. rate limited or overloaded -> back off and hold.
malformed question -> never retried, battery disabled. repeated errors -> halt and message the human.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from ..questions.base import Q, battery_id


@dataclass
class Verdict:
    ok: bool
    kind: str                 # ok|slow|down|rate_limited|malformed|budget|no_key|auth|halt
    action: str               # proceed|hold|safe_state|disable|halt
    answers: dict = field(default_factory=dict)
    model_version: str | None = None
    backend: str | None = None
    latency_ms: int | None = None
    cost_usd: float = 0.0
    message: str = ""
    battery_id: str = ""


class JudgeClient:
    def __init__(self, url: str, secret: str, deadline_ms: int = 9000, halt_after: int = 5):
        self.url = url.rstrip("/")
        self.secret = secret
        self.deadline_ms = deadline_ms
        self.halt_after = halt_after
        self.consecutive_errors = 0
        self.backoff_until = 0.0
        self._backoff_n = 0
        self.disabled: dict[str, str] = {}
        self._http = httpx.AsyncClient()

    async def close(self):
        await self._http.aclose()

    async def health(self) -> dict:
        try:
            r = await self._http.get(self.url + "/health", timeout=3)
            return r.json()
        except (httpx.HTTPError, ValueError):
            return {"ok": False}

    def _fail(self, kind: str, action: str, msg: str, bid: str) -> Verdict:
        if kind not in ("budget", "no_key", "malformed"):
            self.consecutive_errors += 1
        if self.consecutive_errors >= self.halt_after and kind not in ("budget", "no_key"):
            action = "halt"
        return Verdict(ok=False, kind=kind, action=action, message=msg, battery_id=bid)

    async def ask(self, bot: str, qs: list[Q], state: dict, decision_id: str,
                  deadline_ms: int | None = None) -> Verdict:
        bid = battery_id(qs)
        if bid in self.disabled:
            return Verdict(ok=False, kind="malformed", action="disable", message=self.disabled[bid], battery_id=bid)
        if time.time() < self.backoff_until:
            return Verdict(ok=False, kind="rate_limited", action="hold", message="backing off", battery_id=bid)
        dl = int(deadline_ms or self.deadline_ms)
        body = {"bot": bot, "decision_id": decision_id, "deadline_ms": dl,
                "questions": [q.to_dict() for q in qs], "state": state}
        try:
            r = await self._http.post(self.url + "/v1/battery", json=body, timeout=dl / 1000 + 1.5,
                                      headers={"X-Desk-Secret": self.secret})
        except httpx.TimeoutException:
            return self._fail("slow", "hold", "judge did not answer before the deadline", bid)
        except httpx.TransportError as e:
            return self._fail("down", "safe_state", f"judge unreachable: {e!r}", bid)
        try:
            data = r.json()
        except ValueError:
            data = {"error": "bad_json", "message": r.text[:200]}
        if r.status_code == 200 and data.get("ok"):
            self.consecutive_errors = 0
            self._backoff_n = 0
            return Verdict(ok=True, kind="ok", action="proceed", answers=data["answers"],
                           model_version=data.get("model_version"), backend=data.get("backend"),
                           latency_ms=data.get("latency_ms"), cost_usd=float(data.get("cost_usd") or 0),
                           battery_id=data.get("battery_id", bid))
        err = data.get("error", f"http_{r.status_code}")
        msg = data.get("message", "")
        if err == "malformed_question":
            self.disabled[bid] = msg
            return Verdict(ok=False, kind="malformed", action="disable", message=msg, battery_id=bid)
        if err == "deadline":
            return self._fail("slow", "hold", msg, bid)
        if err in ("rate_limited", "overloaded") or (r.status_code in (429, 503) and err != "no_key"):
            self._backoff_n += 1
            self.backoff_until = time.time() + min(300, 2 ** self._backoff_n)
            return self._fail("rate_limited", "hold", msg, bid)
        if err == "budget_exhausted":
            return self._fail("budget", "hold", msg, bid)
        if err == "no_key":
            return self._fail("no_key", "hold", msg, bid)
        if err in ("upstream_auth", "unauthorized"):
            return Verdict(ok=False, kind="auth", action="halt", message=msg or err, battery_id=bid)
        return self._fail("down", "safe_state", f"{err}: {msg}", bid)
