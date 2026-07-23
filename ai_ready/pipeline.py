"""Scan pipeline - runs rules against a KB and aggregates results into a snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_ready.evaluation_policy import EvaluationPolicy
from ai_ready.models import DimensionScore, DocumentRelation, Finding, KnowledgeBase, RuleResult, Severity, Snapshot
from ai_ready.rules import Rule, all_rules

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
    """Orchestrates rule execution, dimension aggregation, and snapshot creation."""

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

    def run(
        self,
        documents: list,
        source: str = "",
        git_commit: str = "",
        relations: list[DocumentRelation] | None = None,
    ) -> Snapshot:
        """Run all enabled rules and produce a snapshot.

        Args:
            documents: List of Document objects to analyze.
            source: Source path string for metadata.
            git_commit: Git commit hash for metadata.
            relations: Optional list of DocumentRelation objects for document relationships.
        """
        # Build knowledge base
        kb = KnowledgeBase(
            documents=documents,
            relations=relations or [],
            source=source,
        )

        # Get available rules
        registry = all_rules()
        if self.enabled_rules:
            rule_ids = [r for r in self.enabled_rules if r in registry]
        else:
            rule_ids = list(registry.keys())

        # Run each rule
        results: list[RuleResult] = []
        all_findings: list[Finding] = []
        for rule_id in rule_ids:
            rule_cls = registry[rule_id]
            rule = rule_cls()
            result = rule.run(kb)
            results.append(result)
            all_findings.extend(result.findings)

        # Apply policy to all findings (enrich bare findings with severity, score, etc.)
        self._apply_policy(all_findings)

        # Aggregate into dimensions (recompute rule scores from assessed findings)
        dimensions = self._aggregate_dimensions(results, all_findings)

        # Compute overall score
        overall_score = self._compute_overall_score(dimensions)

        # Collect metrics
        metrics: dict[str, Any] = {}
        for r in results:
            metrics.update(r.metrics)

        # Create snapshot
        snapshot_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata: dict[str, Any] = {"source": source}
        if git_commit:
            metadata["git_commit"] = git_commit
        metadata["document_count"] = len(documents)
        metadata["relation_count"] = len(kb.relations)

        return Snapshot(
            snapshot_id=snapshot_id,
            score=overall_score,
            dimensions=dimensions,
            findings=all_findings,
            metrics=metrics,
            metadata=metadata,
        )

    def _aggregate_dimensions(
        self, results: list[RuleResult], findings: list[Finding]
    ) -> dict[str, DimensionScore]:
        """Aggregate rule results into dimension scores.

        Rule scores are recomputed from policy-assessed findings rather than
        the placeholder scores emitted by rules.
        """
        dim_results: dict[str, list[RuleResult]] = {}
        for r in results:
            rule_cls = all_rules().get(r.rule_id)
            if rule_cls:
                dim = rule_cls.dimension
                dim_results.setdefault(dim, []).append(r)

        dimensions: dict[str, DimensionScore] = {}
        for dim_name, dim_results_list in dim_results.items():
            # Recompute rule scores from assessed findings
            rule_scores = []
            for r in dim_results_list:
                rule_findings = [f for f in findings if f.rule_id == r.rule_id]
                if rule_findings:
                    # Rule score = 100 - sum of penalties from assessed findings
                    total_penalty = sum(100 - f.score for f in rule_findings)
                    rule_score = max(0, 100 - total_penalty)
                else:
                    rule_score = 100
                rule_scores.append(rule_score)

            avg_score = int(sum(rule_scores) / len(rule_scores)) if rule_scores else 100
            rule_ids = [r.rule_id for r in dim_results_list]
            findings_count = sum(
                len([f for f in findings if f.rule_id == r.rule_id]) for r in dim_results_list
            )

            dimensions[dim_name] = DimensionScore(
                name=dim_name,
                score=avg_score,
                rule_ids=rule_ids,
                findings_count=findings_count,
            )

        return dimensions

    def _apply_policy(self, findings: list[Finding]) -> None:
        """Populate severity, score, ai_impact, and recommendation from policy."""
        for finding in findings:
            entry = self.policy.lookup(finding.rule_id, finding.issue_type)
            if entry:
                finding.severity = entry.severity
                finding.score = 100 - entry.score_penalty
                finding.ai_impact = entry.ai_impact
                # Format recommendation template with evidence data
                finding.recommendation = entry.recommendation_template.format(
                    **finding.evidence
                )
            else:
                # Fallback if policy entry not found
                finding.ai_impact = "No AI impact description available."
                finding.recommendation = f"Issue: {finding.issue_type}"

    def _compute_overall_score(self, dimensions: dict[str, DimensionScore]) -> int:
        """Compute weighted average of dimension scores."""
        total_weight = 0.0
        weighted_sum = 0.0
        for dim_name, dim_score in dimensions.items():
            weight = self.weights.get(dim_name, 0)
            weighted_sum += dim_score.score * weight
            total_weight += weight

        if total_weight == 0:
            return 100
        return int(weighted_sum / total_weight)

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
