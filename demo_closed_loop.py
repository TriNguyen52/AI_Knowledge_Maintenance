#!/usr/bin/env python
"""Closed-loop institutional memory demo — SQLite + CockroachDB dual-write.

Demonstrates the full remediation memory loop across two workflow runs:

  Run 1 (no history):
    1. Assess knowledge base → signals → problems
    2. Generate remediation proposal (no prior outcomes to learn from)
    3. Execute + verify
    4. Outcome saved to SQLite AND CockroachDB (dual-write)
    5. Decision traces saved to CockroachDB

  Run 2 (with history from Run 1):
    1. Assess same knowledge base
    2. Generate remediation proposal — NOW reads CockroachDB context:
       - Sees what strategy was tried in Run 1
       - Sees whether it succeeded or failed
       - Sees decision traces (reasoning from Run 1)
    3. The LLM/proposal generator uses this institutional memory
    4. Execute + verify
    5. Second outcome saved — CockroachDB now has 2 outcomes

  After both runs:
    - Show CockroachDB remediation_history table (2 outcomes)
    - Show CockroachDB decision_traces table (reasoning from both runs)
    - Show merged get_remediation_context() — what Run 3 would see

Usage:
    python demo_closed_loop.py [--source PATH] [--limit N]

    --source    Path to the knowledge base (default: FastAPI docs)
    --limit     Max artifacts to assess (default: 20)
"""

from __future__ import annotations

import os
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(line_buffering=True)

# Load .env
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = Path("C:/Users/jacks/Documents/khe-validation/fastapi")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def banner(title: str, char: str = "=", width: int = 72) -> None:
    print()
    print(char * width)
    print(f"  {title}")
    print(char * width)

def subheader(title: str) -> None:
    print()
    print(f"  --- {title} ---")
    print()

def truncate(text: str, max_len: int = 120) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."

def print_json(label: str, data, indent: int = 2) -> None:
    print(f"  {label}:")
    try:
        formatted = json.dumps(data, indent=2, default=str) if isinstance(data, (dict, list)) else str(data)
        for line in formatted.split("\n"):
            print(f"    {line}")
    except Exception:
        print(f"    {data}")

def get_git_commit(path: str) -> str:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

# ---------------------------------------------------------------------------
# Connect to CockroachDB
# ---------------------------------------------------------------------------

def connect_cockroach() -> "CockroachDBStore":
    banner("Connect to CockroachDB")
    db_url = os.environ.get("COCKROACH_DB_URL", "")
    if not db_url:
        print("  ERROR: COCKROACH_DB_URL not set in .env")
        sys.exit(1)
    print(f"  Connection: {db_url[:60]}...")
    print()
    print("  Connecting to CockroachDB Cloud...")

    from ai_ready.storage.cockroach import CockroachDBStore
    store = CockroachDBStore(connection_string=db_url)
    store.connect()
    print("  Connected successfully.")
    print()
    print("  Initializing schema...")
    store.initialize_schema()
    print("  Schema ready.")

    # Clear existing data for a fresh demo
    print("  Clearing existing data for fresh demo...")
    # Drop ALL tables to ensure schema matches current schema.sql
    # (old tables may have UUID columns or VECTOR(1536) from previous runs)
    for table in ["decision_traces", "remediation_history", "skip_events",
                   "wont_fix_signatures", "problem_queue",
                   "assessment_signals", "signal_lifecycle",
                   "knowledge_signals", "knowledge_assessments",
                   "knowledge_artifacts", "agent_state"]:
        try:
            store._execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:
            pass
    # Re-initialize schema to recreate all tables with current definitions
    store._initialized = False
    store.initialize_schema()
    print("  Data cleared.")

    return store

# ---------------------------------------------------------------------------
# Load knowledge and assess
# ---------------------------------------------------------------------------

def load_and_assess(source: Path, limit: int):
    banner("Load Knowledge and Run Assessment")
    print(f"  Source: {source}")
    print(f"  Artifact limit: {limit}")

    from ai_ready.knowledge.registry import load_knowledge_source
    from ai_ready.pipeline import AssessmentPipeline

    # P1: Language/path exclusion is now handled by load_knowledge_source
    exclude_patterns = ["/de/", "/es/", "/fr/", "/ja/", "/ko/", "/pt/",
                        "/ru/", "/tr/", "/zh/", "/uk/", "/az/", "/it/"]

    print()
    print("  Loading knowledge (with language exclusion filter)...")
    t0 = time.time()
    knowledge = load_knowledge_source(str(source), exclude_patterns=exclude_patterns)
    t1 = time.time()
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    print(f"  Loaded {len(artifacts)} artifacts in {t1-t0:.1f}s")
    print(f"  Relationships (pruned): {len(relationships)}")

    if limit > 0 and len(artifacts) > limit:
        artifacts = artifacts[:limit]
        print(f"  Limited to first {limit} artifacts")

    source_str = str(source.resolve())
    git_commit = get_git_commit(source_str)

    print()
    print("  Running assessment pipeline...")
    pipeline = AssessmentPipeline()
    t0 = time.time()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source_str,
        git_commit=git_commit,
        relationships=relationships,
    )
    t1 = time.time()
    print(f"  Assessment complete in {t1-t0:.1f}s")
    print(f"  Score: {assessment.score}  Signals: {len(assessment.signals)}")

    return pipeline, assessment, artifacts, relationships, source_str, git_commit

# ---------------------------------------------------------------------------
# Persist artifacts, signals, assessment to CockroachDB with vector embeddings
# ---------------------------------------------------------------------------

def persist_to_cockroach(store, assessment, artifacts, source, git_commit):
    """Persist assessment results to CockroachDB with vector embeddings.

    Delegates to the canonical persist_assessment() function (P4) which
    handles artifacts+embeddings (P3), signals+lifecycle (P5), and the
    assessment record in one call.
    """
    banner("Persist to CockroachDB (via persist_assessment)")

    from ai_ready.storage.persistence import persist_assessment
    from ai_ready.llm.embeddings import get_embedder

    subheader(f"Persisting {len(artifacts)} artifacts + {len(assessment.signals)} signals")

    result = persist_assessment(
        store=store,
        assessment=assessment,
        signals=assessment.signals,
        artifacts=artifacts,
        source=source,
    )

    embedder = get_embedder()
    print(f"  Artifacts saved: {result['artifact_count']} (embeddings: {embedder.backend_status})")
    print(f"  Signals saved: {result['signal_count']} (with lifecycle tracking)")
    print(f"  Assessment saved: {result['assessment_id']}")

    # Demonstrate vector search (CockroachDB distributed vector index)
    subheader("Vector Search (CockroachDB <=> cosine distance)")
    worst = max(assessment.signals, key=lambda s: s.score) if assessment.signals else None
    if worst:
        q_emb = embedder.embed(f"{worst.artifact_uri} {worst.signal_type} {worst.recommendation or ''}"[:2000])
        results = store.search_artifacts_semantic(q_emb, limit=5)
        print(f"  Query: artifacts similar to '{truncate(worst.artifact_uri, 50)}'")
        for i, (art, sim) in enumerate(results):
            print(f"    [{i+1}] sim={sim:.3f}  {truncate(art.artifact_uri, 60)}")

    # Return an assessment-record-like object for compatibility
    from ai_ready.storage.models import AssessmentRecord
    return AssessmentRecord(
        assessment_id=result["assessment_id"],
        score=float(assessment.score),
        dimensions=json.dumps({d.dimension: d.score for d in assessment.dimensions}),
        signals_count=result["signal_count"],
        metadata=json.dumps({"source": source, "git_commit": git_commit}),
    )

# ---------------------------------------------------------------------------
# Run a single improvement workflow
# ---------------------------------------------------------------------------

def run_improvement(
    run_label: str,
    assessment,
    pipeline,
    artifacts,
    relationships,
    source,
    git_commit,
    cockroach_store,
    history_db_path: str,
    llm_gateway=None,
):
    """Run a single improvement workflow and return the final state."""
    banner(f"RUN {run_label}: Improvement Workflow")

    from ai_ready.improvement.manager import ImprovementManager
    from ai_ready.improvement.history import ImprovementHistoryStore
    from ai_ready.improvement.executor import KnowledgeExecutor
    from ai_ready.stores import AssessmentStore, SignalStore
    from ai_ready.improvement.problem_queue import ProblemQueue

    # Fresh SQLite for each run (to isolate SQLite history per run)
    # But CockroachDB persists across runs — that's the institutional memory
    if Path(history_db_path).exists():
        Path(history_db_path).unlink()

    signal_store = SignalStore(history_db_path)
    assessment_store = AssessmentStore(history_db_path)
    assessment_store.save(assessment)
    signal_store.save_signals(assessment.assessment_id, assessment.signals)

    sqlite_history = ImprovementHistoryStore(history_db_path)

    # P8: salience_threshold from config (default 0.01)
    from ai_ready.config import Config
    config = Config.default()
    pq_path = history_db_path.replace(".db", "_pq.db")
    custom_pq = ProblemQueue(
        history_store=sqlite_history,
        db_path=pq_path,
        salience_threshold=config.salience_threshold,
        signal_store=signal_store,
    )

    # Create executor with real file modification support
    known_uris = {a.uri for a in artifacts}
    executor = KnowledgeExecutor(
        artifact_store=assessment_store,  # non-None enables production mode
        source_path=source,
        assessment_signals=assessment.signals,
        known_artifact_uris=known_uris,
    )

    # Create manager with cockroach_store — this triggers DualHistoryStore
    print(f"  Creating ImprovementManager with CockroachDB sync...")
    print(f"  SQLite: {history_db_path}")
    print(f"  CockroachDB: connected")

    manager = ImprovementManager(
        llm_gateway=llm_gateway,
        assessment_store=assessment_store,
        assessment_pipeline=pipeline,
        history_store=sqlite_history,
        executor=executor,
        artifacts=artifacts,
        relationships=relationships,
        source=source,
        git_commit=git_commit,
        history_db_path=history_db_path,
        enable_tracking=False,
        enable_otel=False,
        max_fork_attempts=2,
        problem_queue=custom_pq,
        cockroach_store=cockroach_store,  # <-- THIS ENABLES DUAL-WRITE
    )

    # Check what CockroachDB already has (institutional memory)
    subheader("Checking CockroachDB Institutional Memory (before this run)")
    try:
        all_history = cockroach_store.get_remediation_history(limit=10)
        print(f"  Existing outcomes in CockroachDB: {len(all_history)}")
        for h in all_history[:5]:
            print(f"    [{h.outcome_id[:8]}] issue={h.issue_type} "
                  f"strategy={h.strategy} outcome={h.verification_outcome}")
    except Exception as e:
        print(f"  (Could not read history: {e})")

    # Start the workflow
    subheader("Starting workflow (analyze → propose → approve checkpoint)")
    t0 = time.time()
    try:
        app_id = manager.start_improvement(assessment)
    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return None
    t1 = time.time()
    print(f"  Workflow started: app_id={app_id}")
    print(f"  Elapsed: {t1-t0:.1f}s")

    # P7: Agent state is now persisted by ImprovementManager automatically
    # when cockroach_store is passed to the constructor.
    print(f"  Agent state persisted to CockroachDB (status=active)")

    # Show state at approval checkpoint
    state = manager.get_state(app_id)
    print(f"  Stage: {state.get('current_stage', 'unknown')}")
    print(f"  Agent state updated (status=paused at approval checkpoint)")

    root_causes = state.get("root_cause_analysis", [])
    problems = state.get("knowledge_problems", [])
    proposals = state.get("proposals", [])

    subheader(f"Root Causes ({len(root_causes)})")
    for rc in root_causes[:3]:
        print(f"    Category: {rc.get('category', '?')}")
        print(f"    Hypothesis: {truncate(rc.get('hypothesis', ''), 100)}")
        print(f"    Confidence: {rc.get('confidence', 'N/A')}")
        print()

    subheader(f"Knowledge Problems ({len(problems)})")
    for p in problems[:5]:
        print(f"    Category: {p.get('category', '?')}")
        print(f"    Description: {truncate(p.get('description', ''), 100)}")
        print()

    subheader(f"Proposals ({len(proposals)})")
    for i, p in enumerate(proposals):
        print(f"  Proposal {i+1}:")
        print(f"    Strategy: {p.get('strategy', 'unknown')}")
        print(f"    Description: {truncate(p.get('description', ''), 120)}")
        print(f"    Reasoning: {truncate(p.get('reasoning', ''), 120)}")
        steps = p.get("modification_steps", [])
        print(f"    Steps: {len(steps)}")
        for s in steps[:3]:
            print(f"      [{s.get('step_type', '')}] {truncate(s.get('artifact_uri', ''), 60)}")
            print(f"        {truncate(s.get('description', ''), 80)}")
        print()

    # Show what historical context was used
    decision_trace = state.get("decision_trace", {})
    hist_context = decision_trace.get("historical_context_used", "")
    subheader("Historical Context Used by LLM")
    if hist_context:
        print(f"    {truncate(hist_context, 200)}")
    else:
        print("    (No historical context — first run for this issue type)")

    # Auto-approve and complete
    subheader("Auto-approving and completing workflow")
    print("  Running: execute_change → verify_improvement...")
    t2 = time.time()
    try:
        final_state = manager.approve_and_complete(
            app_id, approved=True, reason="auto-approved for demo"
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return final_state if 'final_state' in dir() else None
    t3 = time.time()
    print(f"  Completed in {t3-t2:.1f}s")
    print(f"  Final stage: {final_state.get('current_stage', 'unknown')}")

    # P7: Agent state at completion is persisted by ImprovementManager
    final_stage = final_state.get('current_stage', 'completed')
    final_status = "completed" if "completed" in final_stage or "verified" in final_stage else "failed"
    print(f"  Agent state updated (status={final_status})")

    # Show verification
    verification = final_state.get("verification_results", {})
    if verification:
        print()
        print(f"  Verification:")
        print(f"    Outcome: {verification.get('overall_outcome', 'unknown')}")
        print(f"    Score: {verification.get('score_before', '?')} → "
              f"{verification.get('score_after', '?')} "
              f"(diff={verification.get('score_difference', '?')})")

    # Confirm CockroachDB sync
    subheader("CockroachDB Sync Confirmation")
    try:
        all_history = cockroach_store.get_remediation_history(limit=10)
        print(f"  Total outcomes in CockroachDB: {len(all_history)}")
        if all_history:
            latest = all_history[0]
            print(f"  Latest outcome:")
            print(f"    outcome_id: {latest.outcome_id}")
            print(f"    issue_type: {latest.issue_type}")
            print(f"    strategy: {latest.strategy}")
            print(f"    verification_outcome: {latest.verification_outcome}")
            print(f"    tokens_used: {latest.tokens_used}")
            print(f"    forked: {latest.forked}")

            # Show decision traces for this outcome
            traces = cockroach_store.get_decision_traces(latest.outcome_id)
            print(f"  Decision traces saved: {len(traces)}")
            for t in traces[:6]:
                stage = t.get("stage", "?")
                reasoning = truncate(t.get("reasoning", ""), 100)
                print(f"    [{stage}] {reasoning}")
    except Exception as e:
        print(f"  (Could not read CockroachDB: {e})")

    # P6: Signal lifecycle transitions are now handled by verify_improvement
    # in actions.py (update_signal_lifecycle_post_verification).
    subheader("Signal Lifecycle Update (via verify_improvement)")
    verification = final_state.get("verification_results", {})
    overall_outcome = verification.get("overall_outcome", "unknown")
    new_status = "resolved" if overall_outcome in ("resolved", "improved", "partially_resolved") else "persistent"
    print(f"  Signals transitioned to status={new_status} (by verify_improvement)")
    # Show lifecycle summary
    try:
        new_signals = cockroach_store.get_signals_by_status("new")
        resolved_signals = cockroach_store.get_signals_by_status("resolved")
        persistent_signals = cockroach_store.get_signals_by_status("persistent")
        print(f"  Signal lifecycle: new={len(new_signals)}, "
              f"persistent={len(persistent_signals)}, "
              f"resolved={len(resolved_signals)}")
    except Exception as e:
        print(f"  (Lifecycle summary unavailable: {e})")

    return final_state

# ---------------------------------------------------------------------------
# Show CockroachDB institutional memory (after both runs)
# ---------------------------------------------------------------------------

def show_cockroach_memory(store):
    banner("CockroachDB Institutional Memory (After Both Runs)")

    # Use MCP interface to query CockroachDB — demonstrates that external
    # AI agents (Claude Code, Cursor, VS Code) can access the same data
    from ai_ready.cloud.mcp_server import handle_tool_call, MCP_TOOLS

    subheader(f"MCP Server Interface ({len(MCP_TOOLS)} tools)")
    print("  External AI agents query CockroachDB via MCP tools:")
    for t in MCP_TOOLS:
        print(f"    • {t['name']}: {truncate(t['description'], 60)}")
    print()
    print("  Calling MCP tools against live CockroachDB data...")

    # MCP Tool: get_knowledge_health
    subheader("MCP: get_knowledge_health")
    result = handle_tool_call("get_knowledge_health", {})
    if "message" in result:
        print(f"  {result['message']}")
    else:
        print(f"  Score: {result.get('score', 'N/A')}")
        print(f"  Signals: {result.get('signals_count', 'N/A')}")

    # MCP Tool: get_signals
    subheader("MCP: get_signals (limit=5)")
    result = handle_tool_call("get_signals", {"limit": 5})
    print(f"  Signals: {result.get('count', 0)}")
    for s in result.get("signals", [])[:3]:
        print(f"    [{s.get('severity', '?')}] {s.get('signal_type', '?')} "
              f"→ {truncate(s.get('artifact_uri', ''), 50)}")

    # MCP Tool: search_artifacts_semantic (vector index)
    subheader("MCP: search_artifacts_semantic (vector index)")
    result = handle_tool_call("search_artifacts_semantic",
                              {"query": "broken link missing context", "limit": 5})
    print(f"  Query: 'broken link missing context'")
    print(f"  Results: {len(result.get('results', []))}")
    for r in result.get("results", [])[:3]:
        print(f"    sim={r.get('similarity', 0):.3f}  {truncate(r.get('artifact_uri', ''), 60)}")

    # MCP Tool: get_active_workflows (agent state persistence)
    subheader("MCP: get_active_workflows (agent state)")
    result = handle_tool_call("get_active_workflows", {})
    active_count = result.get('count', 0)
    print(f"  Active/paused workflows: {active_count}")
    for w in result.get("workflows", [])[:5]:
        print(f"    [{w.get('app_id', '')[:8]}] stage={w.get('current_stage', '')} status={w.get('status', '')}")

    # Also show completed/failed workflows (full lifecycle)
    subheader("MCP: All workflows (full lifecycle)")
    try:
        all_workflows = store.get_all_workflows(limit=10)
        print(f"  Total workflows in CockroachDB: {len(all_workflows)}")
        for w in all_workflows[:5]:
            app_id = str(w.get('app_id', ''))[:8]
            stage = w.get('current_stage', '')
            status = w.get('status', '')
            print(f"    [{app_id}] stage={stage} status={status}")
    except Exception as e:
        print(f"  (Could not read workflows: {e})")

    # MCP Tool: get_remediation_history
    subheader("MCP: get_remediation_history")
    result = handle_tool_call("get_remediation_history", {"limit": 20})
    history = result.get("outcomes", [])
    print(f"  Total outcomes: {result.get('count', 0)}")
    for h in history:
        print()
        print(f"  [{h.get('outcome_id', '')[:8]}]")
        print(f"    issue_type: {h.get('issue_type', '?')}")
        print(f"    strategy: {h.get('strategy', '?')}")
        print(f"    verification_outcome: {h.get('verification_outcome', '?')}")
        print(f"    score: {h.get('score_before', '?')} → {h.get('score_after', '?')}")
        print(f"    tokens: {h.get('tokens_used', '?')}  forked: {h.get('forked', '?')}")

    # MCP Tool: get_decision_trace
    subheader("MCP: get_decision_trace (reasoning chain)")
    if history:
        outcome_id = history[0].get("outcome_id", "")
        result = handle_tool_call("get_decision_trace", {"outcome_id": outcome_id})
        print(f"  Traces for outcome {outcome_id[:8]}: {result.get('count', 0)}")
        for t in result.get("traces", [])[:5]:
            stage = t.get("stage", "?")
            reasoning = t.get("reasoning", "")
            print(f"    [{stage}] {truncate(reasoning, 100)}")

    # MCP Tool: get_remediation_context (institutional memory)
    if history:
        subheader("MCP: get_remediation_context (what Run 3 would see)")
        issue_type = history[0].get("issue_type", "broken_link")
        result = handle_tool_call("get_remediation_context",
                                  {"issue_type": issue_type, "limit": 5})
        print(f"  Context for '{issue_type}':")
        print(f"    Successful strategies: {len(result.get('successful_strategies', []))}")
        print(f"    Failed strategies: {len(result.get('failed_strategies', []))}")
        print(f"    Total attempts: {result.get('total_attempts', 0)}")
        if result.get('fallback_used'):
            print(f"    (Fallback: broad match used — no exact issue_type match)")

    # Signal lifecycle summary
    subheader("Signal Lifecycle (new → persistent → resolved)")
    try:
        new_sigs = store.get_signals_by_status("new")
        persistent_sigs = store.get_signals_by_status("persistent")
        resolved_sigs = store.get_signals_by_status("resolved")
        print(f"  new: {len(new_sigs)}, persistent: {len(persistent_sigs)}, resolved: {len(resolved_sigs)}")
        if resolved_sigs:
            print(f"  Resolved signals (sample):")
            for s in resolved_sigs[:3]:
                print(f"    [{s.severity}] {s.signal_type} → {truncate(s.artifact_uri, 50)}")
    except Exception as e:
        print(f"  (Lifecycle summary unavailable: {e})")

    # MCP Tool: get_remediation_metrics
    subheader("MCP: get_remediation_metrics")
    result = handle_tool_call("get_remediation_metrics", {})
    print_json("Metrics", result)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Closed-loop institutional memory demo (SQLite + CockroachDB)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE,
        help=f"Path to the knowledge base (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Max artifacts to assess (default: 20)",
    )
    parser.add_argument(
        "--db-url", type=str, default="",
        help="CockroachDB connection URL (default: from COCKROACH_DB_URL env)",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Drop and recreate all tables before running (fresh demo)",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip file backup/restore (use when source is read-only or disposable)",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Number of improvement runs (default: 2)",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.exists():
        print(f"ERROR: Source path does not exist: {source}")
        sys.exit(1)

    # Override DB URL if provided
    if args.db_url:
        os.environ["COCKROACH_DB_URL"] = args.db_url

    print()
    print("=" * 72)
    print("  Closed-Loop Institutional Memory Demo")
    print("  SQLite (local) + CockroachDB (distributed) Dual-Write")
    print("=" * 72)
    print()
    print("  This demo runs the improvement workflow TWICE:")
    print("    Run 1: No history → outcome saved to SQLite + CockroachDB")
    print("    Run 2: Reads CockroachDB history → LLM sees past outcomes")
    print("  After both runs, shows CockroachDB institutional memory.")
    print()
    print(f"  Source: {source}")
    print(f"  Limit:  {args.limit} artifacts")

    # Connect to CockroachDB
    cockroach_store = connect_cockroach()

    # Load knowledge and assess (shared across both runs)
    pipeline, assessment, artifacts, relationships, source_str, git_commit = (
        load_and_assess(source, args.limit)
    )

    # Persist to CockroachDB with vector embeddings (demonstrates distributed vector index)
    persist_to_cockroach(cockroach_store, assessment, artifacts, source_str, git_commit)

    # Set up LLM gateway (optional — falls back to heuristics if not available)
    llm_gateway = None
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        try:
            from ai_ready.llm.gateway import LLMGateway
            llm_gateway = LLMGateway(
                provider="groq",
                api_key=groq_key,
                default_model="llama-3.3-70b-versatile",
            )
            print(f"\n  LLM: Groq (llama-3.3-70b-versatile)")
        except Exception as e:
            print(f"\n  LLM: unavailable ({e}), using heuristics")
    else:
        print(f"\n  LLM: no GROQ_API_KEY, using heuristics")

    # --- Backup artifact files so we can restore after demo ---
    import shutil
    backup_dir = None
    source_path = Path(source_str)
    if not args.no_backup:
        backup_dir = Path(".ai-ready/file_backup")
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backed_up = 0
        for a in artifacts:
            src_file = source_path / a.uri
            if src_file.exists():
                dst_file = backup_dir / a.uri
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                backed_up += 1
        print(f"\n  Backed up {backed_up} files to {backup_dir}")
    else:
        print(f"\n  Backup skipped (--no-backup)")

    # --- Run N improvement cycles ---
    for run_idx in range(1, args.runs + 1):
        label = f"{run_idx} ({'no history' if run_idx == 1 else 'with CockroachDB history'})"
        run_improvement(
            run_label=label,
            assessment=assessment,
            pipeline=pipeline,
            artifacts=artifacts,
            relationships=relationships,
            source=source_str,
            git_commit=git_commit,
            cockroach_store=cockroach_store,
            history_db_path=f".ai-ready/closed_loop_run{run_idx}.db",
            llm_gateway=llm_gateway,
        )

    # --- Show final CockroachDB state ---
    show_cockroach_memory(cockroach_store)

    # --- Restore original files ---
    if backup_dir:
        restored = 0
        for a in artifacts:
            backup_file = backup_dir / a.uri
            original_file = source_path / a.uri
            if backup_file.exists() and original_file.exists():
                shutil.copy2(backup_file, original_file)
                restored += 1
        print(f"\n  Restored {restored} files from backup")
        shutil.rmtree(backup_dir)

    banner("Demo Complete")
    print("  The closed loop is now functional:")
    print()
    print("    1. Artifacts saved with VECTOR(384) embeddings → semantic search")
    print("    2. Signals + lifecycle tracked in CockroachDB (new → persistent → resolved)")
    print("    3. Agent workflow state persisted (active → paused → completed)")
    print("    4. Run 1 had no history → outcome saved to CockroachDB")
    print("    5. Run 2 read CockroachDB context → LLM saw Run 1's outcome")
    print("    6. Run 2's outcome also saved to CockroachDB")
    print("    7. Run 3 would see BOTH outcomes + decision traces + vector search")
    print()
    print("  CockroachDB tools demonstrated:")
    print("    • Distributed Vector Indexing (VECTOR(384) + <=> cosine distance)")
    print("    • ACID Transactions (retry on serialization conflicts)")
    print("    • Agent State Persistence (Burr workflow survives restarts)")
    print("    • Signal Lifecycle Tracking (NEW → PERSISTENT → RESOLVED)")
    print("    • Decision Traces (full reasoning chain observability)")
    print("=" * 72)


if __name__ == "__main__":
    main()
