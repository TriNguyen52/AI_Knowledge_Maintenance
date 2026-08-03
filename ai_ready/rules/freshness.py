"""Freshness collector — detect stale or undated artifacts.

Flags artifacts whose last-modified metadata or in-content date is older
than a configurable threshold (default 365 days), or that have no date
signal at all. Stale documentation may contain outdated information that
misleads AI systems.

This is a deterministic, LLM-free collector.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ai_ready.models import (
    ArtifactBundle,
    CollectorResult,
    KnowledgeSignal,
    Severity,
)
from ai_ready.rules import SignalCollector, register_collector

# Default staleness threshold in days
DEFAULT_STALENESS_DAYS = 365

# Patterns to find dates in content
_DATE_PATTERNS = [
    # ISO date: 2024-01-15 or 2024/01/15
    re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"),
    # Written: January 15, 2024 or Jan 15, 2024
    re.compile(r"\b(January|February|March|April|May|June|July|August|"
               r"September|October|November|December|"
               r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
               r"\d{1,2},?\s+(20\d{2})\b", re.IGNORECASE),
]


def _extract_date_from_content(artifact: Any) -> datetime | None:
    """Try to find a date in the artifact's content or metadata."""
    # Check metadata first
    meta = artifact.metadata if hasattr(artifact, "metadata") else {}
    if isinstance(meta, dict):
        for key in ("last_modified", "date", "updated", "created", "last_updated"):
            val = meta.get(key)
            if val:
                parsed = _parse_date_str(val)
                if parsed:
                    return parsed

    # Check content text for dates
    text = ""
    if hasattr(artifact, "all_text"):
        text = artifact.all_text
    elif hasattr(artifact, "paragraphs"):
        text = " ".join(p.text for p in artifact.paragraphs)

    # Look for dates in the first 2000 chars (headers/intro area)
    search_text = text[:2000]
    for pattern in _DATE_PATTERNS:
        match = pattern.search(search_text)
        if match:
            parsed = _parse_date_match(match)
            if parsed:
                return parsed

    return None


def _parse_date_str(val: Any) -> datetime | None:
    """Parse a date from a string or datetime value."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return None


def _parse_date_match(match: re.Match) -> datetime | None:
    """Parse a date from a regex match."""
    groups = match.groups()
    if len(groups) == 3:
        try:
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            return datetime(year, month, day)
        except (ValueError, IndexError):
            pass
    elif len(groups) == 2:
        # Month name + day + year
        month_names = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
            "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month_name = groups[0].lower()
        year = int(groups[1])
        # Extract day from the full match
        day_match = re.search(r"\d{1,2}", match.group(0))
        if day_match and month_name in month_names:
            day = int(day_match.group())
            try:
                return datetime(year, month_names[month_name], day)
            except ValueError:
                pass
    return None


@register_collector
class FreshnessCollector(SignalCollector):
    """Flag artifacts that are stale or have no date signal."""

    id = "freshness"
    severity_default = Severity.LOW
    dimension = "trust"
    scope = "per_artifact"

    def __init__(self, staleness_days: int = DEFAULT_STALENESS_DAYS) -> None:
        self._staleness_days = staleness_days
        self._now: datetime | None = None

    def collect(self, bundle: ArtifactBundle) -> dict[str, Any]:
        """Extract date signals from each artifact."""
        self._now = datetime.now(timezone.utc)
        dated: list[dict[str, Any]] = []
        undated: list[dict[str, Any]] = []

        for artifact in bundle.artifacts:
            date = _extract_date_from_content(artifact)
            if date is not None:
                # Normalize to naive for comparison
                if date.tzinfo is not None:
                    date = date.replace(tzinfo=None)
                age_days = (self._now.replace(tzinfo=None) - date).days
                dated.append({
                    "artifact_id": artifact.id,
                    "artifact_uri": artifact.uri,
                    "date": date.isoformat(),
                    "age_days": age_days,
                })
            else:
                undated.append({
                    "artifact_id": artifact.id,
                    "artifact_uri": artifact.uri,
                })

        return {
            "dated": dated,
            "undated": undated,
            "staleness_days": self._staleness_days,
            "total_artifacts": len(bundle.artifacts),
        }

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Classify artifacts as stale, fresh, or undated."""
        threshold = signals["staleness_days"]
        stale = [d for d in signals["dated"] if d["age_days"] > threshold]
        fresh = [d for d in signals["dated"] if d["age_days"] <= threshold]
        undated = signals["undated"]

        return {
            "stale": stale,
            "fresh": fresh,
            "undated": undated,
            "stale_count": len(stale),
            "fresh_count": len(fresh),
            "undated_count": len(undated),
            "total_artifacts": signals["total_artifacts"],
            "staleness_days": threshold,
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[KnowledgeSignal]:
        """Emit signals for stale and undated artifacts."""
        signals: list[KnowledgeSignal] = []

        for item in metrics["stale"]:
            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type="stale_content",
                severity=Severity.LOW,
                score=0,
                artifact_id=item["artifact_id"],
                artifact_uri=item["artifact_uri"],
                evidence={
                    "age_days": item["age_days"],
                    "date": item["date"],
                    "threshold_days": metrics["staleness_days"],
                },
                recommendation="",
            ))

        for item in metrics["undated"]:
            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type="no_date_signal",
                severity=Severity.LOW,
                score=0,
                artifact_id=item["artifact_id"],
                artifact_uri=item["artifact_uri"],
                evidence={"date_found": False},
                recommendation="",
            ))

        return signals

    def report(self) -> CollectorResult:
        stale_count = self._metrics.get("stale_count", 0)
        undated_count = self._metrics.get("undated_count", 0)
        total = self._metrics.get("total_artifacts", 1)
        score = max(0, 100 - int(((stale_count + undated_count) / max(total, 1)) * 100))
        severity = (
            Severity.HIGH if stale_count > total * 0.3
            else Severity.MEDIUM if stale_count > 0 or undated_count > total * 0.5
            else Severity.LOW
        )
        return CollectorResult(
            collector_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "stale_artifacts": stale_count,
                "undated_artifacts": undated_count,
                "fresh_artifacts": self._metrics.get("fresh_count", 0),
                "total_artifacts": total,
                "staleness_threshold_days": self._metrics.get("staleness_days", DEFAULT_STALENESS_DAYS),
            },
            signals=self._findings,
            recommendation=(
                f"{stale_count} stale, {undated_count} undated artifact(s)"
                if stale_count or undated_count
                else "All artifacts are fresh"
            ),
        )
