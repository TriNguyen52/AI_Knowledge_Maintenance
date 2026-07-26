"""Scan pipeline - runs collectors against a KB and aggregates results into a snapshot.

ScanPipeline is now a facade that delegates to CollectOperation and
AssessOperation. The collect phase runs collectors and produces raw signals.
The assess phase applies interpretation policy, aggregates dimensions, and
computes the overall score.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_ready.evaluation_policy import EvaluationPolicy, InterpretationPolicy
from ai_ready.models import DimensionScore, DocumentRelation, Finding, KnowledgeBase, RuleResult, Severity, Snapshot
from ai_ready.rules import Rule, SignalCollector, all_rules, all_collectors
from ai_ready.operations import CollectOperation, AssessOperation

# Default dimension weights
DEFAULT_WEIGHTS: dict[str, float] = {
    "retrieval": 0.25,
    "context": 0.15,
    "consistency": 0.20,
    "trust": 0.20,
    "connectivity": 0.10,
    "workflow": 0.10,
}

# Default thresholds
DEFAULT_THRESHOLDS = {
    "overall_score": 0,
}

DEFAULT_FAIL_ON: list[str] = ["CRITICAL"]


class ScanPipeline:
    """Orchestrates collector execution, dimension aggregation, and snapshot creation.

    Delegates to CollectOperation (run collectors -> raw signals) and
    AssessOperation (apply policy -> aggregate dimensions -> compute score).
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        enabled_rules: list[str] | None = None,
        thresholds: dict[str, Any] | None = None,
        fail_on: list[str] | None = None,
    ) -> None:
        self.weights = weights if weights is not None else DEFAULT_WEIGHTS
        self.thresholds = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
        self.fail_on = fail_on if fail_on is not None else DEFAULT_FAIL_ON
        self.enabled_rules = enabled_rules
        self.policy = EvaluationPolicy()

        # Create delegate operations
        self._collect_op = CollectOperation(enabled_collectors=enabled_rules)
        self._assess_op = AssessOperation(weights=self.weights, policy=self.policy)

    def run(
        self,
        documents: list,
        source: str = "",
        git_commit: str = "",
        relations: list[DocumentRelation] | None = None,
    ) -> Snapshot:
        """Run all enabled collectors and produce a snapshot.

        Delegates to CollectOperation then AssessOperation.

        Args:
            documents: List of Document objects to analyze.
            source: Source path string for metadata.
            git_commit: Git commit hash for metadata.
            relations: Optional list of DocumentRelation objects for document relationships.
        """
        # Collect phase: run collectors -> raw signals
        results, all_signals, kb = self._collect_op.run(
            documents=documents,
            relations=relations,
            source=source,
        )

        # Assess phase: signals -> assessed findings -> aggregate -> score
        return self._assess_op.run(
            results=results,
            signals=all_signals,
            documents=documents,
            kb=kb,
            source=source,
            git_commit=git_commit,
        )

    def run_incremental(
        self,
        prev_snapshot: Snapshot,
        change_events: list,
        documents: list,
        relations: list[DocumentRelation] | None = None,
        source: str = "",
        git_commit: str = "",
    ) -> Snapshot:
        """Run an incremental scan, updating only affected findings.

        Delegates to IncrementalExecutor, reusing this pipeline's helper
        methods for policy application, dimension aggregation, and score
        computation.

        Args:
            prev_snapshot: The previous complete snapshot.
            change_events: List of ChangeEvent objects since the previous scan.
            documents: ALL current Document objects.
            relations: ALL current DocumentRelation objects.
            source: Source path string for metadata.
            git_commit: Git commit hash for metadata.

        Returns:
            A Snapshot identical to what a full scan would produce.
        """
        from ai_ready.incremental import IncrementalExecutor

        executor = IncrementalExecutor(self)
        return executor.run(
            prev_snapshot=prev_snapshot,
            change_events=change_events,
            all_documents=documents,
            relations=relations or [],
            source=source,
            git_commit=git_commit,
        )

    # --- Delegate methods (backward compatibility) ---

    def aggregate_dimensions(
        self, results: list[RuleResult], findings: list[Finding]
    ) -> dict[str, DimensionScore]:
        """Aggregate collector results into dimension scores. Delegates to AssessOperation."""
        return self._assess_op.aggregate_dimensions(results, findings)

    def apply_policy(self, findings: list[Finding]) -> None:
        """Populate severity, score, ai_impact, and recommendation from policy. Delegates to AssessOperation."""
        self._assess_op.apply_policy(findings)

    def compute_overall_score(self, dimensions: dict[str, DimensionScore]) -> int:
        """Compute weighted average of dimension scores. Delegates to AssessOperation."""
        return self._assess_op.compute_overall_score(dimensions)

    def get_exit_code(self, snapshot: Snapshot) -> int:
        """Determine exit code based on snapshot results."""
        fail_severities = set()
        for s in self.fail_on:
            try:
                fail_severities.add(Severity(s))
            except ValueError:
                pass

        has_fail_severity = any(
            f.severity in fail_severities for f in snapshot.findings
        )
        if has_fail_severity:
            return 2

        threshold = self.thresholds.get("overall_score", 0)
        if snapshot.score < threshold:
            return 1

        return 0
