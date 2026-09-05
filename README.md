# ThetaBot

**An autonomous options-*wheel* bot that runs entirely off your own Robinhood account — one login, one small server, no paid data feeds.**

It sells cash-secured puts on stocks you'd be happy to own, manages them to a profit target, and — if assigned — sells covered calls against the shares. The same disciplined rules every day, with no emotion. This is the **community edition**: all market data comes from the *same* Robinhood connection it trades through, so you don't need an Alpaca subscription, a TradingView Pro plan, or anything else.

---

> ## ⚠️ Read this before anything else
>
> - **This bot places real trades with real money.** There is no "demo money" once it's armed live.
> - **Selling puts means you can be assigned** — i.e. *forced to buy 100 shares per contract* at the strike. Only ever put names on your watchlist that you are genuinely willing to own, in sizes you can afford.
> - **Options carry real, sometimes total, loss risk**, including sharp overnight/gap losses. A wheel is *not* a guaranteed-income machine.
> - **This is not financial advice**, not a solicitation, and comes with **no warranty of any kind**. You run it entirely at your own risk.
> - **Not affiliated with, endorsed by, or supported by Robinhood.** It uses Robinhood's agentic connection; you are responsible for complying with Robinhood's Terms of Service. Robinhood can change or revoke that access at any time.
> - **Start in paper mode. Start tiny. Watch it for weeks before you trust it.**
>
> If you are not comfortable reading logs, using SSH, and losing every dollar in the account, **do not run this.**

---

## Table of contents
1. [What it does](#what-it-does)
2. [How it works](#how-it-works)
3. [What you need](#what-you-need)
4. [Setup overview](#setup-overview)
5. [Step 1 — Robinhood account + one-time login](#step-1--robinhood-account--one-time-login)
6. [Step 2 — Spin up a Hostinger VPS](#step-2--spin-up-a-hostinger-vps)
7. [Step 3 — Install & deploy](#step-3--install--deploy)
8. [Step 4 — First run, paper → live](#step-4--first-run-paper--live)
9. [Configuration reference](#configuration-reference)
10. [Optional integrations (Alpaca / TradingView)](#optional-integrations-for-different-setups)
11. [Operating the bot](#operating-the-bot)
11. [Safety & risk controls](#safety--risk-controls)
12. [Troubleshooting](#troubleshooting)
13. [Disclaimer & license](#disclaimer--license)

---

## What it does

The **options wheel**, automated end to end:

1. **Sell a cash-secured put** on a watchlist name — collect a premium for agreeing to buy the stock at a lower ("strike") price.
2. **If it expires worthless** (stock stayed above the strike) → keep the premium, repeat.
3. **If you're assigned** (stock fell below the strike) → you now own 100 shares per contract at that strike.
4. **Sell covered calls** against those shares — collect more premium — until the shares are called away at/above your cost basis. Then back to step 1.

Every scan, the bot screens each watchlist name's live option chain, filters for the right delta / days-to-expiry / liquidity / yield, ranks by how *rich* the premium is (IV rank), sizes the position so no single bet dominates, and places the order. It then monitors open positions and buys them back at a profit target, rolls when it makes sense, or lets them ride to assignment.

It is deliberately a **slow, patient** strategy: 7–14 day expirations, held for days, only on names you'd own.

## How it works

| Component | Job |
|---|---|
| **Scanner** | Every few minutes during market hours: screen the watchlist, rank candidates, size, and open new CSPs / CCs. |
| **Monitor** | Continuously price open positions; take profit, alert on short-DTE, track excursions. |
| **Executor** | Places and confirms orders through Robinhood; never leaves an order in an unknown state. |
| **Reconcile** | Keeps the bot's ledger in sync with what Robinhood actually reports (assignments, expiries, fills). |
| **Risk** | Position/sizing caps, entry gates, a **loss circuit breaker**, and an instant kill switch. |
| **Dashboard** | A read-only web page (password-protected) showing health, positions, P&L, and the "why" behind every decision. |

All of it runs as **one small Docker container** that keeps its state (a local SQLite database + your login token) on a persistent volume. Market data — option chains, quotes, greeks, IV, open interest, and daily price bars — is pulled from **Robinhood's own connection** (`market_data.provider: robinhood`), so there are **no separate data subscriptions**.

## What you need

- **A Robinhood account** with **options trading enabled** and **agentic access** set up (a primary account + the agentic sub-account — see Step 1). Level 2+ options approval is required to sell cash-secured puts.
- **A VPS** to run it 24/7. This guide uses **Hostinger** (Step 2). The bot is lightweight — the smallest KVM plan is plenty.
- **A desktop computer** for the one-time Robinhood login (the login flow needs a browser + your phone).
- Basic comfort with **SSH and the command line**.
- **~30–45 minutes** for first-time setup.

You do **not** need: an Alpaca account, a TradingView subscription, a paid market-data feed, or any cloud provider beyond the VPS.

## Setup overview

```
Desktop:   run the one-time Robinhood login  →  produces data/rh_oauth.json
Hostinger: create a VPS (Ubuntu) → install Docker
VPS:       clone this repo → add your secrets + config → upload rh_oauth.json → docker compose up
Browser:   open the dashboard → verify health → (when ready) arm live
```

---

## Step 1 — Robinhood account + one-time login

1. **Have a Robinhood account with options enabled.** You need approval to sell cash-secured puts (typically options Level 2). Set this up in the Robinhood app first.
2. **Enable agentic access.** Robinhood's automated ("agentic") trading uses a dedicated connection. Follow Robinhood's in-app flow to enable it and create the agentic sub-account you want the bot to trade. **Only that account trades** — the bot cannot place orders on your other accounts.
3. **Run the one-time login on your desktop** (not the VPS — the flow opens a browser and pairs with your phone):

   ```bash
   # on your desktop, with this repo cloned and Python 3.13 + deps installed:
   pip install -e ".[all]"
   python -m agentic.tools.rh_login
   ```

   This walks through Robinhood's OAuth flow and writes **`data/rh_oauth.json`** (your access + refresh tokens). You'll upload that file to the VPS in Step 3. The bot **auto-refreshes** the token from then on — you won't have to log in again unless the refresh chain is broken.

> **Guard `rh_oauth.json` like a password.** It authorizes trading on your account. Never commit it, never share it, never paste it anywhere. It is git-ignored by default.

---

## Step 2 — Spin up a Hostinger VPS

> 💡 **Get a Hostinger VPS here → https://www.hostinger.com?REFERRALCODE=LRBKTHIELNOA** — using this link supports the project at no extra cost to you.

1. Go to Hostinger → **VPS Hosting** and choose a plan. **KVM 1** (1 vCPU / 4 GB RAM / ~50 GB) is more than enough — the bot uses a tiny fraction of it. KVM 2 gives comfortable headroom if you want it.
2. **Operating system:** choose **Ubuntu 24.04** (or 22.04). If Hostinger offers an **"Ubuntu with Docker"** template, pick it — Docker comes pre-installed and you can skip part of Step 3.
3. Set a strong **root password** (or, better, add your **SSH key**) when prompted.
4. Choose a **datacenter** near you (or near US markets — latency isn't critical for this slow strategy).
5. Once it provisions, note the VPS's **public IP address** from the Hostinger control panel (hPanel → VPS → your server).
6. **SSH in** from your desktop:

   ```bash
   ssh root@YOUR_VPS_IP
   ```

## Step 3 — Install & deploy

On the VPS:

1. **Install Docker** (skip if your template already has it):

   ```bash
   curl -fsSL https://get.docker.com | sh
   docker --version   # confirm it's installed
   ```

2. **Clone this repo** and enter it:

   ```bash
   git clone https://github.com/SaltyMeat23/ThetaBot.git
   cd ThetaBot
   ```

3. **Create your secrets file** from the template:

   ```bash
   cp .env.example .env
   nano .env
   ```

   Fill in — at minimum:

   ```ini
   # Dashboard login (pick your own — you'll use these to open the web UI)
   DASHBOARD_USER=admin
   DASHBOARD_PASSWORD=change-this-to-something-strong

   # A random token that protects the control endpoints (pause/resume/etc.)
   CONTROL_TOKEN=another-long-random-string

   # (Optional) desktop push alerts via Pushover — leave blank to skip
   PUSHOVER_TOKEN=
   PUSHOVER_USER=

   # (Optional) AI trade-review commentary via Claude — leave blank to skip
   ANTHROPIC_API_KEY=
   ```

   You do **not** set any Alpaca or TradingView keys in this edition.

4. **Upload your Robinhood token.** The easy path is a bind-mounted `data/` folder. Create it and copy the file you made in Step 1:

   ```bash
   mkdir -p data
   # from your DESKTOP, in a separate terminal:
   scp data/rh_oauth.json root@YOUR_VPS_IP:/root/ThetaBot/data/rh_oauth.json
   ```

   Then make sure `docker-compose.yml` mounts your host `data/` folder (this edition ships that way):

   ```yaml
   volumes:
     - ./data:/app/data
   ```

5. **Set your strategy config:**

   ```bash
   cp config.example.yaml config.yaml
   nano config.yaml
   ```

   The most important line to check first:

   ```yaml
   mode: paper          # START HERE. Change to "live" only when you're ready.
   entry:
     enabled: true
     watchlist: [F, SOFI, ...]     # names you are happy to own
   market_data: robinhood          # default: all data from your Robinhood login (no Alpaca)
   ```

   (Full options in the [Configuration reference](#configuration-reference).)

6. **Launch:**

   ```bash
   docker compose up -d --build
   docker compose logs -f          # watch it boot; Ctrl-C to stop watching
   ```

## Step 4 — First run, paper → live

1. **Open the dashboard:** `http://YOUR_VPS_IP:8000` — log in with the `DASHBOARD_USER` / `DASHBOARD_PASSWORD` you set.
2. **Confirm health:** the top banner should show the broker connected and *not degraded*. Check `http://YOUR_VPS_IP:8000/health` returns `ok`.
3. **Let it run in paper mode for a while.** Watch it screen, "trade," and manage positions with no real money. Read the decision log — make sure its choices make sense to *you*.
4. **Add HTTPS (recommended before going live).** Exposing a password over plain `http://` is risky. The simplest option is to put **Caddy** in front for automatic HTTPS with a domain you point at the VPS — see the wiki/`docs/`. At minimum, restrict port 8000 with a firewall to your own IP.
5. **Go live only when you're ready:** set `mode: live` in `config.yaml`, start with a **small** account and a **short** watchlist, then `docker compose up -d` to apply. The dashboard will show `live_armed: true`.

---

## Configuration reference

`config.yaml` (hot-reloadable via the dashboard). Key sections:

```yaml
mode: paper | live

market_data: robinhood       # robinhood | alpaca | paper  (see "Optional integrations")

entry:
  enabled: true
  watchlist: [F, SOFI, T, ...]   # ONLY names you'd own
  feed: opra                     # ignored for the robinhood provider
  prefer_iv_rank: true           # sell where premium is richest vs the name's own history
  earnings_gate: true            # never hold a short put through earnings
  criteria:                      # the CSP screen
    delta_min: 0.18
    delta_max: 0.28              # ~how likely you are to be assigned
    dte_min: 7
    dte_max: 14
    min_annualized_yield: 0.52   # ~1%/week floor on collateral; lower it for more (thinner) trades
    min_open_interest: 100
    min_volume: 10
    max_spread_pct: 0.10
    max_pct_below_sma200: 0.20   # skip broken downtrends
    require_strike_below_support: true
  cc_criteria:                   # the covered-call screen (post-assignment)
    delta_min: 0.20
    delta_max: 0.30
    min_annualized_yield: 0.20
  sizing:
    target_positions: 20         # spread capital across ~N names (scale-invariant)
    max_position_size_pct: 0.50  # per-name backstop cap
    total_bp_utilization_target: 0.80
    buying_power_reserve_pct: 0.15
    max_pct_of_oi: 0.10          # never take >10% of a strike's open interest

risk:                            # loss circuit breaker (freezes NEW entries; never force-closes)
  loss_breaker_enabled: true
  lookback_days: 7
  max_realized_loss_pct: 0.10    # halt new entries if realized losses over the window exceed 10% of account
  max_consecutive_losses: 4

rules:                           # position management
  - name: profit-trail
    params: { profit_pct: 0.8, trailing: true, trail_gap: 0.2 }   # take profit ~80% of max
```

Tune the yield floor and delta band to your own risk tolerance. Higher `min_annualized_yield` = fewer, richer, higher-IV trades; lower = more, thinner ones.

## Optional integrations (for different setups)

**By default, ThetaBot needs nothing but your Robinhood login** — market data and trading both ride the same connection. But if you already pay for better data or extra signals, you can plug them in. Neither is required.

### Option A — Alpaca for market data

Robinhood's data is complete and works well for this slow strategy, but you can use **Alpaca** instead (e.g. you already have it, or want a separate data source):

1. Create an account at **[alpaca.markets](https://alpaca.markets)** and generate API keys.
2. **Real-time options data (OPRA) requires Alpaca's *paid* market-data subscription.** The free tier is delayed ("indicative") and is **not safe for live entry** — only use it for paper testing. Subscribe to their options/Algo data plan for live trading.
3. Add the keys to `.env`:
   ```ini
   ALPACA_API_KEY=your_key
   ALPACA_API_SECRET=your_secret
   ```
4. Switch the provider (and feed) in `config.yaml`:
   ```yaml
   market_data: alpaca
   entry:
     feed: opra        # real-time (paid). Use "indicative" only for paper/testing.
   ```
5. `docker compose up -d` to apply. Everything else is identical.

### Option B — TradingView for extra trend signals

ThetaBot can *optionally* gate entries on **ADX** (trend strength) and **Bollinger %B** (position in the band), fed from TradingView alerts. This is **pure enrichment — the bot runs fine without it**, and these gates are **off by default**.

Requires a **TradingView plan that supports webhook alerts** (Pro+ or higher).

1. Choose a webhook token and add it to `.env`:
   ```ini
   TRADINGVIEW_WEBHOOK_TOKEN=some_long_random_string
   ```
2. In TradingView, create alerts on your watchlist symbols with the **Webhook URL**:
   ```
   https://YOUR_HOST/webhook/tradingview?token=some_long_random_string
   ```
   and an alert message that posts the indicator values as JSON (the symbol plus its `adx` and `bb_percent_b`). The webhook handler at `POST /webhook/tradingview` ingests those into each name's context.
3. Turn the gates on in `config.yaml`:
   ```yaml
   entry:
     criteria:
       min_adx: 20            # skip weak / choppy trends
       min_bb_percent_b: 20   # skip price pinned to the lower band
   ```

If no fresh alert has arrived for a symbol, these gates simply **don't apply** (fail-open) — they never block a trade for lack of data. To turn them back off, remove the two lines (or set them to `null`).

## Operating the bot

- **Dashboard** (`/`): health, open positions, realized/unrealized P&L, win rate, and the reason behind each close.
- **Pause / resume:** the kill switch halts *all* new orders instantly; use it any time you want to stop trading without touching positions.
- **Loss circuit breaker:** trips automatically on a losing streak (see below) and shows in `/api/ops` — it freezes *new* entries but keeps managing what's open.
- **Logs:** `docker compose logs -f` on the VPS.
- **Updating:** `git pull && docker compose up -d --build`.
- **Backups:** your whole state is `data/` (the SQLite DB + `rh_oauth.json`). Back that folder up.

## Safety & risk controls

The bot is built to *survive*, not to gamble:

- **Only sells names you list** — you curate the universe of what it can be assigned.
- **Position + concentration caps** so no single trade dominates.
- **Entry gates** — skips earnings, broken downtrends, illiquid contracts.
- **Loss circuit breaker** — freezes new entries after a bad realized run (default: −10% of account in 7 days, or 4 straight losers). It **never force-liquidates** — it stops digging, it doesn't panic-sell.
- **Instant kill switch** — halts on broker errors or on your command.
- **No hidden leverage** — cash-secured puts are fully collateralized.

None of this removes market risk. A sharp gap down on a held name is still a real loss.

## Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard won't load | `docker compose ps` (is it running?), firewall allows port 8000, correct IP. |
| Banner shows "degraded"/broker down | `rh_oauth.json` present in `data/`? Token still valid? Re-run `rh_login` on desktop and re-upload. |
| No trades firing | Normal if nothing clears your criteria. Lower `min_annualized_yield` or check the decision log for the gate that's rejecting. |
| "paused" and won't trade | Kill switch or loss breaker engaged — check `/api/ops` for the reason; resume from the dashboard. |
| Token stopped refreshing | Re-run the desktop `rh_login` and re-upload `rh_oauth.json`. |

## Disclaimer & license

This software is provided **"as is", without warranty of any kind**, express or implied. The authors are **not liable** for any losses, damages, or account actions arising from its use. It is **not financial, investment, tax, or legal advice**, and nothing here is a recommendation to buy or sell any security.

**Trading options involves substantial risk of loss.** Cash-secured puts can result in forced purchase of shares; covered calls can cap gains and result in shares being sold. Only trade with money you can afford to lose.

This project is **not affiliated with, endorsed by, or sponsored by Robinhood Markets, Inc.** "Robinhood" is a trademark of its owner. You are solely responsible for complying with Robinhood's Terms of Service and all applicable laws and regulations in your jurisdiction.

By running this software you accept full responsibility for its behavior and any resulting trades.

_License: MIT (see `LICENSE`)._
