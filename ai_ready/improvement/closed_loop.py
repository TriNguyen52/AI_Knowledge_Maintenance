"""Closed-loop multi-run improvement cycle.

Provides ``run_closed_loop_cycle`` — a general-purpose function that
runs a single improvement cycle and records which artifacts were
modified to CockroachDB, so later runs can include those files in
their artifact set.

This is the core of the multi-run re-assessment flow:

    Run 1: load from disk -> assess -> improve -> record modified URIs
    Run 2: load from disk + modified URIs from CockroachDB -> assess -> improve -> record
    Run 3: load from disk + all modified URIs -> assess -> improve -> record

Each run's "before" score genuinely reflects the state left by the
previous run, because the modified URIs (e.g. index.md with added
cross-reference links) are included in the artifact set even when
they fall outside the N-artifact limit.

Usage (CLI or programmatic)::

    from ai_ready.improvement.closed_loop import run_closed_loop_cycle

    result = run_closed_loop_cycle(
        store=cockroach_store,
        source=Path("fastapi/docs/en/docs"),
        limit=30,
        run_idx=1,
        llm_gateway=gateway,
    )
    # result = {run, score_before, score_after, delta, ...}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ai_ready.improvement.manager import ImprovementManager
from ai_ready.improvement.executor import KnowledgeExecutor
from ai_ready.storage.adapters import CockroachAssessmentStore, CockroachHistoryStore
from ai_ready.storage.cockroach import CockroachDBStore
from ai_ready.storage.persistence import (
    persist_assessment,
    record_modified_artifacts,
    get_modified_artifact_uris,
    clear_assessment_signals,
)
from ai_ready.knowledge.registry import load_knowledge_source_with_modifications
from ai_ready.pipeline import AssessmentPipeline
from ai_ready.config import Config

logger = logging.getLogger(__name__)

_EXCLUDE_PATTERNS = [
    "/de/", "/es/", "/fr/", "/ja/", "/ko/", "/pt/",
    "/ru/", "/tr/", "/zh/", "/uk/", "/az/", "/it/",
]


def _get_git_commit(source: str) -> str:
    """Best-effort git commit hash for the source path."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=source,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _extract_modified_uris(final_state: dict, source_str: str) -> list[str]:
    """Extract URIs of artifacts modified by the executor from the
    final workflow state.

    Looks at execution_history entries that were successfully applied
    and collects:
    - The artifact_uri from each step
    - The actual file modified (from details.file, e.g. index.md
      that create_relationship modified to add cross-reference links)
    """
    modified: set[str] = set()
    exec_history = final_state.get("execution_history", [])
    source_resolved = Path(source_str).resolve()

    for step in exec_history:
        if not step.get("applied", True):
            continue
        # The artifact_uri from the step
        uri = step.get("artifact_uri", "")
        if uri:
            modified.add(uri)
        # The actual file modified (e.g. index.md from create_relationship)
        details = step.get("details", {}) or {}
        modified_file = details.get("file", "")
        if modified_file:
            try:
                rel = Path(modified_file).relative_to(source_resolved)
                modified.add(str(rel).replace("\\", "/"))
            except (ValueError, TypeError):
                pass

    return list(modified)


def run_closed_loop_cycle(
    store: CockroachDBStore,
    source: Path,
    limit: int = 0,
    run_idx: int = 1,
    llm_gateway: Any = None,
) -> dict[str, Any]:
    """Run a single closed-loop improvement cycle.

    This function:
    1. Loads knowledge from disk, expanding the artifact set with
       previously-modified URIs from CockroachDB (so prior fixes persist)
    2. Runs the assessment pipeline
    3. Clears old signals and persists the new assessment to CockroachDB
    4. Runs the improvement workflow (analyze -> propose -> execute -> verify)
    5. Records modified artifact URIs to CockroachDB for later runs

    Args:
        store: Connected CockroachDBStore instance.
        source: Path to the knowledge base on disk.
        limit: Maximum number of artifacts (0 = no limit).
        run_idx: 1-based run index (for tracking in modified_artifacts).
        llm_gateway: Optional LLM gateway for proposal generation.

    Returns:
        Dict with keys:
        - run: run_idx
        - score_before: assessment score before improvement
        - score_after: assessment score after improvement
        - score_delta: score_after - score_before
        - signals_resolved: count of resolved signals
        - new_signals: count of new signals
        - outcome: verification outcome string
        - strategy: selected proposal strategy
        - modified_uris: list of URIs modified by the executor
        - memory_influence: dict showing how prior outcomes influenced this run
        - final_state: full final workflow state
    """
    source_str = str(source.resolve())

    # 1. Load knowledge with modifications from prior runs
    modified_uris = []
    if run_idx > 1:
        modified_uris = get_modified_artifact_uris(store, source_str)
        logger.info("Run %d: %d previously modified URIs from CockroachDB",
                    run_idx, len(modified_uris))

    knowledge = load_knowledge_source_with_modifications(
        source=str(source),
        limit=limit,
        exclude_patterns=_EXCLUDE_PATTERNS,
        modified_uris=modified_uris if run_idx > 1 else None,
    )
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    git_commit = _get_git_commit(source_str)

    logger.info("Run %d: loaded %d artifacts, %d relationships",
                run_idx, len(artifacts), len(relationships))

    # 2. Run assessment pipeline
    pipeline = AssessmentPipeline()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source_str,
        git_commit=git_commit,
        relationships=relationships,
    )
    score_before = assessment.score
    logger.info("Run %d: before score = %d, signals = %d",
                run_idx, score_before, len(assessment.signals))

    # 3. Clear old signals (runs 2+) and persist new assessment
    if run_idx > 1:
        clear_assessment_signals(store, source_str)

    persist_assessment(
        store=store,
        assessment=assessment,
        signals=assessment.signals,
        artifacts=artifacts,
        source=source_str,
    )

    # 4. Run improvement workflow
    assessment_store = CockroachAssessmentStore(store)
    history_store = CockroachHistoryStore(store)
    config = Config.default()

    known_uris = {a.uri for a in artifacts}
    executor = KnowledgeExecutor(
        artifact_store=assessment_store,
        source_path=source_str,
        assessment_signals=assessment.signals,
        known_artifact_uris=known_uris,
    )

    manager = ImprovementManager(
        llm_gateway=llm_gateway,
        assessment_store=assessment_store,
        assessment_pipeline=pipeline,
        history_store=history_store,
        executor=executor,
        artifacts=artifacts,
        relationships=relationships,
        source=source_str,
        git_commit=git_commit,
        enable_tracking=False,
        enable_otel=False,
        max_fork_attempts=2,
        cockroach_store=store,
        config=config,
    )

    app_id = manager.start_improvement(assessment)
    final_state = manager.approve_and_complete(
        app_id, approved=True, reason="auto-approved for closed-loop cycle"
    )

    # 5. Record modified URIs to CockroachDB
    new_modified_uris = _extract_modified_uris(final_state, source_str)
    if new_modified_uris:
        record_modified_artifacts(
            store, source_str, new_modified_uris,
            run_idx=run_idx, step_type="mixed",
        )
        logger.info("Run %d: recorded %d modified URIs to CockroachDB",
                     run_idx, len(new_modified_uris))

    # 6. Extract results
    verification = final_state.get("verification_results", {})
    proposals = final_state.get("proposals", [])
    selected_idx = final_state.get("selected_proposal_idx", 0)
    strategy = (
        proposals[selected_idx].get("strategy", "?")
        if proposals and selected_idx < len(proposals)
        else "?"
    )
    memory_influence = final_state.get("memory_influence", {})

    score_after = verification.get("after_score", score_before)
    score_delta = verification.get("score_difference", 0)

    return {
        "run": run_idx,
        "score_before": score_before,
        "score_after": score_after,
        "score_delta": score_delta,
        "signals_resolved": len(verification.get("resolved_signal_ids", [])),
        "new_signals": len(verification.get("new_signal_ids", [])),
        "outcome": verification.get("overall_outcome", "unknown"),
        "strategy": strategy,
        "modified_uris": new_modified_uris,
        "memory_influence": memory_influence,
        "final_state": final_state,
    }
