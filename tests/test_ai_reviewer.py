"""AI trade-analyst: verdict normalization, fail-open behavior, and advisory/veto in the scanner."""
from datetime import date

import pytest

from agentic.ai.reviewer import AIReviewer, build_user_prompt
from agentic.ai.schema import Verdict, normalize_verdict
from agentic.config import AIConfig, Settings
from agentic.brokers.paper_broker import PaperBroker
from agentic.domain.enums import DecisionStatus
from agentic.domain.models import EntryDecision
from agentic.entry.context import UnderlyingContext
from agentic.entry.regime import MarketRegime
from agentic.entry.screener import EntryCandidate
from agentic.marketdata.base import PaperMarketData
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.ai_reviews import AIReviewStore
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.entry_decisions import EntryDecisionStore


class FakeClient:
    """Stand-in for AnthropicReviewerClient — returns a canned raw dict, no network."""
    def __init__(self, raw=None, raises=False):
        self._raw = raw or {}
        self._raises = raises

    async def analyze(self, system, user):
        if self._raises:
            raise RuntimeError("boom")
        return self._raw


def _candidate():
    return EntryCandidate(
        underlying="F", occ_symbol="F260717P00011000", option_id=None, strike=11.0,
        expiration=date(2026, 7, 17), dte=35, delta=-0.25, iv=0.30, premium=0.35,
        ror=3.2, annualized_ror=33.0, max_risk=1100.0, break_even=10.65,
        open_interest=500, volume=120, score=33.0,
    )


# --- schema / normalizer ------------------------------------------------------------------------

def test_normalize_unknown_recommendation_is_caution():
    v = normalize_verdict({"recommendation": "yolo", "confidence": 2.0})
    assert v.recommendation == "caution"        # never silently upgraded to "take"
    assert v.confidence == 1.0                  # clamped to [0,1]


def test_normalize_bad_confidence_defaults_zero():
    v = normalize_verdict({"recommendation": "take", "confidence": "n/a"})
    assert v.recommendation == "take"
    assert v.confidence == 0.0


# --- reviewer -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_returns_verdict():
    reviewer = AIReviewer(AIConfig(enabled=True),
                          FakeClient({"recommendation": "take", "confidence": 0.8,
                                      "rationale": "clean setup", "flags": [], "regime_read": "calm"}))
    v = await reviewer.review(candidate=_candidate(), ctx=None, regime=None,
                              move_class="neutral", tv=None, portfolio={})
    assert v.recommendation == "take"


@pytest.mark.asyncio
async def test_review_none_client_returns_none():
    reviewer = AIReviewer(AIConfig(enabled=True), None)
    assert await reviewer.review(candidate=_candidate(), ctx=None, regime=None,
                                 move_class="x", tv=None, portfolio={}) is None


@pytest.mark.asyncio
async def test_review_fails_open_on_error():
    reviewer = AIReviewer(AIConfig(enabled=True), FakeClient(raises=True))
    assert await reviewer.review(candidate=_candidate(), ctx=None, regime=None,
                                 move_class="x", tv=None, portfolio={}) is None


def test_user_prompt_includes_key_signals():
    prompt = build_user_prompt(
        candidate=_candidate(),
        ctx=UnderlyingContext(symbol="F", price=11.0, drawdown_20d=-0.05),
        regime=MarketRegime(label="risk_off", spy_drawdown_20d=-0.06),
        move_class="systemic", tv={"payload": {"rsi": 38}}, portfolio={"held_names": []},
    )
    assert "F260717P00011000" in prompt
    assert "systemic" in prompt
    assert "risk_off" in prompt


# --- scanner advisory / veto --------------------------------------------------------------------

def _scanner(tmp_path, ai_config):
    db = Database(tmp_path / "ai.db")
    settings = Settings(broker="paper", market_data="paper", ai=ai_config)
    ed = EntryDecisionStore(db)
    sc = OpportunityScanner(
        settings, PaperBroker(seed_positions=[]), PaperMarketData(), ed,
        executor=None, audit=AuditStore(db), killswitch=KillSwitch(db, AuditStore(db)),
        ai_reviews=AIReviewStore(db),
    )
    sc.last_regime = MarketRegime(label="calm", spy_drawdown_20d=-0.01)
    sc.last_context = {"F": UnderlyingContext(symbol="F", drawdown_20d=-0.05)}
    sc.last_holdings = []
    return sc, ed


class _FakeReviewer:
    def __init__(self, verdict):
        self._v = verdict

    async def review(self, **kwargs):
        return self._v


def _decision():
    d = EntryDecision(
        underlying="F", occ_symbol="F260717P00011000", option_id=None, strike=11.0,
        expiration=date(2026, 7, 17), contracts=1, premium=0.35,
        rule_name="csp-screener", reason="test", dedup_key="F:2026-07-17:11.0",
    )
    return d


@pytest.mark.asyncio
async def test_advisory_stores_verdict_and_does_not_veto(tmp_path):
    sc, ed = _scanner(tmp_path, AIConfig(enabled=True, mode="advisory"))
    sc.ai_reviewer = _FakeReviewer(Verdict("skip", 0.9, "market looks ugly"))
    d = _decision()
    ed.insert_if_new(d)
    vetoed = await sc._ai_veto(d, _candidate())
    assert vetoed is False                       # advisory NEVER blocks, even on 'skip'
    reviews = sc.ai_reviews.recent()
    assert len(reviews) == 1 and reviews[0]["recommendation"] == "skip"
    assert reviews[0]["move_class"] == "idiosyncratic"


@pytest.mark.asyncio
async def test_veto_mode_skips_on_skip(tmp_path):
    sc, ed = _scanner(tmp_path, AIConfig(enabled=True, mode="veto"))
    sc.ai_reviewer = _FakeReviewer(Verdict("skip", 0.9, "systemic risk"))
    d = _decision()
    ed.insert_if_new(d)
    vetoed = await sc._ai_veto(d, _candidate())
    assert vetoed is True
    assert ed.get(d.id).status is DecisionStatus.FAILED


@pytest.mark.asyncio
async def test_veto_mode_allows_take(tmp_path):
    sc, ed = _scanner(tmp_path, AIConfig(enabled=True, mode="veto"))
    sc.ai_reviewer = _FakeReviewer(Verdict("take", 0.8, "good entry"))
    d = _decision()
    ed.insert_if_new(d)
    assert await sc._ai_veto(d, _candidate()) is False


@pytest.mark.asyncio
async def test_failopen_when_verdict_none(tmp_path):
    sc, ed = _scanner(tmp_path, AIConfig(enabled=True, mode="veto"))
    sc.ai_reviewer = _FakeReviewer(None)         # e.g. AI error / disabled
    d = _decision()
    ed.insert_if_new(d)
    assert await sc._ai_veto(d, _candidate()) is False
    assert sc.ai_reviews.recent() == []          # nothing stored on a null verdict


# --- selftest diagnostic ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_selftest_ok_with_working_client():
    raw = {"recommendation": "take", "confidence": 0.7, "rationale": "ok",
           "flags": [], "regime_read": "calm"}
    r = await AIReviewer(AIConfig(enabled=True), FakeClient(raw=raw)).selftest()
    assert r["ok"] is True
    assert r["verdict"]["recommendation"] == "take"


@pytest.mark.asyncio
async def test_selftest_reports_error_not_none():
    r = await AIReviewer(AIConfig(enabled=True), FakeClient(raises=True)).selftest()
    assert r["ok"] is False
    assert "boom" in r["error"]


@pytest.mark.asyncio
async def test_selftest_no_client():
    r = await AIReviewer(AIConfig(enabled=True), None).selftest()
    assert r["ok"] is False and "client is None" in r["error"]
