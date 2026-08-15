"""SQLite assessment facade — persists assessment results for regression detection.

AssessmentStoreFacade delegates to AssessmentStore + SignalStore for all persistence.
All methods use canonical types (KnowledgeAssessment, AssessmentDiff,
SignalLifecycle, EvolutionView) exclusively.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_ready.models import (
    AssessmentDiff,
    EvolutionView,
    KnowledgeAssessment,
    SignalLifecycle,
    SignalStatus,
)
from ai_ready.stores import AssessmentStore, SignalStore, _SQLiteStoreMixin
from ai_ready.operations import DiffOperation


class AssessmentStoreFacade(_SQLiteStoreMixin):
    """SQLite-backed assessment storage with lifecycle tracking.

    Delegates persistence to AssessmentStore + SignalStore. All methods
    use canonical types (KnowledgeAssessment, AssessmentDiff,
    SignalLifecycle, EvolutionView).
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._assessment_store = AssessmentStore(db_path)
        self._signal_store = self._assessment_store.signal_store

    # --- Assessment CRUD (delegates to AssessmentStore) ---

    def save(self, assessment: KnowledgeAssessment) -> None:
        """Persist an assessment."""
        self._assessment_store.save(assessment)

    def load(self, assessment_id: str) -> KnowledgeAssessment | None:
        """Load an assessment by ID."""
        return self._assessment_store.load(assessment_id)

    def latest(self) -> KnowledgeAssessment | None:
        """Get the most recent assessment."""
        return self._assessment_store.latest()

    def history(self, limit: int = 10) -> list[KnowledgeAssessment]:
        """Get recent assessments ordered by time descending."""
        return self._assessment_store.history(limit=limit)

    def previous(self, assessment_id: str) -> KnowledgeAssessment | None:
        """Get the assessment immediately before the given one."""
        return self._assessment_store.previous(assessment_id)

    # --- Diff / Regression ---

    def diff(self, prev_id: str, curr_id: str) -> AssessmentDiff | None:
        """Compute diff between two assessments by ID.

        Loads both assessments then delegates to DiffOperation.compute_diff
        for the full contributor-change analysis.
        """
        prev = self.load(prev_id)
        curr = self.load(curr_id)
        if not prev or not curr:
            return None
        return DiffOperation.compute_diff(prev, curr)

    def diff_latest(self) -> AssessmentDiff | None:
        """Diff the two most recent assessments."""
        history = self.history(limit=2)
        if len(history) < 2:
            return None
        return DiffOperation.compute_diff(history[1], history[0])

    # --- Lifecycle queries (delegates to SignalStore) ---

    def get_lifecycle(self, signal_id: str) -> SignalLifecycle | None:
        """Get lifecycle info for a specific signal."""
        return self._signal_store.get_lifecycle(signal_id)

    def get_open_signals(self, limit: int = 100) -> list[SignalLifecycle]:
        """Get all currently open signals (new or persistent or recurring)."""
        return self._signal_store.get_open_signals(limit=limit)

    def get_oldest_signals(self, limit: int = 10) -> list[SignalLifecycle]:
        """Get the signals that have been open the longest."""
        open_signals = self._signal_store.get_open_signals(limit=limit)
        return sorted(open_signals, key=lambda s: s.first_seen)[:limit]

    def get_recurring_signals(self, limit: int = 50) -> list[SignalLifecycle]:
        """Get signals that resolved and then came back."""
        return self._signal_store.get_recurring_signals(limit=limit)

    def get_recently_resolved(self, limit: int = 20) -> list[SignalLifecycle]:
        """Get signals that were resolved in the most recent assessment."""
        latest = self.latest()
        if not latest:
            return []

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM signal_lifecycle
                   WHERE status = ? AND resolved_assessment = ?
                   ORDER BY last_seen DESC
                   LIMIT ?""",
                (SignalStatus.RESOLVED.value, latest.assessment_id, limit),
            ).fetchall()
            return [self._signal_store._row_to_lifecycle(r) for r in rows]

    def get_signal_stats(self) -> dict[str, Any]:
        """Get summary stats about signal lifecycle."""
        return self._signal_store.get_signal_stats()

    # --- Trend analysis (delegates to AssessmentStore) ---

    def get_trend(self, limit: int = 30) -> EvolutionView:
        """Get health evolution across recent assessments."""
        return self._assessment_store.get_trend(limit=limit)

