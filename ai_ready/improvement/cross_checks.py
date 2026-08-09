"""Cross-check diagnostics — detect co-occurring signal patterns across collectors.

While individual collectors detect specific structural facts (broken links,
stale content, generic headings), cross-check rules examine patterns ACROSS
multiple collectors to identify deeper systemic root causes that individual
signals alone don't reveal.

This is distinct from ``salience.cluster_problems_by_root_cause()``, which
groups *similar* problems by their own properties (same directory, same type,
same evidence). Cross-checks detect *co-occurring* problems from *different*
collectors on the *same artifact set*, producing named systemic root causes.

Usage::

    from ai_ready.improvement.cross_checks import run_cross_checks

    diagnostics = run_cross_checks(assessment)
    for d in diagnostics:
        print(f"{d.name}: {d.description}")
        print(f"  Affected: {len(d.affected_artifact_uris)} artifacts")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ai_ready.models import DimensionScore, KnowledgeAssessment, KnowledgeSignal, Severity


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class CrossCheckResult:
    """A named systemic root cause detected by cross-checking signals across
    multiple collectors.

    Unlike individual signals (which describe a single structural fact),
    a CrossCheckResult describes a *pattern* of co-occurring signals that
    indicates a deeper systemic issue.
    """

    rule_id: str
    name: str  # named root cause, e.g. "navigation_collapse"
    description: str
    severity: Severity
    ai_impact: str
    affected_artifact_uris: list[str] = field(default_factory=list)
    affected_signal_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # human-readable evidence lines
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "description": self.description,
            "severity": self.severity.value,
            "ai_impact": self.ai_impact,
            "affected_artifact_uris": self.affected_artifact_uris,
            "affected_signal_ids": self.affected_signal_ids,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


# ---------------------------------------------------------------------------
# Rule base class and registry
# ---------------------------------------------------------------------------

class CrossCheckRule(ABC):
    """Base class for cross-check diagnostic rules.

    Each rule examines signals from multiple collectors and dimension scores
    to detect co-occurring patterns that indicate a systemic root cause.
    """

    id: str = "base"
    name: str = "base"
    description: str = ""
    severity: Severity = Severity.MEDIUM
    ai_impact: str = ""
    recommendation: str = ""

    @abstractmethod
    def check(
        self,
        signals: list[KnowledgeSignal],
        dimensions: dict[str, DimensionScore],
    ) -> CrossCheckResult | None:
        """Examine signals and dimensions for this rule's pattern.

        Returns a CrossCheckResult if the pattern is detected, or None.
        """
        ...

    def _build_result(
        self,
        affected_uris: list[str],
        affected_signal_ids: list[str],
        evidence: list[str],
    ) -> CrossCheckResult:
        """Build a result with this rule's metadata."""
        return CrossCheckResult(
            rule_id=self.id,
            name=self.name,
            description=self.description,
            severity=self.severity,
            ai_impact=self.ai_impact,
            affected_artifact_uris=sorted(affected_uris),
            affected_signal_ids=affected_signal_ids,
            evidence=evidence,
            recommendation=self.recommendation,
        )


# Registry
_CROSS_CHECK_REGISTRY: list[type[CrossCheckRule]] = []


def register_cross_check(rule_class: type[CrossCheckRule]) -> type[CrossCheckRule]:
    """Decorator to register a cross-check rule."""
    _CROSS_CHECK_REGISTRY.append(rule_class)
    return rule_class


def all_cross_checks() -> list[type[CrossCheckRule]]:
    """Return all registered cross-check rule classes."""
    return list(_CROSS_CHECK_REGISTRY)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _signals_by_type(
    signals: list[KnowledgeSignal],
) -> dict[str, list[KnowledgeSignal]]:
    """Group signals by signal_type."""
    by_type: dict[str, list[KnowledgeSignal]] = defaultdict(list)
    for s in signals:
        by_type[s.signal_type].append(s)
    return dict(by_type)


def _signals_by_collector(
    signals: list[KnowledgeSignal],
) -> dict[str, list[KnowledgeSignal]]:
    """Group signals by collector_id."""
    by_collector: dict[str, list[KnowledgeSignal]] = defaultdict(list)
    for s in signals:
        by_collector[s.collector_id].append(s)
    return dict(by_collector)


def _uris_from_signals(signals: list[KnowledgeSignal]) -> set[str]:
    """Extract unique artifact URIs from a list of signals."""
    return {s.artifact_uri for s in signals}


# ---------------------------------------------------------------------------
# Cross-check rules
# ---------------------------------------------------------------------------

@register_cross_check
class NavigationCollapseRule(CrossCheckRule):
    """Detect navigation graph collapse.

    Triggered when broken links + orphan documents + low connectivity
    dimension score co-occur, indicating the knowledge graph navigation
    structure is breaking down — not just individual links failing.
    """

    id = "navigation_collapse"
    name = "navigation_collapse"
    description = (
        "Knowledge graph navigation is collapsing: broken links, orphan "
        "documents, and low connectivity co-occur, indicating systemic "
        "navigation breakdown rather than individual link failures."
    )
    severity = Severity.HIGH
    ai_impact = (
        "AI agents cannot reliably navigate the knowledge base. Broken links "
        "cause dead-end retrieval, orphans are invisible to graph traversal, "
        "and low connectivity means few paths exist — agents will miss "
        "relevant content or follow broken paths to hallucinations."
    )
    recommendation = (
        "Rebuild the navigation hierarchy holistically: fix broken links, "
        "connect orphan documents to the parent-child graph, and add "
        "cross-references between related artifacts. Do not patch links "
        "individually — the structural pattern indicates the navigation "
        "tree needs systematic repair."
    )

    # Thresholds
    MIN_BROKEN_LINKS = 2
    MIN_ORPHANS = 1
    MAX_CONNECTIVITY_SCORE = 50

    def check(
        self,
        signals: list[KnowledgeSignal],
        dimensions: dict[str, DimensionScore],
    ) -> CrossCheckResult | None:
        by_type = _signals_by_type(signals)

        broken_links = by_type.get("broken_link", [])
        orphans = by_type.get("orphan", [])
        orphan_artifacts = by_type.get("orphan_artifact", [])

        if len(broken_links) < self.MIN_BROKEN_LINKS:
            return None
        if len(orphans) + len(orphan_artifacts) < self.MIN_ORPHANS:
            return None

        connectivity = dimensions.get("connectivity")
        if connectivity is None or connectivity.score >= self.MAX_CONNECTIVITY_SCORE:
            return None

        all_orphans = orphans + orphan_artifacts
        affected_uris = _uris_from_signals(broken_links) | _uris_from_signals(all_orphans)
        affected_ids = [s.signal_id for s in broken_links + all_orphans]

        evidence = [
            f"{len(broken_links)} broken_link signal(s) from link_integrity",
            f"{len(all_orphans)} orphan signal(s) from link_integrity/knowledge_connectivity",
            f"connectivity dimension score: {connectivity.score}/100 "
            f"(threshold: <{self.MAX_CONNECTIVITY_SCORE})",
        ]

        return self._build_result(
            affected_uris=list(affected_uris),
            affected_signal_ids=affected_ids,
            evidence=evidence,
        )


@register_cross_check
class StaleKbCollapseRule(CrossCheckRule):
    """Detect stale knowledge base collapse.

    Triggered when stale content + missing canonical sources co-occur
    across multiple artifacts, indicating the knowledge base hasn't been
    maintained holistically — a process gap, not individual pages being
    forgotten.
    """

    id = "stale_kb_collapse"
    name = "stale_kb_collapse"
    description = (
        "Knowledge base maintenance has lapsed systemically: stale content "
        "and missing canonical sources co-occur across multiple artifacts, "
        "indicating a process gap in the maintenance pipeline rather than "
        "individual pages being forgotten."
    )
    severity = Severity.MEDIUM
    ai_impact = (
        "AI agents retrieve outdated information with no source attribution. "
        "Stale content provides deprecated guidance, and missing canonical "
        "sources prevent agents from verifying or updating the information. "
        "This combination degrades both trust and freshness dimensions "
        "simultaneously."
    )
    recommendation = (
        "Establish a maintenance cadence: review stale content for accuracy, "
        "add canonical source links to all content, and set up freshness "
        "monitoring. The co-occurrence pattern suggests a systemic process "
        "gap, not individual page issues."
    )

    MIN_STALE = 2
    MIN_MISSING_SOURCE = 2

    def check(
        self,
        signals: list[KnowledgeSignal],
        dimensions: dict[str, DimensionScore],
    ) -> CrossCheckResult | None:
        by_type = _signals_by_type(signals)

        stale = by_type.get("stale_content", [])
        missing_source = by_type.get("missing_canonical_source", [])

        if len(stale) < self.MIN_STALE:
            return None
        if len(missing_source) < self.MIN_MISSING_SOURCE:
            return None

        affected_uris = _uris_from_signals(stale) | _uris_from_signals(missing_source)
        affected_ids = [s.signal_id for s in stale + missing_source]

        evidence = [
            f"{len(stale)} stale_content signal(s) from freshness collector",
            f"{len(missing_source)} missing_canonical_source signal(s) from "
            f"canonical_source collector",
            f"{len(affected_uris)} distinct artifacts affected",
        ]

        return self._build_result(
            affected_uris=list(affected_uris),
            affected_signal_ids=affected_ids,
            evidence=evidence,
        )


@register_cross_check
class RetrievalBreakdownRule(CrossCheckRule):
    """Detect content retrieval breakdown.

    Triggered when heading quality issues + topic purity issues co-occur
    on the same artifact, indicating the content structure is fundamentally
    broken for retrieval — RAG systems can't effectively chunk and retrieve
    this content regardless of the query.
    """

    id = "retrieval_breakdown"
    name = "retrieval_breakdown"
    description = (
        "Content structure is broken for retrieval: heading quality and "
        "topic purity issues co-occur on the same artifacts, meaning RAG "
        "systems cannot effectively chunk, route, or retrieve this content "
        "regardless of the query."
    )
    severity = Severity.HIGH
    ai_impact = (
        "RAG retrieval fails on two axes simultaneously: poor headings mean "
        "chunk boundaries don't align with semantic content, and mixed "
        "topics mean each chunk contains irrelevant information. The "
        "combination is worse than either alone — retrieval precision and "
        "recall both degrade, and the LLM receives poorly-structured, "
        "off-topic context."
    )
    recommendation = (
        "Restructure affected artifacts: replace generic/placeholder headings "
        "with specific descriptive headings, and split mixed-topic content "
        "into focused single-topic documents. The co-occurrence indicates "
        "structural content issues, not surface-level formatting."
    )

    # Heading quality signal types that indicate structural problems
    HEADING_ISSUE_TYPES = {"generic", "placeholder", "numbered_only", "too_short"}

    def check(
        self,
        signals: list[KnowledgeSignal],
        dimensions: dict[str, DimensionScore],
    ) -> CrossCheckResult | None:
        by_type = _signals_by_type(signals)

        # Collect heading quality signals that are structural issues
        heading_signals = [
            s for st in self.HEADING_ISSUE_TYPES
            for s in by_type.get(st, [])
        ]
        mixed_topics = by_type.get("mixed_topics", [])

        if not heading_signals or not mixed_topics:
            return None

        # Find artifacts that have BOTH signal types
        heading_uris = _uris_from_signals(heading_signals)
        mixed_uris = _uris_from_signals(mixed_topics)
        shared_uris = heading_uris & mixed_uris

        if not shared_uris:
            return None

        # Collect signals on shared artifacts
        affected_signals = [
            s for s in heading_signals + mixed_topics
            if s.artifact_uri in shared_uris
        ]
        affected_ids = [s.signal_id for s in affected_signals]

        heading_types_found = sorted({s.signal_type for s in heading_signals if s.artifact_uri in shared_uris})
        evidence = [
            f"Heading quality issues ({', '.join(heading_types_found)}) on "
            f"{len(shared_uris)} artifact(s)",
            f"mixed_topics signals on the same {len(shared_uris)} artifact(s)",
            f"Artifacts: {', '.join(sorted(shared_uris)[:5])}"
            + ("..." if len(shared_uris) > 5 else ""),
        ]

        return self._build_result(
            affected_uris=list(shared_uris),
            affected_signal_ids=affected_ids,
            evidence=evidence,
        )


@register_cross_check
class ContextFragmentationRule(CrossCheckRule):
    """Detect context fragmentation.

    Triggered when dangling references + orphan artifacts co-occur on the
    same artifact, indicating content is fragmented and can't be retrieved
    as self-contained chunks — the LLM will hallucinate or ignore the
    content because it lacks both internal context and external connections.
    """

    id = "context_fragmentation"
    name = "context_fragmentation"
    description = (
        "Content is fragmented and cannot be retrieved as self-contained "
        "chunks: dangling references and orphan artifacts co-occur, meaning "
        "retrieved content references missing context while being isolated "
        "from the knowledge graph."
    )
    severity = Severity.MEDIUM
    ai_impact = (
        "When an AI agent retrieves this content, it gets a fragment that "
        "references missing context (dangling references) and has no "
        "connections to related content (orphan). The agent cannot resolve "
        "the references or find related information, leading to "
        "hallucination or context loss."
    )
    recommendation = (
        "Resolve dangling references by either adding the referenced content "
        "or removing the references, and connect orphan artifacts to the "
        "knowledge graph via parent-child or cross-reference relationships. "
        "Both issues must be addressed — fixing one without the other "
        "leaves the content partially broken."
    )

    def check(
        self,
        signals: list[KnowledgeSignal],
        dimensions: dict[str, DimensionScore],
    ) -> CrossCheckResult | None:
        by_type = _signals_by_type(signals)

        dangling = by_type.get("dangling_reference", [])
        orphan_artifacts = by_type.get("orphan_artifact", [])

        if not dangling or not orphan_artifacts:
            return None

        dangling_uris = _uris_from_signals(dangling)
        orphan_uris = _uris_from_signals(orphan_artifacts)
        shared_uris = dangling_uris & orphan_uris

        if not shared_uris:
            return None

        affected_signals = [
            s for s in dangling + orphan_artifacts
            if s.artifact_uri in shared_uris
        ]
        affected_ids = [s.signal_id for s in affected_signals]

        evidence = [
            f"{len(dangling)} dangling_reference signal(s) from "
            f"context_independence",
            f"{len(orphan_artifacts)} orphan_artifact signal(s) from "
            f"knowledge_connectivity",
            f"{len(shared_uris)} artifact(s) have both issues",
        ]

        return self._build_result(
            affected_uris=list(shared_uris),
            affected_signal_ids=affected_ids,
            evidence=evidence,
        )


@register_cross_check
class TerminologyContradictionRule(CrossCheckRule):
    """Detect terminology and contradiction co-occurrence.

    Triggered when terminology variants + version conflicts co-occur,
    indicating the knowledge base has divergent information that will cause
    AI agents to retrieve conflicting answers with no way to detect the
    inconsistency.
    """

    id = "terminology_contradiction"
    name = "terminology_contradiction"
    description = (
        "Inconsistent terminology and version conflicts co-occur: the "
        "knowledge base contains divergent information using different "
        "terms for the same concepts, with conflicting version or default "
        "information. AI agents will retrieve contradictory answers."
    )
    severity = Severity.HIGH
    ai_impact = (
        "AI agents retrieve content with inconsistent terminology and "
        "conflicting version information. The agent cannot determine which "
        "term or version is canonical, leading to confused or contradictory "
        "responses. This is worse than either issue alone — terminology "
        "inconsistency makes it hard to detect that the contradiction exists."
    )
    recommendation = (
        "Establish a terminology glossary and version policy: standardize "
        "terms across all artifacts, resolve version conflicts to a single "
        "canonical version, and document deprecated terms. The co-occurrence "
        "means both issues must be addressed together — fixing terminology "
        "without resolving contradictions leaves conflicting information "
        "under consistent names."
    )

    MIN_TERMINOLOGY = 2
    MIN_CONTRADICTIONS = 1

    # Contradiction detection signal types
    CONTRADICTION_TYPES = {"version_conflict", "boolean_default_conflict"}

    def check(
        self,
        signals: list[KnowledgeSignal],
        dimensions: dict[str, DimensionScore],
    ) -> CrossCheckResult | None:
        by_type = _signals_by_type(signals)

        terminology = by_type.get("terminology_variant", [])
        contradictions = [
            s for ct in self.CONTRADICTION_TYPES
            for s in by_type.get(ct, [])
        ]

        if len(terminology) < self.MIN_TERMINOLOGY:
            return None
        if len(contradictions) < self.MIN_CONTRADICTIONS:
            return None

        affected_uris = _uris_from_signals(terminology) | _uris_from_signals(contradictions)
        affected_ids = [s.signal_id for s in terminology + contradictions]

        contradiction_types = sorted({s.signal_type for s in contradictions})
        evidence = [
            f"{len(terminology)} terminology_variant signal(s) from "
            f"terminology_consistency",
            f"{len(contradictions)} contradiction signal(s) "
            f"({', '.join(contradiction_types)}) from contradiction_detection",
            f"{len(affected_uris)} distinct artifacts affected",
        ]

        return self._build_result(
            affected_uris=list(affected_uris),
            affected_signal_ids=affected_ids,
            evidence=evidence,
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_cross_checks(assessment: KnowledgeAssessment) -> list[CrossCheckResult]:
    """Run all cross-check rules against an assessment.

    Args:
        assessment: A completed KnowledgeAssessment with signals and
                    dimension scores.

    Returns:
        List of CrossCheckResult objects for all rules that detected their
        pattern. Empty list if no patterns were detected.
    """
    results: list[CrossCheckResult] = []
    for rule_cls in all_cross_checks():
        rule = rule_cls()
        result = rule.check(assessment.signals, assessment.dimensions)
        if result is not None:
            results.append(result)
    return results


def cross_checks_to_dict(results: list[CrossCheckResult]) -> list[dict[str, Any]]:
    """Serialize cross-check results to a list of dicts for storage/logging."""
    return [r.to_dict() for r in results]
