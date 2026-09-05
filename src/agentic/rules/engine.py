"""RulesEngine: build rules from config and evaluate a position against all of them."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from ..config import RuleConfig
from ..domain.models import CloseDecision, Position, utcnow
from ..marketdata.quote import OptionQuote
from .base import Rule
from .dte import DteRule
from .profit_target import ProfitTargetRule
from .stop_loss import StopLossRule

log = logging.getLogger("agentic.rules")

# SIGNAL rules are evaluated separately (against inbound webhooks), not per-position here.
_RULE_CLASSES = {
    "PROFIT_TARGET": ProfitTargetRule,
    "STOP_LOSS": StopLossRule,
    "DTE": DteRule,
}


def build_rules(configs: list[RuleConfig]) -> list[Rule]:
    rules: list[Rule] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        cls = _RULE_CLASSES.get(cfg.rule_type)
        if cls is None:
            continue  # e.g. SIGNAL — handled in Phase 3
        rules.append(cls(name=cfg.name, requires_approval=cfg.requires_approval, params=cfg.params))
    log.info("Loaded %d position rule(s): %s", len(rules), [r.name for r in rules])
    return rules


def _config_sig(configs: list[RuleConfig]) -> str:
    """A stable signature of the rule config, so hot-reload only rebuilds on real changes."""
    return json.dumps(
        [{"name": c.name, "rule_type": c.rule_type, "enabled": c.enabled,
          "requires_approval": c.requires_approval, "params": c.params} for c in configs],
        sort_keys=True,
    )


class RulesEngine:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._sig: str | None = None

    @classmethod
    def from_configs(cls, configs: list[RuleConfig]) -> "RulesEngine":
        engine = cls(build_rules(configs))
        engine._sig = _config_sig(configs)
        return engine

    def refresh(self, configs: list[RuleConfig]) -> bool:
        """Rebuild rules if the config changed (enables live edits via the settings API). Cheap."""
        sig = _config_sig(configs)
        if sig == self._sig:
            return False
        self.rules = build_rules(configs)
        self._sig = sig
        log.info("Rules hot-reloaded: %s", [r.name for r in self.rules])
        return True

    def evaluate(
        self, position: Position, quote: OptionQuote | None, now: datetime | None = None
    ) -> list[CloseDecision]:
        now = now or utcnow()
        decisions: list[CloseDecision] = []
        for rule in self.rules:
            try:
                decision = rule.evaluate(position, quote, now)
            except Exception as exc:  # noqa: BLE001 — a bad rule must not stop others
                log.exception("Rule %s failed on %s: %s", rule.name, position.occ_symbol, exc)
                continue
            if decision is not None:
                decisions.append(decision)
        return decisions
