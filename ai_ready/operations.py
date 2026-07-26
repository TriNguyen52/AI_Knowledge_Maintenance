"""Collect and Assess operations — extracted from ScanPipeline.

CollectOperation runs collectors against a knowledge base and produces
raw KnowledgeSignals (bare facts without interpretation).

AssessOperation converts signals into AssessedSignals (Findings) by
applying interpretation policy, then aggregates dimensions and computes
the overall score to produce a snapshot (assessment during migration).

ScanPipeline.run() delegates to CollectOperation then AssessOperation.
This is a facade extraction — no behavior change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ai_ready.evaluation_policy import InterpretationPolicy
from ai_ready.models import (
    AssessedSignal,
    DimensionScore,
    DocumentRelation,
    Finding,
    KnowledgeBase,
    KnowledgeSignal,
    RegressionReport,
    RuleResult,
    Severity,
    Snapshot,
    _severity_rank,
)
from ai_ready.rules import SignalCollector, all_collectors


class CollectOperation:
    """Runs collectors against a knowledge base and produces raw signals.

    Extracted from ScanPipeline.run() — the collection phase.

    Collectors (rules) still emit Finding objects internally for backward
    compatibility. CollectOperation converts them to KnowledgeSignal
    (bare facts) as the primary output. The assessment layer converts
    signals back to AssessedSignal (Finding) and enriches them with
    interpretation via InterpretationPolicy.
    """

    def __init__(self, enabled_collectors: list[str] | None = None) -> None:
        self.enabled_collectors = enabled_collectors

    def run(
        self,
        documents: list,
        relations: list[DocumentRelation] | None = None,
        source: str = "",
    ) -> tuple[list[RuleResult], list[KnowledgeSignal], KnowledgeBase]:
        """Run all enabled collectors and return results, signals, and the KB.

        Returns:
            A tuple of (collector_results, all_signals, knowledge_base).
            collector_results still contain Finding objects for backward
            compatibility, but all_signals are the canonical output.
        """
        kb = KnowledgeBase(
            documents=documents,
            relations=relations or [],
            source=source,
        )

        registry = all_collectors()
        if self.enabled_collectors:
            collector_ids = [c for c in self.enabled_collectors if c in registry]
        else:
            collector_ids = list(registry.keys())

        results: list[RuleResult] = []
        all_signals: list[KnowledgeSignal] = []
        for collector_id in collector_ids:
            collector_cls = registry[collector_id]
            collector = collector_cls()
            result = collector.run(kb)
            results.append(result)
            # Convert findings to signals (strip interpretation)
            all_signals.extend(f.to_signal() for f in result.findings)

        return results, all_signals, kb


class AssessOperation:
    """Applies interpretation policy, aggregates dimensions, computes score.

    Extracted from ScanPipeline.run() — the assessment phase.

    Converts KnowledgeSignals to AssessedSignals (Findings) by applying
    InterpretationPolicy, then aggregates dimensions and computes the
    overall score to produce a Snapshot.
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        policy: InterpretationPolicy | None = None,
    ) -> None:
        from ai_ready.pipeline import DEFAULT_WEIGHTS
        self.weights = weights if weights is not None else DEFAULT_WEIGHTS
        self.policy = policy if policy is not None else InterpretationPolicy()

    def run(
        self,
        results: list[RuleResult],
        signals: list[KnowledgeSignal],
        documents: list,
        kb: KnowledgeBase,
        source: str = "",
        git_commit: str = "",
    ) -> Snapshot:
        """Convert signals to assessed findings, aggregate, and produce a snapshot.

        Args:
            results: Collector results from CollectOperation.
            signals: Bare KnowledgeSignals from CollectOperation.
            documents: Original document list (for metadata).
            kb: KnowledgeBase from CollectOperation.
            source: Source path string for metadata.
            git_commit: Git commit hash for metadata.

        Returns:
            A Snapshot with assessed findings, dimension scores, and overall score.
        """
        # Convert signals to AssessedSignals (Findings) with placeholder interpretation
        assessed_signals = self.signals_to_assessed(signals)

        # Apply policy to populate severity, score, ai_impact, recommendation
        self.apply_policy(assessed_signals)

        # Aggregate into dimensions
        dimensions = self.aggregate_dimensions(results, assessed_signals)

        # Compute overall score
        overall_score = self.compute_overall_score(dimensions)

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
            findings=assessed_signals,
            metrics=metrics,
            metadata=metadata,
        )

    @staticmethod
    def signals_to_assessed(signals: list[KnowledgeSignal]) -> list[AssessedSignal]:
        """Convert bare KnowledgeSignals to AssessedSignals with placeholder interpretation.

        The placeholders (severity=LOW, score=100, empty recommendation/ai_impact)
        are enriched by apply_policy() afterwards.
        """
        return [Finding.from_signal(s) for s in signals]

    def aggregate_dimensions(
        self, results: list[RuleResult], findings: list[Finding]
    ) -> dict[str, DimensionScore]:
        """Aggregate collector results into dimension scores.

        Collector scores are recomputed from policy-assessed findings rather than
        the placeholder scores emitted by collectors.
        """
        dim_results: dict[str, list[RuleResult]] = {}
        for r in results:
            collector_cls = all_collectors().get(r.rule_id)
            if collector_cls:
                dim = collector_cls.dimension
                dim_results.setdefault(dim, []).append(r)

        dimensions: dict[str, DimensionScore] = {}
        for dim_name, dim_results_list in dim_results.items():
            # Recompute collector scores from assessed findings
            collector_scores = []
            for r in dim_results_list:
                collector_findings = [f for f in findings if f.rule_id == r.rule_id]
                if collector_findings:
                    total_penalty = sum(100 - f.score for f in collector_findings)
                    collector_score = max(0, 100 - total_penalty)
                else:
                    collector_score = 100
                collector_scores.append(collector_score)

            avg_score = int(sum(collector_scores) / len(collector_scores)) if collector_scores else 100
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

    def apply_policy(self, findings: list[Finding]) -> None:
        """Populate severity, score, ai_impact, and recommendation from policy."""
        for finding in findings:
            entry = self.policy.lookup(finding.rule_id, finding.issue_type)
            if entry:
                finding.severity = entry.severity
                finding.score = 100 - entry.score_penalty
                finding.ai_impact = entry.ai_impact
                finding.recommendation = entry.recommendation_template.format(
                    **finding.evidence
                )
            else:
                finding.ai_impact = "No AI impact description available."
                finding.recommendation = f"Issue: {finding.issue_type}"

    def compute_overall_score(self, dimensions: dict[str, DimensionScore]) -> int:
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


class DiffOperation:
    """Computes diffs between two snapshots or assessments.

    Extracted from SnapshotStore._compute_diff — the diff/regression logic
    is pure computation that doesn't need DB access. This makes it testable
    in isolation and reusable by both SnapshotStore and AssessmentStore.
    """

    @staticmethod
    def compute_diff(prev: Snapshot, curr: Snapshot) -> RegressionReport:
        """Compute regression report between two snapshots using stable finding IDs.

        This is the same logic previously in SnapshotStore._compute_diff,
        extracted as a standalone operation.
        """
        prev_by_id = {f.finding_id: f for f in prev.findings}
        curr_by_id = {f.finding_id: f for f in curr.findings}

        prev_ids = set(prev_by_id.keys())
        curr_ids = set(curr_by_id.keys())

        new_findings = [curr_by_id[fid] for fid in curr_ids - prev_ids]
        resolved_findings = [prev_by_id[fid] for fid in prev_ids - curr_ids]
        persistent_findings = [curr_by_id[fid] for fid in curr_ids & prev_ids]

        # Detect severity changes
        severity_changes: list[dict[str, Any]] = []
        for fid in curr_ids & prev_ids:
            pf = prev_by_id[fid]
            cf = curr_by_id[fid]
            if pf.severity != cf.severity:
                severity_changes.append({
                    "finding_id": fid,
                    "rule_id": cf.rule_id,
                    "document_path": cf.document_path,
                    "prev_severity": pf.severity.value,
                    "curr_severity": cf.severity.value,
                    "direction": "worse" if _severity_rank(cf.severity) > _severity_rank(pf.severity) else "better",
                })

        # Dimension deltas
        dim_deltas: dict[str, int] = {}
        for dim_name in set(list(prev.dimensions.keys()) + list(curr.dimensions.keys())):
            prev_score = prev.dimensions[dim_name].score if dim_name in prev.dimensions else 0
            curr_score = curr.dimensions[dim_name].score if dim_name in curr.dimensions else 0
            dim_deltas[dim_name] = curr_score - prev_score

        new_high = sum(1 for f in new_findings if f.severity in (Severity.HIGH, Severity.CRITICAL))

        # Metric deltas
        prev_contradictions = prev.metrics.get("contradiction_count", 0)
        curr_contradictions = curr.metrics.get("contradiction_count", 0)
        prev_entropy = prev.metrics.get("avg_topic_entropy", 0)
        curr_entropy = curr.metrics.get("avg_topic_entropy", 0)
        prev_orphans = prev.metrics.get("orphan_documents", 0)
        curr_orphans = curr.metrics.get("orphan_documents", 0)
        prev_dupes = prev.metrics.get("duplicate_clusters", 0)
        curr_dupes = curr.metrics.get("duplicate_clusters", 0)

        contributor_changes = DiffOperation._compute_contributor_changes(prev, curr)
        dimension_change_explanations = [
            {
                "dimension": name,
                "prev_score": prev.dimensions[name].score if name in prev.dimensions else None,
                "curr_score": curr.dimensions[name].score if name in curr.dimensions else None,
                "delta": delta,
                "direction": "worse" if delta < 0 else "better" if delta > 0 else "unchanged",
            }
            for name, delta in sorted(
                dim_deltas.items(),
                key=lambda item: (abs(item[1]), item[0]),
                reverse=True,
            )
            if delta != 0
        ]

        # Build explanation
        explanation_parts: list[str] = []
        if new_findings:
            explanation_parts.append(f"{len(new_findings)} new findings")
        if resolved_findings:
            explanation_parts.append(f"{len(resolved_findings)} resolved")
        if severity_changes:
            worse = sum(1 for s in severity_changes if s["direction"] == "worse")
            better = sum(1 for s in severity_changes if s["direction"] == "better")
            if worse:
                explanation_parts.append(f"{worse} findings got worse")
            if better:
                explanation_parts.append(f"{better} findings improved")
        top_regression = next(
            (c for c in contributor_changes if c["delta_estimated_score_gain"] > 0),
            None,
        )
        if top_regression:
            explanation_parts.append(
                f"largest new score pressure: {top_regression['cause']}"
            )
        if not explanation_parts:
            explanation_parts.append("No changes detected")
        explanation = "; ".join(explanation_parts) + "."

        score_change_explanation = {
            "summary": explanation,
            "score_delta": curr.score - prev.score,
            "dimension_changes": dimension_change_explanations,
            "top_regressions": [
                c for c in contributor_changes
                if c["delta_estimated_score_gain"] > 0
            ][:5],
            "top_improvements": [
                c for c in contributor_changes
                if c["delta_estimated_score_gain"] < 0
            ][:5],
        }

        recommendation = ""
        if curr.score < prev.score:
            recommendation = f"Score dropped {prev.score} -> {curr.score}. Review {len(new_findings)} new findings before deployment."
        elif new_high > 0:
            recommendation = f"{new_high} new high-severity findings. Review before deployment."
        elif len(resolved_findings) > 0 and not new_findings:
            recommendation = f"Improving. {len(resolved_findings)} findings resolved, no new issues."
        elif not new_findings:
            recommendation = "No regressions detected."

        return RegressionReport(
            prev_snapshot_id=prev.snapshot_id,
            curr_snapshot_id=curr.snapshot_id,
            prev_score=prev.score,
            curr_score=curr.score,
            score_delta=curr.score - prev.score,
            new_findings=new_findings,
            resolved_findings=resolved_findings,
            persistent_findings=persistent_findings,
            severity_changes=severity_changes,
            dimension_deltas=dim_deltas,
            new_high_count=new_high,
            increased_contradictions=max(0, curr_contradictions - prev_contradictions),
            increased_topic_entropy=max(0, curr_entropy - prev_entropy),
            new_orphan_documents=max(0, curr_orphans - prev_orphans),
            new_duplicate_clusters=max(0, curr_dupes - prev_dupes),
            score_change_explanation=score_change_explanation,
            contributor_changes=contributor_changes[:10],
            recommendation=recommendation,
            explanation=explanation,
        )

    @staticmethod
    def _compute_contributor_changes(
        prev: Snapshot,
        curr: Snapshot,
    ) -> list[dict[str, Any]]:
        """Compare derived score contributors between two snapshots."""
        prev_contributors = {
            c.key: c for c in prev.explain_score(max_contributors=50).dominant_contributors
        }
        curr_contributors = {
            c.key: c for c in curr.explain_score(max_contributors=50).dominant_contributors
        }

        changes: list[dict[str, Any]] = []
        for key in set(prev_contributors) | set(curr_contributors):
            prev_c = prev_contributors.get(key)
            curr_c = curr_contributors.get(key)
            representative = curr_c or prev_c
            if representative is None:
                continue

            prev_gain = prev_c.estimated_score_gain if prev_c else 0.0
            curr_gain = curr_c.estimated_score_gain if curr_c else 0.0
            prev_count = prev_c.finding_count if prev_c else 0
            curr_count = curr_c.finding_count if curr_c else 0
            prev_ids = set(prev_c.finding_ids if prev_c else [])
            curr_ids = set(curr_c.finding_ids if curr_c else [])

            delta = curr_gain - prev_gain
            if delta == 0 and curr_count == prev_count:
                continue

            changes.append({
                "key": key,
                "dimension": representative.dimension,
                "rule": representative.rule_id,
                "issue_type": representative.issue_type,
                "cause": representative.cause,
                "prev_estimated_score_gain": round(prev_gain, 2),
                "curr_estimated_score_gain": round(curr_gain, 2),
                "delta_estimated_score_gain": round(delta, 2),
                "prev_findings": prev_count,
                "curr_findings": curr_count,
                "finding_count_delta": curr_count - prev_count,
                "added_finding_ids": sorted(curr_ids - prev_ids)[:20],
                "removed_finding_ids": sorted(prev_ids - curr_ids)[:20],
            })

        return sorted(
            changes,
            key=lambda c: (
                abs(c["delta_estimated_score_gain"]),
                abs(c["finding_count_delta"]),
            ),
            reverse=True,
        )
