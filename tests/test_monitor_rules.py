"""Phase 1: monitor evaluates rules, creates+notifies decisions, and dedups across cycles."""
import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import RuleConfig, Settings
from agentic.domain.enums import DecisionStatus
from agentic.marketdata.base import PaperMarketData
from agentic.notify.base import Notifier
from agentic.rules.engine import RulesEngine, build_rules
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.positions import PositionStore


@pytest.fixture(autouse=True)
def _market_open(monkeypatch):
    # Rule evaluation is gated to the regular session; force "open" so these tests are
    # deterministic regardless of the wall-clock time they run at.
    monkeypatch.setattr("agentic.services.monitor.is_market_hours", lambda *a, **k: True)


class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title, message, *, priority="normal", actions=None):
        self.sent.append((title, message, priority))


def _build(tmp_path, rules_cfg):
    db = Database(tmp_path / "t.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker()
    notifier = RecordingNotifier()
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=rules_cfg)
    engine = RulesEngine(build_rules(rules_cfg))
    monitor = MonitorLoop(
        settings, broker, PaperMarketData(), positions, audit, killswitch,
        rules_engine=engine, decisions=decisions, notifier=notifier,
    )
    return monitor, broker, decisions, notifier


@pytest.mark.asyncio
async def test_profit_target_creates_decision_and_notifies(tmp_path):
    rules_cfg = [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                            requires_approval=False, params={"profit_pct": 0.5})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)

    # Seeded AAPL covered call: credit 2.00. Drive its price below the 1.00 target.
    aapl = next(p for p in await broker.get_open_positions() if p.underlying == "AAPL")
    broker.set_mark(aapl.occ_symbol, bid=0.90, ask=0.95)

    await monitor.run_once()
    proposed = decisions.list_by_status(DecisionStatus.PROPOSED)
    assert len(proposed) == 1
    assert proposed[0].rule_name == "p50"
    assert any("Close signal" in t for t, _m, _p in notifier.sent)
    # Auto rule -> high priority.
    assert any(p == "high" for _t, _m, p in notifier.sent)


@pytest.mark.asyncio
async def test_decisions_dedup_across_cycles(tmp_path):
    rules_cfg = [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                            requires_approval=False, params={"profit_pct": 0.5})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    aapl = next(p for p in await broker.get_open_positions() if p.underlying == "AAPL")
    broker.set_mark(aapl.occ_symbol, bid=0.90, ask=0.95)

    await monitor.run_once()
    await monitor.run_once()
    await monitor.run_once()
    # Same position+rule+day -> exactly one decision and one notification.
    assert len(decisions.list_by_status(DecisionStatus.PROPOSED)) == 1
    assert len([t for t, _m, _p in notifier.sent if "Close signal" in t]) == 1


@pytest.mark.asyncio
async def test_no_decision_when_target_not_met(tmp_path):
    rules_cfg = [RuleConfig(name="p50", rule_type="PROFIT_TARGET",
                            requires_approval=False, params={"profit_pct": 0.5})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    # Default seed marks are well above target -> no profit-target decision.
    await monitor.run_once()
    assert decisions.list_by_status(DecisionStatus.PROPOSED) == []


class _SpyExecutor:
    def __init__(self):
        self.closed = []

    async def execute_close(self, pos, decision, quote):
        self.closed.append((pos.occ_symbol, decision.rule_name))
        return None


@pytest.mark.asyncio
async def test_dte_alert_never_executes_even_with_executor(tmp_path):
    # Regression: a DTE action=alert rule (requires_approval=false) is a heads-up only. Even with an
    # executor wired in, it must NOT auto buy-to-close — that footgun realized a loss on a tested
    # ONDS put right before it recovered. It notifies; the roll manager / assignment handle exits.
    rules_cfg = [RuleConfig(name="dte-2", rule_type="DTE", requires_approval=False,
                            params={"dte_threshold": 90, "action": "alert"})]  # 90 -> always triggers
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    spy = _SpyExecutor()
    monitor.executor = spy

    await monitor.run_once()

    assert spy.closed == []                                          # never routed to the executor
    proposed = decisions.list_by_status(DecisionStatus.PROPOSED)
    assert proposed and all(d.rule_name == "dte-2" for d in proposed)
    assert any("[ALERT]" in m for _t, m, _p in notifier.sent)       # labeled as an alert, not AUTO
    assert all(p == "normal" for _t, _m, p in notifier.sent)        # not high-priority


@pytest.mark.asyncio
async def test_dte_close_action_executes(tmp_path):
    # The escape hatch still works: action=close is executable and routes to the executor.
    rules_cfg = [RuleConfig(name="dte-close", rule_type="DTE", requires_approval=False,
                            params={"dte_threshold": 90, "action": "close"})]
    monitor, broker, decisions, notifier = _build(tmp_path, rules_cfg)
    spy = _SpyExecutor()
    monitor.executor = spy

    await monitor.run_once()

    assert spy.closed and all(rule == "dte-close" for _occ, rule in spy.closed)
