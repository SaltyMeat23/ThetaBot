"""News/catalyst channel: store dedup+recency, Alpaca parsing, provider fail-open, webhook push,
and AI-reviewer prompt inclusion. Advisory only — no money-path behavior here."""
import pytest
from fastapi.testclient import TestClient

import agentic.marketdata.news as newsmod
from agentic.config import Settings
from agentic.marketdata.news import NullNewsProvider, build_news_provider, parse_alpaca_news
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.news import NewsStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

TOKEN = "tv-secret"


def test_store_dedup_and_recent(tmp_path):
    s = NewsStore(Database(tmp_path / "n.db"))
    assert s.add(symbol="onds", headline="PT raised", dedup_key="benzinga:1:ONDS", source="benzinga")
    assert not s.add(symbol="ONDS", headline="PT raised", dedup_key="benzinga:1:ONDS")  # dup ignored
    s.add(symbol="ONDS", headline="Downgrade", dedup_key="benzinga:2:ONDS")
    items = s.recent_for("ONDS")
    assert len(items) == 2 and {i["headline"] for i in items} == {"PT raised", "Downgrade"}


def test_store_recency_window(tmp_path):
    s = NewsStore(Database(tmp_path / "n.db"))
    s.add(symbol="ONDS", headline="old", dedup_key="x:1:ONDS",
          created_at="2020-01-01T00:00:00+00:00")
    assert s.recent_for("ONDS", max_age_seconds=3600) == []      # older than the window -> excluded
    assert len(s.recent_for("ONDS")) == 1                        # no window -> included


def test_parse_alpaca_news_flattens_per_symbol():
    articles = [
        {"id": 42, "headline": "Big news", "source": "benzinga", "url": "u",
         "created_at": "2026-08-14T17:00:00Z", "symbols": ["ONDS", "SMR"]},
        {"id": 43, "headline": "", "symbols": ["F"]},            # empty headline -> dropped
    ]
    rows = parse_alpaca_news(articles)
    assert len(rows) == 2 and {r["symbol"] for r in rows} == {"ONDS", "SMR"}
    assert rows[0]["dedup_key"] == "benzinga:42:ONDS"


def test_build_provider_failopen(monkeypatch):
    assert isinstance(build_news_provider(Settings(news={"enabled": False})), NullNewsProvider)
    monkeypatch.setattr(newsmod, "get_secret", lambda *a, **k: None)   # no ALPACA keys
    assert isinstance(build_news_provider(Settings(news={"enabled": True})), NullNewsProvider)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_TOKEN", TOKEN)
    db = Database(tmp_path / "w.db")
    audit = AuditStore(db)
    news = NewsStore(db)
    deps = WebDeps(
        settings=Settings(broker="paper", market_data="paper"),
        signals=SignalStore(db), killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db), news=news,
    )
    return TestClient(create_app(deps)), news


def test_news_webhook_push_stores(client):
    c, news = client
    r = c.post("/webhook/tradingview?t=" + TOKEN,
               json={"action": "news", "symbol": "ONDS", "headline": "Upgrade to Buy",
                     "source": "x", "url": "https://x.com/x"})
    assert r.status_code == 202 and r.json()["status"] == "news_stored"
    items = news.recent_for("ONDS")
    assert items and items[0]["headline"] == "Upgrade to Buy" and items[0]["source"] == "x"


def test_news_webhook_requires_symbol_and_headline(client):
    c, _ = client
    r = c.post("/webhook/tradingview?t=" + TOKEN, json={"action": "news", "symbol": "ONDS"})
    assert r.status_code == 400


def test_reviewer_prompt_includes_news():
    from agentic.ai.reviewer import build_user_prompt

    class _Cand:
        underlying = "ONDS"; occ_symbol = "X"; strike = 8.0; dte = 10; delta = -0.25; iv = 0.9
        premium = 0.23; annualized_ror = 90.0; break_even = 7.77; open_interest = 500; volume = 50

    prompt = build_user_prompt(candidate=_Cand(), ctx=None, regime=None, move_class="neutral",
                               tv=None, portfolio=None, news=[{"headline": "SEC probe opened"}])
    assert "SEC probe opened" in prompt
