import asyncio
import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from desk import ledger
from desk.config import load_config
from desk.judge import backend_claude
from desk.judge.backend_claude import BackendError, ClaudeBackend
from desk.judge.backend_mock import MockBackend
from desk.judge.client import JudgeClient
from desk.judge.server import create_app
from desk.questions import gambler as qg
from desk.questions.base import noul

STATE = {"mid": 0.4, "spread_c": 1, "ret_15m_c": 3, "ret_5m_c": 1, "model_edge_c": 5, "imbalance3": 0.2,
         "spread_avg_10m_c": 1, "rv_30m_c": 0.8, "minutes_to_close": 60}


def body(qs=qg.BATTERY):
    return {"bot": "gambler", "decision_id": "d1", "deadline_ms": 3000, "questions": [q.to_dict() for q in qs],
            "state": STATE}


def test_server_ok_and_auth():
    app = create_app(load_config(), backend=MockBackend())
    c = TestClient(app)
    assert c.post("/v1/battery", json=body()).status_code == 401
    r = c.post("/v1/battery", json=body(), headers={"X-Desk-Secret": "test-secret"})
    j = r.json()
    assert r.status_code == 200 and j["ok"] and set(j["answers"]) == {q.id for q in qg.BATTERY}
    assert j["model_version"] == "mock-judge-1"


def test_server_malformed_never_calls_model():
    mb = MockBackend()
    c = TestClient(create_app(load_config(), backend=mb))
    bad = [noul("x", "a", []), noul("x", "b", [])]
    r = c.post("/v1/battery", json=body(bad), headers={"X-Desk-Secret": "test-secret"})
    assert r.status_code == 422 and r.json()["error"] == "malformed_question" and mb.calls == 0


def test_server_deadline_and_budget():
    c = TestClient(create_app(load_config(), backend=MockBackend(delay_s=2)))
    b = body()
    b["deadline_ms"] = 500
    r = c.post("/v1/battery", json=b, headers={"X-Desk-Secret": "test-secret"})
    assert r.status_code == 504 and r.json()["error"] == "deadline"
    ledger.record("judge", 1.0)
    c2 = TestClient(create_app(load_config(), backend=MockBackend()))
    r = c2.post("/v1/battery", json=body(), headers={"X-Desk-Secret": "test-secret"})
    assert r.status_code == 402 and r.json()["error"] == "budget_exhausted"


@pytest.mark.parametrize("kind,status,err", [("rate_limited", 429, "rate_limited"), ("down", 502, "down"),
                                             ("malformed", 422, "malformed_question"), ("auth", 401, "upstream_auth")])
def test_server_maps_backend_errors(kind, status, err):
    c = TestClient(create_app(load_config(), backend=MockBackend(fail=kind)))
    r = c.post("/v1/battery", json=body(), headers={"X-Desk-Secret": "test-secret"})
    assert r.status_code == status and r.json()["error"] == err


def _client_with(handler):
    jc = JudgeClient("http://judge", "test-secret", deadline_ms=1000, halt_after=3)
    jc._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return jc


def test_client_ladder():
    calls = {"n": 0}

    def malformed(req):
        calls["n"] += 1
        return httpx.Response(422, json={"ok": False, "error": "malformed_question", "message": "dup"})
    jc = _client_with(malformed)
    v = asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d"))
    assert v.kind == "malformed" and v.action == "disable"
    v = asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d"))
    assert v.action == "disable" and calls["n"] == 1          # never retried

    def down(req):
        raise httpx.ConnectError("refused")
    jc = _client_with(down)
    acts = [asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d")).action for _ in range(3)]
    assert acts == ["safe_state", "safe_state", "halt"]

    def limited(req):
        return httpx.Response(429, json={"ok": False, "error": "rate_limited"})
    jc = _client_with(limited)
    assert asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d")).kind == "rate_limited"
    v = asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d"))
    assert v.kind == "rate_limited" and v.message == "backing off"

    def budget(req):
        return httpx.Response(402, json={"ok": False, "error": "budget_exhausted"})
    jc = _client_with(budget)
    acts = [asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d")).action for _ in range(5)]
    assert set(acts) == {"hold"}                                # budget never escalates to a halt


def test_claude_backend_request_shape(monkeypatch):
    """The exact request the judge sends to Claude (no network)."""
    seen = {}

    class FakeMessages:
        async def create(self, **kw):
            seen.update(kw)
            data = {q.id: ({"p_true": 0.6} if q.kind == "noul" else
                           {o: 1 for o in q.options} if q.kind == "choice" else {f"L{k}": 1 for k in q.levels})
                    for q in qg.BATTERY}
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text=json.dumps(data))], model="claude-haiku-5-5-test",
                usage=types.SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                            cache_read_input_tokens=0))

    b = ClaudeBackend("sk-ant-test", "claude-haiku-5-5", 600, load_config()["judge"]["pricing_per_mtok"])
    b.client = types.SimpleNamespace(messages=FakeMessages())
    res = asyncio.run(b.ask(qg.BATTERY, STATE))
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["output_config"]["format"]["type"] == "json_schema"
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "STATE" in seen["messages"][0]["content"]
    assert res.model_version == "claude-haiku-5-5-test"
    assert abs(res.cost_usd - (1000 * 0.10 + 200 * 0.50) / 1e6) < 1e-12


def test_no_credit_holds_and_never_disables():
    c = TestClient(create_app(load_config(), backend=MockBackend(fail="no_credit")))
    r = c.post("/v1/battery", json=body(), headers={"X-Desk-Secret": "test-secret"})
    assert r.status_code == 402 and r.json()["error"] == "no_credit"

    def nc(req):
        return httpx.Response(402, json={"ok": False, "error": "no_credit"})
    jc = _client_with(nc)
    acts = [asyncio.run(jc.ask("gambler", qg.BATTERY, STATE, "d")) for _ in range(6)]
    assert {v.action for v in acts} == {"hold"} and {v.kind for v in acts} == {"no_credit"}
