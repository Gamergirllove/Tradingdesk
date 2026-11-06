"""Phone dashboard + seat API. Public routes go through the tunnel and need the dashboard password.
/internal routes are for the seats on this machine only: desk secret header, and never via the tunnel."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets as pysecrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import logs, secrets
from ..config import rungs, LADDER, data_dir, effective_mode
from ..venues.kalshi import KalshiClient, KalshiError, Signer

WEB = Path(__file__).resolve().parent.parent / "web"
COOKIE = "desk_session"
SESSION_DAYS = 30


def _sign(v: str) -> str:
    return hmac.new(secrets.desk_secret().encode(), v.encode(), hashlib.sha256).hexdigest()


def make_cookie() -> str:
    v = f"s:{int(time.time())}:{pysecrets.token_hex(8)}"
    return f"{v}|{_sign(v)}"


def cookie_ok(c: str | None) -> bool:
    if not c or "|" not in c:
        return False
    v, sig = c.rsplit("|", 1)
    if not hmac.compare_digest(sig, _sign(v)):
        return False
    try:
        ts = int(v.split(":")[1])
    except (IndexError, ValueError):
        return False
    return time.time() - ts < SESSION_DAYS * 86400


def setup_done() -> bool:
    return secrets.exists("dashboard_password")


def setup_code() -> str:
    p = data_dir() / "setup_code"
    if not p.exists():
        p.write_text(f"{pysecrets.randbelow(10**6):06d}\n")
    return p.read_text().strip()


def via_tunnel(req: Request) -> bool:
    h = {k.lower() for k in req.headers.keys()}
    return bool(h & {"cf-connecting-ip", "cf-ray", "x-forwarded-for", "cdn-loop"})


async def status_payload(desk) -> dict:
    bots = {}
    for name, bot in desk.bots.items():
        bots[name] = {"mode": effective_mode(desk.cfg, name), "configured_mode": desk.cfg["bots"][name]["mode"],
                      "status": bot.status, "lock": desk.book.held(name),
                      "blocked": desk.risk.blocked(name)}
        try:
            k, pnl = desk.bot_pnl_today(name)
            bots[name]["day"] = {"bot_pnl": round(pnl, 2), "loss_limit": desk.risk.loss_limit(k),
                                 "resume_add": float(desk.cfg["risk"].get("resume_loss_add_usd", 6))}
        except Exception:
            pass
    pos = {}
    for k, b in list(desk.brokers.items()):
        pos[k] = {t: p.__dict__ for t, p in (await b.positions()).items()}
    for name in bots:
        lk = bots[name]["lock"]
        if lk and lk.get("entry") is not None:
            bk = desk.md_for(lk.get("mode", "paper")).st(lk["ticker"]).book
            if bk and bk.mid is not None:
                long_yes = lk.get("side") == "bid"
                ex = bk.best_bid if long_yes else bk.best_ask
                qty = lk.get("qty", 0)
                lk = dict(lk, mark=ex, bid=bk.best_bid, ask=bk.best_ask,
                          upnl=round(((ex - lk["entry"]) if long_yes else (lk["entry"] - ex)) * qty, 4))
                bots[name]["lock"] = lk
    boards = {}
    for name in bots:
        md = desk.md_for(effective_mode(desk.cfg, name))
        b = desk.funnel_for(effective_mode(desk.cfg, name)).board.get(name)
        if b:
            rows = []
            for row in b.get("rows", []):
                st = md.st(row["ticker"])
                if st.book and st.book.mid is not None:      # live quotes, not the ones from the last scan
                    row = dict(row, bid=st.book.best_bid, ask=st.book.best_ask, quote_age_s=round(time.time() - st.book_ts))
                    if row.get("minutes_to_close") is not None and row.get("close_ts"):
                        row["minutes_to_close"] = round((row["close_ts"] - time.time()) / 60, 1)
                rows.append(row)
            boards[name] = dict(b, rows=rows)
    return {
        "ts": time.time(), "uptime_s": int(time.time() - desk.started), "bots": bots,
        "risk": desk.risk.state.__dict__, "equity": desk.equity, "positions": pos,
        "ai": desk.ai_spend(), "judge": desk.judge_health, "approvals": desk.approvals.pending(),
        "keys": {"kalshi": bool(desk.prod_signer), "kalshi_demo": bool(desk.demo_signer)},
        "funnel": desk.funnel.last_stats, "public_url": desk.public_url(), "ladder": rungs(desk.cfg),
        "mode_ceiling": desk.cfg["desk"]["mode_ceiling"],
        "judge_backend": desk.cfg["judge"].get("backend", "claude"),
        "boards": boards, "spot": desk.flow.features() if desk.flow else {},
        "paper_bankroll": desk.cfg["desk"].get("paper_bankroll", 50),
        "gambler_cfg": {k: desk.cfg["bots"]["gambler"].get(k) for k in ("size_pct_equity", "take_profit_pct", "stop_c")},
        "live_profit": desk.live_profit(),
        "milestone": desk.cfg["desk"].get("ai_credit_milestone_usd", 5),
    }


def create_app(desk) -> FastAPI:
    app = FastAPI(title="desk", docs_url=None, redoc_url=None, openapi_url=None)
    attempts: list[float] = []

    def authed(req: Request) -> bool:
        return cookie_ok(req.cookies.get(COOKIE))

    def deny():
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)

    # ---------- pages ----------
    @app.get("/", response_class=HTMLResponse)
    async def index(req: Request):
        if not setup_done():
            return RedirectResponse("/setup")
        if not authed(req):
            return HTMLResponse((WEB / "login.html").read_text())
        return HTMLResponse((WEB / "index.html").read_text())

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page():
        if setup_done():
            return RedirectResponse("/")
        setup_code()
        return HTMLResponse((WEB / "setup.html").read_text())

    @app.get("/static/base.css")
    async def css():
        from fastapi.responses import Response
        return Response((WEB / "base.css").read_text(), media_type="text/css",
                        headers={"Cache-Control": "max-age=3600"})

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse({"name": "Trading Desk", "short_name": "Desk", "display": "standalone",
                             "background_color": "#050a12", "theme_color": "#050a12", "start_url": "/"})

    @app.post("/setup")
    async def do_setup(req: Request):
        if setup_done():
            return JSONResponse({"ok": False, "error": "already set up"}, status_code=409)
        body = await req.json()
        attempts[:] = [t for t in attempts if time.time() - t < 600]
        if len(attempts) >= 10:
            return JSONResponse({"ok": False, "error": "too many attempts, wait 10 minutes"}, status_code=429)
        attempts.append(time.time())
        if str(body.get("code", "")).strip() != setup_code():
            return JSONResponse({"ok": False, "error": "wrong setup code"}, status_code=403)
        pw = str(body.get("password", ""))
        if len(pw) < 8:
            return JSONResponse({"ok": False, "error": "password must be 8+ characters"}, status_code=400)
        res = await _save_keys(desk, body)
        if not res["ok"]:
            return JSONResponse(res, status_code=400)
        secrets.write("dashboard_password", secrets.hash_password(pw))
        (data_dir() / "setup_code").unlink(missing_ok=True)
        logs.write("events", {"kind": "setup_complete", "saved": res["saved"]})
        resp = JSONResponse({"ok": True, **res})
        resp.set_cookie(COOKIE, make_cookie(), max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="strict")
        return resp

    @app.post("/login")
    async def login(req: Request):
        attempts[:] = [t for t in attempts if time.time() - t < 300]
        if len(attempts) >= 8:
            return JSONResponse({"ok": False, "error": "too many attempts, wait 5 minutes"}, status_code=429)
        body = await req.json()
        if not secrets.check_password(str(body.get("password", "")), secrets.read("dashboard_password")):
            attempts.append(time.time())
            return JSONResponse({"ok": False, "error": "wrong password"}, status_code=403)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, make_cookie(), max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="strict")
        return resp

    # ---------- dashboard api ----------
    @app.get("/api/status")
    async def api_status(req: Request):
        if not authed(req):
            return deny()
        return await status_payload(desk)

    @app.get("/api/decisions")
    async def api_decisions(req: Request, n: int = 30):
        if not authed(req):
            return deny()
        return {"decisions": logs.read("decisions", limit=n)[::-1], "vetoes": logs.read("vetoes", limit=20)[::-1],
                "trades": logs.read("trades", limit=20)[::-1], "alerts": logs.read("alerts", limit=20)[::-1]}

    @app.get("/api/reports")
    async def api_reports(req: Request):
        if not authed(req):
            return deny()
        from ..reports import calibration as calrep, daily
        rep = calrep.load_report()
        seat_dir = data_dir() / "reports"
        seat = []
        if seat_dir.exists():
            for p in sorted(seat_dir.glob("*.md"))[-8:][::-1]:
                seat.append({"name": p.stem, "text": p.read_text()[:6000]})
        return {"daily": daily.build_text(desk), "calibration": rep, "seats": seat}

    @app.post("/api/control")
    async def api_control(req: Request):
        if not authed(req):
            return deny()
        b = await req.json()
        act, bot = b.get("action"), b.get("bot")
        if bot not in desk.bots and act not in ("kill_all", "ceiling", "rebuild_calibration", "update_code", "judge"):
            return JSONResponse({"ok": False, "error": "unknown bot"}, status_code=400)
        if act == "pause":                      # reversible: immediate
            desk.risk.pause(bot, "paused from dashboard")
            return {"ok": True, "msg": f"{bot} paused"}
        if act == "unpause":
            if bot in desk.risk.state.halted_bots or desk.risk.state.halted:
                why = desk.risk.state.halted_bots.get(bot) or desk.risk.state.halt_reason
                title = f"Resume {bot} after halt"
                if str(why).startswith("daily loss limit") and bot in desk.risk.state.halted_bots:
                    add = float(desk.cfg["risk"].get("resume_loss_add_usd", 6))
                    _, pnl = desk.bot_pnl_today(bot)
                    title = f"Resume {bot} with ${add:g} more room (stops at -${max(0.0, -pnl) + add:.2f} today)"
                it = desk.approvals.request("resume", title, why,
                                            {"bot": bot if bot in desk.risk.state.halted_bots else None})
                return {"ok": True, "msg": "halted bots need APPROVE", "approval": it}
            desk.risk.resume(bot)
            return {"ok": True, "msg": f"{bot} resumed"}
        if act == "kill_all":                   # safe direction: immediate
            desk.risk.halt("kill switch from dashboard")
            for k, br in list(desk.brokers.items()):
                await br.cancel_all()
            await desk.alert("Kill switch", "All quotes pulled and desk halted. APPROVE to resume.", "high")
            return {"ok": True, "msg": "desk halted, quotes pulled"}
        if act == "mode":
            to = b.get("to")
            if to not in LADDER:
                return JSONResponse({"ok": False, "error": "bad rung"}, status_code=400)
            cur = desk.cfg["bots"][bot]["mode"]
            from ..config import rungs
            lad = rungs(desk.cfg)
            if LADDER.index(to) < LADDER.index(cur):
                from ..config import set_override
                set_override(f"bots.{bot}.mode", to)  # stepping down is always allowed
                desk.reload()
                return {"ok": True, "msg": f"{bot} stepped down to {to}"}
            nxt = next((m for m in lad if LADDER.index(m) > LADDER.index(cur)), None)
            if to not in lad or to != nxt:
                return JSONResponse({"ok": False, "error": f"one rung at a time: {cur} -> {nxt} first"},
                                    status_code=400)
            it = desk.approvals.request("mode_change", f"Move {bot} from {cur} to {to}",
                                        "Trades real money on Kalshi." if to == "live" else
                                        f"Raises real exposure. {' -> '.join(lad)}, one rung at a time.",
                                        {"bot": bot, "to": to})
            return {"ok": True, "msg": "needs APPROVE", "approval": it}
        if act == "rebuild_calibration":
            from ..reports import calibration as calrep
            rep = calrep.build_report(propose=True)
            if rep.get("proposed"):
                desk.approvals.request("apply_calibration", "Apply Platt calibration",
                                       f"Fitted on {rep.get('n_labelled')} labelled answers.", {"at": int(time.time())})
            return {"ok": True, "msg": "calibration report rebuilt", "report": rep.get("summary")}
        if act == "judge":
            to = b.get("to")
            if to not in ("rules", "claude"):
                return JSONResponse({"ok": False, "error": "judge must be rules or claude"}, status_code=400)
            if to == "rules":
                from ..config import set_override
                set_override("judge.backend", "rules")
                desk.risk.pin("")
                desk.reload()
                return {"ok": True, "msg": "rules judge on (free)"}
            it = desk.approvals.request("judge_switch", "Switch the judge to Claude",
                                        "Uses Anthropic credit (capped at the daily AI budget). Thresholds re-pin "
                                        "to the first Claude version that answers.", {"to": "claude"})
            return {"ok": True, "msg": "tap Approve to switch", "approval": it}
        if act == "update_code":
            (data_dir() / "update_requested").write_text(str(time.time()))
            return {"ok": True, "msg": "update requested; the box pulls and restarts within 2 minutes"}
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)

    @app.post("/api/force")
    async def api_force(req: Request):
        if not authed(req):
            return deny()
        b = await req.json()
        seen = b.get("seen_price")
        ok, msg = await desk.bots["gambler"].force(str(b.get("ticker", "")), str(b.get("side", "")),
                                                   float(seen) if seen is not None else None)
        logs.write("events", {"kind": "force_trade", "ticker": b.get("ticker"), "side": b.get("side"), "ok": ok, "msg": msg})
        return {"ok": ok, "msg": msg}

    @app.post("/api/position")
    async def api_position(req: Request):
        if not authed(req):
            return deny()
        b = await req.json()
        bot, act = b.get("bot", "gambler"), b.get("action")
        if bot == "house":
            if act != "stop" or not desk.book.held("house"):
                return {"ok": False, "msg": "No House session to stop"}
            desk.book.update("house", manual_stop=True)
            ok, msg = True, "Ending the House session"
        elif bot == "gambler":
            ok, msg = desk.bots["gambler"].control(str(act))
        else:
            return JSONResponse({"ok": False, "error": "unknown bot"}, status_code=400)
        logs.write("events", {"kind": "position_control", "bot": bot, "action": act, "ok": ok})
        return {"ok": ok, "msg": msg}

    @app.post("/api/approve")
    async def api_approve(req: Request):
        if not authed(req):
            return deny()
        b = await req.json()
        ok, msg = await desk.approvals.decide(str(b.get("id")), str(b.get("text", "")), bool(b.get("approve", True)))
        return {"ok": ok, "msg": msg}

    @app.post("/api/keys")
    async def api_keys(req: Request):
        if not authed(req):
            return deny()
        res = await _save_keys(desk, await req.json())
        return JSONResponse(res, status_code=200 if res["ok"] else 400)

    @app.post("/api/ask_seat")
    async def api_ask_seat(req: Request):
        if not authed(req):
            return deny()
        b = await req.json()
        seat = b.get("seat", "head")
        q = str(b.get("prompt", ""))[:600]
        rd = data_dir() / "seat_requests"
        rd.mkdir(exist_ok=True)
        rid = f"{int(time.time())}_{pysecrets.token_hex(3)}"
        (rd / f"{rid}.json").write_text(json.dumps({"seat": seat, "prompt": q}))
        return {"ok": True, "msg": f"queued for {seat}; the answer shows under Reports in about a minute"}

    # ---------- internal (seats) ----------
    def internal_ok(req: Request) -> bool:
        if via_tunnel(req):
            return False
        s = req.headers.get("x-desk-secret")
        return bool(s) and hmac.compare_digest(s, secrets.desk_secret())

    @app.get("/internal/status")
    async def i_status(req: Request):
        if not internal_ok(req):
            return deny()
        p = await status_payload(desk)
        p.pop("public_url", None)
        return p

    @app.get("/internal/logs/{stream}")
    async def i_logs(req: Request, stream: str, n: int = 50, day: str | None = None):
        if not internal_ok(req) or stream not in ("decisions", "vetoes", "trades", "funnel", "events", "orders",
                                                  "fills", "judge", "outcomes", "alerts"):
            return deny()
        rows = logs.read(stream, day=day, limit=min(n, 200))
        if stream == "funnel":
            rows = [{k: v for k, v in r.items() if k != "rejections"} | {"n_rejections": len(r.get("rejections", []))}
                    for r in rows]
        return {"rows": rows}

    @app.get("/internal/report/{name}")
    async def i_report(req: Request, name: str):
        if not internal_ok(req):
            return deny()
        from ..reports import calibration as calrep, daily
        if name == "calibration":
            return calrep.load_report() or calrep.build_report(propose=False)
        if name == "daily":
            return {"text": daily.build_text(desk)}
        return deny()

    @app.post("/internal/pause")
    async def i_pause(req: Request):
        if not internal_ok(req):
            return deny()
        b = await req.json()
        if b.get("bot") not in desk.bots:
            return JSONResponse({"ok": False, "error": "unknown bot"}, status_code=400)
        desk.risk.pause(b["bot"], f"seat {b.get('seat', '?')}: {str(b.get('reason', ''))[:200]}")
        await desk.alert(f"{b['bot']} paused by {b.get('seat', 'a seat')}", str(b.get("reason", ""))[:300])
        return {"ok": True}

    @app.post("/internal/propose")
    async def i_propose(req: Request):
        if not internal_ok(req):
            return deny()
        b = await req.json()
        try:
            it = desk.approvals.request("config_change", str(b.get("title", "Seat proposal"))[:120],
                                        str(b.get("reason", ""))[:800], {"path": b["path"], "value": b["value"]},
                                        source=f"seat:{b.get('seat', '?')}")
        except (KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        await desk.alert("Proposal waiting for APPROVE", it["title"])
        return {"ok": True, "approval": it}

    @app.post("/internal/universe")
    async def i_universe(req: Request):
        """Reversible: the desk changes its own scan universe and logs it."""
        if not internal_ok(req):
            return deny()
        b = await req.json()
        series = [s for s in b.get("series", []) if isinstance(s, str) and s.startswith("KX") and len(s) < 30][:8]
        if not series:
            return JSONResponse({"ok": False, "error": "no valid series"}, status_code=400)
        from ..config import set_override
        set_override("venue.universe_series", series)
        desk.reload()
        logs.write("events", {"kind": "universe_changed", "series": series, "by": b.get("seat"), "reason": b.get("reason")})
        return {"ok": True, "series": series}

    @app.post("/internal/notify")
    async def i_notify(req: Request):
        if not internal_ok(req):
            return deny()
        b = await req.json()
        await desk.alert(str(b.get("title", "Desk"))[:100], str(b.get("text", ""))[:3000])
        return {"ok": True}

    return app


async def _save_keys(desk, body: dict) -> dict:
    saved, problems = [], []
    ak = str(body.get("anthropic_key", "")).strip()
    if ak:
        if not ak.startswith("sk-ant-"):
            problems.append("Anthropic key should start with sk-ant-")
        else:
            secrets.drop_in_inbox("anthropic_api_key", ak)
            saved.append("anthropic")
    for prefix, label, base in (("kalshi", "Kalshi", desk.cfg["venue"]["rest_prod"]),
                                ("kalshi_demo", "Kalshi demo", desk.cfg["venue"]["rest_demo"])):
        kid = str(body.get(f"{prefix}_key_id", "")).strip()
        pem = str(body.get(f"{prefix}_pem", "")).strip()
        if not kid and not pem:
            continue
        if not kid or not pem:
            problems.append(f"{label}: need both the key ID and the private key")
            continue
        try:
            sg = Signer(kid, pem + "\n")
        except Exception:
            problems.append(f"{label}: private key text didn't parse; paste the whole file including BEGIN/END lines")
            continue
        c = KalshiClient(base, sg)
        try:
            bal = await c.balance()
            note = f"balance ${(bal.get('balance') or 0) / 100:.2f}"
        except KalshiError as e:
            problems.append(f"{label}: key rejected by Kalshi ({e.status})")
            await c.close()
            continue
        await c.close()
        secrets.write(f"{prefix}_key_id", kid)
        secrets.write(f"{prefix}_key.pem", sg.pem)
        saved.append(f"{label} ({note})")
    if problems:
        return {"ok": False, "error": "; ".join(problems), "saved": saved}
    desk.reload_keys()
    return {"ok": True, "saved": saved}
