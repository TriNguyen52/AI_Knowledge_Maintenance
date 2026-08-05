"""Interpretation policy registry - centralized interpretation of signals.

Collectors detect structural facts (e.g., "this heading is generic"). The
policy registry maps those facts to AI-readiness impacts: severity, score
penalty, ai_impact description, and recommendation template. This separates
measurement (what the collector detects) from interpretation (what it means
for AI readiness).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_ready.models import Severity


@dataclass
class InterpretationEntry:
    """What a signal means for AI readiness.

    Maps a (collector_id, signal_type) pair to severity, score penalty,
    AI impact description, and recommendation template.
    """

    severity: Severity
    score_penalty: int
    ai_impact: str
    recommendation_template: str


class InterpretationPolicy:
    """Deterministic lookup registry mapping (collector_id, signal_type) to AI readiness impacts.

    Usage:
        policy = InterpretationPolicy()
        entry = policy.lookup("heading_quality", "placeholder")
        if entry:
            signal.severity = entry.severity
            signal.score = 100 - entry.score_penalty

    """

    def __init__(self) -> None:
        self._registry: dict[tuple[str, str], InterpretationEntry] = {}
        self._load_default_policy()

    def lookup(self, collector_id: str, signal_type: str) -> InterpretationEntry | None:
        """Look up the policy for a signal.

        Args:
            collector_id: The collector that produced the signal.
            signal_type: The type of signal.
        """
        return self._registry.get((collector_id, signal_type))

    def register(
        self, collector_id: str, signal_type: str, entry: InterpretationEntry
    ) -> None:
        """Register a policy entry (for testing/overrides).

        Args:
            collector_id: The collector ID.
            signal_type: The signal type.
            entry: The interpretation entry.
        """
        self._registry[(collector_id, signal_type)] = entry

    def _load_default_policy(self) -> None:
        """Load the built-in policy for all current collectors and signal types."""

        # --- topic_purity ---
        self._registry[("topic_purity", "mixed_topics")] = InterpretationEntry(
            severity=Severity.HIGH,
            score_penalty=15,
            ai_impact=(
                "When a RAG system chunks this document, chunks about one topic "
                "will be retrieved for queries about another, polluting the context "
                "with irrelevant content."
            ),
            recommendation_template=(
                "Split into focused articles. Detected {cluster_count} unrelated "
                "topic clusters: {clusters}"
            ),
        )

        # --- heading_quality ---
        self._registry[("heading_quality", "generic")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "A heading like '{heading}' provides no semantic signal for "
                "retrieval. Embedding-based systems cannot distinguish this section "
                "from others with the same heading."
            ),
            recommendation_template=(
                "Replace generic heading '{heading}' with a specific, "
                "descriptive heading."
            ),
        )
        self._registry[("heading_quality", "placeholder")] = InterpretationEntry(
            severity=Severity.CRITICAL,
            score_penalty=20,
            ai_impact=(
                "A placeholder heading means this section will never be retrieved "
                "for any query. AI agents will skip this content."
            ),
            recommendation_template=(
                "Replace placeholder heading '{heading}' with actual content heading."
            ),
        )
        self._registry[("heading_quality", "numbered_only")] = InterpretationEntry(
            severity=Severity.CRITICAL,
            score_penalty=20,
            ai_impact=(
                "A numbered-only heading provides no semantic signal. This section "
                "will not be retrieved for any query."
            ),
            recommendation_template=(
                "Replace numbered-only heading '{heading}' with a descriptive "
                "heading that includes the action or topic."
            ),
        )
        self._registry[("heading_quality", "too_short")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "A single-word heading provides weak semantic signal for "
                "retrieval systems."
            ),
            recommendation_template=(
                "Heading '{heading}' is too short. Use a more descriptive heading "
                "(2+ words recommended)."
            ),
        )

        # --- context_independence ---
        self._registry[("context_independence", "dangling_reference")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "When this paragraph is retrieved as an independent chunk, "
                "references to 'above', 'below', or 'previous section' are "
                "unresolvable. The LLM will either ignore this content or "
                "hallucinate the missing context."
            ),
            recommendation_template=(
                "Rewrite {dangling_reference_count} paragraph(s) to be "
                "self-contained. Avoid references to 'above', 'below', "
                "'previous section', etc."
            ),
        )
        self._registry[("context_independence", "undefined_entity")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "When this paragraph is retrieved alone, pronouns like 'this' "
                "or 'it' lack antecedents, making the chunk ambiguous."
            ),
            recommendation_template=(
                "Rewrite paragraph to avoid undefined pronouns that require "
                "surrounding context."
            ),
        )

        # --- link_integrity ---
        self._registry[("link_integrity", "broken_link")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "An AI agent following this link reaches a 404, wasting a "
                "tool-call round-trip. In multi-step reasoning, the agent may "
                "conclude the information doesn't exist."
            ),
            recommendation_template="Fix {broken_link_count} broken link(s) in this document.",
        )
        self._registry[("link_integrity", "orphan")] = InterpretationEntry(
            severity=Severity.LOW,
            score_penalty=5,
            ai_impact=(
                "This document is not linked from any other document and is not "
                "in the navigation hierarchy. An AI agent traversing the KB will "
                "never discover it."
            ),
            recommendation_template=(
                "This document is not linked from any other document and is not "
                "in the navigation hierarchy. Consider adding cross-references."
            ),
        )

        # --- canonical_source ---
        self._registry[("canonical_source", "missing_canonical_source")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "This artifact references content without identifying the canonical "
                "source. An AI agent cannot verify the information or trace it to "
                "its origin, reducing trustworthiness of retrieved chunks."
            ),
            recommendation_template=(
                "Add canonical source attribution to this artifact. "
                "Reference the authoritative document or URL for the claims made."
            ),
        )
        self._registry[("canonical_source", "duplicate_content")] = InterpretationEntry(
            severity=Severity.HIGH,
            score_penalty=15,
            ai_impact=(
                "Duplicate content across artifacts means an AI agent may retrieve "
                "multiple chunks with the same information, wasting context window "
                "tokens and increasing the risk of inconsistent answers if one "
                "copy is updated and the other is not."
            ),
            recommendation_template=(
                "Consolidate duplicate content into a single canonical artifact. "
                "Replace other copies with cross-references to the canonical source."
            ),
        )

        # --- contradiction_detection ---
        self._registry[("contradiction_detection", "version_conflict")] = InterpretationEntry(
            severity=Severity.HIGH,
            score_penalty=15,
            ai_impact=(
                "Version conflicts mean different artifacts claim different versions "
                "of the same subject. An AI agent retrieving one version will give "
                "outdated or incorrect answers, with no way to detect the conflict."
            ),
            recommendation_template=(
                "Resolve version conflict for {subject}: artifacts claim versions "
                "{conflicting_versions}. Update all artifacts to the current version "
                "and mark older versions as deprecated."
            ),
        )

        # --- freshness ---
        self._registry[("freshness", "stale_content")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "This content has not been updated in a long time. An AI agent may "
                "retrieve outdated information and present it as current, leading "
                "to incorrect recommendations."
            ),
            recommendation_template=(
                "Review this content for accuracy. Update or mark it with a "
                "stale date warning if the information is still valid but may "
                "change. Last updated: {last_updated}"
            ),
        )
        self._registry[("freshness", "no_date_signal")] = InterpretationEntry(
            severity=Severity.LOW,
            score_penalty=5,
            ai_impact=(
                "This artifact has no date metadata. An AI agent cannot determine "
                "whether the content is current or stale, reducing confidence in "
                "the information."
            ),
            recommendation_template=(
                "Add a date or last-updated metadata field to this artifact so "
                "freshness can be assessed."
            ),
        )

        # --- knowledge_connectivity ---
        self._registry[("knowledge_connectivity", "orphan_artifact")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "This artifact is not connected to the knowledge graph. An AI agent "
                "traversing relationships between documents will never reach it, "
                "making it effectively invisible to graph-based retrieval."
            ),
            recommendation_template=(
                "Add relationships from this artifact to related documents, "
                "and ensure at least one other artifact links to it."
            ),
        )

        # --- terminology_consistency ---
        self._registry[("terminology_consistency", "terminology_variant")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "Inconsistent terminology means the same concept is referred to "
                "by different names. An AI agent embedding queries for one term "
                "will miss content using the variant term, reducing recall."
            ),
            recommendation_template=(
                "Standardize terminology: use {canonical_term} instead of "
                "{variant_terms} throughout the knowledge base."
            ),
        )

        # --- workflow_completeness ---
        self._registry[("workflow_completeness", "incomplete_workflow")] = InterpretationEntry(
            severity=Severity.MEDIUM,
            score_penalty=10,
            ai_impact=(
                "This workflow documentation is incomplete — it references steps, "
                "prerequisites, or outcomes that are not documented elsewhere. An "
                "AI agent following this workflow will get stuck or hallucinate "
                "the missing steps."
            ),
            recommendation_template=(
                "Complete this workflow documentation. Add the missing steps, "
                "link to prerequisite documents, and verify all referenced "
                "procedures exist."
            ),
        )

    def validate(self) -> list[str]:
        """Validate that all entries are well-formed. Return list of errors (empty if valid)."""
        errors: list[str] = []
        for (collector_id, signal_type), entry in self._registry.items():
            if not collector_id or not signal_type:
                errors.append("Empty collector_id or signal_type in registry entry")
            if not entry.ai_impact or not entry.recommendation_template:
                errors.append(
                    f"({collector_id}, {signal_type}) missing ai_impact or recommendation_template"
                )
        return errors
