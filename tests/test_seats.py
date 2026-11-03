import asyncio
import json
import threading
import time

import uvicorn

from desk.engine.api import create_app
from desk.engine.main import Desk
from desk.seats import tools as seat_tools
from desk.seats.charters import CHARTERS, TOOLS
from desk.seats.run import BUILTINS


def test_charters_end_with_a_line_never_crossed():
    for seat, txt in CHARTERS.items():
        assert txt.strip().split("\n\n")[-1].startswith("The line you never cross"), seat
        assert "nothing passes on your say-so" in txt
    assert "propose_change" not in TOOLS["head"] and "pause_bot" not in TOOLS["search"]


def test_agent_options_have_no_builtin_tools():
    from claude_agent_sdk import ClaudeAgentOptions
    server, _ = seat_tools.build_server("risk", TOOLS["risk"])
    o = ClaudeAgentOptions(system_prompt=CHARTERS["risk"], model="claude-haiku-5-5", mcp_servers={"desk": server},
                           tools=[], allowed_tools=[f"mcp__desk__{t}" for t in TOOLS["risk"]],
                           disallowed_tools=BUILTINS, permission_mode="dontAsk", max_turns=5, max_budget_usd=0.01,
                           setting_sources=[])
    assert "Bash" in o.disallowed_tools and o.tools == [] and o.max_budget_usd == 0.01


def test_seat_tools_against_engine(tmp_data, monkeypatch):
    d = Desk()
    app = create_app(d)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=8197, log_level="error")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    monkeypatch.setattr(seat_tools, "ENGINE", "http://127.0.0.1:8197")
    _, out = seat_tools.build_server("risk", TOOLS["risk"])
    t = out["_tools"]

    async def go():
        st = await t["desk_status"].handler({})
        body = json.loads(st["content"][0]["text"])
        assert set(body["bots"]) == {"gambler", "house"} and "public_url" not in body
        r = await t["pause_bot"].handler({"bot": "house", "reason": "test"})
        assert json.loads(r["content"][0]["text"])["ok"]
        r = await t["propose_change"].handler({"path": "policy.gambler.action_conf_min", "value": "0.65",
                                               "title": "tighten", "reason": "evidence"})
        assert json.loads(r["content"][0]["text"])["approval"]["payload"]["value"] == 0.65
        await t["post_report"].handler({"text": "risk report"})
    asyncio.run(go())
    srv.should_exit = True
    th.join(timeout=5)
    assert d.risk.blocked("house") and d.cfg["policy"]["gambler"]["action_conf_min"] == 0.60
    assert any(p.read_text() == "risk report" for p in (tmp_data / "reports").glob("*.md"))
