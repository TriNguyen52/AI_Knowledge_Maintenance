"""Cloud-native Lambda demo logic.

All logic for the Lambda-based closed-loop demo lives here.
``demo_lambda_chain.py`` is call-only — it just calls :func:`run_lambda_demo`.

This module mirrors the local :func:`demo_closed_loop` flow but runs every
assessment and remediation cycle as an AWS Lambda invocation (via Floci).
The display output is identical to the local demo (per-run comparison,
cumulative improvement, CockroachDB verification, MCP tools), with
additional cloud-architecture context (S3, Lambda, EventBridge).

Flow::

    1. Connect to CockroachDB (optionally clear for fresh start)
    2. Initial assessment via Lambda (populate problem queue)
    3. For each cycle:
       a. Invoke Lambda remediate (internally: assess → improve → verify → upload)
       b. Display results (verification, memory influence, knowledge health)
    4. Per-run comparison table
    5. Cumulative improvement summary
    6. CockroachDB verification (context storage, agent state, MCP tools, vector search)
    7. CockroachDB institutional memory (MCP tools, signal lifecycle, metrics)
    8. Cloud demo complete
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ai_ready.cli import display


# ---------------------------------------------------------------------------
# Lambda invocation
# ---------------------------------------------------------------------------

def invoke_lambda(
    payload: dict[str, Any],
    endpoint: str = "http://localhost:4566",
    function_name: str = "ai-knowledge-assess",
    aws_python: str | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    """Invoke the Lambda function via boto3 and return the parsed response.

    Uses boto3's Lambda client directly — no awscli subprocess needed.
    The actual assessment/remediation code runs inside the Lambda container
    on Floci; this function only sends the invocation request from the host.

    Args:
        payload: Event payload dict (e.g. ``{"action": "assess"}``).
        endpoint: Floci/AWS endpoint URL.
        function_name: Lambda function name.
        aws_python: Unused (kept for backward compatibility).
        timeout: Invocation timeout in seconds (max 900 = 15 min Lambda limit).

    Returns:
        Parsed response body dict, or ``{"error": ...}`` on failure.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    action = payload.get("action", "assess")
    display.print_lambda_invoking(action)
    t0 = time.monotonic()

    try:
        client = boto3.client(
            "lambda",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            config=BotoConfig(
                read_timeout=900,
                connect_timeout=30,
                retries={"max_attempts": 0},
            ),
        )

        response = client.invoke(
            FunctionName=function_name,
            Payload=json.dumps(payload).encode("utf-8"),
            InvocationType="RequestResponse",  # synchronous
        )

        elapsed = time.monotonic() - t0

        # Read the response payload
        response_body = response["Payload"].read().decode("utf-8")
        lambda_response = json.loads(response_body)

        if lambda_response.get("statusCode") != 200:
            body = json.loads(lambda_response.get("body", "{}"))
            return {"error": body.get("error", "Unknown error"),
                    "type": body.get("type", "Unknown"),
                    "statusCode": lambda_response.get("statusCode")}

        body = json.loads(lambda_response.get("body", "{}"))
        display.print_lambda_response(action, elapsed, body)
        return body

    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f" [X] Invocation error after {elapsed:.1f}s: {e}")
        return {"error": str(e), "type": type(e).__name__}


# ---------------------------------------------------------------------------
# CockroachDB connection and clearing
# ---------------------------------------------------------------------------

def connect_and_clear_cockroachdb() -> "CockroachDBStore":
    """Connect to CockroachDB, initialize schema, and clear all data.

    Returns:
        Connected CockroachDBStore instance with fresh tables.
    """
    from ai_ready.storage.cockroach import CockroachDBStore

    db_url = os.environ.get("COCKROACH_DB_URL", "")
    if not db_url:
        display.print_error("COCKROACH_DB_URL not set in .env")
        sys.exit(1)

    display.print_db_connecting(db_url[:60])

    store = CockroachDBStore(connection_string=db_url)
    store.connect()
    display.print_db_connected()
    display.print_dim("Initializing schema...")
    store.initialize_schema()
    display.print_db_schema_ready()

    # Clear existing data for a fresh demo
    display.print_dim("Clearing existing data for fresh demo...")
    for view in ["mcp_active_workflows", "mcp_decision_traces",
                 "mcp_health_trend", "mcp_knowledge_health",
                 "mcp_problem_queue", "mcp_remediation_history",
                 "mcp_remediation_metrics", "mcp_signals"]:
        try:
            store._execute(f"DROP VIEW IF EXISTS {view} CASCADE")
        except Exception:
            pass
    for table in ["decision_traces", "remediation_history", "skip_events",
                  "wont_fix_signatures", "problem_queue",
                  "assessment_signals", "signal_lifecycle",
                  "knowledge_signals", "knowledge_assessments",
                  "knowledge_artifacts", "agent_state",
                  "modified_artifacts", "embeddings"]:
        try:
            store._execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:
            pass
    store._initialized = False
    store.initialize_schema()
    display.print_db_cleared()

    return store


def connect_cockroachdb() -> "CockroachDBStore":
    """Connect to CockroachDB and initialize schema without clearing data.

    Returns:
        Connected CockroachDBStore instance with existing data preserved.
    """
    from ai_ready.storage.cockroach import CockroachDBStore

    db_url = os.environ.get("COCKROACH_DB_URL", "")
    if not db_url:
        display.print_error("COCKROACH_DB_URL not set in .env")
        sys.exit(1)

    display.print_db_connecting(db_url[:60])

    store = CockroachDBStore(connection_string=db_url)
    store.connect()
    display.print_db_connected()
    display.print_dim("Initializing schema (preserving existing data)...")
    store.initialize_schema()
    display.print_db_schema_ready()

    return store


# ---------------------------------------------------------------------------
# Per-run result extraction
# ---------------------------------------------------------------------------

def extract_run_result(run_idx: int, response: dict, elapsed: float) -> dict:
    """Extract per-run metrics from a Lambda remediation response.

    Mirrors :func:`demo_closed_loop.extract_run_result` but works with the
    Lambda response format from :func:`run_cloud_remediation_cycle`.
    """
    memory_influence = response.get("memory_influence", {})

    return {
        "run": run_idx,
        "score_before": response.get("score_before", 0),
        "score_after": response.get("score_after", 0),
        "score_delta": response.get("score_delta", 0),
        "signals_resolved": response.get("signals_resolved", 0),
        "new_signals": response.get("new_signals", 0),
        "outcome": response.get("verification_outcome", "unknown"),
        "rolled_back": response.get("rolled_back", False),
        "strategy": response.get("strategy", "?"),
        "elapsed": elapsed,
        "tokens_used": response.get("tokens_used", 0),
        "memory_retrieved_count": len(memory_influence.get("retrieved", [])),
        "memory_avoided_count": len(memory_influence.get("avoided_strategies", [])),
        "memory_reused_strategy": memory_influence.get("reused_strategy"),
        "modified_uris": response.get("modified_uris", []),
        "context_used": response.get("context_used", {}),
    }


# ---------------------------------------------------------------------------
# CockroachDB verification (same as demo_closed_loop)
# ---------------------------------------------------------------------------

def verify_cockroachdb(store, results: list[dict]) -> None:
    """Verify that CockroachDB is actually storing and retrieving data.

    Checks context storage (remediation_history), agent state (Burr
    workflows), MCP tools (all 6 tools return real data), and vector search.
    Also shows per-run institutional memory influence.
    """
    verification = {}

    # Context storage — remediation_history
    history = store.get_remediation_history(limit=100)
    issue_types = list(set(h.issue_type for h in history)) if history else []
    all_have_steps = all(h.modification_steps for h in history) if history else False
    verification["context_storage"] = {
        "outcomes_count": len(history),
        "all_have_modification_steps": all_have_steps,
        "issue_types": issue_types,
    }

    # Agent state — Burr workflow persistence
    all_workflows = store.get_all_workflows(limit=100)
    all_completed = (
        all(w.get("status") == "completed" for w in all_workflows)
        if all_workflows
        else False
    )
    stages = list(set(w.get("current_stage", "?") for w in all_workflows)) if all_workflows else []
    verification["agent_state"] = {
        "workflows_count": len(all_workflows),
        "all_completed": all_completed,
        "stages": stages,
    }

    # MCP tools — verify each returns real data
    from ai_ready.cloud.mcp_server import handle_tool_call, set_store
    set_store(store)

    mcp_results = {}
    for tool_name in [
        "get_knowledge_health",
        "get_signals",
        "search_artifacts_semantic",
        "get_active_workflows",
        "get_remediation_history",
        "get_remediation_metrics",
    ]:
        if tool_name == "search_artifacts_semantic":
            result = handle_tool_call(tool_name, {"query": "broken link missing context", "limit": 5})
        else:
            result = handle_tool_call(tool_name, {"limit": 5})
        mcp_results[tool_name] = result
    verification["mcp_tools"] = mcp_results

    # Vector search
    verification["vector_search"] = mcp_results.get("search_artifacts_semantic", {})

    # Memory influence summary (per run)
    verification["memory_influence"] = [
        {
            "run": r.get("run", "?"),
            "retrieved_count": r.get("memory_retrieved_count", 0),
            "avoided_count": r.get("memory_avoided_count", 0),
            "reused_strategy": r.get("memory_reused_strategy"),
        }
        for r in results
    ]

    display.print_cockroach_verification(verification)


# ---------------------------------------------------------------------------
# Show CockroachDB institutional memory (MCP tools)
# ---------------------------------------------------------------------------

def show_cockroach_memory(store) -> None:
    """Show CockroachDB institutional memory via MCP tools.

    Demonstrates all MCP tools: get_knowledge_health, get_signals,
    search_artifacts_semantic, get_active_workflows, get_remediation_history,
    get_decision_trace, get_remediation_context, A/B memory test,
    signal lifecycle, and metrics.
    """
    from ai_ready.cloud.mcp_server import handle_tool_call, MCP_TOOLS, set_store
    set_store(store)

    display.print_mcp_header(len(MCP_TOOLS), MCP_TOOLS)

    # MCP Tool: get_knowledge_health
    result = handle_tool_call("get_knowledge_health", {})
    if "message" in result:
        display.print_dim(result["message"])
    else:
        display.print_mcp_health(result.get("score", 0), result.get("signals_count", 0))

    # MCP Tool: get_signals
    result = handle_tool_call("get_signals", {"limit": 5})
    display.print_mcp_signals(result.get("signals", []), result.get("count", 0))

    # MCP Tool: search_artifacts_semantic (vector index)
    result = handle_tool_call("search_artifacts_semantic",
                              {"query": "broken link missing context", "limit": 5})
    display.print_mcp_vector_search("broken link missing context", result.get("results", []))

    # MCP Tool: get_active_workflows (agent state persistence)
    result = handle_tool_call("get_active_workflows", {})
    workflows = result.get("workflows", [])

    # Also show completed/failed workflows (full lifecycle)
    try:
        all_workflows = store.get_all_workflows(limit=10)
        display.print_mcp_workflows(workflows, all_workflows)
    except Exception:
        display.print_mcp_workflows(workflows)

    # MCP Tool: get_remediation_history
    result = handle_tool_call("get_remediation_history", {"limit": 20})
    history = result.get("outcomes", [])
    display.print_mcp_remediation_history(history, result.get("count", 0))

    # MCP Tool: get_decision_trace
    if history:
        outcome_id = history[0].get("outcome_id", "")
        result = handle_tool_call("get_decision_trace", {"outcome_id": outcome_id})
        display.print_mcp_decision_traces(outcome_id, result.get("traces", []), result.get("count", 0))

    # MCP Tool: get_remediation_context (institutional memory)
    if history:
        issue_type = history[0].get("issue_type", "broken_link")
        result = handle_tool_call("get_remediation_context",
                                  {"issue_type": issue_type, "limit": 5})
        display.print_mcp_remediation_context(issue_type, result)

    # A/B Memory Test (proves memory influences LLM proposals)
    try:
        from ai_ready.improvement.actions import run_memory_ab_test
        from ai_ready.improvement.history import ImprovementHistoryStore
        from burr.core import State
        if history:
            ab_issue_type = history[0].get("issue_type", "missing_metadata")
            ab_state = State({
                "root_cause_analysis": [{"category": ab_issue_type, "hypothesis": "test", "confidence": 0.8}],
                "knowledge_problems": [],
                "affected_artifact_uris": [],
                "prior_failures": [],
                "assessment_id": "",
                "prior_diagnosis": {},
                "prior_strategy": {},
                "prior_verification": {},
                "decision_trace": {},
                "assessment_summary": "Test assessment for A/B comparison.",
                "cumulative_tokens": 0,
                "problem_saliences": [],
                "prior_modification_steps": [],
                "systemic_clusters": [],
                "assessment_signals": [],
            })
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="ab_demo_")
            ab_db = os.path.join(tmp_dir, "ab_history.db")
            ab_store = ImprovementHistoryStore(ab_db)
            for h in history[:1]:
                from ai_ready.improvement.models import RemediationOutcome
                outcome = RemediationOutcome(
                    issue_type=h.get("issue_type", ab_issue_type),
                    strategy=h.get("strategy", "signal_mapped_remediation"),
                    result=h.get("result", "failure"),
                    score_change=h.get("score_change", 0),
                    verification_outcome=h.get("verification_outcome", "unchanged"),
                    modification_steps=h.get("modification_steps", []),
                )
                ab_store.record_outcome(outcome)

            ab_result = run_memory_ab_test(
                ab_state,
                llm_gateway=None,
                history_store=ab_store,
                assessment_store=None,
            )
            display.print_memory_ab_test(ab_result)

            try:
                os.remove(ab_db)
                os.rmdir(tmp_dir)
            except OSError:
                pass
    except Exception as e:
        display.print_warning(f"A/B memory test unavailable: {e}")

    # Signal lifecycle summary
    try:
        new_sigs = store.get_signals_by_status("new")
        persistent_sigs = store.get_signals_by_status("persistent")
        resolved_sigs = store.get_signals_by_status("resolved")
        recurring_sigs = store.get_signals_by_status("recurring")
        display.print_mcp_signal_lifecycle_summary(new_sigs, persistent_sigs, resolved_sigs, recurring_sigs)
    except Exception as e:
        display.print_warning(f"Lifecycle summary unavailable: {e}")

    # MCP Tool: get_remediation_metrics
    result = handle_tool_call("get_remediation_metrics", {})
    display.print_mcp_metrics(result)


# ---------------------------------------------------------------------------
# Cloud infrastructure summary
# ---------------------------------------------------------------------------

def _get_s3_artifact_count(bucket: str, prefix: str, endpoint: str) -> int:
    """Count objects in an S3 bucket/prefix."""
    import boto3
    from botocore.config import Config
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(s3={"addressing_style": "path"}),
    )
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):
                count += 1
    return count


def _get_eventbridge_rules(endpoint: str) -> list[dict]:
    """Get EventBridge rules and their targets."""
    import boto3
    events = boto3.client(
        "events",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    rules = events.list_rules().get("Rules", [])
    result = []
    for r in rules:
        targets = events.list_targets_by_rule(Rule=r["Name"]).get("Targets", [])
        target_arn = targets[0].get("Arn", "?") if targets else "?"
        result.append({
            "name": r["Name"],
            "schedule": r.get("ScheduleExpression", "N/A"),
            "state": r.get("State", "?"),
            "target": target_arn,
        })
    return result


def show_cloud_infra(endpoint: str, bucket: str, prefix: str) -> int:
    """Display cloud infrastructure summary (S3 + EventBridge).

    Returns the S3 artifact count for use in the intro panel.
    """
    # S3 summary
    try:
        artifact_count = _get_s3_artifact_count(bucket, prefix, endpoint)
        display.print_s3_summary(bucket, prefix, artifact_count)
    except Exception as e:
        display.print_warning(f"S3 summary unavailable: {e}")
        artifact_count = 0

    # EventBridge summary
    try:
        rules = _get_eventbridge_rules(endpoint)
        display.print_eventbridge_summary(rules)
    except Exception as e:
        display.print_warning(f"EventBridge summary unavailable: {e}")

    return artifact_count


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_lambda_demo(
    endpoint: str = "http://localhost:4566",
    function_name: str = "ai-knowledge-assess",
    s3_bucket: str = "knowledge-base",
    s3_prefix: str = "docs100",
    num_cycles: int = 3,
    aws_python: str | None = None,
    clear_db: bool = False,
) -> None:
    """Run the full cloud-native Lambda closed-loop demo.

    This function:
    1. Prints the cloud architecture intro
    2. Connects to CockroachDB (clears existing data only if clear_db=True)
    3. Shows cloud infrastructure (S3, EventBridge)
    4. Runs an initial assessment via Lambda (populates problem queue)
    5. Runs N remediation cycles via Lambda (each: assess → improve → verify → upload)
    6. Displays per-run comparison and cumulative improvement
    7. Verifies CockroachDB (context storage, agent state, MCP tools, vector search)
    8. Shows CockroachDB institutional memory (MCP tools, signal lifecycle, metrics)
    9. Prints cloud demo complete summary

    Args:
        endpoint: Floci/AWS endpoint URL.
        function_name: Lambda function name.
        s3_bucket: S3 bucket containing knowledge artifacts.
        s3_prefix: S3 prefix for the artifact set.
        num_cycles: Number of remediation cycles to run.
        aws_python: Path to Python with AWS CLI installed.
        clear_db: If True, drop all tables and reinitialize (use clear_cockroachdb.py instead for standalone clearing).
    """
    # Ensure UTF-8 output on Windows
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(line_buffering=True)

    # Load .env (explicit path for reliability)
    from dotenv import load_dotenv
    from pathlib import Path
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        # Try parent directory
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    load_dotenv(dotenv_path=str(env_path), override=True)

    total_start = time.monotonic()
    lambda_invocations = 0

    # 1. Print cloud intro
    display.print_cloud_intro(
        endpoint=endpoint,
        function_name=function_name,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        num_cycles=num_cycles,
    )

    # 2. Connect to CockroachDB (clear only if requested)
    if clear_db:
        store = connect_and_clear_cockroachdb()
    else:
        store = connect_cockroachdb()

    try:
        # 3. Show cloud infrastructure
        display.print_banner("Cloud Infrastructure")
        artifact_count = show_cloud_infra(endpoint, s3_bucket, s3_prefix)

        # Update intro with artifact count if we got it
        if artifact_count:
            display.print_dim(f"  Artifact count confirmed: {artifact_count}")

        # 4. Initial assessment via Lambda (populates problem queue)
        display.print_banner("Initial Assessment (Lambda)", "Populating problem queue")

        assess_payload = {
            "action": "assess",
            "s3_bucket": s3_bucket,
            "s3_prefix": s3_prefix,
        }
        assess_result = invoke_lambda(
            assess_payload, endpoint, function_name, aws_python, timeout=900,
        )
        lambda_invocations += 1

        if "error" in assess_result:
            display.print_error(f"Initial assessment failed: {assess_result['error']}")
            display.print_dim("Stopping demo.")
            return

        # Show assessment details
        score = assess_result.get("score", "?")
        signals = assess_result.get("signal_count", 0)
        problems = assess_result.get("problems_discovered", 0)
        display.print_assessment_done(score, signals, 0)

        top_problems = assess_result.get("top_problems", [])
        if top_problems:
            display.print_section("Top Problems Discovered")
            for i, p in enumerate(top_problems[:5]):
                cat = p.get("category", "?")
                desc = display._truncate(p.get("description", ""), 70)
                sal = p.get("salience", 0)
                display.print_kv(f"  {i+1}. {cat}", f"salience={sal:.3f} — {desc}")

        # 5. Run N remediation cycles via Lambda
        results = []

        for cycle in range(1, num_cycles + 1):
            display.print_run_header(str(cycle))

            # Check institutional memory before this run
            try:
                all_history = store.get_remediation_history(limit=10)
                display.print_history_check(all_history)
            except Exception as e:
                display.print_warning(f"Could not read history: {e}")

            # Invoke Lambda remediate
            remediate_payload = {"action": "remediate"}
            t0 = time.monotonic()

            remediate_result = invoke_lambda(
                remediate_payload, endpoint, function_name, aws_python, timeout=900,
            )
            lambda_invocations += 1
            t1 = time.monotonic()
            elapsed = t1 - t0

            if "error" in remediate_result:
                display.print_error(f"Remediation failed: {remediate_result['error']}")
                if "type" in remediate_result:
                    display.print_dim(f"  Type: {remediate_result['type']}")
                display.print_dim("  Continuing to next cycle...")
                continue

            # Display verification results
            verification = remediate_result.get("verification_results", {})
            if verification:
                display.print_verification(verification)
            else:
                # Construct a minimal verification dict from the response
                display.print_verification({
                    "overall_outcome": remediate_result.get("verification_outcome", "unknown"),
                    "before_score": remediate_result.get("score_before", 0),
                    "after_score": remediate_result.get("score_after", 0),
                    "score_difference": remediate_result.get("score_delta", 0),
                })

            # Display memory in action
            memory_influence = remediate_result.get("memory_influence", {})
            if memory_influence:
                display.print_memory_in_action(memory_influence)
            else:
                ctx = remediate_result.get("context_used", {})
                if ctx.get("past_attempts", 0) > 0:
                    display.print_section("Memory in Action (store -> retrieve -> act)")
                    display.print_kv("  Past attempts", ctx.get("past_attempts", 0))
                    display.print_kv("  Successful strategies", ctx.get("successful_strategies", 0))
                    display.print_kv("  Failed strategies", ctx.get("failed_strategies", 0))
                    display.print_kv("  Similar problems", ctx.get("similar_problems", 0))
                else:
                    display.print_section("Memory in Action (store -> retrieve -> act)")
                    display.print_dim("  No prior outcomes (first run for this issue type).")

            # Display knowledge health
            if verification:
                display.print_knowledge_health(verification)

            # Confirm CockroachDB sync
            try:
                all_history = store.get_remediation_history(limit=10)
                latest = all_history[0] if all_history else None
                traces = store.get_decision_traces(latest.outcome_id) if latest else []
                display.print_cockroach_sync(all_history, traces)
            except Exception as e:
                display.print_warning(f"Could not read CockroachDB sync: {e}")

            # Signal lifecycle
            try:
                new_signals = store.get_signals_by_status("new")
                resolved_signals = store.get_signals_by_status("resolved")
                persistent_signals = store.get_signals_by_status("persistent")
                display.print_signal_lifecycle(
                    len(new_signals), len(persistent_signals), len(resolved_signals),
                    resolved_sample=resolved_signals,
                )
            except Exception as e:
                display.print_warning(f"Lifecycle summary unavailable: {e}")

            # Extract per-run result
            run_result = extract_run_result(cycle, remediate_result, elapsed)
            results.append(run_result)

            rollback_tag = " [ROLLED BACK]" if run_result.get("rolled_back") else ""
            display.print_dim(
                f"Run {cycle}: {run_result['score_before']} -> {run_result['score_after']} "
                f"(+{run_result['score_delta']}), {run_result['signals_resolved']} resolved, "
                f"{run_result['new_signals']} new, "
                f"{len(run_result.get('modified_uris', []))} modified"
                f"{rollback_tag}"
            )

        # 6. Per-run comparison and cumulative improvement
        if results:
            display.print_per_run_summary(results)
            display.print_cumulative_improvement(results)

        # 7. CockroachDB verification
        if results:
            verify_cockroachdb(store, results)

        # 8. Show CockroachDB institutional memory (MCP tools)
        show_cockroach_memory(store)

        # 9. Cloud demo complete
        total_elapsed = time.monotonic() - total_start
        display.print_cloud_demo_complete(num_cycles, total_elapsed, lambda_invocations)

    finally:
        store.close()
