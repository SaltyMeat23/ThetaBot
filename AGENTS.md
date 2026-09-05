# AGENTS.md — instructions for AI coding assistants

You are almost certainly reading this because a human asked you (Claude Code, Cursor, Copilot, an
LLM chat, etc.) to help them **set up and run ThetaBot** on their own machine or server. This file is
your runbook. Follow it exactly. The human-facing guide is `README.md`; this is the agent version,
with the safety rules that matter most when a machine is doing the setup.

---

## 🚨 Non-negotiable safety rules — read before doing ANYTHING

ThetaBot **places real options trades with real money** through the user's Robinhood account. Treat
every action accordingly.

1. **NEVER commit, print, echo, paste, or upload secrets.** That means `.env`, `config.yaml`, and
   anything under `data/` — especially `data/rh_oauth.json` (it authorizes trading on the user's
   account). They are git-ignored; keep them that way. Do not `cat` them into the chat. Do not add
   them to a commit "for convenience." If you're about to run `git add`, verify none are staged.
2. **NEVER set the bot live without the user's explicit, in-session confirmation.** Keep
   `mode: paper` and `i_understand_live_trading: false` until the user *clearly states* they
   understand it will trade real money and want it live. Going live is the user's decision, made in
   words, every time — never inferred, never a default you flip.
3. **You cannot do the Robinhood login for them.** `python -m agentic.tools.rh_login` opens a browser
   and pairs with the user's phone — it must be run **by the human, on their own desktop.** Instruct
   them to run it; never attempt to automate it, and never ask for their Robinhood password or MFA.
4. **Start in paper mode. Recommend a small account and short watchlist for the first live run.**
   Selling puts can force the user to buy shares; only names *they* choose go on the watchlist.
5. **Do not change trading logic, risk limits, deltas, or sizing during setup** unless the user asks.
   Your job in setup is to get it running as-configured, not to tune the strategy.
6. **This is not financial advice** and you should not give any. If the user asks "should I trade X"
   or "will this make money," decline the recommendation and point them to the README's disclaimer.

If you cannot follow one of these, stop and tell the user why.

---

## What ThetaBot is (one paragraph)

An autonomous options-**wheel** bot: it sells cash-secured puts on a watchlist of stocks the user is
willing to own, manages them to a profit target, and — if assigned — sells covered calls against the
shares. It runs as one Docker container. In this (community) edition, **all market data comes from
the same Robinhood connection it trades through** (`market_data: robinhood`), so the user needs only
a Robinhood login — no Alpaca or TradingView account required.

## Repo map (where things are)

```
src/agentic/            # the application (package is "agentic")
  services/scanner.py     # entry engine (screens, sizes, opens positions)
  services/monitor.py     # manages open positions (profit target, alerts)
  services/risk_breaker.py# loss circuit breaker (freezes new entries; never force-closes)
  marketdata/robinhood_md.py  # THIS EDITION'S data source (all data from Robinhood MCP)
  marketdata/alpaca_md.py     # optional alternative data source
  brokers/robinhood_mcp.py    # the Robinhood trading connection
  config.py               # all settings + their defaults
  web/dashboard.py        # the read-only web dashboard + JSON API
config.example.yaml     # copy to config.yaml — the strategy config (commented)
.env.example            # copy to .env — secrets (dashboard login, tokens)
docker-compose.yml      # bind-mounts ./data and ./config.yaml; `docker compose up -d`
tests/                  # pytest suite
```

## Setup procedure (follow in order; verify after each step)

**Prerequisites to confirm with the user first:** (a) a Robinhood account with **options trading
enabled** and **agentic access** set up; (b) somewhere to run it 24/7 (a VPS — the README walks
through Hostinger); (c) they've run the desktop login (Step 1) and have a `data/rh_oauth.json`.

1. **Secrets file.**
   ```bash
   cp .env.example .env
   ```
   Have the user fill in `DASHBOARD_PASSWORD` and `CONTROL_TOKEN` (long random strings). Optional keys
   can stay blank. Do **not** print the resulting `.env`.

2. **Robinhood token.** The user runs `python -m agentic.tools.rh_login` **on their desktop** (not the
   server), which produces `data/rh_oauth.json`. They copy that file to `./data/` on the server (e.g.
   `scp`). Verify it exists: `ls -l data/rh_oauth.json` — do not print its contents.

3. **Strategy config.**
   ```bash
   cp config.example.yaml config.yaml
   ```
   Confirm these keys with the user (leave `mode: paper` for now):
   ```yaml
   mode: paper
   market_data: robinhood      # default: one-login, no extra data keys
   entry:
     enabled: true
     watchlist: [ ... ]        # ONLY names the user is willing to own
   ```

4. **Launch.**
   ```bash
   docker compose up -d --build
   ```

5. **Verify it's healthy** (do this, report the result):
   ```bash
   curl -sf http://localhost:8000/health && echo OK
   docker compose logs --tail=50
   ```
   Expect `/health` → `{"status":"ok",...}` and logs showing the broker connected and a scan loop
   starting. The dashboard is at `http://<server-ip>:8000` (login with the `.env` credentials).

6. **Run the tests** (optional but recommended — proves the clone is coherent):
   ```bash
   pip install -e ".[all]" && python -m pytest -q
   ```

7. **Going live is a separate, explicit step.** Only when the user confirms in words: set
   `mode: live` and `i_understand_live_trading: true` in `config.yaml`, then
   `docker compose up -d`. Recommend HTTPS in front of the dashboard first (see README Step 4).

## Choosing a data source (if the user asks)

- **Default `market_data: robinhood`** — nothing extra to set up. Recommend this.
- **`alpaca`** — only if the user *wants* it and understands real-time OPRA options data is a **paid**
  Alpaca subscription. Then: add `ALPACA_API_KEY` / `ALPACA_API_SECRET` to `.env`, set
  `market_data: alpaca` and `entry.feed: opra`. See README → "Optional integrations."
- **TradingView signals** — optional ADX/%B entry gates; needs a TradingView plan with webhooks. Off
  by default and fail-open. See README → "Optional integrations."

## Verification & operations commands

| Goal | Command |
|---|---|
| Is it up/healthy? | `curl -sf http://localhost:8000/health` |
| Live status (mode, paused, broker, breaker) | `curl -s -u USER:PASS http://localhost:8000/api/ops` |
| Recent logs | `docker compose logs --tail=100 -f` |
| Restart after a config change | `docker compose up -d` |
| Update to latest | `git pull && docker compose up -d --build` |
| Stop trading immediately (keeps positions) | pause from the dashboard, or the kill switch |

## Common failure modes

| Symptom | Likely cause → fix |
|---|---|
| `/health` fails / container restarts | check `docker compose logs`; usually a `.env` or `config.yaml` typo. |
| Banner "degraded" / broker not connected | `data/rh_oauth.json` missing or expired → user re-runs desktop `rh_login`, re-uploads. |
| No trades ever fire | normal if nothing clears the screen; check the dashboard's decision log for the rejecting gate, or lower `entry.criteria.min_annualized_yield`. |
| "paused" won't trade | kill switch or loss breaker engaged → check `/api/ops` for the reason. |

## What NOT to do

- Do not weaken or remove the safety rules above.
- Do not hardcode any secret into source, config, or a commit.
- Do not `git add` `.env`, `config.yaml`, or `data/` (they're git-ignored — leave it that way).
- Do not flip `mode: live` on your own initiative.
- Do not "improve" the strategy, risk limits, or sizing during setup.

When in doubt, ask the user. This bot moves real money — being cautious is correct.
