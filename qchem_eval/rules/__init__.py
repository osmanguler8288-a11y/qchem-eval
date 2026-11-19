"""规则层对外入口。上层只 import 这里，不直接碰 evaluator。"""

from __future__ import annotations

from .evaluator import (
    ACTION_PRIORITY,
    HARTREE_TO_KCAL,
    SEVERITY_PENALTY,
    SEVERITY_VERDICT,
    Rule,
    RuleCatalogError,
    RuleOutcome,
    RuleSet,
    aggregate,
    build_context,
    describe_missing,
    load_ruleset,
    run_rules,
)

__all__ = [
    "ACTION_PRIORITY",
    "HARTREE_TO_KCAL",
    "SEVERITY_PENALTY",
    "SEVERITY_VERDICT",
    "Rule",
    "RuleCatalogError",
    "RuleOutcome",
    "RuleSet",
    "aggregate",
    "build_context",
    "describe_missing",
    "load_ruleset",
    "run_rules",
]
