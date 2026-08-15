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

**Rollback-on-regression safety net:**

Before the executor modifies any files, the cycle backs up the
current content of every artifact in the assessment set.  After
verification, if the score regressed (``score_after < score_before``),
the cycle restores all modified files from the backup and marks
the outcome as ``rolled_back``.  This ensures the closed loop never
degrades system health — a bad proposal is detected by verification
and undone, exactly as a human reviewer would reject a change that
made things worse.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
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


# ---------------------------------------------------------------------------
# Rollback-on-regression helpers
# ---------------------------------------------------------------------------

def _backup_artifacts(
    source: Path,
    artifacts: list[Any],
    backup_dir: Path,
) -> dict[str, str]:
    """Snapshot every artifact file to *backup_dir*.

    Returns a mapping of ``uri -> backup_path`` for files that were
    successfully backed up.  Only files that exist on disk and are
    inside *source* are included.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed_up: dict[str, str] = {}
    for artifact in artifacts:
        uri = artifact.uri
        src_file = source / uri
        if src_file.exists() and src_file.is_file():
            dst_file = backup_dir / uri
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            backed_up[uri] = str(dst_file)
    logger.info("Backed up %d artifact files to %s", len(backed_up), backup_dir)
    return backed_up


def _restore_artifacts(
    source: Path,
    backed_up: dict[str, str],
) -> int:
    """Restore files from the backup mapping.

    Returns the number of files restored.
    """
    restored = 0
    for uri, backup_path in backed_up.items():
        src_file = source / uri
        backup_file = Path(backup_path)
        if backup_file.exists():
            src_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, src_file)
            restored += 1
    logger.info("Restored %d artifact files from backup", restored)
    return restored


def _cleanup_backup(backup_dir: Path) -> None:
    """Remove the backup directory."""
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
        logger.info("Cleaned up backup directory %s", backup_dir)


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
    offset: int = 0,
    halt_at_approval: bool = False,
) -> dict[str, Any]:
    """Run a single closed-loop improvement cycle.

    This function:
    1. Loads knowledge from disk, expanding the artifact set with
       previously-modified URIs from CockroachDB (so prior fixes persist)
    2. Runs the assessment pipeline
    3. Clears old signals and persists the new assessment to CockroachDB
    4. Runs the improvement workflow (analyze -> propose -> execute -> verify)
    5. Records modified artifact URIs to CockroachDB for later runs

    When ``halt_at_approval=True``, the function stops after step 4's
    proposal stage (``start_improvement``) and returns the proposals for
    human review.  The workflow state is persisted to CockroachDB so
    :func:`resume_closed_loop_cycle` can complete it later.

    Args:
        store: Connected CockroachDBStore instance.
        source: Path to the knowledge base on disk.
        limit: Maximum number of base artifacts (0 = no limit).  The first
            N artifacts are kept, then modified URIs from prior runs are
            appended.
        run_idx: 1-based run index (for tracking in modified_artifacts).
        llm_gateway: Optional LLM gateway for proposal generation.
        offset: Deprecated, ignored.  Kept for backward compatibility.
        halt_at_approval: If True, stop after proposal generation and
            return proposals for human review.  Use
            :func:`resume_closed_loop_cycle` to complete the workflow.

    Returns:
        When ``halt_at_approval=False`` (default):
        - run, score_before, score_after, score_delta, signals_resolved,
          new_signals, outcome, strategy, modified_uris, memory_influence,
          final_state, rolled_back

        When ``halt_at_approval=True``:
        - run, score_before, app_id, halted_at_approval,
          proposals, root_causes, knowledge_problems, memory_influence,
          signals_count, dimensions
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

    # 3.5. Backup artifact files before the executor can modify them.
    # Use /tmp as base (Lambda-safe) with fallback to CWD for local runs.
    import tempfile
    backup_dir = Path(tempfile.gettempdir()) / "ai-ready-rollback" / f"run_{run_idx}"
    backed_up = _backup_artifacts(source, artifacts, backup_dir)

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

    # -- Halt at approval for human-in-the-loop workflow --
    if halt_at_approval:
        state = manager.get_state(app_id) or {}
        proposals = state.get("proposals", [])
        root_causes = state.get("root_cause_analysis", [])
        knowledge_problems = state.get("knowledge_problems", [])
        memory_influence = state.get("memory_influence", {})
        selected_idx = state.get("selected_proposal_idx", 0)

        logger.info(
            "Run %d: halted at approval — %d proposals, app_id=%s",
            run_idx, len(proposals), app_id,
        )
        return {
            "run": run_idx,
            "score_before": score_before,
            "app_id": app_id,
            "halted_at_approval": True,
            "proposals": proposals,
            "selected_proposal_idx": selected_idx,
            "root_causes": root_causes,
            "knowledge_problems": knowledge_problems,
            "memory_influence": memory_influence,
            "signals_count": len(assessment.signals),
            "dimensions": {
                d.name: d.score for d in assessment.dimensions.values()
            },
        }

    final_state = manager.approve_and_complete(
        app_id, approved=True, reason="auto-approved for closed-loop cycle"
    )

    return _finalize_cycle(
        final_state=final_state,
        store=store,
        source=source,
        source_str=source_str,
        run_idx=run_idx,
        score_before=score_before,
        backed_up=backed_up,
        backup_dir=backup_dir,
    )


def _finalize_cycle(
    final_state: dict[str, Any],
    store: CockroachDBStore,
    source: Path,
    source_str: str,
    run_idx: int,
    score_before: int,
    backed_up: dict[str, str],
    backup_dir: Path,
) -> dict[str, Any]:
    """Post-approval finalisation: regression check, rollback, record, cleanup.

    Shared by :func:`run_closed_loop_cycle` (auto-approve path) and
    :func:`resume_closed_loop_cycle` (human-approve path) so the
    rollback / record / cleanup logic is not duplicated.
    """
    verification = final_state.get("verification_results", {})
    score_after = verification.get("after_score", score_before)
    score_delta = verification.get("score_difference", 0)
    outcome = verification.get("overall_outcome", "unknown")
    rolled_back = False

    new_modified_uris = _extract_modified_uris(final_state, source_str)

    if score_after < score_before and backed_up:
        logger.warning(
            "Run %d: score regressed (%d -> %d), rolling back %d files",
            run_idx, score_before, score_after, len(backed_up),
        )
        _restore_artifacts(source, backed_up)
        rolled_back = True
        outcome = "rolled_back"
        new_modified_uris = []
        score_after = score_before
        score_delta = 0
    elif new_modified_uris:
        record_modified_artifacts(
            store, source_str, new_modified_uris,
            run_idx=run_idx, step_type="mixed",
        )
        logger.info("Run %d: recorded %d modified URIs to CockroachDB",
                     run_idx, len(new_modified_uris))

    _cleanup_backup(backup_dir)

    proposals = final_state.get("proposals", [])
    selected_idx = final_state.get("selected_proposal_idx", 0)
    strategy = (
        proposals[selected_idx].get("strategy", "?")
        if proposals and selected_idx < len(proposals)
        else "?"
    )
    memory_influence = final_state.get("memory_influence", {})

    return {
        "run": run_idx,
        "score_before": score_before,
        "score_after": score_after,
        "score_delta": score_delta,
        "signals_resolved": len(verification.get("resolved_signal_ids", [])),
        "new_signals": len(verification.get("new_signal_ids", [])),
        "outcome": outcome,
        "strategy": strategy,
        "modified_uris": new_modified_uris,
        "memory_influence": memory_influence,
        "final_state": final_state,
        "rolled_back": rolled_back,
    }


def resume_closed_loop_cycle(
    store: CockroachDBStore,
    source: Path,
    app_id: str,
    run_idx: int = 1,
    llm_gateway: Any = None,
    approved: bool = True,
    reason: str = "approved by user",
) -> dict[str, Any]:
    """Resume a halted workflow from CockroachDB and complete the cycle.

    Counterpart to :func:`run_closed_loop_cycle` with
    ``halt_at_approval=True``.  The workflow state is loaded from
    CockroachDB via :meth:`ImprovementManager.resume_workflow`, then
    :meth:`ImprovementManager.approve_and_complete` executes the
    approved proposal, verifies, and the shared
    :func:`_finalize_cycle` helper handles rollback / record / cleanup.

    Args:
        store: Connected CockroachDBStore instance.
        source: Path to the knowledge base on disk.
        app_id: The workflow app_id returned by the halted proposal cycle.
        run_idx: 1-based run index (must match the proposal cycle).
        llm_gateway: Optional LLM gateway (needed for forking on failure).
        approved: True to approve the proposal, False to reject.
        reason: Human-readable reason for the decision.

    Returns:
        Same shape as :func:`run_closed_loop_cycle` (non-halt path).
    """
    source_str = str(source.resolve())

    # Re-load artifacts (executor needs files on disk)
    modified_uris = []
    if run_idx > 1:
        modified_uris = get_modified_artifact_uris(store, source_str)

    knowledge = load_knowledge_source_with_modifications(
        source=str(source),
        exclude_patterns=_EXCLUDE_PATTERNS,
        modified_uris=modified_uris if run_idx > 1 else None,
    )
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    git_commit = _get_git_commit(source_str)

    pipeline = AssessmentPipeline()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source_str,
        git_commit=git_commit,
        relationships=relationships,
    )
    score_before = assessment.score

    # Backup before executor modifies anything
    import tempfile
    backup_dir = Path(tempfile.gettempdir()) / "ai-ready-rollback" / f"run_{run_idx}"
    backed_up = _backup_artifacts(source, artifacts, backup_dir)

    # Create fresh manager (simulates new Lambda container)
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

    # Resume from CockroachDB state
    manager.resume_workflow(app_id, assessment)

    # Complete the workflow (execute + verify)
    final_state = manager.approve_and_complete(
        app_id, approved=approved, reason=reason,
    )

    return _finalize_cycle(
        final_state=final_state,
        store=store,
        source=source,
        source_str=source_str,
        run_idx=run_idx,
        score_before=score_before,
        backed_up=backed_up,
        backup_dir=backup_dir,
    )


def demonstrate_pause_resume(
    store: CockroachDBStore,
    source: Path,
    limit: int = 0,
    llm_gateway: Any = None,
) -> dict[str, Any]:
    """Demonstrate pause/resume across a simulated process restart.

    This function proves that workflow state survives across process
    restarts via CockroachDB persistence. It:

    1. Loads and assesses knowledge (same as run_closed_loop_cycle)
    2. Starts an improvement workflow (persists to CockroachDB as "paused")
    3. Simulates a process restart by creating a NEW ImprovementManager
    4. Resumes the workflow from CockroachDB on the new manager
    5. Verifies the resumed state matches the persisted state
    6. Approves and completes the resumed workflow
    7. Returns verification of the pause/resume cycle

    Args:
        store: Connected CockroachDBStore instance.
        source: Path to the knowledge base on disk.
        limit: Maximum number of base artifacts (0 = no limit).
        llm_gateway: Optional LLM gateway for proposal generation.

    Returns:
        Dict with keys:
        - paused_stage: The stage when the workflow was paused
        - resumed_stage: The stage after resuming (should match)
        - state_match: True if resumed state matches persisted state
        - completed: True if the workflow reached a terminal stage
        - final_stage: The terminal stage after completion
        - app_id: The workflow app_id
    """
    source_str = str(source.resolve())

    # 1. Load knowledge and assess
    knowledge = load_knowledge_source_with_modifications(
        source=str(source),
        limit=limit,
        exclude_patterns=_EXCLUDE_PATTERNS,
    )
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    git_commit = _get_git_commit(source_str)

    pipeline = AssessmentPipeline()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source_str,
        git_commit=git_commit,
        relationships=relationships,
    )

    persist_assessment(
        store=store,
        assessment=assessment,
        signals=assessment.signals,
        artifacts=artifacts,
        source=source_str,
    )

    # 2. Start improvement workflow (persists to CockroachDB as "paused")
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

    manager1 = ImprovementManager(
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

    app_id = manager1.start_improvement(assessment)

    # Capture the paused stage
    paused_state = store.get_agent_state(app_id)
    paused_stage = paused_state.get("current_stage", "unknown") if paused_state else "unknown"
    logger.info("Pause/Resume demo: workflow paused at stage '%s', app_id=%s", paused_stage, app_id)

    # 3. Simulate process restart: create a FRESH manager (no in-memory state)
    manager2 = ImprovementManager(
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

    # 4. Resume the workflow from CockroachDB on the new manager
    resumed_id = manager2.resume_workflow(app_id, assessment)
    resumed_state = manager2.get_state(app_id)
    resumed_stage = resumed_state.get("current_stage", "unknown") if resumed_state else "unknown"

    # 5. Verify state match
    state_match = (paused_stage == resumed_stage)
    logger.info("Pause/Resume demo: resumed at stage '%s', state_match=%s", resumed_stage, state_match)

    # 6. Approve and complete the resumed workflow
    final_state = manager2.approve_and_complete(
        app_id, approved=True, reason="auto-approved for pause/resume demo"
    )
    final_stage = final_state.get("current_stage", "unknown")

    # Check if the workflow reached a terminal stage
    terminal_stages = [
        "completed", "failed_verification", "failed_analysis",
        "failed_execution", "verified",
    ]
    completed = final_stage in terminal_stages

    logger.info("Pause/Resume demo: completed=%s, final_stage='%s'", completed, final_stage)

    return {
        "paused_stage": paused_stage,
        "resumed_stage": resumed_stage,
        "state_match": state_match,
        "completed": completed,
        "final_stage": final_stage,
        "app_id": app_id,
    }


# ---------------------------------------------------------------------------
# Cloud (S3-backed) closed-loop remediation
# ---------------------------------------------------------------------------

def _download_s3_artifacts(bucket: str, prefix: str, local_dir: Path) -> None:
    """Download all artifacts from S3 bucket/prefix to a local directory.

    Preserves the full S3 key structure as the relative path, so artifacts
    have the same URIs as they would in the local demo.
    """
    import boto3
    from botocore.config import Config

    _endpoint = os.environ.get("AWS_ENDPOINT_URL")
    _s3_kw: dict = {"config": Config(s3={"addressing_style": "path"})}
    if _endpoint:
        _s3_kw["endpoint_url"] = _endpoint
    s3 = boto3.client("s3", **_s3_kw)
    local_dir.mkdir(parents=True, exist_ok=True)

    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            local_file = local_dir / key
            local_file.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(local_file))

    logger.info("Downloaded S3 artifacts from s3://%s/%s to %s",
                bucket, prefix, local_dir)


def _upload_modified_to_s3(
    bucket: str, prefix: str, local_dir: Path, modified_uris: list[str]
) -> None:
    """Upload only the modified files from local directory back to S3.

    The executor modifies specific files in /tmp — only those are uploaded,
    not the entire artifact set.
    """
    import boto3
    from botocore.config import Config

    _endpoint = os.environ.get("AWS_ENDPOINT_URL")
    _s3_kw: dict = {"config": Config(s3={"addressing_style": "path"})}
    if _endpoint:
        _s3_kw["endpoint_url"] = _endpoint
    s3 = boto3.client("s3", **_s3_kw)

    for uri in modified_uris:
        local_file = local_dir / uri
        if local_file.exists():
            s3_key = uri  # URI already contains the full key path
            s3.upload_file(str(local_file), bucket, s3_key)

    logger.info("Uploaded %d modified files to s3://%s/%s",
                len(modified_uris), bucket, prefix)


def _get_next_run_idx(store: CockroachDBStore) -> int:
    """Determine the next run_idx by counting existing remediation outcomes.

    run_idx > 1 triggers lookup of previously modified URIs from CockroachDB
    so prior fixes are included in the artifact set — same mechanism as
    demo_closed_loop.py's loop counter.
    """
    try:
        result = store._execute_fetchone(
            "SELECT COUNT(*) as cnt FROM remediation_history"
        )
        return (result["cnt"] if result else 0) + 1
    except Exception:
        return 1


def run_cloud_remediation_cycle(
    store: CockroachDBStore,
    bucket: str,
    prefix: str = "",
    problem_id: str | None = None,
    llm_gateway: Any = None,
    phase: str = "full",
    app_id: str | None = None,
    approved: bool = True,
    reason: str = "approved by user",
) -> dict[str, Any]:
    """Run a cloud remediation cycle with S3-backed artifacts.

    Supports three phases for human-in-the-loop approval:

    - ``phase="full"`` (default): assess → propose → execute → verify → upload.
      The original auto-approve behaviour.
    - ``phase="propose"``: assess → propose → halt at approval checkpoint.
      Returns proposals for human review.  State persisted to CockroachDB.
    - ``phase="execute"``: resume from CockroachDB state → execute → verify →
      upload.  Requires ``app_id`` from the propose phase.

    Args:
        store: Connected CockroachDBStore instance.
        bucket: S3 bucket containing knowledge artifacts.
        prefix: S3 prefix (optional, defaults to "").
        problem_id: Specific problem to remediate (optional).
        llm_gateway: Optional LLM gateway for proposal generation.
        phase: "full", "propose", or "execute".
        app_id: Required for phase="execute" — the workflow ID returned
            by the propose phase.
        approved: For phase="execute" — True to approve, False to reject.
        reason: Human-readable reason for the approval decision.

    Returns:
        For phase="full": same as before (action, problem_id, outcome_id, …).
        For phase="propose": action="propose", app_id, proposals, root_causes,
            score_before, signals_count, dimensions, memory_influence.
        For phase="execute": action="execute", app_id, verification_outcome,
            score_before, score_after, score_delta, modified_uris, ….
    """
    from ai_ready.storage.models import RemediationRecord

    start_time = time.monotonic()

    # 1. Download S3 artifacts to /tmp
    local_source = Path("/tmp/knowledge_base")
    _download_s3_artifacts(bucket, prefix, local_source)

    # 2. Determine run_idx from CockroachDB
    run_idx = _get_next_run_idx(store)

    # 3. Get the target problem and remediation context
    if problem_id:
        problems = store.get_problems(status="open", limit=50)
        target_problem = next(
            (p for p in problems if p.problem_id == problem_id), None
        )
        if target_problem is None:
            return {"error": f"Problem {problem_id} not found or not open"}
    else:
        problems = store.get_problems(status="open", limit=1)
        if not problems:
            return {"message": "No open problems in the queue"}
        target_problem = problems[0]

    artifact_uris = (
        target_problem.artifact_uris if hasattr(target_problem, "artifact_uris") else []
    )
    remediation_context = store.get_remediation_context(target_problem.category)
    similar_problems = store.get_similar_problems(
        target_problem.category, artifact_uris
    )

    # 4. Run the appropriate phase
    if phase == "propose":
        result = run_closed_loop_cycle(
            store=store,
            source=local_source,
            limit=0,
            run_idx=run_idx,
            llm_gateway=llm_gateway,
            halt_at_approval=True,
        )
        # Return proposals for human review
        return {
            "action": "propose",
            "problem_id": target_problem.problem_id,
            "problem_category": target_problem.category,
            "app_id": result.get("app_id", ""),
            "score_before": result.get("score_before", 0),
            "proposals": result.get("proposals", []),
            "selected_proposal_idx": result.get("selected_proposal_idx", 0),
            "root_causes": result.get("root_causes", []),
            "knowledge_problems": result.get("knowledge_problems", []),
            "memory_influence": result.get("memory_influence", {}),
            "signals_count": result.get("signals_count", 0),
            "dimensions": result.get("dimensions", {}),
            "context_used": {
                "past_attempts": remediation_context.get("total_attempts", 0),
                "successful_strategies": len(remediation_context.get("successful_strategies", [])),
                "failed_strategies": len(remediation_context.get("failed_strategies", [])),
                "similar_problems": len(similar_problems),
            },
            "elapsed_seconds": round(time.monotonic() - start_time, 2),
        }

    if phase == "execute":
        if not app_id:
            return {"error": "phase='execute' requires app_id from propose phase"}
        result = resume_closed_loop_cycle(
            store=store,
            source=local_source,
            app_id=app_id,
            run_idx=run_idx,
            llm_gateway=llm_gateway,
            approved=approved,
            reason=reason,
        )
    else:
        # phase == "full" (default) — original auto-approve behaviour
        result = run_closed_loop_cycle(
            store=store,
            source=local_source,
            limit=0,
            run_idx=run_idx,
            llm_gateway=llm_gateway,
        )

    # 5. Upload only the modified files back to S3
    modified_uris = result.get("modified_uris", [])
    if modified_uris and not result.get("rolled_back", False):
        _upload_modified_to_s3(bucket, prefix, local_source, modified_uris)

    # 6. Store outcome in CockroachDB (institutional memory)
    final_state = result.get("final_state", {})
    verification_outcome = result.get("outcome", "unchanged")
    score_before = result.get("score_before", 0)
    score_after = result.get("score_after", 0)
    strategy = result.get("strategy", "unknown")
    tokens_used = final_state.get("cumulative_tokens", 0)
    forked = final_state.get("forked", False)

    record = RemediationRecord.new(
        issue_type=target_problem.category,
        strategy=strategy,
        strategy_description=final_state.get("strategy_description", ""),
        verification_outcome=verification_outcome,
        score_before=score_before,
        score_after=score_after,
        proposal_reasoning=final_state.get("proposal_reasoning", ""),
        tokens_used=tokens_used,
        latency_ms=(time.monotonic() - start_time) * 1000,
        forked=forked,
    )
    store.save_remediation_outcome(record)

    # 7. Update problem status
    if verification_outcome == "resolved":
        store.update_problem_status(target_problem.problem_id, "resolved")
    elif verification_outcome == "partially_resolved":
        store.update_problem_status(target_problem.problem_id, "in_progress")

    # 8. Store decision traces
    proposals = final_state.get("proposals", [])
    if proposals:
        store.save_decision_trace(
            outcome_id=record.outcome_id,
            stage="proposal_generation",
            reasoning=json.dumps(proposals, default=str),
            llm_metadata={"proposal_count": len(proposals)},
        )

    diagnosis = final_state.get("diagnosis", {})
    if diagnosis:
        store.save_decision_trace(
            outcome_id=record.outcome_id,
            stage="root_cause_analysis",
            reasoning=json.dumps(diagnosis, default=str),
            llm_metadata={},
        )

    elapsed = time.monotonic() - start_time

    return {
        "action": "execute" if phase == "execute" else "remediate",
        "problem_id": target_problem.problem_id,
        "problem_category": target_problem.category,
        "outcome_id": record.outcome_id,
        "app_id": final_state.get("app_id", ""),
        "verification_outcome": verification_outcome,
        "score_before": score_before,
        "score_after": score_after,
        "score_delta": score_after - score_before,
        "strategy": strategy,
        "tokens_used": tokens_used,
        "forked": forked,
        "context_used": {
            "past_attempts": remediation_context.get("total_attempts", 0),
            "successful_strategies": len(remediation_context.get("successful_strategies", [])),
            "failed_strategies": len(remediation_context.get("failed_strategies", [])),
            "similar_problems": len(similar_problems),
        },
        "modified_uris": modified_uris,
        "rolled_back": result.get("rolled_back", False),
        "elapsed_seconds": round(elapsed, 2),
        "signals_resolved": result.get("signals_resolved", 0),
        "new_signals": result.get("new_signals", 0),
        "memory_influence": result.get("memory_influence", {}),
        "verification_results": final_state.get("verification_results", {}),
    }


def run_cloud_assessment_cycle(
    store: CockroachDBStore,
    bucket: str,
    prefix: str = "",
    limit: int = 0,
    dedup_key: str = "",
) -> dict[str, Any]:
    """Run a full knowledge assessment cycle with S3-backed artifacts.

    This is the cloud equivalent of the assessment half of
    ``run_closed_loop_cycle`` — it downloads S3 artifacts to /tmp,
    runs the assessment pipeline, persists results to CockroachDB,
    discovers and ranks problems, and stores them in the queue.

    No improvement/remediation is performed — use
    ``run_cloud_remediation_cycle`` for that.

    Args:
        store: Connected CockroachDBStore instance.
        bucket: S3 bucket containing knowledge artifacts.
        prefix: S3 prefix (optional, defaults to "").
        limit: Maximum number of artifacts to assess (0 = no limit).
        dedup_key: Optional idempotency key — if an assessment with this
            key already exists, the function returns early.

    Returns:
        Dict with keys:
        - action: "assess"
        - assessment_id, score, dimensions
        - artifact_count, signal_count
        - problems_discovered, problems_skipped
        - top_problems (list of dicts with category, description, artifact_uris, salience)
        - elapsed_seconds
        - source
    """
    start_time = time.monotonic()

    s3_uri = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"

    # Idempotency check
    if dedup_key:
        existing = store._execute_fetchone(
            "SELECT assessment_id FROM knowledge_assessments "
            "WHERE metadata->>'dedup_key' = %s LIMIT 1",
            (dedup_key,),
        )
        if existing:
            return {
                "action": "assess",
                "assessment_id": existing["assessment_id"],
                "message": "Assessment already exists (idempotent skip)",
                "source": s3_uri,
            }

    # 1. Download S3 artifacts to /tmp
    local_source = Path("/tmp/knowledge_base")
    _download_s3_artifacts(bucket, prefix, local_source)

    source_str = str(local_source.resolve())
    git_commit = _get_git_commit(source_str)

    # 2. Load knowledge from local /tmp copy
    knowledge = load_knowledge_source_with_modifications(
        source=str(local_source),
        limit=limit,
        exclude_patterns=_EXCLUDE_PATTERNS,
    )
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships

    logger.info("Cloud assessment: loaded %d artifacts, %d relationships from S3",
                len(artifacts), len(relationships))

    # 3. Run assessment pipeline
    pipeline = AssessmentPipeline()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source_str,
        git_commit=git_commit,
        relationships=relationships,
    )

    logger.info("Cloud assessment: score=%d, signals=%d",
                assessment.score, len(assessment.signals))

    # 4. Persist assessment to CockroachDB
    persist_result = persist_assessment(
        store=store,
        assessment=assessment,
        signals=assessment.signals,
        artifacts=artifacts,
        source=source_str,
        dedup_key=dedup_key,
    )
    assessment_record_id = persist_result["assessment_id"]
    signal_count = persist_result["signal_count"]
    signal_id_map = persist_result["signal_id_map"]

    # 5. Discover and rank problems by salience
    from ai_ready.improvement.salience import (
        discover_problems_heuristic,
        rank_problems_by_salience,
    )
    from ai_ready.storage.models import ProblemRecord

    salience_threshold = float(os.environ.get("SALIENCE_THRESHOLD", "0.01"))
    signal_ids = list(signal_id_map.keys())
    problems, hypotheses = discover_problems_heuristic(signal_ids, assessment)
    ranked, saliences, skipped = rank_problems_by_salience(
        problems, assessment, history_store=None,
        threshold=salience_threshold,
    )

    # 6. Store top problems in queue
    for problem in ranked[:25]:
        salience_info = next(
            (s for s in saliences if s.problem_id == problem.problem_id), None
        )
        problem_record = ProblemRecord.new(
            category=problem.category,
            artifact_uris=problem.artifact_uris,
            salience=salience_info.total if salience_info else 0.0,
            encoding_score=salience_info.encoding if salience_info else 0.0,
            outcome_score=salience_info.outcome if salience_info else 0.0,
            retrieval_score=salience_info.retrieval if salience_info else 0.0,
            staleness_factor=salience_info.staleness_factor if salience_info else 1.0,
            description=problem.description,
        )
        store.save_problem(problem_record)

    elapsed = time.monotonic() - start_time

    return {
        "action": "assess",
        "assessment_id": assessment_record_id,
        "score": assessment.score,
        "dimensions": {name: d.score for name, d in assessment.dimensions.items()},
        "artifact_count": len(artifacts),
        "signal_count": signal_count,
        "problems_discovered": len(ranked),
        "problems_skipped": len(skipped),
        "top_problems": [
            {
                "category": p.category,
                "description": p.description,
                "artifact_uris": p.artifact_uris[:3],
                "salience": next(
                    (s.total for s in saliences if s.problem_id == p.problem_id), 0.0
                ),
            }
            for p in ranked[:5]
        ],
        "elapsed_seconds": round(elapsed, 2),
        "source": s3_uri,
    }
