#!/usr/bin/env python
"""Closed-loop institutional memory demo

Demonstrates the full remediation memory loop across 3 workflow runs,
with all reads and writes going to CockroachDB (no SQLite dependency).
Each run re-assesses the knowledge base from disk, so the "before" score
genuinely reflects modifications from the previous run:

  Run 1 (no history):
    1. Assess knowledge base → signals → problems
    2. Generate remediation proposal (no prior outcomes to learn from)
    3. Execute + verify
    4. Outcome saved to CockroachDB

  Run 2 (with history from Run 1):
    1. Re-assess knowledge base (picks up Run 1's modifications)
    2. Generate remediation proposal — reads CockroachDB context:
       - Sees what strategy was tried in Run 1
       - Sees whether it succeeded or failed
       - Sees decision traces (reasoning from Run 1)
    3. Execute + verify
    4. Second outcome saved — CockroachDB now has 2 outcomes

  Run 3 (with history from Runs 1+2):
    1. Re-assess knowledge base (picks up Run 2's modifications)
    2. Generate remediation proposal — reads CockroachDB context:
       - Sees both prior outcomes + decision traces
    3. Execute + verify
    4. Third outcome saved — CockroachDB now has 3 outcomes

  After all runs:
    - Per-run comparison table (score before/after, delta, signals resolved)
    - Cumulative improvement summary
    - CockroachDB verification (context storage, agent state, MCP tools, vector search)
    - CockroachDB institutional memory (MCP tools showing all outcomes)

Usage:
    python demo_closed_loop.py [--source PATH] [--limit N] [--runs N]

    --source    Path to the knowledge base (default: FastAPI docs)
    --limit     Max artifacts to assess (default: 20)
    --runs      Number of improvement runs (default: 3)

All formatting logic lives in ai_ready.cli.display — this script is call-only.
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

# Import display module — all formatting lives here
from ai_ready.cli import display

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = Path("./fastapi/docs/en/docs")

# ---------------------------------------------------------------------------
# Helpers (non-display logic only)
# ---------------------------------------------------------------------------

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
    db_url = os.environ.get("COCKROACH_DB_URL", "")
    if not db_url:
        display.print_error("COCKROACH_DB_URL not set in .env")
        sys.exit(1)
    display.print_db_connecting(db_url[:60])

    from ai_ready.storage.cockroach import CockroachDBStore
    store = CockroachDBStore(connection_string=db_url)
    store.connect()
    display.print_db_connected()
    display.print_dim("Initializing schema...")
    store.initialize_schema()
    display.print_db_schema_ready()

    # Clear existing data for a fresh demo
    display.print_dim("Clearing existing data for fresh demo...")
    for table in ["decision_traces", "remediation_history", "skip_events",
                   "wont_fix_signatures", "problem_queue",
                   "assessment_signals", "signal_lifecycle",
                   "knowledge_signals", "knowledge_assessments",
                   "knowledge_artifacts", "agent_state",
                   "modified_artifacts"]:
        try:
            store._execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:
            pass
    store._initialized = False
    store.initialize_schema()
    display.print_db_cleared()

    return store

# ---------------------------------------------------------------------------
# Load knowledge and assess
# ---------------------------------------------------------------------------

def load_and_assess(source: Path, limit: int, offset: int = 0):
    display.print_load_start(str(source), limit)

    from ai_ready.knowledge.registry import load_knowledge_source
    from ai_ready.pipeline import AssessmentPipeline

    exclude_patterns = ["/de/", "/es/", "/fr/", "/ja/", "/ko/", "/pt/",
                        "/ru/", "/tr/", "/zh/", "/uk/", "/az/", "/it/"]

    t0 = time.time()
    knowledge = load_knowledge_source(str(source), exclude_patterns=exclude_patterns)
    t1 = time.time()
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    display.print_load_done(len(artifacts), len(relationships), t1 - t0)

    if offset > 0 and len(artifacts) > offset:
        artifacts = artifacts[offset:]
    if limit > 0 and len(artifacts) > limit:
        artifacts = artifacts[:limit]
        display.print_load_done(len(artifacts), len(relationships), t1 - t0, limited=limit)

    source_str = str(source.resolve())
    git_commit = get_git_commit(source_str)

    display.print_assessment_start()
    pipeline = AssessmentPipeline()
    t0 = time.time()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source_str,
        git_commit=git_commit,
        relationships=relationships,
    )
    t1 = time.time()
    display.print_assessment_done(assessment.score, len(assessment.signals), t1 - t0)

    return pipeline, assessment, artifacts, relationships, source_str, git_commit

# ---------------------------------------------------------------------------
# Persist artifacts, signals, assessment to CockroachDB with vector embeddings
# ---------------------------------------------------------------------------

def persist_to_cockroach(store, assessment, artifacts, source, git_commit):
    """Persist assessment results to CockroachDB with vector embeddings."""
    display.print_persist_header(len(artifacts), len(assessment.signals))

    from ai_ready.storage.persistence import persist_assessment
    from ai_ready.llm.embeddings import get_embedder

    result = persist_assessment(
        store=store,
        assessment=assessment,
        signals=assessment.signals,
        artifacts=artifacts,
        source=source,
    )

    embedder = get_embedder()

    artifact_count_db = store._execute_fetchone(
        "SELECT COUNT(*) as cnt FROM knowledge_artifacts WHERE source = %s", (source,)
    )
    signal_count_db = store._execute_fetchone(
        "SELECT COUNT(*) as cnt FROM knowledge_signals"
    )
    assessment_db = store._execute_fetchone(
        "SELECT assessment_id FROM knowledge_assessments WHERE assessment_id = %s",
        (result["assessment_id"],),
    )
    display.print_persist_done(
        artifact_count_db['cnt'] if artifact_count_db else 0,
        signal_count_db['cnt'] if signal_count_db else 0,
        assessment_db['assessment_id'] if assessment_db else 'NOT FOUND',
        embedder.backend_status,
    )

    # Demonstrate vector search (CockroachDB distributed vector index)
    worst = max(assessment.signals, key=lambda s: s.score) if assessment.signals else None
    if worst:
        q_emb = embedder.embed(f"{worst.artifact_uri} {worst.signal_type} {worst.recommendation or ''}"[:2000])
        results = store.search_artifacts_semantic(q_emb, limit=5)
        display.print_vector_search(
            worst.artifact_uri,
            [{"similarity": sim, "artifact_uri": art.artifact_uri} for art, sim in results],
        )

    from ai_ready.storage.models import AssessmentRecord
    return AssessmentRecord(
        assessment_id=result["assessment_id"],
        score=float(assessment.score),
        dimensions=json.dumps({name: d.score for name, d in assessment.dimensions.items()}),
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
    llm_gateway=None,
):
    """Run a single improvement workflow and return the final state."""
    display.print_run_header(run_label)

    from ai_ready.improvement.manager import ImprovementManager
    from ai_ready.improvement.executor import KnowledgeExecutor
    from ai_ready.storage.adapters import CockroachAssessmentStore, CockroachHistoryStore

    assessment_store = CockroachAssessmentStore(cockroach_store)
    history_store = CockroachHistoryStore(cockroach_store)

    from ai_ready.config import Config
    config = Config.default()

    known_uris = {a.uri for a in artifacts}
    executor = KnowledgeExecutor(
        artifact_store=assessment_store,
        source_path=source,
        assessment_signals=assessment.signals,
        known_artifact_uris=known_uris,
    )

    display.print_manager_created()

    manager = ImprovementManager(
        llm_gateway=llm_gateway,
        assessment_store=assessment_store,
        assessment_pipeline=pipeline,
        history_store=history_store,
        executor=executor,
        artifacts=artifacts,
        relationships=relationships,
        source=source,
        git_commit=git_commit,
        enable_tracking=False,
        enable_otel=False,
        max_fork_attempts=2,
        cockroach_store=cockroach_store,
        config=config,
    )

    # Check what CockroachDB already has (institutional memory)
    try:
        all_history = cockroach_store.get_remediation_history(limit=10)
        display.print_history_check(all_history)
    except Exception as e:
        display.print_warning(f"Could not read history: {e}")

    # Start the workflow
    display.print_workflow_starting()
    t0 = time.time()
    try:
        app_id = manager.start_improvement(assessment)
    except Exception as e:
        display.print_error(str(e))
        import traceback
        traceback.print_exc()
        return None
    t1 = time.time()
    display.print_workflow_started(app_id, t1 - t0)

    # Agent state is persisted by ImprovementManager automatically
    agent_state_db = cockroach_store.get_agent_state(app_id)
    if agent_state_db:
        display.print_agent_state(
            agent_state_db.get('status', 'unknown'),
            agent_state_db.get('current_stage', 'unknown'),
        )
    else:
        display.print_agent_state_not_found()

    # Show state at approval checkpoint
    state = manager.get_state(app_id)
    display.print_kv("Stage", state.get('current_stage', 'unknown'))
    agent_state_paused = cockroach_store.get_agent_state(app_id)
    if agent_state_paused:
        display.print_kv("Agent state in DB", f"status={agent_state_paused.get('status', 'unknown')}")

    root_causes = state.get("root_cause_analysis", [])
    problems = state.get("knowledge_problems", [])
    proposals = state.get("proposals", [])

    display.print_root_causes(root_causes)
    display.print_knowledge_problems(problems)
    display.print_proposals(proposals, selected_idx=0)

    # Show what historical context was used
    decision_trace = state.get("decision_trace", {})
    hist_context = decision_trace.get("historical_context_used", "")
    display.print_historical_context(hist_context)

    # Auto-approve and complete
    display.print_auto_approve()
    t2 = time.time()
    try:
        final_state = manager.approve_and_complete(
            app_id, approved=True, reason="auto-approved for demo"
        )
    except Exception as e:
        display.print_error(str(e))
        import traceback
        traceback.print_exc()
        return final_state if 'final_state' in dir() else None
    t3 = time.time()

    final_stage = final_state.get('current_stage', 'completed')
    final_status = "completed" if "completed" in final_stage or "verified" in final_stage else "failed"
    display.print_execution_done(t3 - t2, final_stage, final_status)

    # Show verification
    verification = final_state.get("verification_results", {})
    display.print_verification(verification)

    memory_influence = final_state.get("memory_influence", {})
    display.print_memory_in_action(memory_influence)

    display.print_knowledge_health(verification)

    # Confirm CockroachDB sync
    try:
        all_history = cockroach_store.get_remediation_history(limit=10)
        latest = all_history[0] if all_history else None
        traces = cockroach_store.get_decision_traces(latest.outcome_id) if latest else []
        display.print_cockroach_sync(all_history, traces)
    except Exception as e:
        display.print_warning(f"Could not read CockroachDB: {e}")

    verification = final_state.get("verification_results", {})
    overall_outcome = verification.get("overall_outcome", "unknown")
    try:
        new_signals = cockroach_store.get_signals_by_status("new")
        resolved_signals = cockroach_store.get_signals_by_status("resolved")
        persistent_signals = cockroach_store.get_signals_by_status("persistent")
        display.print_signal_lifecycle(
            len(new_signals), len(persistent_signals), len(resolved_signals),
            resolved_sample=resolved_signals,
        )
    except Exception as e:
        display.print_warning(f"Lifecycle summary unavailable: {e}")

    return final_state

# ---------------------------------------------------------------------------
# Show CockroachDB institutional memory (after both runs)
# ---------------------------------------------------------------------------

def show_cockroach_memory(store):
    from ai_ready.cloud.mcp_server import handle_tool_call, MCP_TOOLS, set_store
    set_store(store)

    display.print_mcp_header(len(MCP_TOOLS), MCP_TOOLS)

    # MCP Tool: get_knowledge_health
    result = handle_tool_call("get_knowledge_health", {})
    if "message" in result:
        display.print_dim(result['message'])
    else:
        display.print_mcp_health(result.get('score', 0), result.get('signals_count', 0))

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
    except Exception as e:
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
# Per-run result extraction
# ---------------------------------------------------------------------------

def extract_run_result(run_idx: int, final_state: dict, elapsed: float) -> dict:
    """Extract per-run metrics from the final workflow state."""
    verification = final_state.get("verification_results", {})
    proposals = final_state.get("proposals", [])
    selected_idx = final_state.get("selected_proposal_idx", 0)
    strategy = (
        proposals[selected_idx].get("strategy", "?")
        if proposals and selected_idx < len(proposals)
        else "?"
    )
    memory_influence = final_state.get("memory_influence", {})

    return {
        "run": run_idx,
        "score_before": verification.get("before_score", 0),
        "score_after": verification.get("after_score", 0),
        "score_delta": verification.get("score_difference", 0),
        "signals_resolved": len(verification.get("resolved_signal_ids", [])),
        "new_signals": len(verification.get("new_signal_ids", [])),
        "remaining_signals": len(verification.get("remaining_signal_ids", [])),
        "outcome": verification.get("overall_outcome", "unknown"),
        "strategy": strategy,
        "tokens_used": final_state.get("cumulative_tokens", 0),
        "elapsed": elapsed,
        "memory_retrieved_count": len(memory_influence.get("retrieved", [])),
        "memory_avoided_count": len(memory_influence.get("avoided_strategies", [])),
        "memory_reused_strategy": memory_influence.get("reused_strategy"),
    }


# ---------------------------------------------------------------------------
# Verify CockroachDB institutional memory
# ---------------------------------------------------------------------------

def verify_cockroachdb(store, results: list[dict]) -> None:
    """Verify that CockroachDB is actually storing and retrieving data."""
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Closed-loop institutional memory demo (CockroachDB-only, no SQLite)",
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
        "--offset", type=int, default=0,
        help="Skip first N artifacts (use with --limit to test different subsets)",
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
        "--runs", type=int, default=3,
        help="Number of improvement runs (default: 3)",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.exists():
        display.print_error(f"Source path does not exist: {source}")
        sys.exit(1)

    if args.db_url:
        os.environ["COCKROACH_DB_URL"] = args.db_url

    display.print_intro(str(source), args.limit, args.runs)

    # Connect to CockroachDB
    cockroach_store = connect_cockroach()

    try:
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
              display.print_llm_info("Groq", "llama-3.3-70b-versatile")
          except Exception as e:
              display.print_llm_unavailable(str(e))
      else:
          display.print_llm_no_key()

      # --- Run N improvement cycles with per-run re-assessment ---
      import shutil
      from ai_ready.improvement.closed_loop import run_closed_loop_cycle

      backup_dir = None
      source_path = None
      results = []

      for run_idx in range(1, args.runs + 1):
          # Backup files on first run only (before any modifications)
          if run_idx == 1 and not args.no_backup:
              from ai_ready.knowledge.registry import load_knowledge_source
              exclude_patterns = ["/de/", "/es/", "/fr/", "/ja/", "/ko/", "/pt/",
                                  "/ru/", "/tr/", "/zh/", "/uk/", "/az/", "/it/"]
              ks = load_knowledge_source(str(source), exclude_patterns=exclude_patterns)
              backup_dir = Path(".ai-ready/file_backup")
              if backup_dir.exists():
                  shutil.rmtree(backup_dir)
              backup_dir.mkdir(parents=True, exist_ok=True)
              backed_up = 0
              for a in (ks.artifacts[args.offset:args.offset+args.limit] if args.limit > 0 else ks.artifacts[args.offset:]):
                  src_file = source / a.uri
                  if src_file.exists():
                      dst_file = backup_dir / a.uri
                      dst_file.parent.mkdir(parents=True, exist_ok=True)
                      shutil.copy2(src_file, dst_file)
                      backed_up += 1
              display.print_backup_done(backed_up, str(backup_dir))
          elif run_idx == 1 and args.no_backup:
              display.print_backup_skipped()

          # Run one closed-loop cycle (codebase handles everything)
          display.print_dim(f"--- Run {run_idx} ---")
          t0 = time.time()
          result = run_closed_loop_cycle(
              store=cockroach_store,
              source=source,
              limit=args.limit,
              run_idx=run_idx,
              llm_gateway=llm_gateway,
              offset=args.offset,
          )
          t1 = time.time()

          rollback_tag = " [ROLLED BACK]" if result.get("rolled_back") else ""
          display.print_dim(
              f"Run {run_idx}: {result['score_before']} -> {result['score_after']} "
              f"(+{result['score_delta']}), {result['signals_resolved']} resolved, "
              f"{result['new_signals']} new, {len(result['modified_uris'])} modified"
              f"{rollback_tag}"
          )

          results.append({
              "run": run_idx,
              "score_before": result["score_before"],
              "score_after": result["score_after"],
              "score_delta": result["score_delta"],
              "signals_resolved": result["signals_resolved"],
              "new_signals": result["new_signals"],
              "outcome": result["outcome"],
              "rolled_back": result.get("rolled_back", False),
              "strategy": result["strategy"],
              "elapsed": t1 - t0,
              "memory_retrieved_count": len(result.get("memory_influence", {}).get("retrieved", [])),
              "memory_avoided_count": len(result.get("memory_influence", {}).get("avoided_strategies", [])),
              "memory_reused_strategy": result.get("memory_influence", {}).get("reused_strategy"),
          })

          source_path = source

      # --- Per-run comparison and cumulative improvement ---
      if results:
          display.print_per_run_summary(results)
          display.print_cumulative_improvement(results)

      # --- CockroachDB verification ---
      verify_cockroachdb(cockroach_store, results)

      # --- Show final CockroachDB state (MCP tools) ---
      show_cockroach_memory(cockroach_store)

      # --- Restore original files ---
      if backup_dir and source_path:
          restored = 0
          for backup_file in backup_dir.rglob("*.md"):
              rel = backup_file.relative_to(backup_dir)
              original_file = source_path / rel
              if original_file.exists():
                  shutil.copy2(backup_file, original_file)
                  restored += 1
          display.print_restore_done(restored)
          shutil.rmtree(backup_dir)

      display.print_demo_complete(args.runs)
    finally:
      cockroach_store.close()


if __name__ == "__main__":
    main()
