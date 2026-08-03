"""Canonical Source collector — detect missing or duplicate canonical sources.

Flags artifacts that:
1. Discuss a topic without linking to an authoritative/canonical source
   (e.g., a tutorial about FastAPI that doesn't link to the official docs)
2. Are duplicate content (same or very similar text appearing in multiple artifacts)

This is a deterministic, LLM-free collector.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ai_ready.models import (
    ArtifactBundle,
    CollectorResult,
    KnowledgeSignal,
    Severity,
)
from ai_ready.rules import SignalCollector, register_collector

# Known canonical source domains
_CANONICAL_DOMAINS = [
    "docs.python.org",
    "fastapi.tiangolo.com",
    "developer.mozilla.org",
    "nodejs.org",
    "react.dev",
    "typescriptlang.org",
    "postgresql.org",
    "mongodb.com",
    "redis.io",
    "docker.com",
    "kubernetes.io",
    "cloud.google.com",
    "aws.amazon.com",
    "azure.microsoft.com",
    "github.com",
]

# Topic keywords that suggest the artifact discusses a specific technology
_TOPIC_KEYWORDS = {
    "python": "docs.python.org",
    "fastapi": "fastapi.tiangolo.com",
    "mdn": "developer.mozilla.org",
    "node": "nodejs.org",
    "nodejs": "nodejs.org",
    "react": "react.dev",
    "typescript": "typescriptlang.org",
    "postgresql": "postgresql.org",
    "postgres": "postgresql.org",
    "mongodb": "mongodb.com",
    "redis": "redis.io",
    "docker": "docker.com",
    "kubernetes": "kubernetes.io",
    "gcp": "cloud.google.com",
    "aws": "aws.amazon.com",
    "azure": "azure.microsoft.com",
}


@register_collector
class CanonicalSourceCollector(SignalCollector):
    """Detect artifacts missing canonical source links and duplicate content."""

    id = "canonical_source"
    severity_default = Severity.LOW
    dimension = "trust"
    scope = "cross_artifact"

    def collect(self, bundle: ArtifactBundle) -> dict[str, Any]:
        """Check each artifact for canonical source links and compute similarity."""
        missing_canonical: list[dict[str, Any]] = []
        external_links: dict[str, set[str]] = defaultdict(set)  # domain -> artifact URIs

        for artifact in bundle.artifacts:
            text = artifact.all_text if hasattr(artifact, "all_text") else ""
            if not text:
                text = " ".join(p.text for p in artifact.paragraphs)
            text_lower = text.lower()

            # Collect external links
            for link in artifact.links:
                if not link.is_internal:
                    domain = self._extract_domain(link.target)
                    if domain:
                        external_links[domain].add(artifact.uri)

            # Check for topic keywords without canonical source link
            for keyword, canonical_domain in _TOPIC_KEYWORDS.items():
                if keyword in text_lower:
                    # Check if this artifact links to the canonical domain
                    has_canonical = any(
                        canonical_domain in link.target
                        for link in artifact.links
                        if not link.is_internal
                    )
                    if not has_canonical:
                        # Also check if canonical domain is in external_links
                        has_canonical = artifact.uri in external_links.get(canonical_domain, set())
                    if not has_canonical:
                        missing_canonical.append({
                            "artifact_uri": artifact.uri,
                            "artifact_id": artifact.id,
                            "topic_keyword": keyword,
                            "canonical_domain": canonical_domain,
                        })

        # Duplicate detection: compare first 200 chars of each artifact
        content_signatures: dict[str, list[str]] = defaultdict(list)
        for artifact in bundle.artifacts:
            text = artifact.all_text if hasattr(artifact, "all_text") else ""
            if not text:
                text = " ".join(p.text for p in artifact.paragraphs)
            # Use first 200 chars as a signature
            sig = re.sub(r"\s+", " ", text[:200]).strip().lower()
            if len(sig) > 50:  # Only check substantial content
                content_signatures[sig].append(artifact.uri)

        duplicates: list[dict[str, Any]] = []
        for sig, uris in content_signatures.items():
            if len(uris) > 1:
                duplicates.append({
                    "artifact_uris": uris,
                    "signature": sig[:100],
                })

        return {
            "missing_canonical": missing_canonical,
            "duplicates": duplicates,
            "external_link_domains": dict(external_links),
            "total_artifacts": len(bundle.artifacts),
        }

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        return {
            "missing_canonical": signals["missing_canonical"],
            "duplicates": signals["duplicates"],
            "missing_count": len(signals["missing_canonical"]),
            "duplicate_groups": len(signals["duplicates"]),
            "duplicate_artifact_count": sum(len(d["artifact_uris"]) for d in signals["duplicates"]),
            "external_domains": len(signals["external_link_domains"]),
            "total_artifacts": signals["total_artifacts"],
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[KnowledgeSignal]:
        signals: list[KnowledgeSignal] = []

        for item in metrics["missing_canonical"]:
            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type="missing_canonical_source",
                severity=Severity.LOW,
                score=0,
                artifact_id=item["artifact_id"],
                artifact_uri=item["artifact_uri"],
                evidence={
                    "topic_keyword": item["topic_keyword"],
                    "canonical_domain": item["canonical_domain"],
                },
                recommendation="",
            ))

        for dup in metrics["duplicates"]:
            for uri in dup["artifact_uris"]:
                signals.append(KnowledgeSignal(
                    collector_id=self.id,
                    signal_type="duplicate_content",
                    severity=Severity.LOW,
                    score=0,
                    artifact_id=uri,
                    artifact_uri=uri,
                    evidence={
                        "duplicate_uris": dup["artifact_uris"],
                        "signature": dup["signature"],
                    },
                    recommendation="",
                ))

        return signals

    def report(self) -> CollectorResult:
        missing = self._metrics.get("missing_count", 0)
        dup_groups = self._metrics.get("duplicate_groups", 0)
        total = self._metrics.get("total_artifacts", 1)
        score = max(0, 100 - missing * 3 - dup_groups * 10)
        severity = (
            Severity.HIGH if dup_groups > 3
            else Severity.MEDIUM if missing > 0 or dup_groups > 0
            else Severity.LOW
        )
        return CollectorResult(
            collector_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "missing_canonical_sources": missing,
                "duplicate_groups": dup_groups,
                "duplicate_artifacts": self._metrics.get("duplicate_artifact_count", 0),
                "external_domains_linked": self._metrics.get("external_domains", 0),
                "total_artifacts": total,
            },
            signals=self._findings,
            recommendation=(
                f"{missing} missing canonical source(s), {dup_groups} duplicate group(s)"
                if missing or dup_groups
                else "All artifacts have canonical sources"
            ),
        )

    @staticmethod
    def _extract_domain(url: str) -> str | None:
        """Extract domain from a URL."""
        # Remove protocol
        url = re.sub(r"^https?://", "", url)
        # Remove path
        domain = url.split("/")[0]
        # Remove port
        domain = domain.split(":")[0]
        return domain if domain else None
