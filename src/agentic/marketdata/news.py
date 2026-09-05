"""News/catalyst provider — READ-ONLY, advisory.

Pulls recent headlines per symbol from Alpaca's Benzinga-sourced news API
(``https://data.alpaca.markets/v1beta1/news``), reusing the same ALPACA_API_KEY / ALPACA_API_SECRET
the market-data provider already uses (no new vendor/cost). Fail-open everywhere: no keys / any
error -> returns []. Never places orders and is NEVER a trade-picker — it only supplies context.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from ..config import Settings, get_secret

log = logging.getLogger("agentic.news")

_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"


class NewsProvider(Protocol):
    async def fetch(self, symbols: list[str], *, limit: int = 50) -> list[dict[str, Any]]: ...


class NullNewsProvider:
    """No news source — the channel is inert (fail-open)."""
    async def fetch(self, symbols: list[str], *, limit: int = 50) -> list[dict[str, Any]]:
        return []


def parse_alpaca_news(articles: list[dict]) -> list[dict[str, Any]]:
    """Flatten Alpaca articles into per-(symbol, article) news_items rows (one row per tagged symbol)."""
    out: list[dict[str, Any]] = []
    for a in articles:
        headline = a.get("headline")
        if not headline:
            continue
        aid = a.get("id")
        source = a.get("source") or "alpaca"
        url = a.get("url")
        created = a.get("created_at") or a.get("updated_at")
        for sym in (a.get("symbols") or []):
            s = str(sym).upper()
            out.append({
                "symbol": s, "headline": headline, "source": source, "url": url,
                "created_at": created, "dedup_key": f"{source}:{aid}:{s}",
            })
    return out


class AlpacaNewsProvider:
    """Fetches recent Benzinga headlines for a set of symbols in one request."""

    def __init__(self) -> None:
        self._key = get_secret("ALPACA_API_KEY")
        self._secret = get_secret("ALPACA_API_SECRET")

    async def fetch(self, symbols: list[str], *, limit: int = 50) -> list[dict[str, Any]]:
        if not symbols or not self._key or not self._secret:
            return []
        import httpx
        params = {"symbols": ",".join(s.upper() for s in symbols),
                  "limit": min(int(limit), 50), "sort": "desc"}
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(_NEWS_URL, params=params, headers=headers)
            if r.status_code != 200:
                log.warning("Alpaca news fetch failed: %s %s", r.status_code, r.text[:150])
                return []
            return parse_alpaca_news(r.json().get("news", []))
        except Exception as exc:  # noqa: BLE001 — advisory; never break the scan
            log.warning("Alpaca news fetch error: %s", exc)
            return []


def build_news_provider(settings: Settings) -> NewsProvider:
    """Alpaca news provider when news is enabled AND keys are present; else a no-op (fail-open)."""
    if not settings.news.enabled or settings.news.provider == "none":
        return NullNewsProvider()
    if not (get_secret("ALPACA_API_KEY") and get_secret("ALPACA_API_SECRET")):
        log.info("news.enabled but ALPACA keys unset — news pull inactive (fail-open).")
        return NullNewsProvider()
    return AlpacaNewsProvider()
