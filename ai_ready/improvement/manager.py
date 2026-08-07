"""ImprovementManager — facade tying together the full improvement workflow.

This is the main entry point for AI-Ready to trigger knowledge improvements.
It orchestrates:
  1. Creating a Burr application from an assessment
  2. Running the workflow to the approval checkpoint
  3. Handling approval (human-in-the-loop)
  4. Running execution and verification
  5. Recording outcomes in the history store
  6. Forking failed workflows for retry
  7. Generating regression tests from failures

Usage:
    manager = ImprovementManager(
        llm_gateway=gateway,
        assessment_store=assessment_store,
        assessment_pipeline=pipeline,
        history_store=history_store,
        executor=executor,
        cockroach_store=cockroach_db,  # optional: enables dual-write
    )

    # Start an improvement from an assessment
    app_id = manager.start_improvement(assessment)

    # Wait for approval, then resume
    final_state = manager.approve_and_complete(app_id, approved=True)

    # If it failed, try forking
    if manager.should_retry(app_id):
        forked_app_id = manager.fork_and_retry(app_id)

When cockroach_store is provided alongside history_store (or
history_db_path), the manager wraps them in a DualHistoryStore that
dual-writes outcomes to both SQLite and CockroachDB, and merges
remediation context from both sources during proposal generation.
This closes the institutional memory loop across local and distributed
storage without changing any workflow action code.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ai_ready.improvement.models import (
    RemediationStatus,
    RemediationOutcome,
    VerificationResult,
    initial_state,
)
from ai_ready.improvement.application import (
    create_improvement_app,
    run_until_approval,
    approve_and_run,
)
from ai_ready.improvement.forking import fork_from_proposal, should_fork
from ai_ready.improvement.history import ImprovementHistoryStore, OutcomeEMATracker
from ai_ready.improvement.dual_history import DualHistoryStore
from ai_ready.improvement.test_generation import generate_test
from ai_ready.improvement.diagnosis_quality import DiagnosisQualityTracker
from ai_ready.improvement.problem_queue import ProblemQueue

logger = logging.getLogger(__name__)


class ImprovementManager:
    """Facade for the knowledge improvement workflow.

    Ties together Burr application lifecycle, LLM gateway, AI-Ready
    stores, executor, history, forking, and test generation.

    The manager is the ONLY component that AI-Ready's pipeline calls
    directly. Everything else is internal to the improvement package.
    """

    def __init__(
        self,
        llm_gateway: Any = None,
        assessment_store: Any = None,
        assessment_pipeline: Any = None,
        history_store: ImprovementHistoryStore | None = None,
        executor: Any = None,
        artifacts: list[Any] = None,
        relationships: list[Any] = None,
        source: str = "",
        git_commit: str = "",
        history_db_path: str | None = None,
        test_output_dir: str = "tests/improvement",
        enable_tracking: bool = True,
        enable_otel: bool = False,
        max_fork_attempts: int = 3,
        diagnosis_quality_tracker: Any = None,
        problem_queue: Any = None,
        cockroach_store: Any = None,
        config: Any = None,
    ) -> None:
        self.llm_gateway = llm_gateway
        self.assessment_store = assessment_store
        self.assessment_pipeline = assessment_pipeline
        self.executor = executor
        self.artifacts = artifacts
        self.relationships = relationships
        self.source = source
        self.git_commit = git_commit
        self.test_output_dir = test_output_dir
        self.enable_tracking = enable_tracking
        self.enable_otel = enable_otel
        self.max_fork_attempts = max_fork_attempts
        self.cockroach_store = cockroach_store
        self.config = config

        # Initialize history store if not provided
        if history_store:
            sqlite_store = history_store
        elif history_db_path:
            sqlite_store = ImprovementHistoryStore(history_db_path)
        else:
            sqlite_store = None

        # Detect if history_store is already a DualHistoryStore or a
        # CockroachHistoryStore — if so, wrapping it again in
        # DualHistoryStore would cause duplicate CockroachDB writes.
        # (DualHistoryStore.record_outcome writes to both its sqlite_store
        #  and cockroach_store; if sqlite_store IS a CockroachHistoryStore,
        #  the same CockroachDB gets written twice per outcome.)
        _already_dual = isinstance(sqlite_store, DualHistoryStore)
        _already_cockroach = False
        if sqlite_store is not None:
            # CockroachHistoryStore wraps a CockroachDBStore in _store
            if hasattr(sqlite_store, "_store") and not isinstance(sqlite_store, DualHistoryStore):
                from ai_ready.storage.adapters import CockroachHistoryStore
                _already_cockroach = isinstance(sqlite_store, CockroachHistoryStore)

        # Wrap in DualHistoryStore if CockroachDB is available AND the
        # existing store is not already dual-write or cockroach-backed
        if sqlite_store and cockroach_store and not _already_dual and not _already_cockroach:
            self.history_store = DualHistoryStore(
                sqlite_store=sqlite_store,
                cockroach_store=cockroach_store,
            )
            logger.info(
                "ImprovementManager initialized with DualHistoryStore "
                "(SQLite + CockroachDB). Outcomes will sync to both stores."
            )
        elif sqlite_store and cockroach_store and (_already_dual or _already_cockroach):
            # Store is already dual-write or cockroach-backed — use as-is
            self.history_store = sqlite_store
            if _already_cockroach:
                logger.info(
                    "ImprovementManager: history_store is already a "
                    "CockroachHistoryStore — skipping DualHistoryStore wrapper "
                    "to prevent duplicate CockroachDB writes."
                )
            else:
                logger.info(
                    "ImprovementManager: history_store is already a "
                    "DualHistoryStore — skipping re-wrap to prevent "
                    "duplicate writes."
                )
        else:
            self.history_store = sqlite_store

        # Initialize diagnosis quality tracker if not provided
        if diagnosis_quality_tracker:
            self.diagnosis_quality_tracker = diagnosis_quality_tracker
        elif history_db_path:
            dq_path = history_db_path.replace(".db", "_diagnosis_quality.db")
            self.diagnosis_quality_tracker = DiagnosisQualityTracker(db_path=dq_path)
        else:
            self.diagnosis_quality_tracker = None

        # Initialize problem queue if not provided
        if problem_queue:
            self.problem_queue = problem_queue
        elif history_db_path:
            pq_path = history_db_path.replace(".db", "_problem_queue.db")
            salience_threshold = (
                config.salience_threshold if config else None
            )
            self.problem_queue = ProblemQueue(
                history_store=self.history_store,
                db_path=pq_path,
                signal_store=assessment_store.signal_store if assessment_store else None,
                salience_threshold=salience_threshold,
            )
        else:
            self.problem_queue = None

        # Track active applications
        self._apps: dict[str, Any] = {}

    def start_improvement(
        self,
        assessment: Any,
        signal_ids: list[str] | None = None,
        artifact_uris: list[str] | None = None,
    ) -> str:
        """Start an improvement workflow from an assessment.

        Args:
            assessment: The KnowledgeAssessment that triggered this improvement.
            signal_ids: Optional subset of signal IDs to address (default: all).
            artifact_uris: Optional subset of artifact URIs (default: from signals).

        Returns:
            The app_id of the created Burr application.
        """
        remediation_id = str(uuid.uuid4())
        assessment_id = assessment.assessment_id

        # Determine which signals and artifacts to address
        if signal_ids is None:
            signal_ids = [s.signal_id for s in assessment.signals]

        if artifact_uris is None:
            artifact_uris = list({s.artifact_uri for s in assessment.signals
                                  if s.signal_id in signal_ids})

        # Create initial state
        state_dict = initial_state(
            remediation_id=remediation_id,
            assessment_id=assessment_id,
            signal_ids=signal_ids,
            affected_artifact_uris=artifact_uris,
            assessment_signals=[
                {
                    "signal_id": s.signal_id,
                    "signal_type": s.signal_type,
                    "artifact_uri": s.artifact_uri,
                    "artifact_id": s.artifact_id,
                    "collector_id": s.collector_id,
                    "severity": s.severity.value if hasattr(s.severity, "value") else str(s.severity),
                    "score": s.score,
                    "evidence": s.evidence if isinstance(s.evidence, dict) else {},
                    "recommendation": s.recommendation or "",
                }
                for s in assessment.signals
                if s.signal_id in signal_ids
            ],
        )

        # Create the Burr application
        app = create_improvement_app(
            initial_state_dict=state_dict,
            llm_gateway=self.llm_gateway,
            assessment_store=self.assessment_store,
            assessment_pipeline=self.assessment_pipeline,
            history_store=self.history_store,
            executor=self.executor,
            artifacts=self.artifacts,
            relationships=self.relationships,
            source=self.source,
            git_commit=self.git_commit,
            enable_tracking=self.enable_tracking,
            enable_otel=self.enable_otel,
            diagnosis_quality_tracker=self.diagnosis_quality_tracker,
            config=self.config,
            cockroach_store=self.cockroach_store,
        )

        app_id = app.uid
        self._apps[app_id] = app

        # P7: Persist agent state at start (active, diagnose stage)
        if self.cockroach_store:
            self.cockroach_store.save_agent_state(
                app_id=app_id,
                state_data=dict(app.state),
                current_stage="diagnose",
                assessment_id=assessment_id,
                status="active",
            )

        # Run until the approval checkpoint
        run_until_approval(app)

        # P7: Persist agent state at pause (paused, awaiting approval)
        if self.cockroach_store:
            self.cockroach_store.save_agent_state(
                app_id=app_id,
                state_data=dict(app.state),
                current_stage=app.state.get("current_stage", "awaiting_approval"),
                assessment_id=assessment_id,
                status="paused",
            )

        return app_id

    def resume_workflow(
        self,
        app_id: str,
        assessment: Any,
        signal_ids: list[str] | None = None,
        artifact_uris: list[str] | None = None,
    ) -> str:
        """Resume a paused workflow from persisted CockroachDB state.

        After a process restart, in-memory Burr apps are lost. This method
        reconstructs the Burr application graph and restores its state from
        the snapshot persisted by ``start_improvement`` (status=paused).

        Requirements:
            - ``cockroach_store`` must be set on this manager instance.
            - The workflow must have been persisted with status "paused".
            - The same ``assessment`` object (or an equivalent reconstruction)
              must be passed so the Burr graph can be rebuilt with correct
              dependencies.

        Args:
            app_id: The app_id of the paused workflow.
            assessment: The KnowledgeAssessment that triggered the workflow.
            signal_ids: Same signal_ids used in start_improvement.
            artifact_uris: Same artifact_uris used in start_improvement.

        Returns:
            The same app_id, now registered in-memory and ready for
            ``approve_and_complete``.

        Raises:
            RuntimeError: If cockroach_store is not set or the persisted
                state is not found.
        """
        if not self.cockroach_store:
            raise RuntimeError(
                "resume_workflow requires cockroach_store to be set on "
                "the ImprovementManager instance."
            )

        # Read persisted state snapshot from CockroachDB
        persisted = self.cockroach_store.get_agent_state(app_id)
        if persisted is None:
            raise RuntimeError(
                f"No persisted state found for app_id={app_id}. "
                "The workflow may not have been started or may have "
                "already completed."
            )

        if persisted.get("status") != "paused":
            raise RuntimeError(
                f"Cannot resume workflow {app_id}: persisted status is "
                f"'{persisted.get('status')}', expected 'paused'."
            )

        # Reconstruct signal_ids and artifact_uris if not provided
        state_data = persisted.get("state_data", {})
        if signal_ids is None:
            signal_ids = state_data.get("signal_ids", [])
        if artifact_uris is None:
            artifact_uris = state_data.get("affected_artifact_uris", [])

        # Rebuild the Burr application with the same graph
        assessment_id = state_data.get("assessment_id", assessment.assessment_id)
        remediation_id = state_data.get("remediation_id", str(uuid.uuid4()))

        initial_state_dict = initial_state(
            remediation_id=remediation_id,
            assessment_id=assessment_id,
            signal_ids=signal_ids,
            affected_artifact_uris=artifact_uris,
        )

        app = create_improvement_app(
            initial_state_dict=initial_state_dict,
            llm_gateway=self.llm_gateway,
            assessment_store=self.assessment_store,
            assessment_pipeline=self.assessment_pipeline,
            history_store=self.history_store,
            executor=self.executor,
            artifacts=self.artifacts,
            relationships=self.relationships,
            source=self.source,
            git_commit=self.git_commit,
            enable_tracking=self.enable_tracking,
            enable_otel=self.enable_otel,
            diagnosis_quality_tracker=self.diagnosis_quality_tracker,
            config=self.config,
            cockroach_store=self.cockroach_store,
        )

        # Restore the persisted state into the rebuilt app
        # The persisted state_data contains the full workflow state at pause
        restored_state = state_data
        if restored_state:
            app.update_state(app.state.update(**restored_state))

        # Register the rebuilt app under the original app_id
        # Note: Burr generates a new uid on creation, so we override
        self._apps[app_id] = app

        logger.info(
            f"Resumed workflow {app_id} from persisted state "
            f"(stage={persisted.get('current_stage', 'unknown')})"
        )

        return app_id

    def get_state(self, app_id: str) -> dict[str, Any]:
        """Get the current state of an improvement workflow."""
        app = self._apps.get(app_id)
        if app is None:
            raise KeyError(f"Unknown app_id: {app_id}")
        return dict(app.state)

    def get_proposals(self, app_id: str) -> list[dict[str, Any]]:
        """Get the generated proposals for an improvement workflow."""
        return self.get_state(app_id).get("proposals", [])

    def approve_and_complete(
        self,
        app_id: str,
        approved: bool,
        reason: str = "",
        selected_proposal_idx: int | None = None,
    ) -> dict[str, Any]:
        """Set approval and run the rest of the workflow.

        Args:
            app_id: The app_id of the workflow waiting for approval.
            approved: Whether the proposal was approved.
            reason: Optional reason for the decision.
            selected_proposal_idx: Optional index to select a different proposal.

        Returns:
            Final state dict.
        """
        app = self._apps.get(app_id)
        if app is None:
            raise KeyError(f"Unknown app_id: {app_id}")

        if selected_proposal_idx is not None:
            app.update_state(app.state.update(selected_proposal_idx=selected_proposal_idx))

        final_state = approve_and_run(app, approved=approved, reason=reason)

        # P7: Persist agent state at completion
        if self.cockroach_store:
            stage = final_state.get("current_stage", "completed")
            status = "completed" if stage == RemediationStatus.COMPLETED.value else "failed"
            self.cockroach_store.save_agent_state(
                app_id=app_id,
                state_data=final_state,
                current_stage=stage,
                assessment_id=final_state.get("assessment_id", ""),
                status=status,
            )

        # Record outcome in history
        self._record_outcome(app_id, final_state)

        # Generate regression test if failed
        if final_state.get("current_stage") in {
            RemediationStatus.FAILED_EXECUTION.value,
            RemediationStatus.FAILED_VERIFICATION.value,
        }:
            try:
                generate_test(
                    state=final_state,
                    output_dir=self.test_output_dir,
                    app_id=app_id if self.enable_tracking else None,
                )
            except Exception as e:
                logger.warning(f"Test generation failed: {e}")

        return final_state

    def auto_approve(
        self,
        app_id: str,
        selected_proposal_idx: int | None = None,
    ) -> dict[str, Any]:
        """Auto-approve a proposal based on institutional memory (Item 3).

        Checks prior outcomes for the selected proposal's strategy.
        If the strategy has consistently failed in prior attempts,
        auto-rejects instead of auto-approving.

        Args:
            app_id: The app_id of the workflow waiting for approval.
            selected_proposal_idx: Optional index to select a different proposal.

        Returns:
            Final state dict.
        """
        state = self.get_state(app_id)
        proposals = state.get("proposals", [])
        idx = selected_proposal_idx or state.get("selected_proposal_idx", 0)

        if not proposals or idx >= len(proposals):
            return self.approve_and_complete(
                app_id, approved=False,
                reason="No valid proposal to auto-approve",
            )

        selected = proposals[idx]
        strategy = selected.get("strategy", "")

        # Check prior outcomes for this strategy
        should_approve = True
        reject_reason = ""
        memory_influence = state.get("memory_influence", {})
        avoided = memory_influence.get("avoided_strategies", [])
        for failed in avoided:
            if failed.get("strategy", "") == strategy:
                should_approve = False
                reject_reason = (
                    f"Strategy '{strategy}' has failed in prior attempts "
                    f"(reason: {failed.get('failure_reason', 'unknown')}). "
                    f"Auto-rejecting based on institutional memory."
                )
                break

        return self.approve_and_complete(
            app_id,
            approved=should_approve,
            reason=reject_reason if not should_approve else "Auto-approved",
            selected_proposal_idx=selected_proposal_idx,
        )

    def should_retry(self, app_id: str) -> bool:
        """Check if a failed workflow should be retried via forking."""
        state = self.get_state(app_id)
        return should_fork(state, max_attempts=self.max_fork_attempts)

    def fork_and_retry(self, app_id: str) -> str:
        """Fork a failed workflow and retry with an alternative strategy.

        Args:
            app_id: The app_id of the failed workflow.

        Returns:
            The app_id of the new forked application.
        """
        prior_state = self.get_state(app_id)

        if not should_fork(prior_state, max_attempts=self.max_fork_attempts):
            raise ValueError(f"Workflow {app_id} should not be forked")

        # Fork from the proposal generation step
        app = fork_from_proposal(
            prior_app_id=app_id,
            prior_state=prior_state,
            prior_sequence_id=-1,  # Will be resolved by tracking if available
            llm_gateway=self.llm_gateway,
            assessment_store=self.assessment_store,
            assessment_pipeline=self.assessment_pipeline,
            history_store=self.history_store,
            executor=self.executor,
            artifacts=self.artifacts,
            relationships=self.relationships,
            source=self.source,
            git_commit=self.git_commit,
            enable_tracking=self.enable_tracking,
            enable_otel=self.enable_otel,
            diagnosis_quality_tracker=self.diagnosis_quality_tracker,
        )

        new_app_id = app.uid
        self._apps[new_app_id] = app

        # Run until approval again
        run_until_approval(app)

        return new_app_id

    def _record_outcome(self, app_id: str, state: dict[str, Any]) -> None:
        """Record the workflow outcome in the history store.

        Records the new fields (verification_outcome,
        strategy_description, proposal_reasoning) so that future
        workflows can use richer historical context.
        """
        if self.history_store is None:
            return

        current_stage = state.get("current_stage", "")
        proposals = state.get("proposals", [])
        selected_idx = state.get("selected_proposal_idx", 0)
        verification = state.get("verification_results", {})
        root_causes = state.get("root_cause_analysis", [])
        knowledge_problems = state.get("knowledge_problems", [])
        decision_trace = state.get("decision_trace", {})

        strategy = (proposals[selected_idx].get("strategy", "unknown")
                    if selected_idx < len(proposals) else "unknown")
        issue_type = (root_causes[0].get("category", "unknown")
                      if root_causes else "unknown")

        result = "success" if current_stage == RemediationStatus.COMPLETED.value else "failure"
        score_change = verification.get("score_difference", 0)
        score_before = verification.get("before_score", 0)
        score_after = verification.get("after_score", 0)
        root_cause = root_causes[0].get("hypothesis", "") if root_causes else ""
        artifact_uris = state.get("affected_artifact_uris", [])
        failure_reason = (
            verification.get("failure_explanation", "")
            or verification.get("summary", "")
            if result == "failure" else ""
        )
        attempt_number = state.get("attempt_number", 1)
        forked_from = state.get("forked_from_app_id", "")

        # New fields for richer history
        verification_outcome = verification.get("overall_outcome", "")
        strategy_description = (
            proposals[selected_idx].get("description", "")
            if selected_idx < len(proposals) else ""
        )
        proposal_reasoning = (
            proposals[selected_idx].get("reasoning", "")
            if selected_idx < len(proposals) else ""
        )
        modification_steps = (
            proposals[selected_idx].get("modification_steps", [])
            if selected_idx < len(proposals) else []
        )

        outcome = RemediationOutcome(
            issue_type=issue_type,
            strategy=strategy,
            result=result,
            score_change=score_change,
            score_before=score_before,
            score_after=score_after,
            root_cause=root_cause,
            artifact_uris=artifact_uris,
            failure_reason=failure_reason,
            attempt_number=attempt_number,
            verification_outcome=verification_outcome,
            strategy_description=strategy_description,
            proposal_reasoning=proposal_reasoning,
            modification_steps=modification_steps,
        )

        self.history_store.record_outcome(
            outcome=outcome,
            remediation_id=state.get("remediation_id", ""),
            forked_from_app_id=forked_from,
            metadata={
                "app_id": app_id,
                "current_stage": current_stage,
                "llm_metadata": state.get("llm_metadata", {}),
                "knowledge_problems": knowledge_problems,
                "decision_trace": decision_trace,
            },
        )

    def run_auto(
        self,
        assessment: Any,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Run the full improvement workflow with auto-approval (for testing/dry-run).

        Runs the workflow, auto-approving proposals, and automatically
        forks on failure up to max_attempts times.

        Args:
            assessment: The assessment to improve from.
            max_attempts: Maximum number of attempts (including forks).

        Returns:
            Final state dict from the last attempt.
        """
        app_id = self.start_improvement(assessment)
        state = self.approve_and_complete(app_id, approved=True, reason="auto-approved")

        attempts = 1
        while (state.get("current_stage") in {
            RemediationStatus.FAILED_EXECUTION.value,
            RemediationStatus.FAILED_VERIFICATION.value,
        } and attempts < max_attempts):
            if not self.should_retry(app_id):
                break

            logger.info(f"Auto-forking attempt #{attempts + 1}")
            app_id = self.fork_and_retry(app_id)
            state = self.approve_and_complete(app_id, approved=True, reason="auto-approved (forked retry)")
            attempts += 1

        return state

    def get_metrics(self) -> dict[str, Any]:
        """Get aggregate remediation quality metrics.

        Returns metrics showing whether AI-Ready is becoming more
        effective at resolving Knowledge Problems over time:

        - total_workflows, problems_resolved, problems_partially_resolved
        - verification_success_rate, avg_score_improvement
        - strategy_reuse_rate, strategy_success_rate
        - avg_forks_before_resolution

        Returns:
            Dict of remediation quality metrics, or empty dict if
            no history store is configured.
        """
        if self.history_store is None:
            return {}
        return self.history_store.get_remediation_metrics()

    def get_decision_trace(self, app_id: str) -> dict[str, Any]:
        """Get the full decision trace for a workflow.

        Returns the explainability chain showing reasoning at each stage:
        Knowledge Problems → Evidence → Root Cause → Historical Context →
        LLM Proposal → Approval → Execution → Verification → Final Outcome

        Args:
            app_id: The app_id of the workflow.

        Returns:
            DecisionTrace dict, or empty dict if not available.
        """
        return self.get_state(app_id).get("decision_trace", {})

    def get_diagnosis_quality(self) -> dict[str, Any]:
        """Get the diagnosis quality report (quantitative feedback loop).

        Returns the correlation between diagnosis confidence and
        verification outcomes, along with status and calibration
        adjustment text. All deterministic — no LLM.

        Returns:
            DiagnosisQualityReport dict, or empty dict if no tracker.
        """
        if self.diagnosis_quality_tracker is None:
            return {}
        return self.diagnosis_quality_tracker.get_report().to_dict()

    def get_strategy_ema(self, issue_type: str) -> dict[str, Any]:
        """Get EMA-based strategy success probabilities (deterministic).

        Returns the exponential moving average of success rates per
        strategy for the given issue type. Recent outcomes weigh more
        than old ones. Includes diversity floor enforcement.

        Args:
            issue_type: The root cause category to query.

        Returns:
            StrategyEMAReport dict, or empty dict if no history store.
        """
        if self.history_store is None:
            return {}
        tracker = OutcomeEMATracker(self.history_store)
        return tracker.get_ema_report(issue_type).to_dict()

    def discover_problems(
        self,
        assessment: Any,
        signal_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover and queue problems from an assessment (deterministic, no LLM).

        Runs heuristic problem discovery and salience ranking on the
        assessment's signals. New problems are added to the persistent
        queue; existing problems are updated with fresh salience scores.

        Args:
            assessment: The KnowledgeAssessment to analyze.
            signal_ids: Optional list of signal IDs to focus on.
                If None, all signals in the assessment are used.

        Returns:
            List of problem queue entry dicts that were added or updated.
        """
        if self.problem_queue is None:
            return []
        entries = self.problem_queue.update(assessment, signal_ids)
        return [e.to_dict() for e in entries]

    def get_problem_queue(
        self,
        limit: int = 20,
        status: str = "open",
        category: str | None = None,
        min_salience: float | None = None,
    ) -> list[dict[str, Any]]:
        """Get the ranked problem queue (deterministic, no LLM).

        Returns problems sorted by salience (descending). The queue
        is continuously updated by discover_problems() and persists
        across assessments.

        Args:
            limit: Maximum number of problems to return.
            status: Filter by status ("open", "in_progress", "resolved",
                "wont_fix", or "all").
            category: Optional category filter.
            min_salience: Optional minimum salience threshold.

        Returns:
            List of problem queue entry dicts, sorted by salience.
        """
        if self.problem_queue is None:
            return []
        return self.problem_queue.get_ranked_problems(
            limit=limit,
            status=status,
            category=category,
            min_salience=min_salience,
        )

    def get_problem_queue_summary(self) -> dict[str, Any]:
        """Get a summary of the problem queue (deterministic, no LLM).

        Returns counts by status and category, plus the top 5 most
        salient open problems.

        Returns:
            Summary dict with counts and top problems.
        """
        if self.problem_queue is None:
            return {}
        return self.problem_queue.get_queue_summary()

    def set_problem_status(self, problem_id: str, status: str) -> bool:
        """Update the status of a problem in the queue.

        Args:
            problem_id: The problem ID to update.
            status: New status ("open", "in_progress", "resolved", "wont_fix").

        Returns:
            True if the problem was found and updated, False otherwise.
        """
        if self.problem_queue is None:
            return False
        return self.problem_queue.set_status(problem_id, status)

    def get_skip_events(self, app_id: str) -> list[dict[str, Any]]:
        """Get skip events for a workflow (transparency records).

        Returns the list of decisions NOT to act, with reasons.
        Includes signal-delta skips and low-salience problem skips.

        Args:
            app_id: The app_id of the workflow.

        Returns:
            List of SkipEvent dicts.
        """
        return self.get_state(app_id).get("skip_events", [])

    def get_problem_saliences(self, app_id: str) -> list[dict[str, Any]]:
        """Get the salience scores for problems in a workflow.

        Returns the quantitative ranking of knowledge problems by
        salience, with full decomposition (encoding, outcome,
        retrieval, size_penalty).

        Args:
            app_id: The app_id of the workflow.

        Returns:
            List of ProblemSalience dicts.
        """
        return self.get_state(app_id).get("problem_saliences", [])

    def get_cockroach_metrics(self) -> dict[str, Any]:
        """Get remediation metrics from CockroachDB specifically.

        When a DualHistoryStore is configured, this returns the
        CockroachDB-specific metrics sub-dict. Otherwise returns
        empty dict.

        Returns:
            CockroachDB remediation metrics, or empty dict.
        """
        if self.cockroach_store is None:
            return {}
        try:
            return self.cockroach_store.get_remediation_metrics()
        except Exception as e:
            logger.warning(f"Failed to read CockroachDB metrics: {e}")
            return {}

    def get_cockroach_decision_traces(self, outcome_id: str) -> list[dict[str, Any]]:
        """Get decision traces from CockroachDB for a specific outcome.

        Retrieves the full reasoning chain (analysis, proposal, approval,
        execution, verification, final_outcome) stored in CockroachDB.

        Args:
            outcome_id: The CockroachDB remediation outcome ID.

        Returns:
            List of decision trace dicts, or empty list if unavailable.
        """
        if self.cockroach_store is None:
            return []
        try:
            return self.cockroach_store.get_decision_traces(outcome_id)
        except Exception as e:
            logger.warning(f"Failed to read CockroachDB traces: {e}")
            return []

    def explain_memory_influence(self, app_id: str) -> dict[str, Any]:
        """Explain how institutional memory influenced a workflow's proposal.

        Returns the ``memory_influence`` object captured during
        ``generate_proposal``, showing:
        - ``retrieved``: prior outcomes from CockroachDB used as context
        - ``avoided_strategies``: strategies that failed before
        - ``reused_strategy``: a successful strategy reused (or None)
        - ``rationale``: why the proposal was shaped this way

        Args:
            app_id: The workflow application ID.

        Returns:
            memory_influence dict, or empty dict if unavailable.
        """
        state = self.get_state(app_id)
        return state.get("memory_influence", {})
