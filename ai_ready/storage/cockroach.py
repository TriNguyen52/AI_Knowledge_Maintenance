"""
CockroachDB storage backend for AI Knowledge Maintenance.

This module provides a persistent memory layer using CockroachDB with:
- Distributed vector indexing for semantic similarity search on knowledge artifacts
- Agent state persistence — Burr workflow state survives across Lambda invocations
- Full signal lifecycle tracking (new → persistent → resolved → recurring)
- Assessment history with regression diffing
- Problem queue with salience ranking and wont-fix suppression
- Remediation history for institutional memory (EMA strategy scoring)
- Context retrieval — agent reads past outcomes to inform future decisions
- Decision traces for full observability of the agentic workflow
- Transactional writes with CockroachDB retry protocol for distributed safety

CockroachDB is the persistent memory layer — the agent's long-term memory.
All knowledge artifacts, signals, assessments, problems, remediation
outcomes, AND agent workflow state are stored here and survive across
agent restarts. The agent both writes to and reads from CockroachDB,
creating a closed-loop memory: past outcomes inform future decisions.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from .models import (
    ArtifactRecord,
    AssessmentRecord,
    ProblemRecord,
    RemediationRecord,
    SignalRecord,
    WontFixSignature,
)

logger = logging.getLogger(__name__)

# CockroachDB retry configuration
MAX_RETRIES = 3
RETRY_DELAY_MS = 100  # Initial backoff in milliseconds


class CockroachDBStore:
    """
    Persistent memory store backed by CockroachDB.

    Uses psycopg2 for connection management. Supports vector similarity
    search via CockroachDB's distributed vector indexing.

    Connection string can be provided directly or via the COCKROACH_DB_URL
    environment variable. Format:
        postgresql://<user>:<password>@<host>:<port>/<database>?sslmode=require
    """

    SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

    def __init__(self, connection_string: str | None = None):
        self.connection_string = connection_string or os.environ.get(
            "COCKROACH_DB_URL",
            ""
        )
        self._conn = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Connection management with retry logic
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish connection to CockroachDB cluster with retry on transient failures."""
        import psycopg2  # type: ignore[import-untyped]

        if not self.connection_string:
            raise ValueError(
                "No connection string provided. Set COCKROACH_DB_URL env var "
                "or pass connection_string to CockroachDBStore()."
            )

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                self._conn = psycopg2.connect(
                    self.connection_string,
                    application_name="ai-knowledge-maintenance",
                    connect_timeout=10,
                )
                self._conn.autocommit = True
                logger.info("Connected to CockroachDB (attempt %d)", attempt + 1)
                return
            except psycopg2.OperationalError as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY_MS * (2 ** attempt) / 1000.0
                    logger.warning(
                        "CockroachDB connection attempt %d failed: %s. Retrying in %.1fs",
                        attempt + 1, e, delay,
                    )
                    time.sleep(delay)

        raise ConnectionError(
            f"Failed to connect to CockroachDB after {MAX_RETRIES} attempts: {last_error}"
        )

    @property
    def conn(self):
        if self._conn is None or self._conn.closed:
            self.connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def run_transaction(self, operations: Callable[[Any], None]) -> None:
        """Execute multiple operations in a single CockroachDB transaction.

        Uses CockroachDB's ACID transactions with automatic retry on
        serialization conflicts. This is critical for distributed databases
        where concurrent writes can cause conflicts.

        Args:
            operations: A callable that takes a cursor and executes SQL.
                        All operations are committed atomically — either all
                        succeed or all roll back.
        """
        conn = self.conn
        # Temporarily disable autocommit for transaction
        conn.autocommit = False
        try:
            for attempt in range(MAX_RETRIES):
                try:
                    with conn.cursor() as cur:
                        operations(cur)
                    conn.commit()
                    return
                except Exception as e:
                    conn.rollback()
                    if attempt < MAX_RETRIES - 1:
                        # Check if this is a retryable error (serialization conflict)
                        error_str = str(e).lower()
                        if any(code in error_str for code in [
                            "40001",  # serialization failure
                            "40101",  # retry write
                            "40003",  # connection failure
                        ]):
                            delay = RETRY_DELAY_MS * (2 ** attempt) / 1000.0
                            logger.warning(
                                "Transaction retry %d: %s. Backing off %.1fs",
                                attempt + 1, e, delay,
                            )
                            time.sleep(delay)
                            continue
                    raise
        finally:
            conn.autocommit = True

    def initialize_schema(self) -> None:
        """Create all tables if they don't exist.

        The schema.sql file contains all DDL including a vector index.
        If the cluster version does not support CREATE VECTOR INDEX IF NOT EXISTS,
        we fall back to a guarded CREATE VECTOR INDEX (which will no-op if the
        index already exists, or fail gracefully if vector indexes are unsupported).
        """
        if self._initialized:
            return

        with open(self.SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        with self.conn.cursor() as cur:
            cur.execute(schema_sql)

        # Ensure the vector index exists — some CockroachDB versions don't
        # support IF NOT EXISTS on vector indexes.  Try the guarded form.
        try:
            cur.execute(
                "CREATE VECTOR INDEX IF NOT EXISTS idx_artifacts_embedding "
                "ON knowledge_artifacts (embedding)"
            )
        except Exception as e:
            logger.info(
                "Vector index creation skipped (may already exist or "
                "unsupported on this cluster version): %s", e
            )

        self._initialized = True

    def _execute(self, query: str, params: tuple = ()) -> Any:
        """Execute a write query and return None (cursor is closed).

        For write operations (INSERT/UPDATE/DELETE), the cursor is closed
        immediately after execute.  For read operations, use
        ``_execute_fetchone`` or ``_execute_fetchall`` which also close
        the cursor after fetching.
        """
        cur = self.conn.cursor()
        try:
            cur.execute(query, params)
        finally:
            cur.close()
        return None

    def _execute_fetchone(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict."""
        cur = self.conn.cursor()
        try:
            cur.execute(query, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
        finally:
            cur.close()

    def _execute_fetchall(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as dicts."""
        cur = self.conn.cursor()
        try:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # Knowledge Artifacts (with vector embeddings)
    # ------------------------------------------------------------------

    def save_artifact(self, record: ArtifactRecord) -> str:
        """Insert or update a knowledge artifact with its embedding."""
        embedding_str = None
        if record.embedding is not None:
            # CockroachDB vector format: '[1.0, 2.0, 3.0, ...]'
            embedding_str = f"[{','.join(str(x) for x in record.embedding)}]"

        self._execute(
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
        return record.artifact_id

    def get_artifact(self, artifact_uri: str) -> ArtifactRecord | None:
        """Retrieve a single artifact by URI."""
        row = self._execute_fetchone(
            "SELECT * FROM knowledge_artifacts WHERE artifact_uri = %s",
            (artifact_uri,),
        )
        if row is None:
            return None
        return self._row_to_artifact(row)

    def get_all_artifacts(self) -> list[ArtifactRecord]:
        """Retrieve all artifacts."""
        rows = self._execute_fetchall("SELECT * FROM knowledge_artifacts ORDER BY artifact_uri")
        return [self._row_to_artifact(r) for r in rows]

    def search_artifacts_semantic(
        self, query_embedding: list[float], limit: int = 10
    ) -> list[tuple[ArtifactRecord, float]]:
        """
        Semantic similarity search using CockroachDB distributed vector indexing.

        Returns list of (artifact, similarity_score) tuples sorted by similarity.
        Uses CockroachDB's built-in vector distance operators.
        """
        embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
        rows = self._execute_fetchall(
            """
            SELECT *, embedding <=> %s::vector AS distance
            FROM knowledge_artifacts
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding_str, embedding_str, limit),
        )
        results = []
        for row in rows:
            distance = row.pop("distance", 1.0)
            similarity = 1.0 - distance  # Convert distance to similarity
            results.append((self._row_to_artifact(row), similarity))
        return results

    def _row_to_artifact(self, row: dict[str, Any]) -> ArtifactRecord:
        embedding = None
        if row.get("embedding") is not None:
            emb_str = str(row["embedding"])
            # Parse CockroachDB vector string '[1.0, 2.0, ...]'
            emb_str = emb_str.strip("[]")
            if emb_str:
                embedding = [float(x) for x in emb_str.split(",")]

        return ArtifactRecord(
            artifact_id=str(row["artifact_id"]),
            artifact_uri=row["artifact_uri"],
            artifact_type=row["artifact_type"],
            source=row["source"],
            content=row["content"],
            metadata=row["metadata"] if isinstance(row["metadata"], str) else json.dumps(row["metadata"]),
            embedding=embedding,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Knowledge Signals
    # ------------------------------------------------------------------

    def save_signal(self, record: SignalRecord) -> str:
        """Insert a new knowledge signal."""
        self._execute(
            """
            INSERT INTO knowledge_signals
                (signal_id, artifact_uri, collector_id, signal_type,
                 severity, score, recommendation, ai_impact, evidence,
                 status, last_seen, access_count, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.signal_id, record.artifact_uri, record.collector_id,
                record.signal_type, record.severity, record.score,
                record.recommendation, record.ai_impact, record.evidence,
                record.status, record.last_seen, record.access_count,
                record.created_at,
            ),
        )
        return record.signal_id

    def get_signals_for_artifact(self, artifact_uri: str) -> list[SignalRecord]:
        """Get all signals for a specific artifact."""
        rows = self._execute_fetchall(
            "SELECT * FROM knowledge_signals WHERE artifact_uri = %s ORDER BY created_at",
            (artifact_uri,),
        )
        return [self._row_to_signal(r) for r in rows]

    def get_signals_by_status(self, status: str) -> list[SignalRecord]:
        """Get all signals with a given status."""
        rows = self._execute_fetchall(
            "SELECT * FROM knowledge_signals WHERE status = %s ORDER BY last_seen DESC",
            (status,),
        )
        return [self._row_to_signal(r) for r in rows]

    def get_all_signals(self) -> list[SignalRecord]:
        """Get all signals."""
        rows = self._execute_fetchall(
            "SELECT * FROM knowledge_signals ORDER BY created_at"
        )
        return [self._row_to_signal(r) for r in rows]

    def update_signal_status(self, signal_id: str, status: str) -> None:
        """Update a signal's lifecycle status."""
        self._execute(
            "UPDATE knowledge_signals SET status = %s, last_seen = now() WHERE signal_id = %s",
            (status, signal_id),
        )

    def increment_signal_access(self, signal_id: str) -> None:
        """Increment access count for a signal (LTP retrieval feedback)."""
        self._execute(
            """
            UPDATE knowledge_signals
            SET access_count = access_count + 1, last_seen = now()
            WHERE signal_id = %s
            """,
            (signal_id,),
        )

    def _row_to_signal(self, row: dict[str, Any]) -> SignalRecord:
        return SignalRecord(
            signal_id=str(row["signal_id"]),
            artifact_uri=row["artifact_uri"],
            collector_id=row["collector_id"],
            signal_type=row["signal_type"],
            severity=row["severity"],
            score=row["score"],
            recommendation=row["recommendation"],
            ai_impact=row["ai_impact"],
            evidence=row["evidence"] if isinstance(row["evidence"], str) else json.dumps(row["evidence"]),
            status=row["status"],
            last_seen=row["last_seen"],
            access_count=row["access_count"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Signal Lifecycle
    # ------------------------------------------------------------------

    def save_signal_lifecycle(
        self, signal_id: str, assessment_id: str, status: str
    ) -> None:
        """Insert or update signal lifecycle tracking."""
        self._execute(
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
            (signal_id, assessment_id, status, assessment_id),
        )

    def get_signal_lifecycle(self, signal_id: str) -> dict[str, Any] | None:
        row = self._execute_fetchone(
            "SELECT * FROM signal_lifecycle WHERE signal_id = %s",
            (signal_id,),
        )
        return row

    def get_oldest_signals(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get signals that have been unresolved the longest."""
        return self._execute_fetchall(
            """
            SELECT sl.*, s.artifact_uri, s.collector_id, s.signal_type, s.severity
            FROM signal_lifecycle sl
            JOIN knowledge_signals s ON sl.signal_id = s.signal_id
            WHERE sl.status != 'resolved'
            ORDER BY sl.first_seen ASC
            LIMIT %s
            """,
            (limit,),
        )

    # ------------------------------------------------------------------
    # Assessments
    # ------------------------------------------------------------------

    def save_assessment(self, record: AssessmentRecord) -> str:
        """Insert a new assessment snapshot."""
        self._execute(
            """
            INSERT INTO knowledge_assessments
                (assessment_id, score, dimensions, signals_count, metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                record.assessment_id, record.score, record.dimensions,
                record.signals_count, record.metadata, record.created_at,
            ),
        )
        return record.assessment_id

    def link_signal_to_assessment(self, assessment_id: str, signal_id: str) -> None:
        """Link a signal to an assessment (junction table)."""
        self._execute(
            "INSERT INTO assessment_signals (assessment_id, signal_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (assessment_id, signal_id),
        )

    def get_latest_assessment(self) -> AssessmentRecord | None:
        row = self._execute_fetchone(
            "SELECT * FROM knowledge_assessments ORDER BY created_at DESC LIMIT 1"
        )
        if row is None:
            return None
        return self._row_to_assessment(row)

    def get_assessment_history(self, limit: int = 10) -> list[AssessmentRecord]:
        rows = self._execute_fetchall(
            "SELECT * FROM knowledge_assessments ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [self._row_to_assessment(r) for r in rows]

    def diff_assessments(
        self, prev_id: str, curr_id: str
    ) -> dict[str, Any]:
        """Compute diff between two assessments."""
        prev = self._execute_fetchone(
            "SELECT * FROM knowledge_assessments WHERE assessment_id = %s",
            (prev_id,),
        )
        curr = self._execute_fetchone(
            "SELECT * FROM knowledge_assessments WHERE assessment_id = %s",
            (curr_id,),
        )
        if prev is None or curr is None:
            return {}

        prev_signals = {
            r["signal_id"] for r in
            self._execute_fetchall(
                "SELECT signal_id FROM assessment_signals WHERE assessment_id = %s",
                (prev_id,),
            )
        }
        curr_signals = {
            r["signal_id"] for r in
            self._execute_fetchall(
                "SELECT signal_id FROM assessment_signals WHERE assessment_id = %s",
                (curr_id,),
            )
        }

        new_signals = curr_signals - prev_signals
        resolved_signals = prev_signals - curr_signals
        persistent_signals = curr_signals & prev_signals

        return {
            "prev_score": prev["score"],
            "curr_score": curr["score"],
            "score_delta": curr["score"] - prev["score"],
            "new_signal_ids": list(new_signals),
            "resolved_signal_ids": list(resolved_signals),
            "persistent_signal_ids": list(persistent_signals),
            "new_count": len(new_signals),
            "resolved_count": len(resolved_signals),
            "persistent_count": len(persistent_signals),
        }

    def _row_to_assessment(self, row: dict[str, Any]) -> AssessmentRecord:
        return AssessmentRecord(
            assessment_id=str(row["assessment_id"]),
            score=row["score"],
            dimensions=row["dimensions"] if isinstance(row["dimensions"], str) else json.dumps(row["dimensions"]),
            signals_count=row["signals_count"],
            metadata=row["metadata"] if isinstance(row["metadata"], str) else json.dumps(row["metadata"]),
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Problem Queue
    # ------------------------------------------------------------------

    def save_problem(self, record: ProblemRecord) -> str:
        """Insert or update a problem in the queue."""
        self._execute(
            """
            INSERT INTO problem_queue
                (problem_id, category, artifact_uris, salience,
                 encoding_score, outcome_score, retrieval_score,
                 staleness_factor, status, description, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (problem_id) DO UPDATE SET
                salience = EXCLUDED.salience,
                encoding_score = EXCLUDED.encoding_score,
                outcome_score = EXCLUDED.outcome_score,
                retrieval_score = EXCLUDED.retrieval_score,
                staleness_factor = EXCLUDED.staleness_factor,
                updated_at = now()
            """,
            (
                record.problem_id, record.category, record.artifact_uris,
                record.salience, record.encoding_score, record.outcome_score,
                record.retrieval_score, record.staleness_factor, record.status,
                record.description, record.created_at, record.updated_at,
            ),
        )
        return record.problem_id

    def get_problems(
        self, status: str | None = None, category: str | None = None,
        min_salience: float = 0.0, limit: int = 50
    ) -> list[ProblemRecord]:
        """Query problems from the queue with optional filters."""
        query = "SELECT * FROM problem_queue WHERE salience >= %s"
        params: list[Any] = [min_salience]

        if status is not None:
            query += " AND status = %s"
            params.append(status)
        if category is not None:
            query += " AND category = %s"
            params.append(category)

        query += " ORDER BY salience DESC LIMIT %s"
        params.append(limit)

        rows = self._execute_fetchall(query, tuple(params))
        return [self._row_to_problem(r) for r in rows]

    def update_problem_status(self, problem_id: str, status: str) -> None:
        """Update a problem's status."""
        self._execute(
            "UPDATE problem_queue SET status = %s, updated_at = now() WHERE problem_id = %s",
            (status, problem_id),
        )

    def get_queue_summary(self) -> dict[str, Any]:
        """Get summary statistics for the problem queue."""
        rows = self._execute_fetchall(
            """
            SELECT status, category, count(*) as count
            FROM problem_queue
            GROUP BY status, category
            ORDER BY status, count DESC
            """
        )
        summary: dict[str, Any] = {"by_status": {}, "by_category": {}}
        for row in rows:
            s = row["status"]
            c = row["category"]
            cnt = row["count"]
            summary["by_status"][s] = summary["by_status"].get(s, 0) + cnt
            summary["by_category"][c] = summary["by_category"].get(c, 0) + cnt

        top_open = self.get_problems(status="open", limit=5)
        summary["top_open"] = [p.to_dict() for p in top_open]
        return summary

    def _row_to_problem(self, row: dict[str, Any]) -> ProblemRecord:
        return ProblemRecord(
            problem_id=str(row["problem_id"]),
            category=row["category"],
            artifact_uris=row["artifact_uris"] if isinstance(row["artifact_uris"], str) else json.dumps(row["artifact_uris"]),
            salience=row["salience"],
            encoding_score=row["encoding_score"],
            outcome_score=row["outcome_score"],
            retrieval_score=row["retrieval_score"],
            staleness_factor=row["staleness_factor"],
            status=row["status"],
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Wont-Fix Suppression
    # ------------------------------------------------------------------

    def add_wont_fix_signature(self, category: str, artifact_uris: list[str]) -> str:
        """Register a wont-fix signature to suppress re-discovery."""
        sig = WontFixSignature.new(category, artifact_uris)
        self._execute(
            """
            INSERT INTO wont_fix_signatures (signature_id, category, artifact_uris, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (sig.signature_id, sig.category, sig.artifact_uris, sig.created_at),
        )
        return sig.signature_id

    def get_wont_fix_signatures(self) -> list[dict[str, Any]]:
        """Get all wont-fix signatures."""
        return self._execute_fetchall(
            "SELECT * FROM wont_fix_signatures ORDER BY created_at DESC"
        )

    def is_wont_fix(self, category: str, artifact_uris: list[str]) -> bool:
        """Check if a problem signature is in the wont-fix set."""
        sorted_uris = json.dumps(sorted(artifact_uris))
        row = self._execute_fetchone(
            """
            SELECT 1 FROM wont_fix_signatures
            WHERE category = %s AND artifact_uris = %s LIMIT 1
            """,
            (category, sorted_uris),
        )
        return row is not None

    # ------------------------------------------------------------------
    # Remediation History (Institutional Memory)
    # ------------------------------------------------------------------

    def save_remediation_outcome(self, record: RemediationRecord) -> str:
        """Store a remediation outcome for institutional memory."""
        self._execute(
            """
            INSERT INTO remediation_history
                (outcome_id, issue_type, strategy, strategy_description,
                 verification_outcome, score_before, score_after,
                 proposal_reasoning, tokens_used, latency_ms, forked, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.outcome_id, record.issue_type, record.strategy,
                record.strategy_description, record.verification_outcome,
                record.score_before, record.score_after,
                record.proposal_reasoning, record.tokens_used,
                record.latency_ms, record.forked, record.created_at,
            ),
        )
        return record.outcome_id

    def get_remediation_history(
        self, issue_type: str | None = None, limit: int = 50
    ) -> list[RemediationRecord]:
        """Query remediation history, optionally filtered by issue type."""
        if issue_type:
            rows = self._execute_fetchall(
                "SELECT * FROM remediation_history WHERE issue_type = %s ORDER BY created_at DESC LIMIT %s",
                (issue_type, limit),
            )
        else:
            rows = self._execute_fetchall(
                "SELECT * FROM remediation_history ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
        return [self._row_to_remediation(r) for r in rows]

    def get_successful_strategies(self, issue_type: str) -> list[dict[str, Any]]:
        """Get strategies that successfully resolved problems of a given type."""
        return self._execute_fetchall(
            """
            SELECT strategy, count(*) as count, avg(score_after - score_before) as avg_improvement
            FROM remediation_history
            WHERE issue_type = %s AND verification_outcome IN ('resolved', 'partially_resolved')
            GROUP BY strategy ORDER BY count DESC
            """,
            (issue_type,),
        )

    def get_failed_strategies(self, issue_type: str) -> list[dict[str, Any]]:
        """Get strategies that failed to resolve problems of a given type."""
        return self._execute_fetchall(
            """
            SELECT strategy, count(*) as count
            FROM remediation_history
            WHERE issue_type = %s AND verification_outcome IN ('unchanged', 'regressed', 'misdiagnosed')
            GROUP BY strategy ORDER BY count DESC
            """,
            (issue_type,),
        )

    def get_remediation_metrics(self) -> dict[str, Any]:
        """Get aggregate metrics for all remediation outcomes."""
        row = self._execute_fetchone(
            """
            SELECT
                count(*) as total_workflows,
                count(*) FILTER (WHERE verification_outcome = 'resolved') as problems_resolved,
                count(*) FILTER (WHERE verification_outcome IN ('resolved', 'partially_resolved')) as successful,
                avg(score_after - score_before) as avg_score_improvement,
                count(*) FILTER (WHERE forked = true) as total_forks,
                sum(tokens_used) as total_tokens
            FROM remediation_history
            """
        )
        if row is None:
            return {}
        total = row.get("total_workflows", 0) or 0
        successful = row.get("successful", 0) or 0
        return {
            "total_workflows": total,
            "problems_resolved": row.get("problems_resolved", 0),
            "verification_success_rate": successful / total if total > 0 else 0.0,
            "avg_score_improvement": row.get("avg_score_improvement", 0.0) or 0.0,
            "total_forks": row.get("total_forks", 0),
            "total_tokens": row.get("total_tokens", 0),
        }

    def _row_to_remediation(self, row: dict[str, Any]) -> RemediationRecord:
        return RemediationRecord(
            outcome_id=str(row["outcome_id"]),
            issue_type=row["issue_type"],
            strategy=row["strategy"],
            strategy_description=row["strategy_description"],
            verification_outcome=row["verification_outcome"],
            score_before=row["score_before"],
            score_after=row["score_after"],
            proposal_reasoning=row["proposal_reasoning"],
            tokens_used=row["tokens_used"],
            latency_ms=row["latency_ms"],
            forked=row["forked"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Decision Traces (Observability)
    # ------------------------------------------------------------------

    def save_decision_trace(
        self, outcome_id: str, stage: str, reasoning: str,
        llm_metadata: dict[str, Any] | None = None
    ) -> str:
        """Store a decision trace for observability."""
        trace_id = str(uuid_mod.uuid4())

        self._execute(
            """
            INSERT INTO decision_traces (trace_id, outcome_id, stage, reasoning, llm_metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (trace_id, outcome_id, stage, reasoning,
             json.dumps(llm_metadata or {}), datetime.now(timezone.utc)),
        )
        return trace_id

    def get_decision_traces(self, outcome_id: str) -> list[dict[str, Any]]:
        """Get all decision traces for a remediation outcome."""
        return self._execute_fetchall(
            "SELECT * FROM decision_traces WHERE outcome_id = %s ORDER BY created_at",
            (outcome_id,),
        )

    # ------------------------------------------------------------------
    # Skip Events (Autonomy Gate / Signal-Delta)
    # ------------------------------------------------------------------

    def save_skip_event(
        self, lane: str, reason: str, config_key: str = "",
        artifact_uri: str = ""
    ) -> str:
        """Record a skip event (when processing was intentionally skipped)."""
        skip_id = str(uuid_mod.uuid4())
        self._execute(
            """
            INSERT INTO skip_events (skip_id, lane, reason, config_key, artifact_uri, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (skip_id, lane, reason, config_key, artifact_uri, datetime.now(timezone.utc)),
        )
        return skip_id

    def get_skip_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent skip events."""
        return self._execute_fetchall(
            "SELECT * FROM skip_events ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    # ------------------------------------------------------------------
    # Agent State Persistence (Burr workflow state in CockroachDB)
    # ------------------------------------------------------------------

    def save_agent_state(
        self, app_id: str, state_data: dict[str, Any],
        current_stage: str, assessment_id: str | None = None,
        remediation_id: str | None = None, status: str = "active",
    ) -> None:
        """Persist agent workflow state to CockroachDB.

        This is the core of agentic memory — the agent's working state
        (Burr state machine) is stored in CockroachDB so it survives across
        Lambda invocations. A workflow can be started in one invocation,
        paused at the human approval checkpoint, and resumed in a later
        invocation.

        Args:
            app_id: Unique workflow identifier (Burr application ID).
            state_data: Full serialized workflow state as JSON.
            current_stage: Current workflow stage (diagnose, propose, etc.).
            assessment_id: Associated assessment if any.
            remediation_id: Associated remediation if any.
            status: 'active', 'paused', 'completed', 'failed'.
        """
        self._execute(
            """
            INSERT INTO agent_state
                (app_id, state_data, current_stage, assessment_id,
                 remediation_id, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (app_id) DO UPDATE SET
                state_data = EXCLUDED.state_data,
                current_stage = EXCLUDED.current_stage,
                assessment_id = EXCLUDED.assessment_id,
                remediation_id = EXCLUDED.remediation_id,
                status = EXCLUDED.status,
                updated_at = now()
            """,
            (app_id, json.dumps(state_data), current_stage,
             assessment_id, remediation_id, status),
        )

    def get_agent_state(self, app_id: str) -> dict[str, Any] | None:
        """Retrieve persisted agent workflow state.

        Returns None if the workflow doesn't exist (first invocation).
        Otherwise returns the full state dict for resuming the workflow.
        """
        row = self._execute_fetchone(
            "SELECT * FROM agent_state WHERE app_id = %s",
            (app_id,),
        )
        if row is None:
            return None
        state_json = row.get("state_data", "{}")
        if isinstance(state_json, str):
            state_json = json.loads(state_json)
        return {
            "app_id": row["app_id"],
            "state_data": state_json,
            "current_stage": row["current_stage"],
            "assessment_id": row["assessment_id"],
            "remediation_id": row["remediation_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_active_workflows(self) -> list[dict[str, Any]]:
        """Get all active or paused workflows (for resumption)."""
        return self._execute_fetchall(
            """
            SELECT app_id, current_stage, status, updated_at
            FROM agent_state
            WHERE status IN ('active', 'paused')
            ORDER BY updated_at DESC
            """
        )

    def get_all_workflows(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get all workflows (active, paused, completed, failed) — full lifecycle."""
        return self._execute_fetchall(
            """
            SELECT app_id, current_stage, status, assessment_id, updated_at
            FROM agent_state
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )

    def update_agent_status(self, app_id: str, status: str) -> None:
        """Update workflow status (e.g., 'paused' at approval checkpoint)."""
        self._execute(
            "UPDATE agent_state SET status = %s, updated_at = now() WHERE app_id = %s",
            (status, app_id),
        )

    # ------------------------------------------------------------------
    # Context Retrieval (Agent reads past outcomes to inform decisions)
    # ------------------------------------------------------------------

    def get_remediation_context(self, issue_type: str, limit: int = 5) -> dict[str, Any]:
        """Retrieve historical context for a problem type.

        This is the closed-loop memory: before the agent generates a
        remediation proposal, it queries CockroachDB for:
        1. Strategies that previously worked for this issue type
        2. Strategies that previously failed (to avoid repeating)
        3. Average score improvement for this issue type
        4. Decision traces from past attempts (reasoning history)

        This makes the agent progressively better at resolving problems
        of the same type — it learns from its institutional memory.

        Fallback: If no exact match is found for the issue_type, falls back
        to returning all recent outcomes. This handles the case where the
        LLM generates different category names for the same underlying
        problem across runs (e.g., "broken_links" vs "poor_maintenance").
        """
        successful = self.get_successful_strategies(issue_type)
        failed = self.get_failed_strategies(issue_type)

        # Get recent outcomes with decision traces (exact match)
        recent = self._execute_fetchall(
            """
            SELECT r.outcome_id, r.issue_type, r.strategy, r.verification_outcome,
                   r.score_before, r.score_after, r.proposal_reasoning,
                   r.tokens_used, r.created_at
            FROM remediation_history r
            WHERE r.issue_type = %s
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (issue_type, limit),
        )

        # Fallback: if no exact match, get all recent outcomes
        # The LLM may generate different category names for the same problem
        fallback_used = False
        if not recent:
            recent = self._execute_fetchall(
                """
                SELECT r.outcome_id, r.issue_type, r.strategy, r.verification_outcome,
                       r.score_before, r.score_after, r.proposal_reasoning,
                       r.tokens_used, r.created_at
                FROM remediation_history r
                ORDER BY r.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            if recent:
                fallback_used = True
                # Also get all successful/failed strategies (not filtered by type)
                successful = self._execute_fetchall(
                    """
                    SELECT strategy, count(*) as count,
                           avg(score_after - score_before) as avg_improvement
                    FROM remediation_history
                    WHERE verification_outcome IN ('resolved', 'improved')
                    GROUP BY strategy ORDER BY avg_improvement DESC LIMIT %s
                    """,
                    (limit,),
                )
                failed = self._execute_fetchall(
                    """
                    SELECT strategy, count(*) as count,
                           avg(score_after - score_before) as avg_improvement
                    FROM remediation_history
                    WHERE verification_outcome NOT IN ('resolved', 'improved')
                    GROUP BY strategy ORDER BY count DESC LIMIT %s
                    """,
                    (limit,),
                )

        # Fetch decision traces for the most recent outcome
        traces = []
        if recent:
            traces = self.get_decision_traces(recent[0]["outcome_id"])

        return {
            "issue_type": issue_type,
            "successful_strategies": successful,
            "failed_strategies": failed,
            "recent_outcomes": recent,
            "last_decision_traces": traces,
            "total_attempts": len(recent),
            "fallback_used": fallback_used,
        }

    def get_similar_problems(
        self, category: str, artifact_uris: list[str], limit: int = 5
    ) -> list[dict[str, Any]]:
        """Find similar past problems by category and overlapping artifacts.

        Uses exact match on category plus JSONB containment for artifact
        overlap. This helps the agent understand whether a problem is
        recurring and what was tried last time.
        """
        return self._execute_fetchall(
            """
            SELECT problem_id, category, status, salience, description,
                   created_at, updated_at
            FROM problem_queue
            WHERE category = %s
              AND status IN ('resolved', 'in_progress', 'open')
            ORDER BY salience DESC
            LIMIT %s
            """,
            (category, limit),
        )

    def get_knowledge_health_trend(self, days: int = 30) -> list[dict[str, Any]]:
        """Get assessment score trend over time for monitoring.

        Returns daily average scores to track whether knowledge health
        is improving, degrading, or stable. This is the observability
        dimension — the agent monitors its own effectiveness.
        """
        return self._execute_fetchall(
            """
            SELECT
                date_trunc('day', created_at) as day,
                avg(score) as avg_score,
                count(*) as assessment_count,
                avg(signals_count) as avg_signals
            FROM knowledge_assessments
            WHERE created_at > now() - interval '%s days'
            GROUP BY date_trunc('day', created_at)
            ORDER BY day ASC
            """,
            (days,),
        )
