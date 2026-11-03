# trading-desk

A 24/7 desk for Kalshi event contracts. Code computes the state, Claude Haiku 5.5 answers typed questions
through one judge service, code applies thresholds, a hard risk engine checks every order, and Claude Agent
SDK seats (HEAD, RISK, SEARCH, EXIT, REVIEW) run the routines and reports. You approve anything that raises
exposure or changes a limit by typing `APPROVE` on the phone dashboard.

```
state/ (code)  ->  judge/ (Claude, typed answers)  ->  policy/ (code)  ->  risk/ (code, final)  ->  broker/
```

| Bot | Makes money by | Battery |
| --- | --- | --- |
| Gambler | direction (one position at a time) | regime, pressure, setup score, action, worth-it, execution degraded |
| House | quoting both sides (maker fees are 0 on the default series) | range state, toxic flow, environment score, volume alive |

Rungs: `shadow` (judge + policy logged, nothing executes) → `paper` (simulated fills on the live book)
→ `demo` (real orders, Kalshi demo money) → `live`. Every bot starts in shadow. Raising a rung needs APPROVE.

Deploy: `deploy/PHONE_SETUP.md` (free Google Cloud e2-micro, set up from a phone).

Local run: `pip install -r requirements.txt`, then `python -m desk.judge.server`, `python -m desk.engine.main`,
`python -m desk.seats.run`; open http://127.0.0.1:8080. Tests: `pytest -q`.

Thresholds and limits: `config/desk.yaml`. Approved changes land in `<data>/overrides.yaml`.
Keys never live in this repo; they are pasted into the dashboard and stored on the server only.
