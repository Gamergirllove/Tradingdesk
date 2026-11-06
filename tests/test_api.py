from fastapi.testclient import TestClient

from desk.engine.api import create_app
from desk.engine.main import Desk


def make():
    d = Desk()
    return d, TestClient(create_app(d), base_url="https://testserver")


def test_setup_login_and_status(tmp_data):
    d, c = make()
    assert c.get("/", follow_redirects=False).status_code in (302, 307)
    code = (tmp_data / "setup_code").read_text().strip() if (tmp_data / "setup_code").exists() else None
    c.get("/setup")
    code = (tmp_data / "setup_code").read_text().strip()
    r = c.post("/setup", json={"code": "000000" if code != "000000" else "111111", "password": "longpassword"})
    assert r.status_code == 403
    r = c.post("/setup", json={"code": code, "password": "longpassword", "anthropic_key": "sk-ant-abc"})
    assert r.status_code == 200 and r.json()["ok"]
    assert (tmp_data / "secrets" / "inbox" / "anthropic_api_key").exists()
    c2 = TestClient(create_app(d), base_url="https://testserver")
    assert c2.get("/api/status").status_code == 401
    assert c2.post("/login", json={"password": "nope"}).status_code == 403
    assert c2.post("/login", json={"password": "longpassword"}).status_code == 200
    s = c2.get("/api/status").json()
    assert set(s["bots"]) == {"gambler", "house", "sports"} and s["bots"]["gambler"]["mode"] == "shadow"


def test_mode_change_needs_literal_approve(tmp_data):
    from desk.config import set_override
    set_override("desk.skip_demo", False)          # this test walks the full ladder, demo included
    d, c = make()
    c.get("/setup")
    code = (tmp_data / "setup_code").read_text().strip()
    c.post("/setup", json={"code": code, "password": "longpassword"})
    r = c.post("/api/control", json={"action": "mode", "bot": "gambler", "to": "paper"}).json()
    aid = r["approval"]["id"]
    assert d.cfg["bots"]["gambler"]["mode"] == "shadow"
    assert not c.post("/api/approve", json={"id": aid, "text": "approve"}).json()["ok"]
    assert not c.post("/api/approve", json={"id": aid, "text": "ok APPROVE"}).json()["ok"]
    assert c.post("/api/approve", json={"id": aid, "text": "APPROVE"}).json()["ok"]
    assert d.cfg["bots"]["gambler"]["mode"] == "paper"
    # stepping down is immediate
    assert c.post("/api/control", json={"action": "mode", "bot": "gambler", "to": "shadow"}).json()["ok"]
    assert d.cfg["bots"]["gambler"]["mode"] == "shadow"
    # one rung at a time
    assert c.post("/api/control", json={"action": "mode", "bot": "house", "to": "demo"}).status_code == 400
    aid = c.post("/api/control", json={"action": "mode", "bot": "house", "to": "paper"}).json()["approval"]["id"]
    assert c.post("/api/approve", json={"id": aid, "text": "APPROVE"}).json()["ok"]
    # demo needs a demo key
    aid = c.post("/api/control", json={"action": "mode", "bot": "house", "to": "demo"}).json()["approval"]["id"]
    res = c.post("/api/approve", json={"id": aid, "text": "APPROVE"}).json()
    assert not res["ok"] and "DEMO" in res["msg"]


def test_kill_and_resume_needs_approve(tmp_data):
    d, c = make()
    c.get("/setup")
    c.post("/setup", json={"code": (tmp_data / "setup_code").read_text().strip(), "password": "longpassword"})
    assert c.post("/api/control", json={"action": "kill_all"}).json()["ok"]
    assert d.risk.state.halted
    r = c.post("/api/control", json={"action": "unpause", "bot": "gambler"}).json()
    assert "approval" in r and d.risk.state.halted
    c.post("/api/approve", json={"id": r["approval"]["id"], "text": "APPROVE"})
    assert not d.risk.state.halted


def test_internal_routes(tmp_data):
    d, c = make()
    assert c.get("/internal/status").status_code == 401
    h = {"X-Desk-Secret": "test-secret"}
    assert c.get("/internal/status", headers=h).status_code == 200
    assert c.get("/internal/status", headers={**h, "CF-Connecting-IP": "1.2.3.4"}).status_code == 401
    r = c.post("/internal/propose", headers=h, json={"path": "risk.daily_loss_limit_usd", "value": 50,
                                                     "title": "raise", "reason": "x", "seat": "risk"}).json()
    assert r["ok"] and d.cfg["risk"]["daily_loss_limit_usd"] == 5      # proposal only, nothing applied
    bad = c.post("/internal/propose", headers=h, json={"path": "judge.url", "value": "http://evil"})
    assert bad.status_code == 400
    assert c.post("/internal/pause", headers=h, json={"bot": "house", "reason": "test", "seat": "risk"}).json()["ok"]
    assert d.risk.blocked("house")


def test_judge_switch_and_milestone(tmp_data):
    import asyncio, json
    from desk import logs
    d, c = make()
    c.get("/setup")
    c.post("/setup", json={"code": (tmp_data / "setup_code").read_text().strip(), "password": "longpassword"})
    r = c.post("/api/control", json={"action": "judge", "to": "claude"}).json()
    assert d.cfg["judge"]["backend"] == "rules" and r["approval"]["kind"] == "judge_switch"
    assert c.post("/api/approve", json={"id": r["approval"]["id"], "text": "APPROVE"}).json()["ok"]
    assert d.cfg["judge"]["backend"] == "claude"
    assert c.post("/api/control", json={"action": "judge", "to": "rules"}).json()["ok"]
    assert d.cfg["judge"]["backend"] == "rules"
    logs.write("trades", {"bot": "gambler", "mode": "paper", "pnl_after_fees": 50})   # paper never counts
    logs.write("trades", {"bot": "gambler", "mode": "live", "pnl_after_fees": 3.0})
    asyncio.run(d.check_milestone())
    assert not (tmp_data / "milestones.json").exists()
    logs.write("trades", {"bot": "house", "mode": "live", "pnl_after_fees": 2.25})
    asyncio.run(d.check_milestone())
    assert json.loads((tmp_data / "milestones.json").read_text())["ai_credit"]["profit"] == 5.25
    assert any("Kalshi made" in a["title"] for a in logs.read("alerts"))
    assert c.get("/api/status").json()["live_profit"] == 5.25


def test_manual_trades_do_not_count_toward_daily_loss(tmp_data):
    import asyncio
    from desk import logs
    d, c = make()
    logs.write("trades", {"bot": "gambler", "mode": "paper", "manual": True, "pnl_after_fees": -8.0})
    logs.write("trades", {"bot": "gambler", "mode": "paper", "manual": False, "pnl_after_fees": -1.0})
    assert d.manual_pnl_today("paper_gambler") == -8.0
    d.risk.state.day_start_equity["paper_gambler"] = 50.0
    hit, pnl = d.risk.daily_loss_breached("paper_gambler", 41.0 - d.manual_pnl_today("paper_gambler"))
    assert not hit and pnl == -1.0                      # $9 down, but only $1 of it was the bot
    hit, _ = d.risk.daily_loss_breached("paper_gambler", 44.0 - 0)
    assert hit                                          # $6 lost by the bot alone trips the $5 limit


def test_resume_after_loss_limit_adds_room(tmp_data):
    d, c = make()
    c.get("/setup")
    c.post("/setup", json={"code": (tmp_data / "setup_code").read_text().strip(), "password": "longpassword"})
    k = d.broker_key("gambler", "paper")
    d.cfg["bots"]["gambler"]["mode"] = "paper"
    d.risk.state.day_start_equity[k] = 50.0
    d.equity = {k: 43.47}                                          # bot lost $6.53 today
    hit, pnl = d.risk.daily_loss_breached(k, 43.47)
    assert hit and pnl == -6.53
    d.risk.halt(f"daily loss limit on {k}: {pnl:.2f} USD", bot="gambler")
    r = c.post("/api/control", json={"action": "unpause", "bot": "gambler"}).json()
    assert "$6 more" in r["approval"]["title"] and "12.53" in r["approval"]["title"]
    res = c.post("/api/approve", json={"id": r["approval"]["id"], "text": "APPROVE"}).json()
    assert res["ok"] and "12.53" in res["msg"] and "gambler" not in d.risk.state.halted_bots
    assert d.risk.loss_limit(k) == 12.53
    assert not d.risk.daily_loss_breached(k, 40.0)[0]            # -10: still inside the new room
    assert d.risk.daily_loss_breached(k, 37.47)[0]               # -12.53: stops again
    s = c.get("/api/status").json()["bots"]["gambler"]["day"]
    assert s["loss_limit"] == 12.53 and s["resume_add"] == 6
    d.risk.state.day = "2000-01-01"
    d.risk.roll_day({k: 50.0})
    assert d.risk.loss_limit(k) == 5                              # new day: back to the base limit


def test_skip_demo_paper_to_live(tmp_data):
    d, c = make()
    c.get("/setup")
    c.post("/setup", json={"code": (tmp_data / "setup_code").read_text().strip(), "password": "longpassword"})
    d.cfg["desk"]["skip_demo"] = True
    d.cfg["bots"]["gambler"]["mode"] = "paper"
    assert c.get("/api/status").json()["ladder"] == ["shadow", "paper", "live"]
    r = c.post("/api/control", json={"action": "mode", "bot": "gambler", "to": "live"}).json()
    assert r["approval"]["payload"] == {"bot": "gambler", "to": "live"}
    assert c.post("/api/control", json={"action": "mode", "bot": "gambler", "to": "demo"}).status_code == 400
    d.cfg["desk"]["skip_demo"] = False
    assert c.post("/api/control", json={"action": "mode", "bot": "gambler", "to": "live"}).status_code == 400
