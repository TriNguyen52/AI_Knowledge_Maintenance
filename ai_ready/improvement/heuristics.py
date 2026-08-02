"""Heuristic fallbacks, proposal filtering, and LLM response parsing.

These functions provide:
  1. Non-LLM fallbacks for root cause analysis and proposal generation
     (used when llm_gateway is None or LLM calls fail)
  2. Proposal filtering to reject generic or duplicate strategies
  3. Domain-specific parsing of LLM JSON responses into typed dataclasses

Extracted from actions.py to keep action functions focused on
orchestration logic rather than parsing and fallback handling.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ai_ready.improvement.models import (
    RootCauseHypothesis,
    KnowledgeProblem,
    RemediationProposal,
    SystemicCluster,
)
from ai_ready.improvement.parsing import parse_llm_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic fallbacks (no LLM required)
# ---------------------------------------------------------------------------

def heuristic_analysis(
    signal_ids: list[str],
    artifact_uris: list[str],
    assessment_store: Any,
) -> RootCauseHypothesis:
    """Produce a basic heuristic root cause when no LLM is available."""
    return RootCauseHypothesis(
        hypothesis=f"Quality issues detected in {len(artifact_uris)} artifact(s) with {len(signal_ids)} signal(s). Manual analysis required.",
        evidence=[f"{len(signal_ids)} signals detected", f"{len(artifact_uris)} artifacts affected"],
        confidence=0.3,
        affected_artifact_uris=artifact_uris,
        category="unknown",
    )


def heuristic_problems(
    hypotheses: list[RootCauseHypothesis],
    signal_ids: list[str],
    artifact_uris: list[str],
) -> list[KnowledgeProblem]:
    """Synthesize Knowledge Problems from hypotheses when LLM doesn't provide them.

    Each hypothesis becomes one Knowledge Problem, with all signals and
    artifacts assigned to it.
    """
    problems = []
    for idx, h in enumerate(hypotheses):
        problem_id = f"kp_{h.category or 'unknown'}_{idx}"
        kp = KnowledgeProblem(
            problem_id=problem_id,
            category=h.category or "unknown",
            description=h.hypothesis,
            root_cause_idx=idx,
            signal_ids=signal_ids,
            artifact_uris=h.affected_artifact_uris or artifact_uris,
            evidence_summary="; ".join(h.evidence),
        )
        problems.append(kp)
    return problems


def heuristic_proposal(
    root_causes: list[dict],
    artifact_uris: list[str],
    prior_strategies: list[str],
) -> RemediationProposal:
    """Produce a basic proposal when no LLM is available."""
    strategy = "manual_review"
    if root_causes:
        category = root_causes[0].get("category", "")
        if category and category not in prior_strategies:
            strategy = f"address_{category}"

    return RemediationProposal(
        strategy=strategy,
        description=f"Heuristic proposal: address {len(root_causes)} root cause(s) in {len(artifact_uris)} artifact(s).",
        expected_impact="unknown",
        risks=["No LLM available for detailed analysis"],
        affected_artifact_uris=artifact_uris,
        modification_steps=[
            {"step_type": "update_document", "artifact_uri": uri, "description": "Manual review and update", "parameters": {}}
            for uri in artifact_uris
        ],
    )


# ---------------------------------------------------------------------------
# Proposal filtering
# ---------------------------------------------------------------------------

def reject_generic_proposals(
    proposals: list[RemediationProposal],
    prior_strategies: list[str],
) -> list[RemediationProposal]:
    """Filter out generic or duplicate proposals.

    A proposal is rejected if:
      1. Strategy name is empty or "unknown"
      2. Description is fewer than 20 characters (too vague)
      3. No modification steps provided
      4. Strategy name matches a prior failed strategy (exact match)
    """
    GENERIC_STRATEGIES = {"unknown", "", "manual_review", "fix", "improve"}
    filtered = []
    for p in proposals:
        # Reject empty/unknown strategies
        if p.strategy.lower() in GENERIC_STRATEGIES:
            logger.info(f"Rejecting generic proposal: strategy='{p.strategy}'")
            continue
        # Reject too-short descriptions
        if len(p.description.strip()) < 20:
            logger.info(f"Rejecting vague proposal: description too short ({len(p.description)} chars)")
            continue
        # Reject proposals with no modification steps
        if not p.modification_steps:
            logger.info(f"Rejecting proposal with no steps: strategy='{p.strategy}'")
            continue
        # Reject proposals that match prior failed strategies (case-insensitive)
        if p.strategy.lower() in [s.lower() for s in prior_strategies]:
            logger.info(f"Rejecting duplicate strategy: '{p.strategy}' matches prior failure")
            continue
        filtered.append(p)
    return filtered


# ---------------------------------------------------------------------------
# LLM response parsing (domain-specific)
# ---------------------------------------------------------------------------

def parse_hypotheses_and_problems(
    content: str,
    signal_ids: list[str],
    artifact_uris: list[str],
) -> tuple[list[RootCauseHypothesis], list[KnowledgeProblem]]:
    """Parse LLM response into RootCauseHypothesis and KnowledgeProblem objects.

    The LLM is asked to return both hypotheses and knowledge_problems in a
    single JSON response. If knowledge_problems is missing, we synthesize
    them from the hypotheses using heuristic_problems().
    """
    try:
        data = parse_llm_json(content)

        hypotheses_data = data.get("hypotheses", data if isinstance(data, list) else [])
        hypotheses = [RootCauseHypothesis.from_dict(h) for h in hypotheses_data]

        problems_data = data.get("knowledge_problems", [])
        if problems_data:
            knowledge_problems = [KnowledgeProblem.from_dict(p) for p in problems_data]
        else:
            # Synthesize one problem per hypothesis if LLM didn't provide them
            knowledge_problems = heuristic_problems(hypotheses, signal_ids, artifact_uris)

        return hypotheses, knowledge_problems
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse LLM hypotheses/problems: {e}")
        # Fallback: create a single hypothesis from the raw response
        hypotheses = [RootCauseHypothesis(
            hypothesis=content[:500],
            evidence=["LLM response could not be parsed as JSON"],
            confidence=0.3,
            category="unknown",
        )]
        knowledge_problems = heuristic_problems(hypotheses, signal_ids, artifact_uris)
        return hypotheses, knowledge_problems


def parse_proposals(content: str) -> list[RemediationProposal]:
    """Parse LLM response into RemediationProposal objects."""
    try:
        data = parse_llm_json(content)

        proposals_data = data.get("proposals", data if isinstance(data, list) else [])
        return [RemediationProposal.from_dict(p) for p in proposals_data]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse LLM proposals: {e}")
        return [RemediationProposal(
            strategy="manual_review",
            description=f"LLM response could not be parsed. Raw: {content[:200]}",
            expected_impact="unknown",
            risks=["LLM parsing failure"],
        )]


# ---------------------------------------------------------------------------
# Modification step deduplication (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _step_signature(step: dict[str, Any]) -> str:
    """Compute a normalizable signature for a modification step.

    Two steps with the same signature are considered duplicates
    and can be merged. The signature is based on:
      - step_type (exact match required)
      - artifact_uri (exact match required)
      - description (normalized: lowercased, stripped, whitespace-collapsed)

    Parameters are NOT included in the signature because different
    proposals may use different parameter names for the same operation.
    """
    step_type = step.get("step_type", "unknown")
    artifact_uri = step.get("artifact_uri", "")
    description = step.get("description", "")
    # Normalize description: lowercase, strip, collapse whitespace
    normalized_desc = " ".join(description.lower().strip().split())
    return f"{step_type}|{artifact_uri}|{normalized_desc}"


def deduplicate_modification_steps(
    proposals: list[RemediationProposal],
) -> list[RemediationProposal]:
    """Deduplicate modification steps across proposals.

    When multiple proposals contain the same modification step (same
    step_type, same artifact_uri, same normalized description), the
    duplicate steps are removed from all but the first proposal that
    contains them. This prevents the executor from applying the same
    change multiple times.

    Steps are considered duplicates only if ALL of:
      - Same step_type
      - Same artifact_uri
      - Same normalized description (case-insensitive, whitespace-collapsed)

    Args:
        proposals: List of RemediationProposal objects to deduplicate.

    Returns:
        New list of RemediationProposal with deduplicated steps.
        Proposals that become empty after deduplication are removed.
    """
    if not proposals:
        return proposals

    seen_signatures: set[str] = set()
    result: list[RemediationProposal] = []

    for proposal in proposals:
        deduped_steps: list[dict[str, Any]] = []
        for step in proposal.modification_steps:
            sig = _step_signature(step)
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                deduped_steps.append(step)

        # Skip proposals that have no remaining steps after dedup
        if not deduped_steps:
            logger.info(
                f"Removing proposal '{proposal.strategy}' — all "
                f"steps were duplicates of earlier proposals"
            )
            continue

        # Create a new proposal with deduplicated steps
        new_proposal = RemediationProposal(
            strategy=proposal.strategy,
            description=proposal.description,
            expected_impact=proposal.expected_impact,
            risks=proposal.risks,
            affected_artifact_uris=proposal.affected_artifact_uris,
            modification_steps=deduped_steps,
            root_cause_idx=proposal.root_cause_idx,
            reasoning=proposal.reasoning,
            affected_dimensions=proposal.affected_dimensions,
            confidence=proposal.confidence,
            assumptions=proposal.assumptions,
            rollback_considerations=proposal.rollback_considerations,
        )
        result.append(new_proposal)

    return result


# ---------------------------------------------------------------------------
# Heuristic systemic proposal (no LLM required)
# ---------------------------------------------------------------------------

def heuristic_systemic_proposal(
    systemic_clusters: list[SystemicCluster],
    artifact_uris: list[str],
    prior_strategies: list[str],
) -> RemediationProposal:
    """Produce a systemic proposal from SystemicClusters without LLM.

    When no LLM is available, this function generates a proposal that
    addresses the highest-salience systemic cluster. It creates
    modification steps for each affected artifact in the cluster.

    Args:
        systemic_clusters: List of SystemicCluster objects, sorted by
            cluster_salience descending.
        artifact_uris: All affected artifact URIs (fallback scope).
        prior_strategies: Strategy names to avoid (prior failures).

    Returns:
        A RemediationProposal addressing the top systemic cluster.
    """
    if not systemic_clusters:
        return heuristic_proposal([], artifact_uris, prior_strategies)

    # Pick the highest-salience cluster
    top_cluster = systemic_clusters[0]
    cluster_artifacts = top_cluster.artifact_uris or artifact_uris

    # Build a strategy name from the pattern type
    strategy = f"systemic_{top_cluster.pattern_type}"
    if strategy in prior_strategies:
        strategy = f"systemic_{top_cluster.pattern_type}_v2"

    # Create modification steps for each affected artifact
    modification_steps = [
        {
            "step_type": "update_document",
            "artifact_uri": uri,
            "description": (
                f"Address {top_cluster.pattern_type} pattern: "
                f"{top_cluster.pattern_description[:100]}"
            ),
            "parameters": {
                "cluster_id": top_cluster.cluster_id,
                "pattern_type": top_cluster.pattern_type,
            },
        }
        for uri in cluster_artifacts
    ]

    return RemediationProposal(
        strategy=strategy,
        description=(
            f"Systemic proposal: address {top_cluster.pattern_type} pattern "
            f"affecting {len(cluster_artifacts)} artifact(s). "
            f"{top_cluster.pattern_description}"
        ),
        expected_impact=f"Resolve {len(top_cluster.signal_ids)} signal(s) across {len(cluster_artifacts)} artifact(s)",
        risks=["Heuristic proposal — no LLM analysis of specific changes needed"],
        affected_artifact_uris=cluster_artifacts,
        modification_steps=modification_steps,
        reasoning=f"Generated from systemic cluster '{top_cluster.cluster_id}' (salience={top_cluster.cluster_salience:.3f})",
        confidence=0.3,
        assumptions=["Artifacts are still accessible and modifiable"],
        rollback_considerations="Revert individual document updates to restore prior state",
    )
