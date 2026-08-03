"""Quantitative salience and eligibility models for knowledge improvement.

All functions are deterministic and require NO LLM calls — they use
pure mathematical computation based on signal properties, assessment
scores, and historical outcomes.

Three core components:

1. **Problem Salience** — Ranks KnowledgeProblems by importance using a
   3-subscore formula:
     salience = (0.40 * encoding + 0.25 * outcome + 0.35 * retrieval) * size_penalty

   - encoding: signal severity distribution (how bad the signals are)
   - outcome: historical success rate for this problem type (learned from history)
   - retrieval: dimension impact (how much room to improve)
   - size_penalty: logarithmic decay (1/(1+log2(N))) — gentle penalty for
     broad problems, preserves salience up to ~100 artifacts

2. **Signal-Delta Eligibility** — Deterministic gate that checks whether
   new signals have appeared since the last analysis. Prevents wasted
   LLM calls on unchanged signals.

3. **Heuristic Problem Discovery** — Deterministic signal clustering into
   KnowledgeProblem candidates using rule-based patterns. Provides a
   baseline analysis that works without any LLM.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_ready.improvement.models import KnowledgeProblem, RootCauseHypothesis, SystemicCluster

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity weights (deterministic, no LLM)
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 1.0,
    "high": 0.7,
    "medium": 0.4,
    "low": 0.1,
}

# Salience sub-score weights
W_ENCODING = 0.40
W_OUTCOME = 0.25
W_RETRIEVAL = 0.35

# Default salience threshold — problems below this are skipped
DEFAULT_SALIENCE_THRESHOLD = 0.05

# ---------------------------------------------------------------------------
# Recency decay parameters (deterministic, no LLM)
# ---------------------------------------------------------------------------

# Dual half-life model: fast term captures recent activity, slow floor
# ensures unreviewed-forever problems keep drifting down instead of parking.
FAST_HALF_LIFE_DAYS = 21.0    # recent activity boost halves every 21 days
SLOW_HALF_LIFE_DAYS = 180.0   # floor itself halves every 180 days
SLOW_FLOOR_WEIGHT = 0.1       # floor amplitude (10% of fast term at t=0)
DECAY_EPSILON = 0.01           # prevents collapse to exactly 0

# ---------------------------------------------------------------------------
# Edit budget — maximum fraction of an artifact that may be changed
# ---------------------------------------------------------------------------

MAX_EDIT_BUDGET_PCT = 0.20      # 20% — reject proposals that change more


# ---------------------------------------------------------------------------
# 1. Problem Salience
# ---------------------------------------------------------------------------

@dataclass
class ProblemSalience:
    """Salience decomposition for a single KnowledgeProblem.

    Every field is deterministic — no LLM involvement. The decomposition
    allows explainability: "this problem was prioritized because
    encoding=0.9, outcome=0.78, retrieval=0.85, staleness=0.72".
    """

    problem_id: str
    encoding: float       # signal severity distribution [0, 1]
    outcome: float        # historical success rate for this problem type [0, 1]
    retrieval: float      # dimension impact [0, 1]
    size_penalty: float   # 1 / (1 + log2(artifact_count)) [0, 1]
    staleness_factor: float  # recency decay from SignalLifecycle.last_seen [0, 1]
    total: float          # weighted sum * size_penalty * staleness_factor [0, 1]
    explanation: str      # human-readable decomposition

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "encoding": round(self.encoding, 4),
            "outcome": round(self.outcome, 4),
            "retrieval": round(self.retrieval, 4),
            "size_penalty": round(self.size_penalty, 4),
            "staleness_factor": round(self.staleness_factor, 4),
            "total": round(self.total, 4),
            "explanation": self.explanation,
        }


def compute_staleness_factor(
    problem: KnowledgeProblem,
    signal_lifecycles: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> float:
    """Compute a recency decay factor from signal lifecycle data.

    Uses a dual half-life model inspired by biological memory:
    - Fast term: halves every 21 days (recent activity boost)
    - Slow floor: 0.1 amplitude, halves every 180 days (ensures
      unreviewed-forever problems keep drifting down, don't park)

    The most recent last_seen across all problem signals is used.
    Problems whose signals have never been seen get epsilon (0.01),
    keeping them ordinal for ranking but at minimal weight.

    Pure deterministic computation — no LLM calls.

    Args:
        problem: The KnowledgeProblem to score.
        signal_lifecycles: Optional dict mapping signal_id to an object
            with a ``last_seen`` ISO timestamp string. If None or empty,
            returns 1.0 (no decay — backwards compatible).
        now: Optional reference timestamp. Defaults to current UTC time.

    Returns:
        Staleness factor in [0.01, 1.1]. Higher = more recently active.
    """
    if not signal_lifecycles:
        return 1.0  # no lifecycle data → no decay (backwards compatible)

    if now is None:
        now = datetime.now(timezone.utc)

    # Collect last_seen timestamps from all problem signals
    last_seen_times: list[datetime] = []
    for signal_id in problem.signal_ids:
        lc = signal_lifecycles.get(signal_id)
        if lc is None:
            continue
        last_seen_str = getattr(lc, "last_seen", None) if not isinstance(lc, dict) else lc.get("last_seen")
        if not last_seen_str:
            continue
        try:
            last_seen_times.append(datetime.fromisoformat(last_seen_str))
        except (ValueError, TypeError):
            continue

    if not last_seen_times:
        return DECAY_EPSILON  # never seen → minimal weight, still ordinal

    # Use the most recent last_seen (most generous interpretation)
    most_recent = max(last_seen_times)
    age_days = (now - most_recent).total_seconds() / 86_400.0

    # Dual half-life decay
    fast_term = 0.5 ** (age_days / FAST_HALF_LIFE_DAYS)
    slow_floor = SLOW_FLOOR_WEIGHT * 0.5 ** (age_days / SLOW_HALF_LIFE_DAYS)
    return max(DECAY_EPSILON, fast_term + slow_floor)


def compute_problem_salience(
    problem: KnowledgeProblem,
    assessment: Any,
    history_store: Any = None,
    signal_lifecycles: dict[str, Any] | None = None,
) -> ProblemSalience:
    """Compute a unified salience score for a KnowledgeProblem.

    Pure deterministic computation — no LLM calls.

    Args:
        problem: The KnowledgeProblem to score.
        assessment: The KnowledgeAssessment containing signals and dimensions.
        history_store: Optional ImprovementHistoryStore for outcome sub-score.
        signal_lifecycles: Optional dict mapping signal_id to lifecycle
            objects with ``last_seen`` timestamps. Enables recency decay.

    Returns:
        ProblemSalience with full decomposition.
    """
    # --- Encoding sub-score: signal severity distribution ---
    # Higher severity signals -> higher encoding score.
    signal_severities: list[float] = []
    if assessment:
        for signal in assessment.signals:
            if signal.signal_id in problem.signal_ids:
                weight = SEVERITY_WEIGHTS.get(signal.severity.value, 0.1)
                signal_severities.append(weight)
    if signal_severities:
        encoding = sum(signal_severities) / len(signal_severities)
    else:
        encoding = 0.0

    # --- Outcome sub-score: historical success rate for this problem type ---
    # If we have no history, default to neutral 0.5.
    outcome = 0.5  # prior probability of success
    if history_store:
        try:
            metrics = history_store.get_remediation_metrics()
            strategy_success = metrics.get("strategy_success_rate", {})
            problem_category = problem.category
            for strategy_name, stats in strategy_success.items():
                if stats.get("total", 0) > 0:
                    rate = stats.get("success_rate", 0.5)
                    outcome = max(outcome, rate)
                    break
        except Exception:
            pass  # Keep default 0.5 if history unavailable

    # --- Retrieval sub-score: dimension impact ---
    # Lower dimension scores -> higher retrieval salience (more room to improve).
    retrieval = 0.5  # neutral default
    if assessment and hasattr(assessment, "dimensions"):
        affected_dims: set[str] = set()
        if assessment:
            for signal in assessment.signals:
                if signal.signal_id in problem.signal_ids:
                    affected_dims.add(signal.collector_id)
        if affected_dims:
            dim_scores: list[float] = []
            for dim_name in affected_dims:
                if dim_name in assessment.dimensions:
                    score = assessment.dimensions[dim_name].score
                    dim_scores.append(1.0 - (score / 100.0))
                else:
                    dim_scores.append(0.5)
            retrieval = sum(dim_scores) / len(dim_scores)

    # --- Size penalty: problems affecting many artifacts are harder to fix ---
    # Logarithmic decay: 1 artifact → 1.0, 5 → 0.31, 10 → 0.24, 20 → 0.19, 100 → 0.13
    # This preserves salience for broad problems while slightly deprioritizing
    # very large ones (500+ artifacts) that are too vague to act on.
    artifact_count = len(problem.artifact_uris) if problem.artifact_uris else 1
    size_penalty = 1.0 / (1.0 + math.log2(max(artifact_count, 1)))

    # --- Staleness factor: recency decay from signal lifecycle data ---
    staleness_factor = compute_staleness_factor(problem, signal_lifecycles)

    # --- Total: weighted sum * size_penalty * staleness_factor ---
    total = (
        W_ENCODING * encoding + W_OUTCOME * outcome + W_RETRIEVAL * retrieval
    ) * size_penalty * staleness_factor

    explanation = (
        f"Prioritized because: encoding={encoding:.2f} "
        f"(severity of {len(signal_severities)} signal(s)), "
        f"outcome={outcome:.2f} (historical success rate for '{problem.category}'), "
        f"retrieval={retrieval:.2f} (dimension impact), "
        f"size_penalty={size_penalty:.2f} ({artifact_count} artifact(s)), "
        f"staleness={staleness_factor:.4f} (recency decay). "
        f"Total salience={total:.4f}."
    )

    return ProblemSalience(
        problem_id=problem.problem_id,
        encoding=encoding,
        outcome=outcome,
        retrieval=retrieval,
        size_penalty=size_penalty,
        staleness_factor=staleness_factor,
        total=total,
        explanation=explanation,
    )


def rank_problems_by_salience(
    problems: list[KnowledgeProblem],
    assessment: Any,
    history_store: Any = None,
    threshold: float = DEFAULT_SALIENCE_THRESHOLD,
    signal_lifecycles: dict[str, Any] | None = None,
) -> tuple[list[KnowledgeProblem], list[ProblemSalience], list[KnowledgeProblem]]:
    """Rank KnowledgeProblems by salience and filter below threshold.

    Pure deterministic computation — no LLM calls.

    Args:
        problems: List of KnowledgeProblems to rank.
        assessment: The KnowledgeAssessment containing signals and dimensions.
        history_store: Optional ImprovementHistoryStore for outcome sub-score.
        threshold: Minimum salience to include (default 0.15).
        signal_lifecycles: Optional dict mapping signal_id to lifecycle
            objects with ``last_seen`` timestamps. Enables recency decay.

    Returns:
        Tuple of (ranked_problems, salience_scores, skipped_problems).
        ranked_problems: sorted by salience descending, above threshold.
        salience_scores: corresponding ProblemSalience for each ranked problem.
        skipped_problems: problems below threshold (for skip event recording).
    """
    scored: list[tuple[KnowledgeProblem, ProblemSalience]] = []
    for problem in problems:
        salience = compute_problem_salience(problem, assessment, history_store, signal_lifecycles)
        scored.append((problem, salience))

    # Sort by total salience descending
    scored.sort(key=lambda x: x[1].total, reverse=True)

    ranked = []
    saliences = []
    skipped = []
    for problem, salience in scored:
        if salience.total >= threshold:
            ranked.append(problem)
            saliences.append(salience)
        else:
            skipped.append(problem)

    return ranked, saliences, skipped


# ---------------------------------------------------------------------------
# 2. Signal-Delta Eligibility
# ---------------------------------------------------------------------------

@dataclass
class EligibilityResult:
    """Result of a signal-delta eligibility check.

    Deterministic — no LLM. Only process when there are new signals
    since the last analysis to avoid wasting tokens on unchanged state.
    """

    eligible: bool
    new_signal_ids: list[str] = field(default_factory=list)
    unchanged_signal_ids: list[str] = field(default_factory=list)
    removed_signal_ids: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "new_signal_ids": self.new_signal_ids,
            "unchanged_signal_ids": self.unchanged_signal_ids,
            "removed_signal_ids": self.removed_signal_ids,
            "reason": self.reason,
        }


def check_signal_delta(
    current_signal_ids: list[str],
    last_analysis_signal_ids: list[str] | None,
) -> EligibilityResult:
    """Check whether new signals have appeared since the last analysis.

    Deterministic set comparison — no LLM. An assessment is only
    eligible for analysis if there are new signals since the last
    analysis run.

    Args:
        current_signal_ids: Signal IDs from the current assessment.
        last_analysis_signal_ids: Signal IDs from the last analysis,
            or None if this is the first analysis.

    Returns:
        EligibilityResult with eligibility flag and signal deltas.
    """
    current_set = set(current_signal_ids)

    if last_analysis_signal_ids is None:
        # First analysis — always eligible
        return EligibilityResult(
            eligible=True,
            new_signal_ids=list(current_set),
            unchanged_signal_ids=[],
            removed_signal_ids=[],
            reason="First analysis — all signals are new.",
        )

    last_set = set(last_analysis_signal_ids)
    new_ids = current_set - last_set
    unchanged_ids = current_set & last_set
    removed_ids = last_set - current_set

    if new_ids:
        return EligibilityResult(
            eligible=True,
            new_signal_ids=list(new_ids),
            unchanged_signal_ids=list(unchanged_ids),
            removed_signal_ids=list(removed_ids),
            reason=f"{len(new_ids)} new signal(s) since last analysis.",
        )
    else:
        return EligibilityResult(
            eligible=False,
            new_signal_ids=[],
            unchanged_signal_ids=list(unchanged_ids),
            removed_signal_ids=list(removed_ids),
            reason=(
                f"No new signals since last analysis "
                f"({len(unchanged_ids)} unchanged, {len(removed_ids)} removed). "
                f"Skipping LLM analysis to save tokens."
            ),
        )


# ---------------------------------------------------------------------------
# 3. Heuristic Problem Discovery (no LLM)
# ---------------------------------------------------------------------------

def discover_problems_heuristic(
    signal_ids: list[str],
    assessment: Any,
) -> tuple[list[KnowledgeProblem], list[RootCauseHypothesis]]:
    """Discover KnowledgeProblems from signals using deterministic rules.

    No LLM needed — uses rule-based clustering:

    1. Same (collector, type, artifact) -> single problem (signal cluster)
    2. Same signal_type across multiple artifacts -> systemic problem
    3. Severity escalation (CRITICAL/HIGH signals) -> urgent problem

    This provides a baseline analysis that works without any LLM.
    When an LLM is available, it can enhance these heuristic problems
    with richer root cause hypotheses.

    Args:
        signal_ids: Signal IDs to analyze.
        assessment: The KnowledgeAssessment containing signal details.

    Returns:
        Tuple of (knowledge_problems, hypotheses).
    """
    if not assessment:
        return [], []

    # Collect signals of interest
    target_signals = [s for s in assessment.signals if s.signal_id in set(signal_ids)]
    if not target_signals:
        return [], []

    problems: list[KnowledgeProblem] = []
    hypotheses: list[RootCauseHypothesis] = []

    # --- Rule 1: Cluster by (collector, type, artifact) ---
    clusters: dict[str, list[Any]] = {}
    for signal in target_signals:
        key = f"{signal.collector_id}|{signal.signal_type}|{signal.artifact_uri}"
        if key not in clusters:
            clusters[key] = []
        clusters[key].append(signal)

    for idx, (key, cluster_signals) in enumerate(clusters.items()):
        parts = key.split("|")
        collector, signal_type, artifact_uri = parts[0], parts[1], parts[2]

        # Determine the dominant severity
        severities = [s.severity.value for s in cluster_signals]
        max_severity = max(severities, key=lambda s: SEVERITY_WEIGHTS.get(s, 0.1))

        hypothesis = RootCauseHypothesis(
            hypothesis=(
                f"{collector} reports {signal_type} on {artifact_uri} "
                f"({len(cluster_signals)} signal(s), max severity={max_severity}). "
                f"Likely cause: {signal_type} issue in the knowledge artifact."
            ),
            evidence=[
                f"Signal {s.signal_id}: {s.recommendation}"
                for s in cluster_signals[:3]
            ],
            confidence=min(0.3 + 0.1 * len(cluster_signals), 0.8),
            affected_artifact_uris=[artifact_uri],
            category=signal_type,
        )
        hypotheses.append(hypothesis)

        problem = KnowledgeProblem(
            problem_id=f"kp_heuristic_{idx}",
            category=signal_type,
            description=(
                f"{len(cluster_signals)} {signal_type} signal(s) from {collector} "
                f"on {artifact_uri} (max severity: {max_severity})"
            ),
            root_cause_idx=idx,
            signal_ids=[s.signal_id for s in cluster_signals],
            artifact_uris=[artifact_uri],
            evidence_summary=f"Clustered {len(cluster_signals)} signals by ({collector}, {signal_type}, {artifact_uri})",
        )
        problems.append(problem)

    # --- Rule 2: Systemic problems (same type across multiple artifacts) ---
    type_to_artifacts: dict[str, set[str]] = {}
    type_to_signals: dict[str, list[str]] = {}
    for signal in target_signals:
        if signal.signal_type not in type_to_artifacts:
            type_to_artifacts[signal.signal_type] = set()
            type_to_signals[signal.signal_type] = []
        type_to_artifacts[signal.signal_type].add(signal.artifact_uri)
        type_to_signals[signal.signal_type].append(signal.signal_id)

    for signal_type, artifacts in type_to_artifacts.items():
        if len(artifacts) >= 2:
            idx = len(hypotheses)
            hypothesis = RootCauseHypothesis(
                hypothesis=(
                    f"Systemic issue: {signal_type} appears across {len(artifacts)} "
                    f"artifacts. This suggests a process or pipeline-level problem "
                    f"rather than an individual artifact issue."
                ),
                evidence=[
                    f"Affected artifacts: {', '.join(list(artifacts)[:5])}",
                    f"Total signals: {len(type_to_signals[signal_type])}",
                ],
                confidence=0.6,
                affected_artifact_uris=list(artifacts),
                category=f"systemic_{signal_type}",
            )
            hypotheses.append(hypothesis)

            problem = KnowledgeProblem(
                problem_id=f"kp_systemic_{signal_type}",
                category=f"systemic_{signal_type}",
                description=(
                    f"Systemic {signal_type} issue across {len(artifacts)} artifacts "
                    f"({len(type_to_signals[signal_type])} total signals)"
                ),
                root_cause_idx=idx,
                signal_ids=type_to_signals[signal_type],
                artifact_uris=list(artifacts),
                evidence_summary=f"Same signal type across {len(artifacts)} artifacts indicates a systemic issue.",
            )
            problems.append(problem)

    # --- Rule 3: Urgent problems (CRITICAL/HIGH severity) ---
    urgent_signals = [
        s for s in target_signals
        if SEVERITY_WEIGHTS.get(s.severity.value, 0.1) >= 0.7
    ]
    if urgent_signals and len(urgent_signals) >= 2:
        idx = len(hypotheses)
        hypothesis = RootCauseHypothesis(
            hypothesis=(
                f"Urgent: {len(urgent_signals)} high-severity signal(s) detected. "
                f"These require immediate attention as they significantly impact "
                f"AI readiness."
            ),
            evidence=[
                f"Signal {s.signal_id}: severity={s.severity.value}, {s.recommendation}"
                for s in urgent_signals[:5]
            ],
            confidence=0.7,
            affected_artifact_uris=list({s.artifact_uri for s in urgent_signals}),
            category="urgent_high_severity",
        )
        hypotheses.append(hypothesis)

        problem = KnowledgeProblem(
            problem_id="kp_urgent_high_severity",
            category="urgent_high_severity",
            description=(
                f"{len(urgent_signals)} high-severity signal(s) requiring immediate attention"
            ),
            root_cause_idx=idx,
            signal_ids=[s.signal_id for s in urgent_signals],
            artifact_uris=list({s.artifact_uri for s in urgent_signals}),
            evidence_summary=f"{len(urgent_signals)} signals at HIGH/CRITICAL severity.",
        )
        problems.append(problem)

    return problems, hypotheses


# ---------------------------------------------------------------------------
# 4. Root-Cause-Level Clustering (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _directory_prefix(uri: str) -> str:
    """Extract the directory prefix from an artifact URI.

    For file paths like "docs/api/auth.md" → "docs/api/"
    For URIs like "https://example.com/docs/api/auth" → "https://example.com/docs/api/"
    Falls back to the parent directory or empty string for bare names.
    """
    # Normalize separators
    normalized = uri.replace("\\", "/")
    idx = normalized.rfind("/")
    if idx > 0:
        return normalized[:idx + 1]
    return ""


def _extract_evidence_refs(evidence_summary: str) -> list[str]:
    """Extract normalizable evidence references from an evidence summary.

    Extracts quoted strings and common reference patterns from the
    evidence summary text. These are used to detect shared-evidence
    patterns (e.g., same broken link target, same heading text).
    """
    refs: list[str] = []
    # Extract quoted strings (single quotes used in evidence summaries)
    import re
    # Match content within single quotes
    for m in re.finditer(r"'([^']+)'", evidence_summary):
        ref = m.group(1).strip().lower()
        if ref and len(ref) > 2:
            refs.append(ref)
    # Also extract content within double quotes
    for m in re.finditer(r'"([^"]+)"', evidence_summary):
        ref = m.group(1).strip().lower()
        if ref and len(ref) > 2:
            refs.append(ref)
    return refs


def cluster_problems_by_root_cause(
    problems: list[KnowledgeProblem],
    saliences: list[ProblemSalience] | None = None,
    assessment: Any = None,
) -> list[SystemicCluster]:
    """Cluster KnowledgeProblems into systemic groups by root cause patterns.

    Pure deterministic computation — no LLM calls. Groups problems
    beyond the (collector, type, artifact) level by identifying
    deeper patterns:

    1. **directory_cluster**: Artifacts sharing a directory prefix
       (e.g., all broken links in docs/api/ → page-move pattern)
    2. **systemic_type**: Same signal_type across 2+ artifacts
       (process-level issue, not individual artifact)
    3. **shared_evidence**: Problems sharing common evidence references
       (e.g., same broken link target, same template heading)
    4. **shared_category**: Problems with the same category that don't
       fit other patterns (broad systemic pattern)

    Each problem is assigned to exactly one cluster (highest-priority
    pattern wins). Singletons (problems that don't match any pattern)
    get their own cluster with pattern_type="singleton".

    Args:
        problems: Ranked KnowledgeProblems to cluster.
        saliences: Optional ProblemSalience list (parallel to problems).
            Used to compute cluster_salience as the max member salience.
        assessment: Optional KnowledgeAssessment for evidence extraction.

    Returns:
        List of SystemicCluster, sorted by cluster_salience descending.
    """
    if not problems:
        return []

    # Build salience lookup
    salience_map: dict[str, float] = {}
    if saliences:
        for s in saliences:
            salience_map[s.problem_id] = s.total
    else:
        for p in problems:
            salience_map[p.problem_id] = 0.5  # neutral default

    # Build signal → evidence lookup from assessment
    signal_evidence: dict[str, dict[str, Any]] = {}
    if assessment:
        for signal in assessment.signals:
            signal_evidence[signal.signal_id] = signal.evidence if isinstance(signal.evidence, dict) else {}

    clusters: list[SystemicCluster] = []
    assigned_problem_ids: set[str] = set()
    cluster_counter = 0

    # --- Pattern 1: Directory clusters ---
    # Group problems whose artifacts share a directory prefix.
    # Only applies when 2+ artifacts share the same directory.
    dir_to_problems: dict[str, list[KnowledgeProblem]] = {}
    for problem in problems:
        for uri in problem.artifact_uris:
            prefix = _directory_prefix(uri)
            if prefix:
                dir_to_problems.setdefault(prefix, []).append(problem)

    for prefix, dir_problems in dir_to_problems.items():
        # Only cluster if 2+ distinct problems share this directory
        unique_problems = list({p.problem_id: p for p in dir_problems}.values())
        if len(unique_problems) < 2:
            continue
        # Only include problems not already assigned
        unassigned = [p for p in unique_problems if p.problem_id not in assigned_problem_ids]
        if len(unassigned) < 2:
            continue

        cluster_counter += 1
        all_signal_ids: list[str] = []
        all_artifact_uris: list[str] = []
        for p in unassigned:
            all_signal_ids.extend(p.signal_ids)
            all_artifact_uris.extend(p.artifact_uris)
        max_salience = max(salience_map.get(p.problem_id, 0.0) for p in unassigned)

        clusters.append(SystemicCluster(
            cluster_id=f"sc_dir_{cluster_counter}",
            pattern_type="directory_cluster",
            pattern_description=(
                f"Multiple problems in directory '{prefix}' ({len(unassigned)} problems, "
                f"{len(set(all_artifact_uris))} artifacts). Likely a page-move or "
                f"directory-level restructuring issue."
            ),
            problem_ids=[p.problem_id for p in unassigned],
            signal_ids=list(set(all_signal_ids)),
            artifact_uris=list(set(all_artifact_uris)),
            shared_evidence=[f"Shared directory: {prefix}"],
            cluster_salience=max_salience,
        ))
        assigned_problem_ids.update(p.problem_id for p in unassigned)

    # --- Pattern 2: Systemic type clusters ---
    # Group problems with the same signal_type (from category) across
    # 2+ artifacts that haven't been assigned yet.
    type_to_problems: dict[str, list[KnowledgeProblem]] = {}
    for problem in problems:
        if problem.problem_id in assigned_problem_ids:
            continue
        # Use category as the type proxy (it encodes signal_type)
        type_to_problems.setdefault(problem.category, []).append(problem)

    for category, type_problems in type_to_problems.items():
        # Only cluster if 2+ artifacts are affected across these problems
        all_artifacts = set()
        for p in type_problems:
            all_artifacts.update(p.artifact_uris)
        if len(all_artifacts) < 2 or len(type_problems) < 1:
            continue
        # Skip if only 1 problem with 1 artifact (not systemic)
        if len(type_problems) == 1 and len(all_artifacts) == 1:
            continue

        cluster_counter += 1
        all_signal_ids: list[str] = []
        for p in type_problems:
            all_signal_ids.extend(p.signal_ids)
        max_salience = max(salience_map.get(p.problem_id, 0.0) for p in type_problems)

        clusters.append(SystemicCluster(
            cluster_id=f"sc_type_{cluster_counter}",
            pattern_type="systemic_type",
            pattern_description=(
                f"Systemic {category} issue across {len(all_artifacts)} artifact(s) "
                f"({len(type_problems)} problem(s)). Indicates a process or pipeline-level "
                f"problem rather than individual artifact issues."
            ),
            problem_ids=[p.problem_id for p in type_problems],
            signal_ids=list(set(all_signal_ids)),
            artifact_uris=list(set(all_artifacts)),
            shared_evidence=[f"Shared signal type: {category}"],
            cluster_salience=max_salience,
        ))
        assigned_problem_ids.update(p.problem_id for p in type_problems)

    # --- Pattern 3: Shared evidence clusters ---
    # Group problems whose signals share common evidence references
    # (e.g., same broken link target, same heading text).
    ref_to_problems: dict[str, list[KnowledgeProblem]] = {}
    for problem in problems:
        if problem.problem_id in assigned_problem_ids:
            continue
        # Extract evidence refs from the problem's evidence_summary
        refs = _extract_evidence_refs(problem.evidence_summary)
        # Also extract from signal evidence in the assessment
        for sid in problem.signal_ids:
            ev = signal_evidence.get(sid, {})
            # Check common evidence fields
            for field in ("target", "heading", "reference", "link_target"):
                val = ev.get(field, "")
                if val and isinstance(val, str):
                    refs.append(val.strip().lower())

        for ref in refs:
            ref_to_problems.setdefault(ref, []).append(problem)

    for ref, ref_problems in ref_to_problems.items():
        unique_problems = list({p.problem_id: p for p in ref_problems}.values())
        if len(unique_problems) < 2:
            continue
        unassigned = [p for p in unique_problems if p.problem_id not in assigned_problem_ids]
        if len(unassigned) < 2:
            continue

        cluster_counter += 1
        all_signal_ids: list[str] = []
        all_artifact_uris: list[str] = []
        for p in unassigned:
            all_signal_ids.extend(p.signal_ids)
            all_artifact_uris.extend(p.artifact_uris)
        max_salience = max(salience_map.get(p.problem_id, 0.0) for p in unassigned)

        clusters.append(SystemicCluster(
            cluster_id=f"sc_evidence_{cluster_counter}",
            pattern_type="shared_evidence",
            pattern_description=(
                f"Problems share evidence reference '{ref[:80]}' ({len(unassigned)} problems, "
                f"{len(set(all_artifact_uris))} artifacts). Same root reference suggests a "
                f"template, shared source, or common dependency issue."
            ),
            problem_ids=[p.problem_id for p in unassigned],
            signal_ids=list(set(all_signal_ids)),
            artifact_uris=list(set(all_artifact_uris)),
            shared_evidence=[f"Shared reference: {ref[:120]}"],
            cluster_salience=max_salience,
        ))
        assigned_problem_ids.update(p.problem_id for p in unassigned)

    # --- Pattern 4: Shared category (catch-all for same-category problems) ---
    cat_to_problems: dict[str, list[KnowledgeProblem]] = {}
    for problem in problems:
        if problem.problem_id in assigned_problem_ids:
            continue
        cat_to_problems.setdefault(problem.category, []).append(problem)

    for category, cat_problems in cat_to_problems.items():
        if len(cat_problems) < 2:
            continue

        cluster_counter += 1
        all_signal_ids: list[str] = []
        all_artifact_uris: list[str] = []
        for p in cat_problems:
            all_signal_ids.extend(p.signal_ids)
            all_artifact_uris.extend(p.artifact_uris)
        max_salience = max(salience_map.get(p.problem_id, 0.0) for p in cat_problems)

        clusters.append(SystemicCluster(
            cluster_id=f"sc_category_{cluster_counter}",
            pattern_type="shared_category",
            pattern_description=(
                f"Multiple {category} problems ({len(cat_problems)} problems, "
                f"{len(set(all_artifact_uris))} artifacts). Broad pattern suggesting "
                f"a systemic {category} issue across the knowledge base."
            ),
            problem_ids=[p.problem_id for p in cat_problems],
            signal_ids=list(set(all_signal_ids)),
            artifact_uris=list(set(all_artifact_uris)),
            shared_evidence=[f"Shared category: {category}"],
            cluster_salience=max_salience,
        ))
        assigned_problem_ids.update(p.problem_id for p in cat_problems)

    # --- Singletons: unassigned problems get their own cluster ---
    for problem in problems:
        if problem.problem_id in assigned_problem_ids:
            continue
        cluster_counter += 1
        clusters.append(SystemicCluster(
            cluster_id=f"sc_singleton_{cluster_counter}",
            pattern_type="singleton",
            pattern_description=(
                f"Individual {problem.category} problem on {', '.join(problem.artifact_uris[:3])}. "
                f"No systemic pattern detected."
            ),
            problem_ids=[problem.problem_id],
            signal_ids=problem.signal_ids,
            artifact_uris=problem.artifact_uris,
            shared_evidence=[],
            cluster_salience=salience_map.get(problem.problem_id, 0.0),
        ))
        assigned_problem_ids.add(problem.problem_id)

    # Sort by cluster salience descending
    clusters.sort(key=lambda c: c.cluster_salience, reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# 5. Skip Event (for visible no-ops)
# ---------------------------------------------------------------------------

@dataclass
class SkipEvent:
    """Records when the system chose not to act, with reason.

    Transparency mechanism — the system records its decisions NOT to
    act so users can see what was considered and rejected.
    """

    lane: str           # which process was skipped
    reason: str         # why (low_salience, no_new_signals, gated, etc.)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# 6. Edit budget — deterministic gate for remediation proposals
# ---------------------------------------------------------------------------

def compute_edit_delta(
    modification_steps: list[dict[str, Any]],
    artifact_uri: str,
    original_content: str | None = None,
) -> float:
    """Estimate the fraction of an artifact that a set of steps will change.

    When ``original_content`` is provided and a step contains
    ``new_content`` (or ``content``), the edit delta is computed from
    the actual text diff using ``difflib.SequenceMatcher.ratio()``.
    This prevents an LLM from labeling a full rewrite as a minor
    ``update_document`` (weight 0.15) to bypass the edit budget.

    When ``original_content`` is not available, falls back to the
    static per-step-type weight table (conservative heuristic).

    This is a *conservative* heuristic: it estimates the proportion of
    the artifact touched by the proposed modifications.  When the
    fraction exceeds MAX_EDIT_BUDGET_PCT (20%), the proposal is
    rejected to prevent destructive overwrites.

    The estimate uses step_type to classify the scope of each change:

    +-------------------------+----------+-----------------------------------+
    | step_type               | weight   | rationale                         |
    +=========================+==========+===================================+
    | replace_document        | 1.0      | replaces the entire artifact      |
    | rewrite_section         | 0.3      | replaces a major section (~30%)   |
    | update_document         | 0.15     | partial content update (~15%)     |
    | add_metadata            | 0.05     | metadata only, content untouched  |
    | add_section             | 0.1      | appends new content (~10%)        |
    | delete_section          | 0.2      | removes content (~20%)            |
    | rename                  | 0.05     | small reference rename             |
    | (other / unknown)       | 0.1      | conservative default               |
    +-------------------------+----------+-----------------------------------+

    Multiple steps targeting the same artifact are summed (capped at 1.0).

    Args:
        modification_steps: The proposal's modification_steps list.
        artifact_uri: The artifact being evaluated (steps targeting
            other artifacts are ignored).
        original_content: Optional original text content of the artifact.
            When provided and a step has ``new_content`` or ``content``,
            the edit delta is computed from the actual text diff instead
            of the static weight table.

    Returns:
        Float in [0.0, 1.0] representing the estimated fraction changed.
    """
    import difflib

    _STEP_WEIGHTS: dict[str, float] = {
        "replace_document": 1.0,
        "rewrite_section": 0.3,
        "update_document": 0.15,
        "add_metadata": 0.05,
        "add_section": 0.1,
        "delete_section": 0.2,
        "rename": 0.05,
        # Relationship steps — low weight, just adds links to index page
        "create_relationship": 0.05,
        "remove_relationship": 0.05,
        "add_cross_reference": 0.05,
        "add_cross_references": 0.05,
        # Link fix aliases — same as update_document
        "fix_broken_link": 0.15,
        "fix_broken_links": 0.15,
        "fix_dangling_reference": 0.15,
        "fix_dangling_references": 0.15,
        "rewrite_content": 0.15,
        # Orphan fixes
        "link_orphaned_document": 0.05,
        "link_orphaned_documents": 0.05,
        # Automated checking — metadata level
        "implement_automated_link_checking": 0.05,
    }
    _DEFAULT_WEIGHT = 0.1

    total = 0.0
    for step in modification_steps:
        step_uri = step.get("artifact_uri", "")
        if step_uri and step_uri != artifact_uri:
            continue
        step_type = step.get("step_type", "unknown")

        # Fix 7: Use real diff when original_content and new_content are available
        new_content = step.get("new_content") or step.get("content")
        if original_content is not None and new_content is not None:
            # Compute actual change fraction from text diff
            similarity = difflib.SequenceMatcher(
                None, original_content, new_content
            ).ratio()
            change_fraction = 1.0 - similarity
            total += change_fraction
        else:
            # Fall back to static weight table
            weight = _STEP_WEIGHTS.get(step_type, _DEFAULT_WEIGHT)
            total += weight

    return min(total, 1.0)


def check_edit_budget(
    modification_steps: list[dict[str, Any]],
    artifact_uri: str,
    max_pct: float = MAX_EDIT_BUDGET_PCT,
    original_content: str | None = None,
) -> tuple[bool, float, str]:
    """Check whether a proposal stays within the edit budget.

    Args:
        modification_steps: The proposal's modification_steps list.
        artifact_uri: The artifact being modified.
        max_pct: Maximum allowed fraction (default 20%).
        original_content: Optional original text content for real diff
            computation (Fix 7).

    Returns:
        Tuple of (within_budget, edit_delta, reason).
        If within_budget is False, reason explains the rejection.
    """
    delta = compute_edit_delta(modification_steps, artifact_uri, original_content)
    if delta > max_pct:
        return (
            False,
            delta,
            f"Edit budget exceeded: proposal would change {delta:.0%} of "
            f"artifact '{artifact_uri}' (max allowed: {max_pct:.0%}). "
            f"Reject to prevent destructive overwrite.",
        )
    return True, delta, ""
