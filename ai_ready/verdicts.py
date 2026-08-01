"""Multi-level health verdicts for knowledge assessments.

Replaces the simple pass/fail threshold with a graduated verdict
that accounts for data sufficiency, staleness, score, signal
severity, and trajectory.  All computation is deterministic — no
LLM calls.

Verdict hierarchy (checked in order, first match wins):
    1. INSUFFICIENT_DATA — not enough artifacts/signals to judge
    2. STALE — assessment is too old to be reliable
    3. CRITICAL — score below 30 or ≥3 CRITICAL signals
    4. DEGRADING — score dropped >10 points from prior assessment
    5. HEALTHY — score ≥70 and no CRITICAL signals
    6. MODERATE — everything else (score 30-69, or ≥70 with CRITICAL signals)

Usage:
    verdict = compute_verdict(assessment, prior_assessment=prev)
    print(verdict.label)      # "HEALTHY"
    print(verdict.reason)     # "Score 82/100 with no critical signals."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

from ai_ready.models import KnowledgeAssessment, Severity


class HealthVerdict(Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE = "STALE"
    CRITICAL = "CRITICAL"
    DEGRADING = "DEGRADING"
    HEALTHY = "HEALTHY"
    MODERATE = "MODERATE"


# --- Thresholds (frozen, deterministic) ---
MIN_ARTIFACTS = 10
MIN_SIGNALS = 5
STALE_DAYS = 90
CRITICAL_SCORE_THRESHOLD = 30
CRITICAL_SIGNAL_COUNT = 3
DEGRADING_DROP_POINTS = 10
HEALTHY_SCORE_THRESHOLD = 70


@dataclass(frozen=True)
class Verdict:
    """The computed health verdict with a human-readable reason.

    Attributes:
        label: The HealthVerdict enum value.
        reason: Human-readable explanation of why this verdict was
            chosen, suitable for display in dashboards or reports.
    """

    label: HealthVerdict
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "reason": self.reason,
        }


def compute_verdict(
    assessment: KnowledgeAssessment,
    prior_assessment: KnowledgeAssessment | None = None,
    now: datetime | None = None,
) -> Verdict:
    """Compute a multi-level health verdict for an assessment.

    Args:
        assessment: The current assessment to evaluate.
        prior_assessment: The previous assessment (for trajectory /
            degrading check).  If None, DEGRADING verdict is skipped.
        now: Reference timestamp for staleness check.  Defaults to
            current UTC time.

    Returns:
        A Verdict with label and reason.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. INSUFFICIENT_DATA
    artifact_count = assessment.metadata.get("artifact_count", 0)
    signal_count = len(assessment.signals)
    if artifact_count < MIN_ARTIFACTS or signal_count < MIN_SIGNALS:
        return Verdict(
            label=HealthVerdict.INSUFFICIENT_DATA,
            reason=(
                f"Not enough data to assess: {artifact_count} artifacts "
                f"(need {MIN_ARTIFACTS}), {signal_count} signals "
                f"(need {MIN_SIGNALS})."
            ),
        )

    # 2. STALE
    # Parse assessment_id as ISO timestamp (the pipeline uses
    # "%Y-%m-%dT%H:%M:%SZ" format)
    try:
        assessed_at = datetime.strptime(
            assessment.assessment_id, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        age_days = (now - assessed_at).days
        if age_days > STALE_DAYS:
            return Verdict(
                label=HealthVerdict.STALE,
                reason=(
                    f"Assessment is {age_days} days old "
                    f"(threshold: {STALE_DAYS} days). "
                    f"Re-assess for a current verdict."
                ),
            )
    except (ValueError, TypeError):
        # If we can't parse the timestamp, skip staleness check
        pass

    # 3. CRITICAL
    critical_signals = sum(
        1 for s in assessment.signals if s.severity == Severity.CRITICAL
    )
    if assessment.score < CRITICAL_SCORE_THRESHOLD or critical_signals >= CRITICAL_SIGNAL_COUNT:
        reasons = []
        if assessment.score < CRITICAL_SCORE_THRESHOLD:
            reasons.append(f"score {assessment.score} < {CRITICAL_SCORE_THRESHOLD}")
        if critical_signals >= CRITICAL_SIGNAL_COUNT:
            reasons.append(
                f"{critical_signals} CRITICAL signals (threshold: {CRITICAL_SIGNAL_COUNT})"
            )
        return Verdict(
            label=HealthVerdict.CRITICAL,
            reason=f"Critical: {' and '.join(reasons)}.",
        )

    # 4. DEGRADING
    if prior_assessment is not None:
        score_drop = prior_assessment.score - assessment.score
        if score_drop > DEGRADING_DROP_POINTS:
            return Verdict(
                label=HealthVerdict.DEGRADING,
                reason=(
                    f"Score dropped {score_drop} points "
                    f"({prior_assessment.score} -> {assessment.score}), "
                    f"exceeding degradation threshold of {DEGRADING_DROP_POINTS}."
                ),
            )

    # 5. HEALTHY
    if assessment.score >= HEALTHY_SCORE_THRESHOLD and critical_signals == 0:
        return Verdict(
            label=HealthVerdict.HEALTHY,
            reason=(
                f"Score {assessment.score}/{HEALTHY_SCORE_THRESHOLD}+ "
                f"with no critical signals."
            ),
        )

    # 6. MODERATE (fallthrough)
    if critical_signals > 0:
        return Verdict(
            label=HealthVerdict.MODERATE,
            reason=(
                f"Score {assessment.score} with {critical_signals} "
                f"critical signal(s) present."
            ),
        )
    return Verdict(
        label=HealthVerdict.MODERATE,
        reason=f"Score {assessment.score} — below healthy threshold of {HEALTHY_SCORE_THRESHOLD}.",
    )
