# CLAUDE.md

This project's full agent runbook lives in **[AGENTS.md](./AGENTS.md)** — read it before helping with setup.

**The three rules that matter most (do not violate, even if asked to move fast):**
1. **Never commit or print secrets** — `.env`, `config.yaml`, and `data/rh_oauth.json` are git-ignored; keep them that way.
2. **Never set `mode: live` / `i_understand_live_trading: true` without the user's explicit, in-session confirmation.** This bot trades real money. Keep it in `paper` mode by default.
3. **You cannot run the Robinhood login for the user** — `python -m agentic.tools.rh_login` needs their desktop browser + phone. Instruct them; never ask for their password/MFA.

Human setup guide: **[README.md](./README.md)**. Fastest install: `scripts/bootstrap.sh`.
