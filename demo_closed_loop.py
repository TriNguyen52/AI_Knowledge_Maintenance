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
    for table in ["decision_traces", "remediation_history", "skip_events",
                   "wont_fix_signatures", "problem_queue",
                   "knowledge_signals", "signal_lifecycle",
                   "assessment_signals", "knowledge_assessments",
                   "knowledge_artifacts", "agent_state"]:
        try:
            store._execute(f"DELETE FROM {table}")
        except Exception:
            pass  # table might not exist yet
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

    print()
    print("  Loading knowledge...")
    t0 = time.time()
    knowledge = load_knowledge_source(str(source))
    t1 = time.time()
    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    print(f"  Loaded {len(artifacts)} artifacts in {t1-t0:.1f}s")

    # Filter to English docs only (skip translations like de/, es/, fr/, etc.)
    en_artifacts = [a for a in artifacts if "/de/" not in a.uri and "/es/" not in a.uri
                    and "/fr/" not in a.uri and "/ja/" not in a.uri
                    and "/ko/" not in a.uri and "/pt/" not in a.uri
                    and "/ru/" not in a.uri and "/tr/" not in a.uri
                    and "/zh/" not in a.uri and "/uk/" not in a.uri
                    and "/az/" not in a.uri and "/it/" not in a.uri]
    print(f"  English docs: {len(en_artifacts)} (filtered from {len(artifacts)})")
    artifacts = en_artifacts

    # Filter relationships to only include filtered artifacts
    artifact_uris = {a.uri for a in artifacts}
    relationships = [r for r in relationships
                     if r.source_uri in artifact_uris and r.target_uri in artifact_uris]
    print(f"  Relationships (filtered): {len(relationships)}")

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

    # Custom problem queue with low threshold for demo
    pq_path = history_db_path.replace(".db", "_pq.db")
    custom_pq = ProblemQueue(
        history_store=sqlite_history,
        db_path=pq_path,
        salience_threshold=0.01,
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

    # Show state at approval checkpoint
    state = manager.get_state(app_id)
    print(f"  Stage: {state.get('current_stage', 'unknown')}")

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

    return final_state

# ---------------------------------------------------------------------------
# Show CockroachDB institutional memory (after both runs)
# ---------------------------------------------------------------------------

def show_cockroach_memory(store):
    banner("CockroachDB Institutional Memory (After Both Runs)")

    # Remediation history
    subheader("Remediation History Table")
    history = store.get_remediation_history(limit=20)
    print(f"  Total outcomes: {len(history)}")
    for h in history:
        print()
        print(f"  [{h.outcome_id[:8]}]")
        print(f"    issue_type: {h.issue_type}")
        print(f"    strategy: {h.strategy}")
        print(f"    strategy_description: {truncate(h.strategy_description, 100)}")
        print(f"    verification_outcome: {h.verification_outcome}")
        print(f"    score: {h.score_before} → {h.score_after}")
        print(f"    tokens: {h.tokens_used}  latency: {h.latency_ms:.0f}ms  forked: {h.forked}")
        print(f"    created: {h.created_at}")

    # Decision traces for each outcome
    subheader("Decision Traces (Full Reasoning Chain)")
    for h in history:
        traces = store.get_decision_traces(h.outcome_id)
        if traces:
            print()
            print(f"  Outcome [{h.outcome_id[:8]}] — {h.strategy}:")
            for t in traces:
                stage = t.get("stage", "?")
                reasoning = t.get("reasoning", "")
                print(f"    [{stage}]")
                for line in reasoning.split("\n")[:3]:
                    print(f"      {line}")
                if len(reasoning.split("\n")) > 3:
                    print(f"      ... ({len(reasoning.split(chr(10)))} lines total)")

    # Show merged remediation context (what Run 3 would see)
    if history:
        subheader("Merged Remediation Context (What Run 3 Would See)")
        # Get a sample issue type from the history
        sample_issue = history[0].issue_type
        print(f"  Querying context for issue_type='{sample_issue}'...")
        context = store.get_remediation_context(sample_issue, limit=5)
        print_json("Context", context)

    # Remediation metrics
    subheader("Remediation Metrics")
    metrics = store.get_remediation_metrics()
    print_json("Metrics", metrics)

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
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.exists():
        print(f"ERROR: Source path does not exist: {source}")
        sys.exit(1)

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
    backup_dir = Path(".ai-ready/file_backup")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(source_str)
    backed_up = 0
    for a in artifacts:
        src_file = source_path / a.uri
        if src_file.exists():
            dst_file = backup_dir / a.uri
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            backed_up += 1
    print(f"\n  Backed up {backed_up} files to {backup_dir}")

    # --- RUN 1: No history ---
    run_improvement(
        run_label="1 (no history)",
        assessment=assessment,
        pipeline=pipeline,
        artifacts=artifacts,
        relationships=relationships,
        source=source_str,
        git_commit=git_commit,
        cockroach_store=cockroach_store,
        history_db_path=".ai-ready/closed_loop_run1.db",
        llm_gateway=llm_gateway,
    )

    # --- RUN 2: With CockroachDB history from Run 1 ---
    run_improvement(
        run_label="2 (with CockroachDB history)",
        assessment=assessment,
        pipeline=pipeline,
        artifacts=artifacts,
        relationships=relationships,
        source=source_str,
        git_commit=git_commit,
        cockroach_store=cockroach_store,
        history_db_path=".ai-ready/closed_loop_run2.db",
        llm_gateway=llm_gateway,
    )

    # --- Show final CockroachDB state ---
    show_cockroach_memory(cockroach_store)

    # --- Restore original files ---
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
    print("    1. Run 1 had no history → outcome saved to CockroachDB")
    print("    2. Run 2 read CockroachDB context → LLM saw Run 1's outcome")
    print("    3. Run 2's outcome also saved to CockroachDB")
    print("    4. Run 3 would see BOTH outcomes + decision traces")
    print()
    print("  The DualHistoryStore transparently syncs outcomes to both")
    print("  SQLite (fast EMA/strategy stats) and CockroachDB (distributed")
    print("  persistence + decision traces) without changing workflow code.")
    print("=" * 72)


if __name__ == "__main__":
    main()
