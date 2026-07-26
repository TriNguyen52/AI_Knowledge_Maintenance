"""SQLite snapshot store - persists scan results for regression detection."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ai_ready.models import (
    DimensionScore,
    Finding,
    FindingLifecycle,
    FindingStatus,
    RegressionReport,
    Severity,
    Snapshot,
    TrendReport,
    _severity_rank,
)


class SnapshotStore:
    """SQLite-backed snapshot storage with lifecycle tracking."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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

    def _init_db(self) -> None:
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
            # Lifecycle table - tracks findings across scans
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

    # --- Snapshot CRUD ---

    def save(self, snapshot: Snapshot) -> None:
        """Persist a snapshot, its findings, and update lifecycle tracking."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO snapshots
                   (snapshot_id, timestamp, score, dimensions, metrics, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id,
                    timestamp,
                    snapshot.score,
                    json.dumps({k: {"name": v.name, "score": v.score, "rule_ids": v.rule_ids,
                                    "findings_count": v.findings_count}
                                for k, v in snapshot.dimensions.items()}),
                    json.dumps(snapshot.metrics),
                    json.dumps(snapshot.metadata),
                ),
            )
            for f in snapshot.findings:
                conn.execute(
                    """INSERT INTO findings
                       (snapshot_id, finding_id, rule_id, issue_type, severity, score,
                        document_id, document_path, evidence, recommendation, ai_impact, line)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.snapshot_id,
                        f.finding_id,
                        f.rule_id,
                        f.issue_type,
                        f.severity.value,
                        f.score,
                        f.document_id,
                        f.document_path,
                        json.dumps(f.evidence),
                        f.recommendation,
                        f.ai_impact,
                        f.line,
                    ),
                )

            # Update lifecycle tracking
            self._update_lifecycle(conn, snapshot, timestamp)

    def _update_lifecycle(self, conn: sqlite3.Connection, snapshot: Snapshot, timestamp: str) -> None:
        """Update finding lifecycle table based on current snapshot findings."""
        # Get previous snapshot's finding IDs
        prev_snapshot = self._get_previous_snapshot_id(conn, snapshot.snapshot_id)
        prev_finding_ids: set[str] = set()
        if prev_snapshot:
            rows = conn.execute(
                "SELECT finding_id FROM findings WHERE snapshot_id = ?", (prev_snapshot,)
            ).fetchall()
            prev_finding_ids = {r["finding_id"] for r in rows}

        curr_finding_ids = {f.finding_id for f in snapshot.findings}

        # Mark resolved findings
        resolved_ids = prev_finding_ids - curr_finding_ids
        for fid in resolved_ids:
            row = conn.execute(
                "SELECT * FROM finding_lifecycle WHERE finding_id = ?", (fid,)
            ).fetchone()
            if row and row["status"] != "resolved":
                conn.execute(
                    "UPDATE finding_lifecycle SET status = ?, resolved_snapshot = ? WHERE finding_id = ?",
                    (FindingStatus.RESOLVED.value, snapshot.snapshot_id, fid),
                )

        # Upsert current findings
        for f in snapshot.findings:
            existing = conn.execute(
                "SELECT * FROM finding_lifecycle WHERE finding_id = ?", (f.finding_id,)
            ).fetchone()

            severity_entry = {
                "snapshot_id": snapshot.snapshot_id,
                "severity": f.severity.value,
                "score": f.score,
            }

            if existing:
                # Update existing finding
                sev_history = json.loads(existing["severity_history"])
                sev_history.append(severity_entry)

                snap_ids = json.loads(existing["snapshot_ids"])
                if snapshot.snapshot_id not in snap_ids:
                    snap_ids.append(snapshot.snapshot_id)

                # Determine status
                if existing["status"] == FindingStatus.RESOLVED.value:
                    status = FindingStatus.RECURRING.value
                elif existing["status"] == FindingStatus.NEW.value:
                    status = FindingStatus.PERSISTENT.value
                else:
                    status = existing["status"]

                conn.execute(
                    """UPDATE finding_lifecycle
                       SET last_seen = ?, status = ?, severity_history = ?, snapshot_ids = ?,
                           resolved_snapshot = ''
                       WHERE finding_id = ?""",
                    (timestamp, status, json.dumps(sev_history), json.dumps(snap_ids), f.finding_id),
                )
            else:
                # New finding
                conn.execute(
                    """INSERT INTO finding_lifecycle
                       (finding_id, rule_id, document_path, first_seen, last_seen, status,
                        severity_history, snapshot_ids, resolved_snapshot)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f.finding_id,
                        f.rule_id,
                        f.document_path,
                        timestamp,
                        timestamp,
                        FindingStatus.NEW.value,
                        json.dumps([severity_entry]),
                        json.dumps([snapshot.snapshot_id]),
                        "",
                    ),
                )

    def _get_previous_snapshot_id(self, conn: sqlite3.Connection, snapshot_id: str) -> str | None:
        """Get the snapshot ID immediately before the given one."""
        row = conn.execute(
            "SELECT timestamp FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        prev = conn.execute(
            "SELECT snapshot_id FROM snapshots WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1",
            (row["timestamp"],),
        ).fetchone()
        return prev["snapshot_id"] if prev else None

    def load(self, snapshot_id: str) -> Snapshot | None:
        """Load a snapshot by ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_snapshot(conn, row)

    def latest(self) -> Snapshot | None:
        """Get the most recent snapshot."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return self._row_to_snapshot(conn, row)

    def history(self, limit: int = 10) -> list[Snapshot]:
        """Get recent snapshots ordered by time descending."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_snapshot(conn, row) for row in rows]

    def previous(self, snapshot_id: str) -> Snapshot | None:
        """Get the snapshot immediately before the given one."""
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
            return self._row_to_snapshot(conn, row)

    # --- Diff / Regression ---

    def diff(self, prev_id: str, curr_id: str) -> RegressionReport | None:
        """Compute regression between two snapshots by ID."""
        prev = self.load(prev_id)
        curr = self.load(curr_id)
        if not prev or not curr:
            return None
        return self._compute_diff(prev, curr)

    def diff_latest(self) -> RegressionReport | None:
        """Diff the two most recent snapshots."""
        history = self.history(limit=2)
        if len(history) < 2:
            return None
        return self._compute_diff(history[1], history[0])

    def _compute_diff(self, prev: Snapshot, curr: Snapshot) -> RegressionReport:
        """Compute regression report between two snapshots using stable finding IDs."""
        prev_by_id = {f.finding_id: f for f in prev.findings}
        curr_by_id = {f.finding_id: f for f in curr.findings}

        prev_ids = set(prev_by_id.keys())
        curr_ids = set(curr_by_id.keys())

        new_findings = [curr_by_id[fid] for fid in curr_ids - prev_ids]
        resolved_findings = [prev_by_id[fid] for fid in prev_ids - curr_ids]
        persistent_findings = [curr_by_id[fid] for fid in curr_ids & prev_ids]

        # Detect severity changes
        severity_changes: list[dict[str, Any]] = []
        for fid in curr_ids & prev_ids:
            pf = prev_by_id[fid]
            cf = curr_by_id[fid]
            if pf.severity != cf.severity:
                severity_changes.append({
                    "finding_id": fid,
                    "rule_id": cf.rule_id,
                    "document_path": cf.document_path,
                    "prev_severity": pf.severity.value,
                    "curr_severity": cf.severity.value,
                    "direction": "worse" if _severity_rank(cf.severity) > _severity_rank(pf.severity) else "better",
                })

        # Dimension deltas
        dim_deltas: dict[str, int] = {}
        for dim_name in set(list(prev.dimensions.keys()) + list(curr.dimensions.keys())):
            prev_score = prev.dimensions[dim_name].score if dim_name in prev.dimensions else 0
            curr_score = curr.dimensions[dim_name].score if dim_name in curr.dimensions else 0
            dim_deltas[dim_name] = curr_score - prev_score

        new_high = sum(1 for f in new_findings if f.severity in (Severity.HIGH, Severity.CRITICAL))

        # Metric deltas
        prev_contradictions = prev.metrics.get("contradiction_count", 0)
        curr_contradictions = curr.metrics.get("contradiction_count", 0)
        prev_entropy = prev.metrics.get("avg_topic_entropy", 0)
        curr_entropy = curr.metrics.get("avg_topic_entropy", 0)
        prev_orphans = prev.metrics.get("orphan_documents", 0)
        curr_orphans = curr.metrics.get("orphan_documents", 0)
        prev_dupes = prev.metrics.get("duplicate_clusters", 0)
        curr_dupes = curr.metrics.get("duplicate_clusters", 0)

        contributor_changes = self._compute_contributor_changes(prev, curr)
        dimension_change_explanations = [
            {
                "dimension": name,
                "prev_score": prev.dimensions[name].score if name in prev.dimensions else None,
                "curr_score": curr.dimensions[name].score if name in curr.dimensions else None,
                "delta": delta,
                "direction": "worse" if delta < 0 else "better" if delta > 0 else "unchanged",
            }
            for name, delta in sorted(
                dim_deltas.items(),
                key=lambda item: (abs(item[1]), item[0]),
                reverse=True,
            )
            if delta != 0
        ]

        # Build explanation
        explanation_parts: list[str] = []
        if new_findings:
            explanation_parts.append(f"{len(new_findings)} new findings")
        if resolved_findings:
            explanation_parts.append(f"{len(resolved_findings)} resolved")
        if severity_changes:
            worse = sum(1 for s in severity_changes if s["direction"] == "worse")
            better = sum(1 for s in severity_changes if s["direction"] == "better")
            if worse:
                explanation_parts.append(f"{worse} findings got worse")
            if better:
                explanation_parts.append(f"{better} findings improved")
        top_regression = next(
            (c for c in contributor_changes if c["delta_estimated_score_gain"] > 0),
            None,
        )
        if top_regression:
            explanation_parts.append(
                f"largest new score pressure: {top_regression['cause']}"
            )
        if not explanation_parts:
            explanation_parts.append("No changes detected")
        explanation = "; ".join(explanation_parts) + "."

        score_change_explanation = {
            "summary": explanation,
            "score_delta": curr.score - prev.score,
            "dimension_changes": dimension_change_explanations,
            "top_regressions": [
                c for c in contributor_changes
                if c["delta_estimated_score_gain"] > 0
            ][:5],
            "top_improvements": [
                c for c in contributor_changes
                if c["delta_estimated_score_gain"] < 0
            ][:5],
        }

        recommendation = ""
        if curr.score < prev.score:
            recommendation = f"Score dropped {prev.score} -> {curr.score}. Review {len(new_findings)} new findings before deployment."
        elif new_high > 0:
            recommendation = f"{new_high} new high-severity findings. Review before deployment."
        elif len(resolved_findings) > 0 and not new_findings:
            recommendation = f"Improving. {len(resolved_findings)} findings resolved, no new issues."
        elif not new_findings:
            recommendation = "No regressions detected."

        return RegressionReport(
            prev_snapshot_id=prev.snapshot_id,
            curr_snapshot_id=curr.snapshot_id,
            prev_score=prev.score,
            curr_score=curr.score,
            score_delta=curr.score - prev.score,
            new_findings=new_findings,
            resolved_findings=resolved_findings,
            persistent_findings=persistent_findings,
            severity_changes=severity_changes,
            dimension_deltas=dim_deltas,
            new_high_count=new_high,
            increased_contradictions=max(0, curr_contradictions - prev_contradictions),
            increased_topic_entropy=max(0, curr_entropy - prev_entropy),
            new_orphan_documents=max(0, curr_orphans - prev_orphans),
            new_duplicate_clusters=max(0, curr_dupes - prev_dupes),
            score_change_explanation=score_change_explanation,
            contributor_changes=contributor_changes[:10],
            recommendation=recommendation,
            explanation=explanation,
        )

    def _compute_contributor_changes(
        self,
        prev: Snapshot,
        curr: Snapshot,
    ) -> list[dict[str, Any]]:
        """Compare derived score contributors between two snapshots."""
        prev_contributors = {
            c.key: c for c in prev.explain_score(max_contributors=50).dominant_contributors
        }
        curr_contributors = {
            c.key: c for c in curr.explain_score(max_contributors=50).dominant_contributors
        }

        changes: list[dict[str, Any]] = []
        for key in set(prev_contributors) | set(curr_contributors):
            prev_c = prev_contributors.get(key)
            curr_c = curr_contributors.get(key)
            representative = curr_c or prev_c
            if representative is None:
                continue

            prev_gain = prev_c.estimated_score_gain if prev_c else 0.0
            curr_gain = curr_c.estimated_score_gain if curr_c else 0.0
            prev_count = prev_c.finding_count if prev_c else 0
            curr_count = curr_c.finding_count if curr_c else 0
            prev_ids = set(prev_c.finding_ids if prev_c else [])
            curr_ids = set(curr_c.finding_ids if curr_c else [])

            delta = curr_gain - prev_gain
            if delta == 0 and curr_count == prev_count:
                continue

            changes.append({
                "key": key,
                "dimension": representative.dimension,
                "rule": representative.rule_id,
                "issue_type": representative.issue_type,
                "cause": representative.cause,
                "prev_estimated_score_gain": round(prev_gain, 2),
                "curr_estimated_score_gain": round(curr_gain, 2),
                "delta_estimated_score_gain": round(delta, 2),
                "prev_findings": prev_count,
                "curr_findings": curr_count,
                "finding_count_delta": curr_count - prev_count,
                "added_finding_ids": sorted(curr_ids - prev_ids)[:20],
                "removed_finding_ids": sorted(prev_ids - curr_ids)[:20],
            })

        return sorted(
            changes,
            key=lambda c: (
                abs(c["delta_estimated_score_gain"]),
                abs(c["finding_count_delta"]),
            ),
            reverse=True,
        )

    # --- Lifecycle queries ---

    def get_lifecycle(self, finding_id: str) -> FindingLifecycle | None:
        """Get lifecycle info for a specific finding."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM finding_lifecycle WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_lifecycle(row)

    def get_open_findings(self, limit: int = 100) -> list[FindingLifecycle]:
        """Get all currently open findings (new or persistent or recurring)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status IN (?, ?, ?)
                   ORDER BY first_seen ASC
                   LIMIT ?""",
                (FindingStatus.NEW.value, FindingStatus.PERSISTENT.value, FindingStatus.RECURRING.value, limit),
            ).fetchall()
            return [self._row_to_lifecycle(r) for r in rows]

    def get_oldest_findings(self, limit: int = 10) -> list[FindingLifecycle]:
        """Get the findings that have been open the longest."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status IN (?, ?, ?)
                   ORDER BY first_seen ASC
                   LIMIT ?""",
                (FindingStatus.NEW.value, FindingStatus.PERSISTENT.value, FindingStatus.RECURRING.value, limit),
            ).fetchall()
            return [self._row_to_lifecycle(r) for r in rows]

    def get_recurring_findings(self, limit: int = 50) -> list[FindingLifecycle]:
        """Get findings that resolved and then came back."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM finding_lifecycle
                   WHERE status = ?
                   ORDER BY last_seen DESC
                   LIMIT ?""",
                (FindingStatus.RECURRING.value, limit),
            ).fetchall()
            return [self._row_to_lifecycle(r) for r in rows]

    def get_recently_resolved(self, limit: int = 20) -> list[FindingLifecycle]:
        """Get findings that were resolved in the most recent scan."""
        latest = self.latest()
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
            return [self._row_to_lifecycle(r) for r in rows]

    def get_finding_stats(self) -> dict[str, Any]:
        """Get summary stats about finding lifecycle."""
        with self._conn() as conn:
            stats = {}
            for status in FindingStatus:
                row = conn.execute(
                    "SELECT COUNT(*) as count FROM finding_lifecycle WHERE status = ?",
                    (status.value,),
                ).fetchone()
                stats[status.value] = row["count"]

            # Average age of open findings
            row = conn.execute(
                """SELECT AVG(julianday('now') - julianday(first_seen)) as avg_age
                   FROM finding_lifecycle
                   WHERE status IN (?, ?, ?)""",
                (FindingStatus.NEW.value, FindingStatus.PERSISTENT.value, FindingStatus.RECURRING.value),
            ).fetchone()
            stats["avg_age_days"] = round(row["avg_age"], 1) if row["avg_age"] else 0

            return stats

    # --- Trend analysis ---

    def get_trend(self, limit: int = 30) -> TrendReport:
        """Get health trend across recent snapshots."""
        snapshots = self.history(limit=limit)
        if len(snapshots) < 2:
            return TrendReport(
                snapshots=[],
                trajectory="unknown",
                summary="Not enough snapshots for trend analysis (need at least 2).",
            )

        # Reverse to chronological order
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

        # Determine trajectory
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

    # --- Row converters ---

    def _row_to_snapshot(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Snapshot:
        """Convert a DB row to a Snapshot object."""
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

    def _row_to_lifecycle(self, row: sqlite3.Row) -> FindingLifecycle:
        """Convert a DB row to a FindingLifecycle object."""
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
