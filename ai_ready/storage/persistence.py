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

    All DB writes (artifacts, signals, assessment record, lifecycle) are
    executed in a single CockroachDB transaction via ``run_transaction``.
    If any write fails, the entire batch rolls back — no partial state.

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
    import json as _json
    from datetime import datetime, timezone

    # 0. Pre-compute embeddings (outside the transaction — no DB needed)
    artifact_records: list[tuple[ArtifactRecord, list[float] | None]] = []
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
            artifact_records.append((record, emb))
    elif artifacts:
        for artifact in artifacts:
            content_text = _artifact_to_text(artifact)
            record = ArtifactRecord.new(
                artifact_uri=artifact.uri,
                artifact_type="document",
                source=source,
                content=content_text,
                metadata=artifact.metadata if hasattr(artifact, "metadata") else {},
            )
            artifact_records.append((record, None))

    # 1. Pre-build signal records with stable IDs (P5)
    signal_count = 0
    signal_id_map: dict[str, str] = {}
    signal_records: list[SignalRecord] = []
    for signal in signals:
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
        signal_record.signal_id = sig_id
        signal_records.append(signal_record)
        signal_id_map[sig_id] = sig_id
        signal_count += 1

    # 2. Pre-build assessment record — use the original assessment_id
    #    so that verify_improvement can load it by the same ID that's
    #    stored in the workflow state.
    assessment_record = AssessmentRecord.new(
        score=assessment.score,
        dimensions={name: d.score for name, d in assessment.dimensions.items()},
        signals_count=signal_count,
        metadata={
            "source": source,
            "artifact_count": len(artifacts),
            **({"dedup_key": dedup_key} if dedup_key else {}),
        },
        assessment_id=assessment.assessment_id,
    )

    # 3. Execute all writes in a single transaction (atomic — all or nothing)
    def _do_writes(cur: Any) -> None:
        # Artifacts
        for record, emb in artifact_records:
            embedding_str = None
            if emb is not None:
                embedding_str = f"[{','.join(str(x) for x in emb)}]"
            cur.execute(
                """
                INSERT INTO knowledge_artifacts
                    (artifact_id, artifact_uri, artifact_type, source, content,
                     metadata, embedding, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_uri) DO UPDATE SET
                    artifact_type = EXCLUDED.artifact_type,
                    source = EXCLUDED.source,
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
                """,
                (
                    record.artifact_id, record.artifact_uri, record.artifact_type,
                    record.source, record.content, record.metadata,
                    embedding_str, record.created_at, record.updated_at,
                ),
            )

        # Signals
        for sr in signal_records:
            cur.execute(
                """
                INSERT INTO knowledge_signals
                    (signal_id, artifact_uri, collector_id, signal_type,
                     severity, score, recommendation, ai_impact, evidence,
                     status, last_seen, access_count, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    severity = EXCLUDED.severity,
                    score = EXCLUDED.score,
                    recommendation = EXCLUDED.recommendation,
                    ai_impact = EXCLUDED.ai_impact,
                    evidence = EXCLUDED.evidence,
                    status = EXCLUDED.status,
                    last_seen = EXCLUDED.last_seen
                """,
                (
                    sr.signal_id, sr.artifact_uri, sr.collector_id,
                    sr.signal_type, sr.severity, sr.score,
                    sr.recommendation, sr.ai_impact, sr.evidence,
                    sr.status, sr.last_seen, sr.access_count, sr.created_at,
                ),
            )

        # Assessment record
        cur.execute(
            """
            INSERT INTO knowledge_assessments
                (assessment_id, score, dimensions, signals_count, metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                assessment_record.assessment_id, assessment_record.score,
                assessment_record.dimensions, assessment_record.signals_count,
                assessment_record.metadata, assessment_record.created_at,
            ),
        )

        # Assessment-Signal junction table — links each signal to this
        # assessment so that CockroachAssessmentStore._get_signals_for_assessment()
        # can reconstruct the full signal list via a JOIN.
        for sig_id in signal_id_map:
            cur.execute(
                """
                INSERT INTO assessment_signals (assessment_id, signal_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (assessment_record.assessment_id, sig_id),
            )

        # Signal lifecycle (new → persistent)
        for sig_id in signal_id_map:
            cur.execute(
                """
                INSERT INTO signal_lifecycle (signal_id, last_seen, last_assessment_id, status)
                VALUES (%s, now(), %s, %s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    last_seen = now(),
                    last_assessment_id = EXCLUDED.last_assessment_id,
                    status = EXCLUDED.status,
                    assessment_ids = array_append(signal_lifecycle.assessment_ids, %s),
                    access_count = signal_lifecycle.access_count + 1
                """,
                (sig_id, assessment_record.assessment_id, "persistent",
                 assessment_record.assessment_id),
            )

    store.run_transaction(_do_writes)

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
        # Update the signal_lifecycle table (tracking table with assessment IDs)
        store.save_signal_lifecycle(
            signal_id=sig_id,
            assessment_id=assessment_id,
            status=status,
        )
        # Also update the knowledge_signals.status column so that
        # get_signals_by_status() returns the correct lifecycle state.
        # Without this, signals remain 'new' in knowledge_signals even
        # after they've been resolved, because save_signal_lifecycle only
        # writes to the separate signal_lifecycle table.
        store.update_signal_status(
            signal_id=sig_id,
            status=status,
        )
