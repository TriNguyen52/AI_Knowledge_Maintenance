"""Knowledge Connectivity collector — detect orphan artifacts with no inbound links.

Uses the relationship graph from the ArtifactBundle to identify artifacts
with in-degree 0 (no other artifact links to them or references them).
These are "orphan" documents that are unreachable from the rest of the
knowledge base, making them invisible to navigation and discovery.

This is a deterministic, LLM-free collector.
"""

from __future__ import annotations

from typing import Any

from ai_ready.models import (
    ArtifactBundle,
    CollectorResult,
    KnowledgeSignal,
    Severity,
)
from ai_ready.rules import SignalCollector, register_collector


@register_collector
class KnowledgeConnectivityCollector(SignalCollector):
    """Flag artifacts that have no inbound links (graph orphans)."""

    id = "knowledge_connectivity"
    severity_default = Severity.LOW
    dimension = "connectivity"
    scope = "cross_artifact"

    def collect(self, bundle: ArtifactBundle) -> dict[str, Any]:
        """Build in-degree map from relationships and inline links."""
        all_uris = {a.uri for a in bundle.artifacts}

        # Inbound links from relationships
        inbound_rel: dict[str, int] = {}
        for rel in bundle.relationships:
            if rel.target_uri in all_uris:
                inbound_rel[rel.target_uri] = inbound_rel.get(rel.target_uri, 0) + 1

        # Inbound links from inline links in artifact content
        inbound_link: dict[str, int] = {}
        for artifact in bundle.artifacts:
            for link in artifact.links:
                if not link.is_internal:
                    continue
                target = link.target.split("#")[0].split("?")[0]
                if target in all_uris:
                    inbound_link[target] = inbound_link.get(target, 0) + 1

        return {
            "all_uris": all_uris,
            "inbound_rel": inbound_rel,
            "inbound_link": inbound_link,
            "total_artifacts": len(bundle.artifacts),
            "total_relationships": len(bundle.relationships),
        }

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Identify artifacts with zero inbound links."""
        orphans: list[dict[str, Any]] = []
        for uri in signals["all_uris"]:
            in_rel = signals["inbound_rel"].get(uri, 0)
            in_link = signals["inbound_link"].get(uri, 0)
            total_in = in_rel + in_link
            if total_in == 0:
                orphans.append({
                    "artifact_uri": uri,
                    "inbound_relationships": in_rel,
                    "inbound_links": in_link,
                })

        return {
            "orphans": orphans,
            "total_artifacts": signals["total_artifacts"],
            "total_relationships": signals["total_relationships"],
            "orphan_count": len(orphans),
            "avg_inbound": (
                sum(signals["inbound_rel"].values()) + sum(signals["inbound_link"].values())
            ) / max(signals["total_artifacts"], 1),
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[KnowledgeSignal]:
        """Emit signals for orphan artifacts."""
        signals: list[KnowledgeSignal] = []
        for orphan in metrics["orphans"]:
            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type="orphan_artifact",
                severity=Severity.LOW,
                score=0,
                artifact_id=orphan["artifact_uri"],
                artifact_uri=orphan["artifact_uri"],
                evidence={
                    "inbound_relationships": orphan["inbound_relationships"],
                    "inbound_links": orphan["inbound_links"],
                    "total_inbound": 0,
                },
                recommendation="",
            ))
        return signals

    def report(self) -> CollectorResult:
        orphan_count = self._metrics.get("orphan_count", 0)
        total = self._metrics.get("total_artifacts", 1)
        score = max(0, 100 - int((orphan_count / max(total, 1)) * 100))
        severity = (
            Severity.HIGH if orphan_count > total * 0.3
            else Severity.MEDIUM if orphan_count > 0
            else Severity.LOW
        )
        return CollectorResult(
            collector_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "orphan_artifacts": orphan_count,
                "total_artifacts": total,
                "avg_inbound_degree": round(self._metrics.get("avg_inbound", 0), 2),
            },
            signals=self._findings,
            recommendation=(
                f"{orphan_count} orphan artifact(s) with no inbound links"
                if orphan_count
                else "All artifacts have inbound links"
            ),
        )
