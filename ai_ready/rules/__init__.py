"""Signal collector base class and registry.

The SignalCollector is the generalized form of the original Rule. Rules
already follow the collect -> measure -> evaluate -> report pipeline and
emit bare findings (signals without interpretation). SignalCollector
formalizes this pattern with signal-centric naming.

Rule inherits from SignalCollector so all existing rule classes work
unchanged. The registry provides both old (register_rule/get_rule/all_rules)
and new (register_collector/get_collector/all_collectors) names.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ai_ready.models import Document, Finding, KnowledgeBase, RuleResult, Severity


class SignalCollector(ABC):
    """Base collector contract — every collector implements collect/measure/evaluate/report.

    Collectors receive a KnowledgeBase (documents + relations) instead of a raw
    list of documents. This allows collectors to access document relationships
    (navigation hierarchy, cross-references) for richer analysis.

    The pipeline produces signals (bare facts) that are later interpreted by
    the assessment layer via InterpretationPolicy.
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
        """Evaluate metrics and produce signals (findings during migration)."""
        ...

    @abstractmethod
    def report(self) -> RuleResult:
        """Produce the final collector result."""
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

    @property
    def artifacts(self) -> list[Document]:
        """Convenience accessor for artifacts (alias for documents during migration)."""
        return self.documents


class Rule(SignalCollector):
    """Base rule contract — inherits from SignalCollector.

    Deprecated: use SignalCollector directly for new collectors.
    All existing rule classes inherit from Rule and work unchanged.
    """

    pass


# Registry of all available collectors (shared with rule registry)
_COLLECTOR_REGISTRY: dict[str, type[SignalCollector]] = {}

# Alias for backward compatibility
_RULE_REGISTRY = _COLLECTOR_REGISTRY


def register_collector(collector_class: type[SignalCollector]) -> type[SignalCollector]:
    """Decorator to register a collector class."""
    _COLLECTOR_REGISTRY[collector_class.id] = collector_class
    return collector_class


def get_collector(collector_id: str) -> type[SignalCollector] | None:
    """Look up a collector class by ID."""
    return _COLLECTOR_REGISTRY.get(collector_id)


def all_collectors() -> dict[str, type[SignalCollector]]:
    """Return all registered collectors."""
    return dict(_COLLECTOR_REGISTRY)


# Backward-compatible aliases
register_rule = register_collector
get_rule = get_collector
all_rules = all_collectors


# Dimension -> collector mapping
DIMENSION_COLLECTORS: dict[str, list[str]] = {
    "retrieval": ["topic_purity", "heading_quality"],
    "context": ["context_independence"],
    "consistency": ["terminology_consistency", "contradiction_detection"],
    "trust": ["canonical_source", "freshness"],
    "connectivity": ["knowledge_connectivity", "link_integrity"],
    "workflow": ["workflow_completeness"],
}

# Backward-compatible alias
DIMENSION_RULES = DIMENSION_COLLECTORS


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

# Re-export collector aliases for convenient access.
# These are set by each rule module at import time.
from ai_ready.rules.topic_purity import TopicPurityCollector  # noqa: E402, F401
from ai_ready.rules.heading_quality import HeadingQualityCollector  # noqa: E402, F401
from ai_ready.rules.context_independence import ContextIndependenceCollector  # noqa: E402, F401
from ai_ready.rules.link_integrity import LinkIntegrityCollector  # noqa: E402, F401


_import_rules()
