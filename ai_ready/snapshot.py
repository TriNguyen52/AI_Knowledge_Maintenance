"""SQLite snapshot store - persists scan results for regression detection.

Phase 9: SnapshotStore now delegates to AssessmentStore + SignalStore
internally. The external API is unchanged — callers still see Snapshot,
RegressionReport, FindingLifecycle, and TrendReport objects. Internally,
data is persisted in the assessments/signals/signal_lifecycle tables
managed by AssessmentStore and SignalStore.

The old snapshots/findings/finding_lifecycle tables are still created
for backward compatibility (reading legacy databases) but are no longer
written to. A fallback read path checks old tables if the new tables
don't have the requested data.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ai_ready.models import (
    AssessmentDiff,
    DimensionScore,
    Finding,
    FindingLifecycle,
    FindingStatus,
    RegressionReport,
    Severity,
    SignalLifecycle,
    Snapshot,
    TrendReport,
    _severity_rank,
)
from ai_ready.stores import AssessmentStore, SignalStore
from ai_ready.operations import DiffOperation


class SnapshotStore:
    """SQLite-backed snapshot storage with lifecycle tracking.

    Delegates persistence to AssessmentStore + SignalStore. The external
    API (Snapshot, RegressionReport, FindingLifecycle, TrendReport) is
    preserved for backward compatibility.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Internal delegation to new stores
        self._assessment_store = AssessmentStore(db_path)
        self._signal_store = self._assessment_store.signal_store
        # Initialize legacy tables for backward-compat reads
        self._init_legacy_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Context manager that properly opens and closes a connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_legacy_db(self) -> None:
        """Create legacy tables for backward-compat reads from old databases."""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    dimensions TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    metadata TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    issue_type TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    document_id TEXT NOT NULL,
                    document_path TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    ai_impact TEXT NOT NULL DEFAULT '',
                    line INTEGER DEFAULT 0,
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
                )
            """)
            self._ensure_column(conn, "findings", "issue_type", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "findings", "ai_impact", "TEXT NOT NULL DEFAULT ''")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_findings_snapshot ON findings(snapshot_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_findings_finding_id ON findings(finding_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS finding_lifecycle (
                    finding_id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL,
                    document_path TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    severity_history TEXT NOT NULL DEFAULT '[]',
                    snapshot_ids TEXT NOT NULL DEFAULT '[]',
                    resolved_snapshot TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_lifecycle_status ON finding_lifecycle(status)
            """)

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """Add a column to an existing table if the local DB predates it."""
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _has_legacy_data(self) -> bool:
        """Check if the legacy snapshots table has any rows."""
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM snapshots").fetchone()
            return row["count"] > 0

    def _has_assessment_data(self) -> bool:
        """Check if the new assessments table has any rows."""
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM assessments").fetchone()
            return row["count"] > 0

    # --- Snapshot CRUD (delegates to AssessmentStore) ---

    def save(self, snapshot: Snapshot) -> None:
        """Persist a snapshot by delegating to AssessmentStore.

        Converts the Snapshot to a KnowledgeAssessment and saves via
        AssessmentStore.save(), which persists to the assessments and
        signals tables and updates signal lifecycle tracking.
        """
        assessment = snapshot.to_assessment()
        self._assessment_store.save(assessment)

    def load(self, snapshot_id: str) -> Snapshot | None:
        """Load a snapshot by ID.

        Tries AssessmentStore first. Falls back to legacy tables if the
        ID is not found in the new tables (for old databases).
        """
        assessment = self._assessment_store.load(snapshot_id)
        if assessment is not None:
            return Snapshot.from_assessment(assessment)

        # Fallback: try legacy tables
        return self._legacy_load(snapshot_id)

    def latest(self) -> Snapshot | None:
        """Get the most recent snapshot.

        Tries AssessmentStore first, falls back to legacy tables.
        """
        assessment = self._assessment_store.latest()
        if assessment is not None:
            return Snapshot.from_assessment(assessment)

        # Fallback: try legacy tables
        return self._legacy_latest()

    def history(self, limit: int = 10) -> list[Snapshot]:
        """Get recent snapshots ordered by time descending.

        Merges results from both new and legacy tables if both have data.
        """
        assessments = self._assessment_store.history(limit=limit)
        if assessments:
            return [Snapshot.from_assessment(a) for a in assessments]

        # Fallback: try legacy tables
        return self._legacy_history(limit)

    def previous(self, snapshot_id: str) -> Snapshot | None:
        """Get the snapshot immediately before the given one."""
        assessment = self._assessment_store.previous(snapshot_id)
        if assessment is not None:
            return Snapshot.from_assessment(assessment)

        # Fallback: try legacy tables
        return self._legacy_previous(snapshot_id)

    # --- Diff / Regression ---

    def diff(self, prev_id: str, curr_id: str) -> RegressionReport | None:
        """Compute regression between two snapshots by ID.

        Loads both snapshots (via AssessmentStore with legacy fallback),
        then delegates to DiffOperation.compute_diff for the full
        contributor-change analysis.
        """
        prev = self.load(prev_id)
        curr = self.load(curr_id)
        if not prev or not curr:
            return None
        return DiffOperation.compute_diff(prev, curr)

    def diff_latest(self) -> RegressionReport | None:
        """Diff the two most recent snapshots."""
        history = self.history(limit=2)
        if len(history) < 2:
            return None
        return DiffOperation.compute_diff(history[1], history[0])

    def _compute_diff(self, prev: Snapshot, curr: Snapshot) -> RegressionReport:
        """Compute regression report between two snapshots.

        Delegates to DiffOperation for the full diff logic including
        contributor changes and score explanations.
        """
        return DiffOperation.compute_diff(prev, curr)

    # --- Lifecycle queries (delegates to SignalStore) ---

    def get_lifecycle(self, finding_id: str) -> FindingLifecycle | None:
        """Get lifecycle info for a specific finding.

        Delegates to SignalStore, converting SignalLifecycle to
        FindingLifecycle for backward compatibility.
        """
        # Try new store first
        signal_lifecycle = self._signal_store.get_lifecycle(finding_id)
        if signal_lifecycle is not None:
            return signal_lifecycle.to_finding_lifecycle()

        # Fallback: try legacy tables
        return self._legacy_get_lifecycle(finding_id)

    def get_open_findings(self, limit: int = 100) -> list[FindingLifecycle]:
        """Get all currently open findings (new or persistent or recurring)."""
        # Try new store first
        open_signals = self._signal_store.get_open_signals(limit=limit)
        if open_signals:
            return [s.to_finding_lifecycle() for s in open_signals]

        # Fallback: try legacy tables
        return self._legacy_get_open_findings(limit)

    def get_oldest_findings(self, limit: int = 10) -> list[FindingLifecycle]:
        """Get the findings that have been open the longest."""
        open_signals = self._signal_store.get_open_signals(limit=limit)
        if open_signals:
            # Sort by first_seen ascending (oldest first)
            sorted_signals = sorted(open_signals, key=lambda s: s.first_seen)
            return [s.to_finding_lifecycle() for s in sorted_signals[:limit]]

        # Fallback: try legacy tables
        return self._legacy_get_oldest_findings(limit)

    def get_recurring_findings(self, limit: int = 50) -> list[FindingLifecycle]:
        """Get findings that resolved and then came back."""
        recurring = self._signal_store.get_recurring_signals(limit=limit)
        if recurring:
            return [s.to_finding_lifecycle() for s in recurring]

        # Fallback: try legacy tables
        return self._legacy_get_recurring_findings(limit)

    def get_recently_resolved(self, limit: int = 20) -> list[FindingLifecycle]:
        """Get findings that were resolved in the most recent scan."""
        latest = self.latest()
        if not latest:
            return []

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM signal_lifecycle
                   WHERE status = ? AND resolved_assessment = ?
                   ORDER BY last_seen DESC
                   LIMIT ?""",
                (FindingStatus.RESOLVED.value, latest.snapshot_id, limit),
            ).fetchall()
            if rows:
                return [
                    SignalLifecycle(
                        signal_id=r["signal_id"],
                        collector_id=r["collector_id"],
                        artifact_uri=r["artifact_uri"],
                        first_seen=r["first_seen"],
                        last_seen=r["last_seen"],
                        status=FindingStatus(r["status"]),
                        severity_history=json.loads(r["severity_history"]),
                        assessment_ids=json.loads(r["assessment_ids"]),
                        resolved_assessment=r["resolved_assessment"],
                    ).to_finding_lifecycle()
                    for r in rows
                ]

        # Fallback: try legacy tables
        return self._legacy_get_recently_resolved(limit)

    def get_finding_stats(self) -> dict[str, Any]:
        """Get summary stats about finding lifecycle."""
        # Try new store first
        stats = self._signal_store.get_signal_stats()
        if stats and any(v > 0 for k, v in stats.items() if k != "avg_age_days"):
            # Rename keys for backward compatibility
            return stats  # Keys are already the same (new, persistent, recurring, resolved)

        # Fallback: try legacy tables
        return self._legacy_get_finding_stats()

    # --- Trend analysis (delegates to AssessmentStore) ---

    def get_trend(self, limit: int = 30) -> TrendReport:
        """Get health trend across recent snapshots.

        Delegates to AssessmentStore.get_trend() and converts the
        EvolutionView to a TrendReport for backward compatibility.
        """
        evolution = self._assessment_store.get_trend(limit=limit)
        trend = evolution.to_trend_report()

        # If new store has data, return it
        if evolution.score_trend:
            return trend

        # Fallback: try legacy tables
        legacy_trend = self._legacy_get_trend(limit)
        if legacy_trend.score_trend:
            return legacy_trend

        return trend

    # --- Legacy fallback methods ---

    def _legacy_load(self, snapshot_id: str) -> Snapshot | None:
        """Load from legacy snapshots table."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if not row:
                return None
            return self._legacy_row_to_snapshot(conn, row)

    def _legacy_latest(self) -> Snapshot | None:
        """Get latest from legacy table."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return self._legacy_row_to_snapshot(conn, row)

    def _legacy_history(self, limit: int = 10) -> list[Snapshot]:
        """Get history from legacy table."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._legacy_row_to_snapshot(conn, row) for row in rows]

    def _legacy_previous(self, snapshot_id: str) -> Snapshot | None:
        """Get previous from legacy table."""
        with self._conn() as conn:
            target = conn.execute(
                "SELECT timestamp FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if not target:
                return None
            row = conn.execute(
                """SELECT * FROM snapshots
                   WHERE timestamp < ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (target["timestamp"],),
            ).fetchone()
            if not row:
                return None
            return self._legacy_row_to_snapshot(conn, row)

    def _legacy_get_lifecycle(self, finding_id: str) -> FindingLifecycle | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM finding_lifecycle WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            if not row:
                return None
            return self._legacy_row_to_lifecycle(row)

    def _legacy_get_open_findings(self, limit: int = 100) -> list[FindingLifecycle]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status IN (?, ?, ?)
                   ORDER BY first_seen ASC
                   LIMIT ?""",
                (FindingStatus.NEW.value, FindingStatus.PERSISTENT.value, FindingStatus.RECURRING.value, limit),
            ).fetchall()
            return [self._legacy_row_to_lifecycle(r) for r in rows]

    def _legacy_get_oldest_findings(self, limit: int = 10) -> list[FindingLifecycle]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status IN (?, ?, ?)
                   ORDER BY first_seen ASC
                   LIMIT ?""",
                (FindingStatus.NEW.value, FindingStatus.PERSISTENT.value, FindingStatus.RECURRING.value, limit),
            ).fetchall()
            return [self._legacy_row_to_lifecycle(r) for r in rows]

    def _legacy_get_recurring_findings(self, limit: int = 50) -> list[FindingLifecycle]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status = ?
                   ORDER BY last_seen DESC
                   LIMIT ?""",
                (FindingStatus.RECURRING.value, limit),
            ).fetchall()
            return [self._legacy_row_to_lifecycle(r) for r in rows]

    def _legacy_get_recently_resolved(self, limit: int = 20) -> list[FindingLifecycle]:
        latest = self._legacy_latest()
        if not latest:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status = ? AND resolved_snapshot = ?
                   ORDER BY last_seen DESC
                   LIMIT ?""",
                (FindingStatus.RESOLVED.value, latest.snapshot_id, limit),
            ).fetchall()
            return [self._legacy_row_to_lifecycle(r) for r in rows]

    def _legacy_get_finding_stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            stats = {}
            for status in FindingStatus:
                row = conn.execute(
                    "SELECT COUNT(*) as count FROM finding_lifecycle WHERE status = ?",
                    (status.value,),
                ).fetchone()
                stats[status.value] = row["count"]

            row = conn.execute(
                """SELECT AVG(julianday('now') - julianday(first_seen)) as avg_age
                   FROM finding_lifecycle
                   WHERE status IN (?, ?, ?)""",
                (FindingStatus.NEW.value, FindingStatus.PERSISTENT.value, FindingStatus.RECURRING.value),
            ).fetchone()
            stats["avg_age_days"] = round(row["avg_age"], 1) if row["avg_age"] else 0

            return stats

    def _legacy_get_trend(self, limit: int = 30) -> TrendReport:
        """Get trend from legacy snapshots table."""
        snapshots = self._legacy_history(limit=limit)
        if len(snapshots) < 2:
            return TrendReport(
                snapshots=[],
                trajectory="unknown",
                summary="Not enough snapshots for trend analysis (need at least 2).",
            )

        snapshots.reverse()

        score_trend = []
        finding_trend = []
        dim_trends: dict[str, list[dict[str, Any]]] = {}

        for snap in snapshots:
            score_trend.append({
                "snapshot_id": snap.snapshot_id,
                "score": snap.score,
                "timestamp": snap.metadata.get("timestamp", snap.snapshot_id),
            })
            finding_trend.append({
                "snapshot_id": snap.snapshot_id,
                "total_findings": len(snap.findings),
                "high_findings": sum(1 for f in snap.findings if f.severity in (Severity.HIGH, Severity.CRITICAL)),
            })
            for dim_name, dim in snap.dimensions.items():
                dim_trends.setdefault(dim_name, []).append({
                    "snapshot_id": snap.snapshot_id,
                    "score": dim.score,
                })

        if len(score_trend) >= 2:
            recent_scores = [s["score"] for s in score_trend[-3:]] if len(score_trend) >= 3 else [s["score"] for s in score_trend]
            first_score = recent_scores[0]
            last_score = recent_scores[-1]
            delta = last_score - first_score
            if delta > 3:
                trajectory = "improving"
            elif delta < -3:
                trajectory = "worsening"
            else:
                trajectory = "stable"
        else:
            trajectory = "stable"

        summary_parts = [
            f"Score: {score_trend[0]['score']} -> {score_trend[-1]['score']} over {len(snapshots)} scans",
            f"Trajectory: {trajectory}",
            f"Findings: {finding_trend[0]['total_findings']} -> {finding_trend[-1]['total_findings']}",
        ]

        return TrendReport(
            snapshots=[{"snapshot_id": s.snapshot_id, "score": s.score} for s in snapshots],
            score_trend=score_trend,
            finding_trend=finding_trend,
            dimension_trends=dim_trends,
            trajectory=trajectory,
            summary=". ".join(summary_parts) + ".",
        )

    # --- Legacy row converters ---

    def _legacy_row_to_snapshot(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Snapshot:
        """Convert a legacy DB row to a Snapshot object."""
        dims_data = json.loads(row["dimensions"])
        dimensions: dict[str, DimensionScore] = {}
        for k, v in dims_data.items():
            dimensions[k] = DimensionScore(
                name=v["name"], score=v["score"], rule_ids=v["rule_ids"],
                findings_count=v["findings_count"],
            )

        findings: list[Finding] = []
        frows = conn.execute(
            "SELECT * FROM findings WHERE snapshot_id = ?", (row["snapshot_id"],)
        ).fetchall()
        for fr in frows:
            keys = set(fr.keys())
            findings.append(Finding(
                finding_id=fr["finding_id"],
                rule_id=fr["rule_id"],
                issue_type=fr["issue_type"] if "issue_type" in keys else "",
                severity=Severity(fr["severity"]),
                score=fr["score"],
                document_id=fr["document_id"],
                document_path=fr["document_path"],
                evidence=json.loads(fr["evidence"]),
                recommendation=fr["recommendation"],
                ai_impact=fr["ai_impact"] if "ai_impact" in keys else "",
                line=fr["line"],
            ))

        return Snapshot(
            snapshot_id=row["snapshot_id"],
            score=row["score"],
            dimensions=dimensions,
            findings=findings,
            metrics=json.loads(row["metrics"]),
            metadata=json.loads(row["metadata"]),
        )

    def _legacy_row_to_lifecycle(self, row: sqlite3.Row) -> FindingLifecycle:
        """Convert a legacy DB row to a FindingLifecycle object."""
        return FindingLifecycle(
            finding_id=row["finding_id"],
            rule_id=row["rule_id"],
            document_path=row["document_path"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            status=FindingStatus(row["status"]),
            severity_history=json.loads(row["severity_history"]),
            snapshot_ids=json.loads(row["snapshot_ids"]),
            resolved_snapshot=row["resolved_snapshot"],
        )
