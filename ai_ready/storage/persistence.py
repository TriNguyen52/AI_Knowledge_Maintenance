"""Canonical persistence orchestration for assessment results.

Promotes demo behavior P4 (full persist orchestration) and P5 (stable
signal IDs) into a single reusable function that both the Lambda handler
and the demo can call.

This module is the single entry point for writing assessment results
(artifacts + embeddings, signals + lifecycle, assessment record, and
problem discovery) to CockroachDB.  It replaces the inline persistence
logic that was duplicated between ``demo_closed_loop.py`` and
``lambda_handler.py``.

Usage::

    from ai_ready.storage.persistence import persist_assessment

    result = persist_assessment(
        store=store,
        assessment=assessment,
        signals=all_signals,
        artifacts=bundle.artifacts,
        source=s3_uri,
    )
"""

from __future__ import annotations

import logging
from typing import Any

from ai_ready.storage.cockroach import CockroachDBStore
from ai_ready.storage.models import (
    ArtifactRecord,
    AssessmentRecord,
    SignalRecord,
)
from ai_ready.llm.embeddings import embed_artifacts
from ai_ready.models import KnowledgeAssessment, KnowledgeSignal

logger = logging.getLogger(__name__)


def _artifact_to_text(artifact: Any) -> str:
    """Convert a KnowledgeArtifact's content to a flat text string for embedding."""
    parts: list[str] = []
    if hasattr(artifact, "title") and artifact.title:
        parts.append(f"# {artifact.title}")
    if hasattr(artifact, "content") and artifact.content:
        content = artifact.content
        if hasattr(content, "headings"):
            for h in content.headings:
                parts.append(f"{'#' * h.level} {h.text}")
        if hasattr(content, "paragraphs"):
            for p in content.paragraphs:
                parts.append(p.text)
    return "\n\n".join(parts)


def persist_assessment(
    store: CockroachDBStore,
    assessment: KnowledgeAssessment,
    signals: list[KnowledgeSignal],
    artifacts: list[Any],
    source: str = "",
    dedup_key: str = "",
    embed: bool = True,
) -> dict[str, Any]:
    """Persist a full assessment cycle to CockroachDB.

    This is the canonical orchestration function (P4).  It handles:
    1. Embedding artifacts via ``embed_artifacts()`` (P3) and storing them
    2. Storing signals with stable signal_ids (P5)
    3. Recording signal lifecycle transitions (new → persistent)
    4. Storing the assessment record
    5. Returning a summary dict with IDs for downstream use

    Args:
        store: Connected CockroachDBStore instance.
        assessment: The KnowledgeAssessment from the pipeline.
        signals: List of KnowledgeSignal objects from collectors.
        artifacts: List of KnowledgeArtifact objects.
        source: Source URI string for metadata.
        dedup_key: Optional idempotency key.
        embed: If True, generate and store embeddings.  Set to False
            when embeddings are already stored or not needed.

    Returns:
        Dict with keys: assessment_id, signal_count, artifact_count,
        signal_id_map (internal_id → db_signal_id).
    """
    # 1. Embed and store artifacts
    if embed and artifacts:
        embeddings = embed_artifacts(artifacts)
        for artifact, emb in zip(artifacts, embeddings):
            content_text = _artifact_to_text(artifact)
            record = ArtifactRecord.new(
                artifact_uri=artifact.uri,
                artifact_type="document",
                source=source,
                content=content_text,
                metadata=artifact.metadata if hasattr(artifact, "metadata") else {},
                embedding=emb,
            )
            store.save_artifact(record)

    # 2. Store signals with stable signal_ids (P5)
    signal_count = 0
    signal_id_map: dict[str, str] = {}
    for signal in signals:
        # Use the signal's own stable signal_id (P5) — don't regenerate
        sig_id = signal.signal_id if hasattr(signal, "signal_id") and signal.signal_id else f"sig_{signal_count}"

        signal_record = SignalRecord.new(
            artifact_uri=signal.artifact_uri,
            collector_id=signal.collector_id,
            signal_type=signal.signal_type,
            severity=signal.severity.value if hasattr(signal.severity, "value") else str(signal.severity),
            score=signal.score,
            recommendation=signal.recommendation or "",
            ai_impact=signal.ai_impact or "",
            evidence=signal.evidence if isinstance(signal.evidence, dict) else {},
        )
        # Override the random UUID with the stable signal_id (P5)
        signal_record.signal_id = sig_id
        store.save_signal(signal_record)
        signal_id_map[sig_id] = sig_id
        signal_count += 1

    # 3. Store assessment record
    assessment_record = AssessmentRecord.new(
        score=assessment.score,
        dimensions={d.dimension: d.score for d in assessment.dimensions},
        signals_count=signal_count,
        metadata={
            "source": source,
            "artifact_count": len(artifacts),
            **({"dedup_key": dedup_key} if dedup_key else {}),
        },
    )
    store.save_assessment(assessment_record)

    # 4. Record signal lifecycle (new → persistent)
    for sig_id in signal_id_map:
        try:
            store.save_signal_lifecycle(
                signal_id=sig_id,
                assessment_id=assessment_record.assessment_id,
                status="persistent",
            )
        except Exception as e:
            logger.debug("Signal lifecycle save skipped for %s: %s", sig_id, e)

    return {
        "assessment_id": assessment_record.assessment_id,
        "signal_count": signal_count,
        "artifact_count": len(artifacts),
        "signal_id_map": signal_id_map,
    }


def update_signal_lifecycle_post_verification(
    store: CockroachDBStore,
    signal_ids: list[str],
    assessment_id: str,
    status: str = "resolved",
) -> None:
    """Transition signal lifecycle status after verification (P6).

    Called by the verify_improvement action (or ImprovementManager) after
    a remediation has been verified.  Signals that were previously
    'persistent' or 'new' are transitioned to 'resolved' (or 'recurring'
    if the problem persists).

    Args:
        store: Connected CockroachDBStore instance.
        signal_ids: List of stable signal IDs to transition.
        assessment_id: The assessment ID that verified the resolution.
        status: New lifecycle status — 'resolved', 'recurring', etc.
    """
    for sig_id in signal_ids:
        try:
            store.save_signal_lifecycle(
                signal_id=sig_id,
                assessment_id=assessment_id,
                status=status,
            )
        except Exception as e:
            logger.debug("Signal lifecycle update skipped for %s: %s", sig_id, e)
