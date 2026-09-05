#!/usr/bin/env bash
# ThetaBot — one-command bootstrap for a fresh Ubuntu VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/SaltyMeat23/ThetaBot/main/scripts/bootstrap.sh | bash
#
# Installs Docker (if missing), clones the repo, and scaffolds your config files.
# It does NOT start trading, does NOT need any secrets, and NEVER overwrites an existing
# .env or config.yaml. Everything after this is a few edits + `docker compose up`.
set -euo pipefail

REPO="${THETABOT_REPO:-https://github.com/SaltyMeat23/ThetaBot.git}"
DIR="${THETABOT_DIR:-$HOME/ThetaBot}"

echo "=================================================="
echo "  ThetaBot bootstrap"
echo "=================================================="

# 1) Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker..."
  curl -fsSL https://get.docker.com | sh
else
  echo "==> Docker already installed."
fi

# 2) Clone (or update) the repo
if [ -d "$DIR/.git" ]; then
  echo "==> Updating existing install at $DIR"
  git -C "$DIR" pull --ff-only || true
else
  echo "==> Cloning ThetaBot to $DIR"
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

# 3) Scaffold config (never clobber existing files)
mkdir -p data
[ -f .env ]         || cp .env.example .env
[ -f config.yaml ]  || cp config.example.yaml config.yaml

echo ""
echo "=================================================="
echo "  ✅ Installed at: $DIR   (nothing is running yet)"
echo "=================================================="
echo ""
echo "NEXT STEPS (you do these — it stays in PAPER mode until you choose otherwise):"
echo ""
echo "  1) Set your dashboard password + control token:"
echo "         nano $DIR/.env"
echo ""
echo "  2) Set your watchlist (names you'd be happy to OWN):"
echo "         nano $DIR/config.yaml       # leave 'mode: paper' for now"
echo ""
echo "  3) Upload your Robinhood token to:  $DIR/data/rh_oauth.json"
echo "     (create it on your DESKTOP:  python -m agentic.tools.rh_login )"
echo ""
echo "  4) Start it:"
echo "         cd $DIR && docker compose up -d --build"
echo ""
echo "  5) Open the dashboard:  http://<this-server-ip>:8000"
echo ""
echo "Going live is a separate, deliberate step (set mode: live in config.yaml). See README.md."
