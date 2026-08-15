"""Contradiction Detection collector — detect conflicting factual claims.

Scans artifacts for common contradiction patterns:
1. Version numbers that disagree across artifacts (e.g., "requires Python 3.9" vs "requires Python 3.11")
2. Deprecation contradictions (deprecated in one doc, recommended in another)
3. Boolean contradictions (a feature is "enabled by default" in one doc, "disabled by default" in another)

This is a deterministic, LLM-free collector using pattern matching.
It does NOT attempt full semantic contradiction detection (which would
require an LLM), but catches common structural contradictions.
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

# Patterns for extracting factual claims
_VERSION_PATTERN = re.compile(
    r"(?:requires?|supports?|needs?|compatible with|minimum)\s+"
    r"(Python|Node\.js|Node|Java|Go|Rust|Ruby|PHP)\s+"
    r"(\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_DEPRECATION_PATTERN = re.compile(
    r"\b(deprecat\w+|removed|no longer supported|obsolete)\b",
    re.IGNORECASE,
)

_RECOMMEND_PATTERN = re.compile(
    r"\b(recommend\w+|should use|best practice|preferred)\b",
    re.IGNORECASE,
)

_BOOLEAN_PATTERN = re.compile(
    r"(enabled|disabled|on|off|true|false|yes|no)\s+by default",
    re.IGNORECASE,
)

_FEATURE_PATTERN = re.compile(
    r"([\w\s]{3,40}?)\s+(?:is|are)\s+(enabled|disabled|on|off|true|false)\s+by default",
    re.IGNORECASE,
)


@register_collector
class ContradictionDetectionCollector(SignalCollector):
    """Detect conflicting factual claims across artifacts."""

    id = "contradiction_detection"
    severity_default = Severity.MEDIUM
    dimension = "consistency"
    scope = "cross_artifact"

    def collect(self, bundle: ArtifactBundle) -> dict[str, Any]:
        """Extract factual claims from each artifact."""
        version_claims: list[dict[str, Any]] = []
        deprecation_claims: list[dict[str, Any]] = []
        boolean_claims: list[dict[str, Any]] = []

        for artifact in bundle.artifacts:
            text = artifact.all_text if hasattr(artifact, "all_text") else ""
            if not text:
                text = " ".join(p.text for p in artifact.paragraphs)

            # Version requirements
            for match in _VERSION_PATTERN.finditer(text):
                version_claims.append({
                    "artifact_uri": artifact.uri,
                    "artifact_id": artifact.id,
                    "subject": match.group(1).lower(),
                    "version": match.group(2),
                    "context": text[max(0, match.start() - 20):match.end() + 20],
                })

            # Deprecation mentions
            for match in _DEPRECATION_PATTERN.finditer(text):
                # Check if this artifact also recommends the same thing
                context = text[max(0, match.start() - 50):match.end() + 50]
                deprecation_claims.append({
                    "artifact_uri": artifact.uri,
                    "artifact_id": artifact.id,
                    "keyword": match.group(1).lower(),
                    "context": context,
                })

            # Boolean defaults
            for match in _FEATURE_PATTERN.finditer(text):
                boolean_claims.append({
                    "artifact_uri": artifact.uri,
                    "artifact_id": artifact.id,
                    "feature": match.group(1).strip().lower(),
                    "value": match.group(2).lower(),
                    "context": text[max(0, match.start() - 20):match.end() + 20],
                })

        return {
            "version_claims": version_claims,
            "deprecation_claims": deprecation_claims,
            "boolean_claims": boolean_claims,
            "total_artifacts": len(bundle.artifacts),
        }

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Find contradictions in extracted claims."""
        contradictions: list[dict[str, Any]] = []

        # Version contradictions: same subject, different versions
        version_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in signals["version_claims"]:
            version_by_subject[claim["subject"]].append(claim)

        for subject, claims in version_by_subject.items():
            versions = {c["version"] for c in claims}
            if len(versions) > 1:
                # Find the minimum version and flag artifacts with higher versions
                # as contradicting those with lower versions
                sorted_versions = sorted(versions, key=lambda v: [int(x) for x in v.split(".")])
                min_version = sorted_versions[0]
                for claim in claims:
                    if claim["version"] != min_version:
                        contradictions.append({
                            "type": "version_conflict",
                            "artifact_uri": claim["artifact_uri"],
                            "subject": subject,
                            "this_version": claim["version"],
                            "conflicting_versions": sorted_versions,
                            "context": claim["context"],
                        })

        # Deprecation vs recommendation contradictions
        # If an artifact mentions deprecation of X and another recommends X,
        # that's a contradiction. We check for overlapping topics.
        dep_artifacts = {c["artifact_uri"] for c in signals["deprecation_claims"]}
        for claim in signals["deprecation_claims"]:
            # Check if any other artifact recommends something in the same context
            # This is a simple heuristic — not a full semantic check
            context_words = set(re.findall(r"\w+", claim["context"].lower()))
            for artifact in self.artifacts:
                if artifact.uri == claim["artifact_uri"]:
                    continue
                text = artifact.all_text if hasattr(artifact, "all_text") else ""
                if not text:
                    text = " ".join(p.text for p in artifact.paragraphs)
                for rec_match in _RECOMMEND_PATTERN.finditer(text):
                    rec_context = text[max(0, rec_match.start() - 30):rec_match.end() + 30]
                    rec_words = set(re.findall(r"\w+", rec_context.lower()))
                    overlap = context_words & rec_words - {"the", "a", "an", "is", "are", "to", "and", "or", "in", "for", "of"}
                    if len(overlap) >= 2:
                        contradictions.append({
                            "type": "deprecation_vs_recommendation",
                            "artifact_uri": claim["artifact_uri"],
                            "conflicting_artifact_uri": artifact.uri,
                            "deprecation_keyword": claim["keyword"],
                            "overlap_terms": list(overlap)[:5],
                        })
                        break  # One contradiction per deprecation claim

        # Boolean contradictions: same feature, different default
        feature_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in signals["boolean_claims"]:
            feature_by_name[claim["feature"]].append(claim)

        for feature, claims in feature_by_name.items():
            values = {c["value"] for c in claims}
            # Normalize: enabled/on/true/yes vs disabled/off/false/no
            def _normalize_bool(v: str) -> str:
                return "enabled" if v in ("enabled", "on", "true", "yes") else "disabled"
            norm_values = {_normalize_bool(v) for v in values}
            if len(norm_values) > 1:
                for claim in claims:
                    contradictions.append({
                        "type": "boolean_default_conflict",
                        "artifact_uri": claim["artifact_uri"],
                        "feature": feature,
                        "this_value": claim["value"],
                        "all_values": list(values),
                        "context": claim["context"],
                    })

        return {
            "contradictions": contradictions,
            "contradiction_count": len(contradictions),
            "total_artifacts": signals["total_artifacts"],
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[KnowledgeSignal]:
        """Emit signals for contradictions."""
        signals: list[KnowledgeSignal] = []
        seen: set[str] = set()

        for c in metrics["contradictions"]:
            key = f"{c['artifact_uri']}:{c['type']}:{c.get('subject', c.get('feature', ''))}"
            if key in seen:
                continue
            seen.add(key)

            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type=c["type"],
                severity=Severity.LOW,
                score=0,
                artifact_id=c["artifact_uri"],
                artifact_uri=c["artifact_uri"],
                evidence={k: v for k, v in c.items() if k != "artifact_uri"},
                recommendation="",
            ))

        return signals

    def report(self) -> CollectorResult:
        count = self._metrics.get("contradiction_count", 0)
        total = self._metrics.get("total_artifacts", 1)
        score = max(0, 100 - count * 10)
        severity = (
            Severity.HIGH if count > 5
            else Severity.MEDIUM if count > 0
            else Severity.LOW
        )
        return CollectorResult(
            collector_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "contradictions": count,
                "total_artifacts": total,
            },
            signals=self._findings,
            recommendation=(
                f"{count} potential contradiction(s) detected"
                if count
                else "No contradictions detected"
            ),
        )
