"""CockroachDB-backed adapters for the improvement workflow.

These adapters wrap CockroachDBStore so it can be used as a drop-in
replacement for the SQLite-based AssessmentStore, SignalStore, and
ImprovementHistoryStore.  The workflow actions (analyze_issue,
generate_proposal, etc.) call ``assessment_store.latest()``,
``assessment_store.load()``, ``signal_store.get_lifecycle()``, and
``history_store.get_remediation_context()`` — these adapters implement
those methods by reading directly from CockroachDB.

This eliminates the dual-write pattern: the workflow reads and writes
exclusively to CockroachDB, with no SQLite dependency.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ai_ready.models import (
    DimensionScore,
    KnowledgeAssessment,
    KnowledgeSignal,
    Severity,
    SignalLifecycle,
)
from ai_ready.storage.cockroach import CockroachDBStore
from ai_ready.storage.models import AssessmentRecord, RemediationRecord, SignalRecord

logger = logging.getLogger(__name__)


class CockroachSignalStore:
    """Adapter that makes CockroachDBStore look like a SignalStore.

    Implements the subset of SignalStore methods that the workflow
    actions and ProblemQueue actually call:
      - get_lifecycle(signal_id) -> SignalLifecycle | None
      - save_signals(assessment_id, signals, timestamp) -> None
    """

    def __init__(self, cockroach_store: CockroachDBStore) -> None:
        self._store = cockroach_store

    def get_lifecycle(self, signal_id: str) -> SignalLifecycle | None:
        """Get lifecycle info for a specific signal from CockroachDB."""
        row = self._store.get_signal_lifecycle(signal_id)
        if row is None:
            return None
        # Row is a dict from CockroachDB
        return SignalLifecycle(
            signal_id=str(row.get("signal_id", signal_id)),
            collector_id=row.get("collector_id", ""),
            artifact_uri=row.get("artifact_uri", ""),
            first_seen=str(row.get("first_seen", "")),
            last_seen=str(row.get("last_seen", "")),
            status=row.get("status", "new"),
            severity_history=json.loads(row.get("severity_history", "[]")) if isinstance(row.get("severity_history"), str) else row.get("severity_history", []),
            assessment_ids=json.loads(row.get("assessment_ids", "[]")) if isinstance(row.get("assessment_ids"), str) else row.get("assessment_ids", []),
            resolved_assessment=row.get("resolved_assessment", ""),
        )

    def save_signals(
        self,
        assessment_id: str,
        signals: list[KnowledgeSignal],
        timestamp: str | None = None,
    ) -> None:
        """Save signals to CockroachDB.

        Signals are already persisted via persist_assessment() in the
        demo, so this is a no-op when using CockroachDB as the primary
        store.  We keep the method for interface compatibility.
        """
        # Signals are already in CockroachDB from persist_assessment()
        pass


class CockroachAssessmentStore:
    """Adapter that makes CockroachDBStore look like an AssessmentStore.

    Implements the subset of AssessmentStore methods that the workflow
    actions actually call:
      - latest() -> KnowledgeAssessment | None
      - load(assessment_id) -> KnowledgeAssessment | None
      - save(assessment) -> None
      - signal_store (attribute) -> CockroachSignalStore
    """

    def __init__(self, cockroach_store: CockroachDBStore) -> None:
        self._store = cockroach_store
        self.signal_store = CockroachSignalStore(cockroach_store)

    def save(self, assessment: KnowledgeAssessment) -> None:
        """Save assessment to CockroachDB.

        The assessment is already persisted via persist_assessment() in
        the demo, so this is a no-op.  We keep the method for interface
        compatibility.
        """
        # Assessment is already in CockroachDB from persist_assessment()
        pass

    def latest(self) -> KnowledgeAssessment | None:
        """Get the most recent assessment from CockroachDB."""
        record = self._store.get_latest_assessment()
        if record is None:
            return None
        return self._record_to_assessment(record)

    def load(self, assessment_id: str) -> KnowledgeAssessment | None:
        """Load an assessment by ID from CockroachDB."""
        row = self._store._execute_fetchone(
            "SELECT * FROM knowledge_assessments WHERE assessment_id = %s",
            (assessment_id,),
        )
        if row is None:
            return None
        record = self._store._row_to_assessment(row)
        return self._record_to_assessment(record)

    def _record_to_assessment(
        self, record: AssessmentRecord
    ) -> KnowledgeAssessment:
        """Reconstruct a KnowledgeAssessment from CockroachDB records."""
        # Parse dimensions
        dims_data = json.loads(record.dimensions) if isinstance(record.dimensions, str) else record.dimensions
        dimensions: dict[str, DimensionScore] = {}
        for k, v in dims_data.items():
            if isinstance(v, dict):
                dimensions[k] = DimensionScore(
                    name=v.get("name", k),
                    score=v.get("score", 0),
                    collector_ids=v.get("collector_ids", []),
                    signals_count=v.get("signals_count", 0),
                )
            else:
                # Simple int score
                dimensions[k] = DimensionScore(
                    name=k, score=v if isinstance(v, int) else 0,
                    collector_ids=[], signals_count=0,
                )

        # Get signals for this assessment from CockroachDB
        signals = self._get_signals_for_assessment(record.assessment_id)

        # Parse metadata
        metadata = json.loads(record.metadata) if isinstance(record.metadata, str) else record.metadata

        return KnowledgeAssessment(
            assessment_id=record.assessment_id,
            score=int(record.score),
            dimensions=dimensions,
            signals=signals,
            metrics={},
            metadata=metadata,
        )

    def _get_signals_for_assessment(
        self, assessment_id: str
    ) -> list[KnowledgeSignal]:
        """Get all signals linked to an assessment from CockroachDB."""
        try:
            rows = self._store._execute_fetchall(
                """SELECT s.* FROM knowledge_signals s
                   JOIN assessment_signals a ON s.signal_id = a.signal_id
                   WHERE a.assessment_id = %s
                   ORDER BY s.created_at""",
                (assessment_id,),
            )
        except Exception:
            # Fallback: get all signals (if assessment_signals table doesn't exist yet)
            rows = self._store._execute_fetchall(
                "SELECT * FROM knowledge_signals ORDER BY created_at"
            )

        signals: list[KnowledgeSignal] = []
        for row in rows:
            evidence = row.get("evidence", "{}")
            if isinstance(evidence, str):
                evidence = json.loads(evidence)

            signals.append(KnowledgeSignal(
                signal_id=str(row.get("signal_id", "")),
                collector_id=row.get("collector_id", ""),
                signal_type=row.get("signal_type", ""),
                artifact_uri=row.get("artifact_uri", ""),
                evidence=evidence if isinstance(evidence, dict) else {},
                artifact_id=row.get("artifact_uri", ""),  # Use URI as ID
                line=row.get("line", 0) if "line" in row else 0,
                severity=Severity(row.get("severity", "MEDIUM")),
                score=int(row.get("score", 0)),
                recommendation=row.get("recommendation", ""),
                ai_impact=row.get("ai_impact", ""),
            ))
        return signals


# ---------------------------------------------------------------------------
# EMA helpers (copied from dual_history.py to avoid the SQLite dependency)
# ---------------------------------------------------------------------------

_EMA_ALPHA = 0.3
_EMA_DIVERSITY_FLOOR = 0.3


def _compute_ema_from_outcomes(
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute a simple EMA report from CockroachDB outcome rows."""
    sorted_outcomes = sorted(
        outcomes, key=lambda o: o.get("created_at", "")
    )
    ema_map: dict[str, dict[str, Any]] = {}
    total_obs = 0
    for o in sorted_outcomes:
        strategy = o.get("strategy", "")
        if not strategy:
            continue
        outcome_str = o.get("verification_outcome", "")
        if outcome_str in ("resolved", "improved"):
            numeric = 1.0
        elif outcome_str in ("partially_resolved", "partial"):
            numeric = 0.5
        else:
            numeric = 0.0
        if strategy not in ema_map:
            ema_map[strategy] = {
                "strategy": strategy,
                "ema": numeric,
                "observation_count": 1,
                "last_outcome": outcome_str,
            }
        else:
            entry = ema_map[strategy]
            entry["ema"] = _EMA_ALPHA * numeric + (1 - _EMA_ALPHA) * entry["ema"]
            entry["observation_count"] += 1
            entry["last_outcome"] = outcome_str
        total_obs += 1
    if not ema_map:
        return {"strategies": [], "total_observations": 0}
    max_ema = max(e["ema"] for e in ema_map.values())
    floor = _EMA_DIVERSITY_FLOOR * max_ema
    strategy_list = []
    for entry in ema_map.values():
        adjusted = max(entry["ema"], floor) if entry["observation_count"] > 0 else floor
        entry["adjusted_ema"] = round(adjusted, 4)
        entry["diversity_floor_applied"] = entry["ema"] < floor
        strategy_list.append(entry)
    strategy_list.sort(key=lambda s: s["adjusted_ema"], reverse=True)
    return {
        "strategies": strategy_list,
        "total_observations": total_obs,
        "best_strategy": strategy_list[0]["strategy"] if strategy_list else "",
        "worst_strategy": strategy_list[-1]["strategy"] if strategy_list else "",
    }


class CockroachHistoryStore:
    """Adapter that makes CockroachDBStore look like an ImprovementHistoryStore.

    Implements the full ImprovementHistoryStore interface by reading and
    writing exclusively to CockroachDB — no SQLite dependency.

    Write path (record_outcome):
      Saves RemediationRecord to CockroachDB + decision traces.

    Read path (get_remediation_context):
      Reads from CockroachDB's get_remediation_context() + computes EMA.
    """

    def __init__(self, cockroach_store: CockroachDBStore) -> None:
        self._store = cockroach_store

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        outcome: Any,  # RemediationOutcome
        remediation_id: str = "",
        forked_from_app_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Record an outcome to CockroachDB with decision traces."""
        metadata = metadata or {}
        llm_meta = metadata.get("llm_metadata", {})

        # Extract token usage
        tokens_used = 0
        for action_key in ("analyze_issue", "generate_proposal"):
            action_meta = llm_meta.get(action_key, {})
            tokens_used += (action_meta.get("prompt_tokens", 0) or 0)
            tokens_used += (action_meta.get("completion_tokens", 0) or 0)

        # Extract latency
        latency_ms = 0.0
        for action_key in ("analyze_issue", "generate_proposal"):
            action_meta = llm_meta.get(action_key, {})
            latency_ms += action_meta.get("latency_ms", 0.0) or 0.0

        # Map RemediationOutcome -> RemediationRecord
        verification_outcome = outcome.verification_outcome or (
            "resolved" if outcome.result == "success" else "unchanged"
        )

        record = RemediationRecord.new(
            issue_type=outcome.issue_type,
            strategy=outcome.strategy,
            strategy_description=outcome.strategy_description,
            verification_outcome=verification_outcome,
            score_before=outcome.score_before,
            score_after=outcome.score_after,
            proposal_reasoning=outcome.proposal_reasoning,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            forked=bool(forked_from_app_id),
            modification_steps=outcome.modification_steps,
        )

        outcome_id = self._store.save_remediation_outcome(record)
        logger.info(
            f"Saved remediation outcome to CockroachDB: "
            f"outcome_id={outcome_id}, issue_type={outcome.issue_type}, "
            f"strategy={outcome.strategy}, result={outcome.result}"
        )

        # Save decision traces
        decision_trace = metadata.get("decision_trace", {})
        if decision_trace:
            self._save_decision_traces(outcome_id, decision_trace, llm_meta)

        # Return a pseudo row ID (CockroachDB uses UUIDs, not int IDs)
        return 0

    def _save_decision_traces(
        self,
        outcome_id: str,
        decision_trace: dict[str, Any],
        llm_meta: dict[str, Any],
    ) -> None:
        """Save decision trace stages to CockroachDB."""
        stages = [
            ("analysis", "root_cause_reasoning"),
            ("proposal", "proposal_reasoning"),
            ("approval", "approval_decision"),
            ("execution", "execution_summary"),
            ("verification", "verification_reasoning"),
            ("final_outcome", "final_outcome"),
        ]
        for stage_name, trace_key in stages:
            reasoning = decision_trace.get(trace_key, "")
            if reasoning:
                try:
                    self._store.save_decision_trace(
                        outcome_id=outcome_id,
                        stage=stage_name,
                        reasoning=reasoning,
                        llm_metadata=llm_meta,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to save decision trace '{stage_name}' "
                        f"to CockroachDB: {e}"
                    )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_remediation_context(
        self,
        issue_type: str,
        artifact_uris: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build historical context from CockroachDB."""
        crdb_context = None
        if issue_type:
            try:
                crdb_context = self._store.get_remediation_context(
                    issue_type, limit=5
                )
            except Exception as e:
                logger.warning(
                    f"Failed to read CockroachDB remediation context: {e}"
                )

        crdb_attempts = (
            crdb_context.get("total_attempts", 0) if crdb_context else 0
        )

        if crdb_attempts > 0 and crdb_context:
            context = {
                "similar_problems": [],
                "successful_strategies": (
                    crdb_context.get("successful_strategies", [])
                ),
                "failed_strategies": (
                    crdb_context.get("failed_strategies", [])
                ),
                "strategy_stats": {},
                "strategy_ema": {"strategies": [], "total_observations": 0},
                "total_prior_attempts": crdb_attempts,
                "recent_outcomes": crdb_context.get("recent_outcomes", []),
                "cockroach_successful_strategies": (
                    crdb_context.get("successful_strategies", [])
                ),
                "cockroach_failed_strategies": (
                    crdb_context.get("failed_strategies", [])
                ),
                "cockroach_decision_traces": (
                    crdb_context.get("last_decision_traces", [])
                ),
                "cockroach_total_attempts": crdb_attempts,
                "cockroach_fallback_used": (
                    crdb_context.get("fallback_used", False)
                ),
                "cockroach_recent_outcomes": (
                    crdb_context.get("recent_outcomes", [])
                ),
            }

            # Compute EMA from CockroachDB outcomes
            crdb_ema = _compute_ema_from_outcomes(
                crdb_context.get("recent_outcomes", [])
            )
            if crdb_ema.get("strategies"):
                context["strategy_ema"] = crdb_ema

            logger.info(
                f"Read context from CockroachDB: {crdb_attempts} "
                f"prior attempts, "
                f"{len(context['cockroach_decision_traces'])} "
                f"decision traces"
                f"{' (fallback: broad match)' if crdb_context.get('fallback_used') else ''}"
            )
            return context

        # No data — first run for this issue type
        return {
            "similar_problems": [],
            "successful_strategies": [],
            "failed_strategies": [],
            "strategy_stats": {},
            "strategy_ema": {"strategies": [], "total_observations": 0},
            "total_prior_attempts": 0,
        }

    def get_remediation_metrics(self) -> dict[str, Any]:
        """Get aggregate metrics from CockroachDB."""
        try:
            return self._store.get_remediation_metrics()
        except Exception:
            return {}

    def get_recent_outcomes(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get recent outcomes from CockroachDB."""
        try:
            records = self._store.get_remediation_history(limit=limit)
            return [r.to_dict() for r in records]
        except Exception:
            return []

    def get_similar_problems(
        self,
        issue_type: str,
        artifact_uris: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get similar problems — not available without SQLite, return empty."""
        return []

    def get_strategy_stats(self, issue_type: str) -> dict[str, Any]:
        """Get strategy stats — not available without SQLite, return empty."""
        return {}
