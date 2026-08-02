"""Dual history store — syncs outcomes to both SQLite and CockroachDB.

Wraps ImprovementHistoryStore (SQLite) and CockroachDBStore to provide:
  - Dual writes: outcomes go to both SQLite and CockroachDB
  - Merged reads: remediation context combines both sources
  - Decision traces: saved to CockroachDB for observability

This closes the institutional memory loop across local SQLite (fast,
rich queries with EMA tracking) and CockroachDB (distributed, persistent,
with decision traces and vector search).

The DualHistoryStore implements the same interface as
ImprovementHistoryStore, so it can be used as a drop-in replacement
wherever the workflow expects a history_store — no action code changes.

Usage:
    sqlite_store = ImprovementHistoryStore("improvement_history.db")
    cockroach_store = CockroachDBStore(...)
    dual = DualHistoryStore(sqlite_store, cockroach_store)

    manager = ImprovementManager(history_store=dual, ...)
    # Workflow now reads from and writes to both stores automatically.
"""

from __future__ import annotations

import logging
from typing import Any

from ai_ready.improvement.history import ImprovementHistoryStore
from ai_ready.improvement.models import RemediationOutcome
from ai_ready.storage.models import RemediationRecord

logger = logging.getLogger(__name__)


class DualHistoryStore:
    """Wraps SQLite and CockroachDB history stores for dual-write/merged-read.

    Implements the same interface as ImprovementHistoryStore so it can
    be used as a drop-in replacement in the workflow without changing
    any action code.

    Write path (record_outcome):
      1. Write to SQLite (primary — has EMA, strategy stats, artifact filtering)
      2. Sync to CockroachDB (distributed persistence + decision traces)
      3. Save decision traces to CockroachDB for observability

    Read path (get_remediation_context):
      1. Read from SQLite (has EMA, strategy stats, artifact-filtered similar problems)
      2. Merge CockroachDB data (decision traces, distributed history)
      3. Return merged context so the LLM sees both local and distributed memory
    """

    def __init__(
        self,
        sqlite_store: ImprovementHistoryStore,
        cockroach_store: Any = None,
    ) -> None:
        """Initialize the dual store.

        Args:
            sqlite_store: The SQLite ImprovementHistoryStore (required, primary).
            cockroach_store: The CockroachDBStore (optional, secondary).
                             If None, behaves as a pure SQLite wrapper.
        """
        self.sqlite_store = sqlite_store
        self.cockroach_store = cockroach_store

    # ------------------------------------------------------------------
    # Write path — dual write
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        outcome: RemediationOutcome,
        remediation_id: str = "",
        forked_from_app_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Record an outcome to both SQLite and CockroachDB.

        Writes to SQLite first (primary), then syncs to CockroachDB.
        CockroachDB failures are logged but don't block the workflow —
        SQLite is the source of truth for EMA and strategy stats.

        Args:
            outcome: The RemediationOutcome to store.
            remediation_id: The workflow's remediation ID.
            forked_from_app_id: If this was a forked retry, the prior app_id.
            metadata: Additional metadata (includes llm_metadata, decision_trace).

        Returns:
            The SQLite row ID (primary key).
        """
        # Write to SQLite (primary — has EMA, strategy stats)
        row_id = self.sqlite_store.record_outcome(
            outcome=outcome,
            remediation_id=remediation_id,
            forked_from_app_id=forked_from_app_id,
            metadata=metadata,
        )

        # Sync to CockroachDB (distributed persistence + decision traces)
        if self.cockroach_store:
            try:
                self._sync_to_cockroach(
                    outcome=outcome,
                    remediation_id=remediation_id,
                    forked_from_app_id=forked_from_app_id,
                    metadata=metadata or {},
                )
            except Exception as e:
                logger.warning(
                    f"Failed to sync outcome to CockroachDB: {e}. "
                    f"SQLite record is safe (row_id={row_id})."
                )

        return row_id

    def _sync_to_cockroach(
        self,
        outcome: RemediationOutcome,
        remediation_id: str,
        forked_from_app_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """Sync a single outcome to CockroachDB with decision traces.

        Creates a RemediationRecord from the RemediationOutcome, saves it,
        then saves the decision trace stages as separate trace entries.
        """
        # Extract token usage from LLM metadata
        llm_meta = metadata.get("llm_metadata", {})
        tokens_used = 0
        for action_key in ("analyze_issue", "generate_proposal"):
            action_meta = llm_meta.get(action_key, {})
            tokens_used += (action_meta.get("prompt_tokens", 0) or 0)
            tokens_used += (action_meta.get("completion_tokens", 0) or 0)

        # Extract latency from LLM metadata (sum of action latencies)
        latency_ms = 0.0
        for action_key in ("analyze_issue", "generate_proposal"):
            action_meta = llm_meta.get(action_key, {})
            latency_ms += action_meta.get("latency_ms", 0.0) or 0.0

        # Map RemediationOutcome to RemediationRecord
        verification_outcome = outcome.verification_outcome or (
            "resolved" if outcome.result == "success" else "unchanged"
        )

        record = RemediationRecord.new(
            issue_type=outcome.issue_type,
            strategy=outcome.strategy,
            strategy_description=outcome.strategy_description,
            verification_outcome=verification_outcome,
            score_before=0,  # Not available in RemediationOutcome
            score_after=outcome.score_change,
            proposal_reasoning=outcome.proposal_reasoning,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            forked=bool(forked_from_app_id),
        )

        outcome_id = self.cockroach_store.save_remediation_outcome(record)
        logger.info(
            f"Synced remediation outcome to CockroachDB: "
            f"outcome_id={outcome_id}, issue_type={outcome.issue_type}, "
            f"strategy={outcome.strategy}, result={outcome.result}"
        )

        # Save decision traces to CockroachDB
        decision_trace = metadata.get("decision_trace", {})
        if decision_trace:
            self._save_decision_traces(outcome_id, decision_trace, llm_meta)

    def _save_decision_traces(
        self,
        outcome_id: str,
        decision_trace: dict[str, Any],
        llm_meta: dict[str, Any],
    ) -> None:
        """Save decision trace stages to CockroachDB.

        Each stage of the workflow gets its own trace entry, preserving
        the full reasoning chain for observability:
        analysis -> proposal -> approval -> verification -> final_outcome
        """
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
                    self.cockroach_store.save_decision_trace(
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
    # Read path — merged read
    # ------------------------------------------------------------------

    def get_remediation_context(
        self,
        issue_type: str,
        artifact_uris: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build merged historical context from SQLite and CockroachDB.

        SQLite provides:
          - similar_problems (artifact-filtered)
          - successful/failed strategies
          - strategy_stats (success/failure rates)
          - strategy_ema (EMA-based predicted success probability)

        CockroachDB adds:
          - cockroach_successful_strategies (distributed history)
          - cockroach_failed_strategies (distributed history)
          - cockroach_decision_traces (reasoning from past attempts)
          - cockroach_total_attempts (count from distributed store)

        The merged context is passed to the LLM in analyze_issue and
        generate_proposal actions, giving it both local (EMA, stats)
        and distributed (traces, cross-instance) institutional memory.

        Args:
            issue_type: The root cause category.
            artifact_uris: Optional artifact URIs for similarity matching.

        Returns:
            Merged context dict for LLM prompt construction.
        """
        # Primary: SQLite (has EMA, strategy stats, artifact filtering)
        context = self.sqlite_store.get_remediation_context(
            issue_type=issue_type,
            artifact_uris=artifact_uris,
        )

        # Merge: CockroachDB (has decision traces, distributed history)
        if self.cockroach_store and issue_type:
            try:
                crdb_context = self.cockroach_store.get_remediation_context(
                    issue_type, limit=5
                )
                crdb_attempts = crdb_context.get("total_attempts", 0)

                if crdb_attempts > 0:
                    # Add CockroachDB-specific fields to the merged context
                    context["cockroach_successful_strategies"] = (
                        crdb_context.get("successful_strategies", [])
                    )
                    context["cockroach_failed_strategies"] = (
                        crdb_context.get("failed_strategies", [])
                    )
                    context["cockroach_decision_traces"] = (
                        crdb_context.get("last_decision_traces", [])
                    )
                    context["cockroach_total_attempts"] = crdb_attempts

                    # Update total to reflect the larger of the two stores
                    context["total_prior_attempts"] = max(
                        context.get("total_prior_attempts", 0),
                        crdb_attempts,
                    )

                    logger.info(
                        f"Merged CockroachDB context: {crdb_attempts} "
                        f"prior attempts, "
                        f"{len(context['cockroach_decision_traces'])} "
                        f"decision traces"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to read CockroachDB remediation context: {e}. "
                    f"Using SQLite-only context."
                )

        return context

    # ------------------------------------------------------------------
    # Delegation methods — pass through to SQLite (primary)
    # ------------------------------------------------------------------

    def get_similar_problems(
        self,
        issue_type: str,
        artifact_uris: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get similar problems from SQLite (primary)."""
        return self.sqlite_store.get_similar_problems(
            issue_type, artifact_uris, limit
        )

    def get_strategy_stats(self, issue_type: str) -> dict[str, Any]:
        """Get strategy stats from SQLite (primary)."""
        return self.sqlite_store.get_strategy_stats(issue_type)

    def get_remediation_metrics(self) -> dict[str, Any]:
        """Merge aggregate metrics from SQLite and CockroachDB.

        SQLite provides local metrics (total_workflows, success_rate,
        avg_score_improvement, strategy_reuse_rate, avg_forks).

        CockroachDB provides distributed metrics (cross-instance totals).

        Returns:
            Merged metrics dict with SQLite metrics plus a
            'cockroachdb' sub-dict for distributed metrics.
        """
        metrics = self.sqlite_store.get_remediation_metrics()

        if self.cockroach_store:
            try:
                crdb_metrics = self.cockroach_store.get_remediation_metrics()
                if crdb_metrics:
                    metrics["cockroachdb"] = crdb_metrics
            except Exception as e:
                logger.warning(
                    f"Failed to read CockroachDB metrics: {e}. "
                    f"Using SQLite-only metrics."
                )

        return metrics
