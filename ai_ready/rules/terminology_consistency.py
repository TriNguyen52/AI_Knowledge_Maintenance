"""Terminology Consistency collector — detect inconsistent term variants.

Normalizes candidate terms (case, spacing, hyphenation) and when one
concept appears in multiple variant forms across artifacts, flags
artifacts using the minority variant. For example, if "OAuth2" appears
in 10 files and "OAuth 2.0" in 2 files, the 2 files using the minority
variant are flagged.

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

# Terms to check: common technical terms that often have variant spellings
# This is a curated list — not exhaustive, but covers common documentation terms
_CANDIDATE_TERMS = [
    "OAuth2", "OAuth 2.0", "oauth2", "oauth 2.0",
    "OpenAPI", "openapi", "Open API", "open-api",
    "RESTful", "REST API", "rest api", "restful",
    "JavaScript", "Javascript", "javascript", "Java Script",
    "TypeScript", "Typescript", "typescript", "Type Script",
    "PostgreSQL", "postgresql", "Postgres", "postgres",
    "MongoDB", "mongodb", "Mongo DB",
    "FastAPI", "Fast API", "fastapi", "fast-api",
    "WebSocket", "Websocket", "websocket", "Web Socket",
    "gRPC", "grpc", "GRPC",
    "CI/CD", "CI-CD", "cicd", "CICD",
    "Microservice", "Microservices", "microservice", "microservices",
    "API", "api", "Api",
]

# Build normalization map: normalize each term to a canonical key
def _normalize_term(term: str) -> str:
    """Normalize a term to its canonical form for grouping."""
    return re.sub(r"[\s\-_/]+", "", term.lower())


# Group terms by normalized form
_TERM_GROUPS: dict[str, list[str]] = defaultdict(list)
for term in _CANDIDATE_TERMS:
    key = _normalize_term(term)
    _TERM_GROUPS[key].append(term)


@register_collector
class TerminologyConsistencyCollector(SignalCollector):
    """Detect artifacts using minority term variants."""

    id = "terminology_consistency"
    severity_default = Severity.LOW
    dimension = "consistency"
    scope = "cross_artifact"

    def collect(self, bundle: ArtifactBundle) -> dict[str, Any]:
        """Scan all artifacts for term variants."""
        # For each normalized group, track which variant each artifact uses
        # term_usage[normalized_key][variant] = set of artifact URIs
        term_usage: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

        for artifact in bundle.artifacts:
            text = artifact.all_text if hasattr(artifact, "all_text") else ""
            if not text:
                # Build text from paragraphs
                text = " ".join(p.text for p in artifact.paragraphs)

            for group_key, variants in _TERM_GROUPS.items():
                for variant in variants:
                    # Use word-boundary matching
                    pattern = r"\b" + re.escape(variant) + r"\b"
                    if re.search(pattern, text, re.IGNORECASE if variant.islower() else 0):
                        term_usage[group_key][variant].add(artifact.uri)

        return {
            "term_usage": {
                k: {v: list(uris) for v, uris in variants.items()}
                for k, variants in term_usage.items()
            },
            "total_artifacts": len(bundle.artifacts),
        }

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Identify groups with multiple variants and find minority variants."""
        inconsistencies: list[dict[str, Any]] = []

        for group_key, variants in signals["term_usage"].items():
            if len(variants) < 2:
                continue  # Only one variant used — consistent

            # Find the majority variant (most artifacts)
            variant_counts = {v: len(uris) for v, uris in variants.items()}
            majority_variant = max(variant_counts, key=variant_counts.get)
            majority_count = variant_counts[majority_variant]
            total_usage = sum(variant_counts.values())

            for variant, uris in variants.items():
                if variant == majority_variant:
                    continue
                # Minority variant: used in fewer artifacts than majority
                for uri in uris:
                    inconsistencies.append({
                        "artifact_uri": uri,
                        "term_group": group_key,
                        "minority_variant": variant,
                        "majority_variant": majority_variant,
                        "minority_count": len(uris),
                        "majority_count": majority_count,
                        "total_variants": len(variants),
                    })

        return {
            "inconsistencies": inconsistencies,
            "inconsistency_count": len(inconsistencies),
            "total_artifacts": signals["total_artifacts"],
            "groups_checked": len(signals["term_usage"]),
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[KnowledgeSignal]:
        """Emit signals for artifacts with minority term variants."""
        signals: list[KnowledgeSignal] = []
        seen: set[str] = set()

        for inc in metrics["inconsistencies"]:
            key = f"{inc['artifact_uri']}:{inc['term_group']}"
            if key in seen:
                continue
            seen.add(key)

            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type="terminology_variant",
                severity=Severity.LOW,
                score=0,
                artifact_id=inc["artifact_uri"],
                artifact_uri=inc["artifact_uri"],
                evidence={
                    "term_group": inc["term_group"],
                    "minority_variant": inc["minority_variant"],
                    "majority_variant": inc["majority_variant"],
                    "minority_count": inc["minority_count"],
                    "majority_count": inc["majority_count"],
                },
                recommendation="",
            ))

        return signals

    def report(self) -> CollectorResult:
        count = self._metrics.get("inconsistency_count", 0)
        total = self._metrics.get("total_artifacts", 1)
        score = max(0, 100 - int((count / max(total, 1)) * 50))
        severity = (
            Severity.HIGH if count > total * 0.2
            else Severity.MEDIUM if count > 0
            else Severity.LOW
        )
        return CollectorResult(
            collector_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "terminology_inconsistencies": count,
                "groups_checked": self._metrics.get("groups_checked", 0),
                "total_artifacts": total,
            },
            signals=self._findings,
            recommendation=(
                f"{count} terminology variant(s) detected"
                if count
                else "Terminology is consistent"
            ),
        )
