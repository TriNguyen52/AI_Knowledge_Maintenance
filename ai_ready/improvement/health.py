"""Knowledge health summary — before/after comparison for demo legibility.

Provides a deterministic function that compares two KnowledgeAssessment
objects and returns a structured summary of what improved, what stayed
the same, and the overall score delta.  Used by the demo to print a
"Knowledge Health: Before → After" block per run.
"""

from __future__ import annotations

from typing import Any

from ai_ready.models import KnowledgeAssessment


def health_summary(
    before: KnowledgeAssessment,
    after: KnowledgeAssessment,
) -> dict[str, Any]:
    """Compute a before→after knowledge health summary.

    Args:
        before: The assessment before remediation.
        after: The assessment after remediation (re-assessed).

    Returns:
        Dict with:
        - score_before: int
        - score_after: int
        - score_delta: int
        - signals_before: int
        - signals_after: int
        - signals_resolved: int
        - signals_new: int
        - dimensions_improved: list[str]
        - dimensions_regressed: list[str]
        - dimensions_unchanged: list[str]
        - dimension_deltas: dict[str, float]
        - verdict_before: str
        - verdict_after: str
    """
    score_before = before.score
    score_after = after.score
    score_delta = score_after - score_before

    signals_before = len(before.signals)
    signals_after = len(after.signals)

    # Signal IDs for resolved/new computation
    before_ids = {s.signal_id for s in before.signals}
    after_ids = {s.signal_id for s in after.signals}
    signals_resolved = len(before_ids - after_ids)
    signals_new = len(after_ids - before_ids)

    # Dimension comparison
    dimension_deltas: dict[str, float] = {}
    dimensions_improved: list[str] = []
    dimensions_regressed: list[str] = []
    dimensions_unchanged: list[str] = []

    all_dims = set(before.dimensions.keys()) | set(after.dimensions.keys())
    for dim in all_dims:
        before_score = before.dimensions.get(dim)
        after_score = after.dimensions.get(dim)
        before_val = before_score.score if before_score else 0.0
        after_val = after_score.score if after_score else 0.0
        delta = round(after_val - before_val, 4)
        dimension_deltas[dim] = delta
        if delta > 0.001:
            dimensions_improved.append(dim)
        elif delta < -0.001:
            dimensions_regressed.append(dim)
        else:
            dimensions_unchanged.append(dim)

    return {
        "score_before": score_before,
        "score_after": score_after,
        "score_delta": score_delta,
        "signals_before": signals_before,
        "signals_after": signals_after,
        "signals_resolved": signals_resolved,
        "signals_new": signals_new,
        "dimensions_improved": dimensions_improved,
        "dimensions_regressed": dimensions_regressed,
        "dimensions_unchanged": dimensions_unchanged,
        "dimension_deltas": dimension_deltas,
        "verdict_before": before.verdict if hasattr(before, "verdict") else "",
        "verdict_after": after.verdict if hasattr(after, "verdict") else "",
    }
