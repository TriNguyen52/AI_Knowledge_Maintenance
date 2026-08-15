"""
AWS Lambda handler for AI Knowledge Maintenance.

This is a **call-only** layer — all business logic lives in the codebase
(``ai_ready.improvement.closed_loop``).  The handler routes events to
the appropriate codebase function and wraps the result in an HTTP-style
response dict.

Supported actions:
    - "assess"    → run_cloud_assessment_cycle  (scan, persist, discover problems)
    - "remediate" → run_cloud_remediation_cycle  (assess → improve → verify → rollback)
    - "propose"   → run_cloud_remediation_cycle(phase="propose")  (assess → propose → halt)
    - "execute"   → run_cloud_remediation_cycle(phase="execute")  (resume → execute → verify)
    - "status"    → CockroachDBStore queries     (queue, metrics, health trend)

S3 event triggers (Records with eventSource "aws:s3") are handled by
downloading the changed file and running a single-file assessment.

Environment variables:
    COCKROACH_DB_URL   — CockroachDB connection string
    S3_KNOWLEDGE_BUCKET — S3 bucket with knowledge artifacts
    S3_KNOWLEDGE_PREFIX — S3 prefix (optional)
    GROQ_API_KEY        — Groq API key (required for remediation)
    LLM_PROVIDER        — "groq" (default) or "bedrock"
    SALIENCE_THRESHOLD  — min salience for problems (optional, default 0.01)
"""

from __future__ import annotations

import json
import os
from typing import Any


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main AWS Lambda entry point — routes to codebase functions.

    Args:
        event: Lambda event payload. Supported keys:
            action: "assess" | "remediate" | "propose" | "execute" | "status"
            s3_bucket: Override S3_KNOWLEDGE_BUCKET env var
            s3_prefix: Override S3_KNOWLEDGE_PREFIX env var
            problem_id: Specific problem to remediate (optional)
            limit: Max artifacts to assess (optional, 0 = no limit)
            dedup_key: Idempotency key for assessment (optional)
            Records: S3 event format for incremental assessment
        context: Lambda context (unused)

    Returns:
        dict with statusCode, body (JSON string), and headers.
    """
    try:
        # S3 event trigger → incremental single-file assessment
        if _is_s3_event(event):
            bucket, key = _extract_s3_event_info(event)
            if bucket and key:
                result = _handle_s3_event(bucket, key)
                return _ok(result)

        # Unwrap API Gateway event shapes
        event = _unwrap_api_gateway(event)

        action = event.get("action", "assess")

        if action == "assess":
            result = run_assessment(event)
        elif action == "remediate":
            result = run_remediation(event)
        elif action == "propose":
            result = run_proposal(event)
        elif action == "execute":
            result = run_execution(event)
        elif action == "status":
            result = run_status(event)
        else:
            return _err(f"Unknown action: {action}", status=400)

        return _ok(result)
    except Exception as e:
        import traceback
        return _err(
            f"{type(e).__name__}: {e}",
            traceback=traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Action handlers — each delegates to a codebase function
# ---------------------------------------------------------------------------

def run_assessment(event: dict[str, Any]) -> dict[str, Any]:
    """Run a full knowledge assessment cycle.

    Delegates to ``run_cloud_assessment_cycle`` in
    ``ai_ready.improvement.closed_loop``, which:
    1. Downloads S3 artifacts to /tmp
    2. Runs the assessment pipeline (collectors → policy → dimensions)
    3. Persists signals and assessment to CockroachDB
    4. Discovers and ranks problems by salience
    5. Stores top problems in the CockroachDB queue

    Args:
        event: Lambda event. Optional keys:
            s3_bucket, s3_prefix, limit, dedup_key

    Returns:
        Assessment summary dict from the codebase function.
    """
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.improvement.closed_loop import run_cloud_assessment_cycle

    bucket = event.get("s3_bucket", os.environ.get("S3_KNOWLEDGE_BUCKET", ""))
    prefix = event.get("s3_prefix", os.environ.get("S3_KNOWLEDGE_PREFIX", ""))
    if not bucket:
        raise ValueError(
            "S3_KNOWLEDGE_BUCKET environment variable or s3_bucket in event required"
        )

    store = CockroachDBStore()
    store.initialize_schema()

    try:
        return run_cloud_assessment_cycle(
            store=store,
            bucket=bucket,
            prefix=prefix,
            limit=int(event.get("limit", 0)),
            dedup_key=event.get("dedup_key", ""),
        )
    finally:
        store.close()


def run_remediation(event: dict[str, Any]) -> dict[str, Any]:
    """Run a full closed-loop remediation cycle (auto-approve).

    Delegates to ``run_cloud_remediation_cycle`` in
    ``ai_ready.improvement.closed_loop``, which:
    1. Downloads S3 artifacts to /tmp
    2. Runs the full closed-loop cycle (assess → improve → verify → rollback)
    3. Uploads only the modified files back to S3
    4. Stores the remediation outcome in CockroachDB (institutional memory)

    Args:
        event: Lambda event. Optional keys:
            s3_bucket, s3_prefix, problem_id

    Returns:
        Remediation result dict from the codebase function.
    """
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.llm.gateway import LLMGateway
    from ai_ready.improvement.closed_loop import run_cloud_remediation_cycle

    bucket = event.get("s3_bucket", os.environ.get("S3_KNOWLEDGE_BUCKET", ""))
    prefix = event.get("s3_prefix", os.environ.get("S3_KNOWLEDGE_PREFIX", ""))
    if not bucket:
        raise ValueError(
            "S3_KNOWLEDGE_BUCKET environment variable or s3_bucket in event required"
        )

    store = CockroachDBStore()
    store.initialize_schema()

    try:
        provider_name = os.environ.get("LLM_PROVIDER", "groq")
        gateway = LLMGateway(provider=provider_name)

        return run_cloud_remediation_cycle(
            store=store,
            bucket=bucket,
            prefix=prefix,
            problem_id=event.get("problem_id"),
            llm_gateway=gateway,
        )
    finally:
        store.close()


def run_proposal(event: dict[str, Any]) -> dict[str, Any]:
    """Generate a remediation proposal for human review (halt at approval).

    Delegates to ``run_cloud_remediation_cycle`` with ``phase="propose"``,
    which runs assessment + analysis + proposal generation, then halts
    at the approval checkpoint.  The workflow state is persisted to
    CockroachDB so :func:`run_execution` can resume it later.

    Args:
        event: Lambda event. Optional keys:
            s3_bucket, s3_prefix, problem_id

    Returns:
        Proposal dict with app_id, proposals, root_causes, score_before, etc.
    """
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.llm.gateway import LLMGateway
    from ai_ready.improvement.closed_loop import run_cloud_remediation_cycle

    bucket = event.get("s3_bucket", os.environ.get("S3_KNOWLEDGE_BUCKET", ""))
    prefix = event.get("s3_prefix", os.environ.get("S3_KNOWLEDGE_PREFIX", ""))
    if not bucket:
        raise ValueError(
            "S3_KNOWLEDGE_BUCKET environment variable or s3_bucket in event required"
        )

    store = CockroachDBStore()
    store.initialize_schema()

    try:
        provider_name = os.environ.get("LLM_PROVIDER", "groq")
        gateway = LLMGateway(provider=provider_name)

        return run_cloud_remediation_cycle(
            store=store,
            bucket=bucket,
            prefix=prefix,
            problem_id=event.get("problem_id"),
            llm_gateway=gateway,
            phase="propose",
        )
    finally:
        store.close()


def run_execution(event: dict[str, Any]) -> dict[str, Any]:
    """Execute an approved proposal (resume from CockroachDB state).

    Delegates to ``run_cloud_remediation_cycle`` with ``phase="execute"``,
    which resumes the workflow from CockroachDB-persisted state, executes
    the approved proposal, verifies the result, and uploads modified files
    to S3.

    Args:
        event: Lambda event. Required keys:
            app_id: The workflow ID returned by the propose phase.
        Optional keys:
            s3_bucket, s3_prefix, problem_id, approved (default True),
            reason (default "approved by user")

    Returns:
        Execution result dict with verification_outcome, score_before/after, etc.
    """
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.llm.gateway import LLMGateway
    from ai_ready.improvement.closed_loop import run_cloud_remediation_cycle

    bucket = event.get("s3_bucket", os.environ.get("S3_KNOWLEDGE_BUCKET", ""))
    prefix = event.get("s3_prefix", os.environ.get("S3_KNOWLEDGE_PREFIX", ""))
    if not bucket:
        raise ValueError(
            "S3_KNOWLEDGE_BUCKET environment variable or s3_bucket in event required"
        )

    app_id = event.get("app_id", "")
    if not app_id:
        raise ValueError("app_id is required for execute action (from propose phase)")

    store = CockroachDBStore()
    store.initialize_schema()

    try:
        provider_name = os.environ.get("LLM_PROVIDER", "groq")
        gateway = LLMGateway(provider=provider_name)

        return run_cloud_remediation_cycle(
            store=store,
            bucket=bucket,
            prefix=prefix,
            problem_id=event.get("problem_id"),
            llm_gateway=gateway,
            phase="execute",
            app_id=app_id,
            approved=event.get("approved", True),
            reason=event.get("reason", "approved by user"),
        )
    finally:
        store.close()


def run_status(event: dict[str, Any]) -> dict[str, Any]:
    """Return current system status: queue, metrics, health trend, active workflows.

    Delegates to ``CockroachDBStore`` query methods.
    """
    from ai_ready.storage.cockroach import CockroachDBStore

    store = CockroachDBStore()
    store.initialize_schema()

    try:
        latest = store.get_latest_assessment()
        return {
            "action": "status",
            "latest_assessment": latest.to_dict() if latest else None,
            "problem_queue": store.get_queue_summary(),
            "remediation_metrics": store.get_remediation_metrics(),
            "health_trend": store.get_knowledge_health_trend(days=30),
            "active_workflows": store.get_active_workflows(),
            "recent_skip_events": store.get_skip_events(limit=10),
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# S3 event handling — incremental single-file assessment
# ---------------------------------------------------------------------------

def _is_s3_event(event: dict[str, Any]) -> bool:
    """Check if the Lambda event is an S3 event trigger."""
    records = event.get("Records", [])
    if not records or not isinstance(records, list):
        return False
    first = records[0]
    return isinstance(first, dict) and first.get("eventSource") == "aws:s3"


def _extract_s3_event_info(event: dict[str, Any]) -> tuple[str, str]:
    """Extract bucket name and object key from an S3 event.

    Returns:
        Tuple of (bucket, key). Either may be empty string if not found.
    """
    try:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        from urllib.parse import unquote_plus
        key = unquote_plus(record["s3"]["object"]["key"])
        return bucket, key
    except (KeyError, IndexError, TypeError):
        return "", ""


def _handle_s3_event(bucket: str, key: str) -> dict[str, Any]:
    """Handle an S3 event by running incremental assessment on the changed file.

    Downloads the single changed file from S3, runs the assessment pipeline
    on just that artifact, and persists new/updated signals to CockroachDB.

    Args:
        bucket: S3 bucket name.
        key: S3 object key (the changed file path).

    Returns:
        Summary dict with new_signals, updated_signals, resolved_signals counts.
    """
    import boto3
    from botocore.config import Config
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.improvement.closed_loop import run_cloud_assessment_cycle

    # For S3 events, we run a cloud assessment cycle scoped to the
    # specific file's bucket/prefix.  The codebase function handles
    # download, assessment, persistence, and problem discovery.
    store = CockroachDBStore()
    store.initialize_schema()

    try:
        # Use the parent prefix of the changed key as the S3 prefix
        # so the assessment covers the relevant artifact set.
        prefix = "/".join(key.split("/")[:-1])

        return run_cloud_assessment_cycle(
            store=store,
            bucket=bucket,
            prefix=prefix,
            limit=0,
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Helpers — API Gateway unwrapping and response formatting
# ---------------------------------------------------------------------------

def _unwrap_api_gateway(event: dict[str, Any]) -> dict[str, Any]:
    """Unwrap API Gateway event shapes if present.

    Handles both REST (httpMethod) and HTTP (routeKey) API Gateway formats.
    """
    if "httpMethod" in event and "body" in event:
        body = event.get("body", "{}")
        if isinstance(body, str):
            return json.loads(body) if body else {}
        return body
    if "routeKey" in event and "body" in event:
        body = event.get("body", "{}")
        if isinstance(body, str):
            return json.loads(body) if body else {}
        return body
    return event


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a result dict in a successful Lambda response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(result, default=str),
    }


def _err(message: str, status: int = 500, traceback: str = "") -> dict[str, Any]:
    """Wrap an error message in a failed Lambda response."""
    body: dict[str, Any] = {"error": message}
    if traceback:
        body["traceback"] = traceback
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
