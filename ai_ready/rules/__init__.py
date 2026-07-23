"""Rule engine base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ai_ready.models import Document, Finding, KnowledgeBase, RuleResult, Severity


class Rule(ABC):
    """Base rule contract - every rule implements collect/measure/evaluate/report.

    Rules receive a KnowledgeBase (documents + relations) instead of a raw
    list of documents. This allows rules to access document relationships
    (navigation hierarchy, cross-references) for richer analysis.
    """

    id: str = "base"
    severity_default: Severity = Severity.LOW
    dimension: str = "retrieval"

    @abstractmethod
    def collect(self, kb: KnowledgeBase) -> dict[str, Any]:
        """Collect raw signals from the knowledge base."""
        ...

    @abstractmethod
    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Compute metrics from collected signals."""
        ...

    @abstractmethod
    def evaluate(self, metrics: dict[str, Any]) -> list[Finding]:
        """Evaluate metrics and produce findings."""
        ...

    @abstractmethod
    def report(self) -> RuleResult:
        """Produce the final rule result."""
        ...

    def run(self, kb: KnowledgeBase) -> RuleResult:
        """Execute the full collect -> measure -> evaluate -> report pipeline."""
        self._kb = kb
        self._signals = self.collect(kb)
        self._metrics = self.measure(self._signals)
        self._findings = self.evaluate(self._metrics)
        return self.report()

    @property
    def documents(self) -> list[Document]:
        """Convenience accessor for the documents in the knowledge base."""
        return self._kb.documents if hasattr(self, "_kb") else []


# Registry of all available rules
_RULE_REGISTRY: dict[str, type[Rule]] = {}


def register_rule(rule_class: type[Rule]) -> type[Rule]:
    """Decorator to register a rule class."""
    _RULE_REGISTRY[rule_class.id] = rule_class
    return rule_class


def get_rule(rule_id: str) -> type[Rule] | None:
    """Look up a rule class by ID."""
    return _RULE_REGISTRY.get(rule_id)


def all_rules() -> dict[str, type[Rule]]:
    """Return all registered rules."""
    return dict(_RULE_REGISTRY)


# Dimension -> rule mapping
DIMENSION_RULES: dict[str, list[str]] = {
    "retrieval": ["topic_purity", "heading_quality"],
    "context": ["context_independence"],
    "consistency": ["terminology_consistency", "contradiction_detection"],
    "trust": ["canonical_source", "freshness"],
    "connectivity": ["knowledge_connectivity", "link_integrity"],
    "workflow": ["workflow_completeness"],
}


# Auto-import all rule modules to trigger @register_rule decorators
def _import_rules() -> None:
    """Import all rule modules to register them."""
    import importlib
    import pkgutil

    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_") or module_info.name == "__init__":
            continue
        importlib.import_module(f"{__name__}.{module_info.name}")


_import_rules()
