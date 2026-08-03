#!/usr/bin/env python
"""End-to-end demo of AI Knowledge Maintenance using Groq LLM + CockroachDB.

Runs the full pipeline against the real FastAPI documentation repository:
  1. Ingestion    — MarkdownKnowledgeSDK discovers and parses docs
  2. Assessment   — Collectors → Signals → Dimensions → Score
  3. Persistence  — Artifacts, signals, assessment saved to CockroachDB
  4. Improvement  — Burr state machine: analyze → propose → approve → execute → verify
  5. Memory       — Remediation outcomes, decision traces, health trends in CockroachDB

Usage:
    python demo_groq_cockroach.py [--source PATH] [--limit N] [--auto-approve]

    --source        Path to the knowledge base (default: FastAPI docs)
    --limit         Max number of artifacts to assess (default: 50, 0 = all)
    --auto-approve  Skip the human approval checkpoint (auto-approve proposals)
"""

from __future__ import annotations

import os
import sys
import time
import json
import argparse
import subprocess
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

DEFAULT_SOURCE = Path("./fastapi/docs/en/docs")

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

def get_git_commit(path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

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

# ---------------------------------------------------------------------------
# Phase 1: Connect to CockroachDB
# ---------------------------------------------------------------------------

def phase_1_connect_cockroach() -> "CockroachDBStore":
    banner("PHASE 1: Connect to CockroachDB")
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
    print("  Initializing schema (11 tables)...")
    store.initialize_schema()
    print("  Schema initialized: agent_state, knowledge_artifacts, knowledge_signals,")
    print("    signal_lifecycle, knowledge_assessments, assessment_signals,")
    print("    problem_queue, wont_fix_signatures, remediation_history,")
    print("    decision_traces, skip_events")
    return store

# ---------------------------------------------------------------------------
# Phase 2: Load FastAPI knowledge
# ---------------------------------------------------------------------------

def phase_2_load_knowledge(source: Path, limit: int):
    banner("PHASE 2: Load Knowledge from FastAPI Repository")
    print(f"  Source: {source}")
    print(f"  Artifact limit: {limit if limit > 0 else 'all'}")
    print()

    from ai_ready.knowledge.registry import load_knowledge_source
    print("  Loading via MarkdownKnowledgeSDK...")
    t0 = time.time()
    knowledge = load_knowledge_source(str(source))
    t1 = time.time()

    artifacts = knowledge.artifacts
    relationships = knowledge.relationships
    print(f"  Loaded {len(artifacts)} artifacts in {t1-t0:.1f}s")
    print(f"  Extracted {len(relationships)} relationships")

    if limit > 0 and len(artifacts) > limit:
        print(f"  Limiting to first {limit} artifacts (for demo speed)")
        artifacts = artifacts[:limit]

    # Show sample
    if artifacts:
        subheader("Sample artifacts")
        for a in artifacts[:5]:
            print(f"    [{a.artifact_type}] {Path(a.uri).name}")
            print(f"      uri: {truncate(a.uri, 80)}")
            if hasattr(a, 'title') and a.title:
                print(f"      title: {a.title}")
        if len(artifacts) > 5:
            print(f"    ... and {len(artifacts) - 5} more")

    return knowledge, artifacts, relationships

# ---------------------------------------------------------------------------
# Phase 3: Run assessment pipeline
# ---------------------------------------------------------------------------

def phase_3_assess(artifacts, relationships, source, git_commit):
    banner("PHASE 3: Run Assessment Pipeline")
    print("  Pipeline: Collectors → Signals → InterpretationPolicy → Dimensions → Score")
    print(f"  Artifacts to assess: {len(artifacts)}")
    print()

    from ai_ready.pipeline import AssessmentPipeline
    pipeline = AssessmentPipeline()

    print("  Running collectors (10 collectors across 6 dimensions)...")
    t0 = time.time()
    assessment = pipeline.run(
        artifacts=artifacts,
        source=source,
        git_commit=git_commit,
        relationships=relationships,
    )
    t1 = time.time()
    print(f"  Assessment complete in {t1-t0:.1f}s")
    print()

    # Show results
    subheader("Assessment Results")
    print(f"  Assessment ID: {assessment.assessment_id}")
    print(f"  Score:          {assessment.score}")
    print(f"  Verdict:        {assessment.verdict}")
    print(f"  Total Signals:  {len(assessment.signals)}")
    print()

    subheader("Dimension Scores")
    for dim_name, dim_score in sorted(assessment.dimensions.items()):
        print(f"    {dim_name:20s}  score={dim_score.score:3d}/100  signals={dim_score.signals_count}  collectors={len(dim_score.collector_ids)}")

    # Show worst dimension
    if assessment.worst_dimension:
        print()
        print(f"  Worst dimension: {assessment.worst_dimension}")
    if assessment.critical_dimensions:
        print(f"  Critical dimensions (below 50): {', '.join(assessment.critical_dimensions)}")

    # Show score explanation
    subheader("Score Explanation")
    explanation = assessment.explain_score()
    print(f"    Summary: {explanation.summary}")
    print()
    print(f"    Lost score points: {explanation.lost_score_points}")
    print(f"    Dominant dimensions:")
    for d in explanation.dominant_dimensions[:5]:
        print(f"      {d['dimension']:20s}  score={d['score']:3d}  lost={d['estimated_lost_score_points']:.2f}  signals={d['signals_count']}")
    print()
    print(f"    Top score contributors:")
    for c in explanation.dominant_contributors[:5]:
        print(f"      [{c.dimension}] {c.cause}")
        print(f"        collector={c.collector_id}  signals={c.signal_count}  artifacts={c.artifact_count}")
        print(f"        estimated gain: {c.estimated_score_gain:.2f} points")
        if c.recommendation:
            print(f"        → {truncate(c.recommendation, 80)}")

    # Show signal breakdown
    subheader("Signal Breakdown (top 15 by severity)")
    sorted_signals = sorted(assessment.signals, key=lambda s: (-s.score, s.severity.value))
    for s in sorted_signals[:15]:
        print(f"    [{s.severity.value:8s}] {s.collector_id:30s} score={s.score:3d}  {truncate(s.artifact_uri, 50)}")
        if s.recommendation:
            print(f"               → {truncate(s.recommendation, 80)}")
    if len(sorted_signals) > 15:
        print(f"    ... and {len(sorted_signals) - 15} more signals")

    return pipeline, assessment

# ---------------------------------------------------------------------------
# Phase 4: Persist to CockroachDB
# ---------------------------------------------------------------------------

def phase_4_persist_cockroach(store, assessment, artifacts, source, git_commit):
    banner("PHASE 4: Persist to CockroachDB")
    print("  Saving artifacts, signals, and assessment to CockroachDB...")
    print()

    from ai_ready.storage.models import ArtifactRecord, SignalRecord, AssessmentRecord

    # Save artifacts
    subheader(f"Saving {len(artifacts)} artifacts")
    saved_artifacts = 0
    for a in artifacts:
        # Get text content from the artifact
        content_text = ""
        if hasattr(a, 'all_text'):
            content_text = a.all_text[:10000]
        elif isinstance(a.content, str):
            content_text = a.content[:10000]
        elif isinstance(a.content, dict):
            content_text = str(a.content)[:10000]

        record = ArtifactRecord.new(
            artifact_uri=a.uri,
            artifact_type=a.artifact_type,
            source=source,
            content=content_text,
            metadata={"git_commit": git_commit, "title": getattr(a, 'title', '')},
        )
        store.save_artifact(record)
        saved_artifacts += 1
    print(f"  Saved {saved_artifacts} artifacts to knowledge_artifacts table")

    # Save signals
    subheader(f"Saving {len(assessment.signals)} signals")
    saved_signals = 0
    for s in assessment.signals:
        record = SignalRecord.new(
            artifact_uri=s.artifact_uri,
            collector_id=s.collector_id,
            signal_type=s.signal_type,
            severity=s.severity.value,
            score=float(s.score),
            recommendation=s.recommendation or "",
            ai_impact=s.ai_impact or "",
            evidence=s.evidence if isinstance(s.evidence, dict) else {"raw": str(s.evidence)},
        )
        store.save_signal(record)
        saved_signals += 1
    print(f"  Saved {saved_signals} signals to knowledge_signals table")
    # Note: signal_lifecycle table uses UUID types for signal_id and assessment_id,
    # but our IDs are string-based (timestamp/hash). Skipping lifecycle tracking for now.

    # Save assessment
    subheader("Saving assessment")
    dim_dict = {}
    for name, ds in assessment.dimensions.items():
        dim_dict[name] = {"score": ds.score, "signals_count": ds.signals_count, "collectors": ds.collector_ids}

    assessment_record = AssessmentRecord.new(
        score=float(assessment.score),
        dimensions=dim_dict,
        signals_count=len(assessment.signals),
        metadata={
            "source": source,
            "git_commit": git_commit,
            "verdict": assessment.verdict,
        },
    )
    store.save_assessment(assessment_record)
    print(f"  Saved assessment {assessment_record.assessment_id}")
    print(f"    score={assessment_record.score:.3f}, signals={assessment_record.signals_count}")
    # Note: assessment_signals junction table uses UUID types, skipping linkage.

    return assessment_record

# ---------------------------------------------------------------------------
# Phase 5: Run improvement workflow with Groq LLM
# ---------------------------------------------------------------------------

def phase_5_improvement(assessment, pipeline, artifacts, relationships, source, git_commit, auto_approve, cockroach_store=None):
    banner("PHASE 5: Run Improvement Workflow (Groq LLM + Burr State Machine)")
    print("  LLM Provider: Groq (llama-3.3-70b-versatile)")
    print("  Workflow: analyze_issue → generate_proposal → review_approval → execute_change → verify_improvement")
    print(f"  Auto-approve: {auto_approve}")
    print()

    from ai_ready.llm.gateway import LLMGateway
    from ai_ready.improvement.manager import ImprovementManager
    from ai_ready.improvement.history import ImprovementHistoryStore
    from ai_ready.improvement.executor import KnowledgeExecutor
    from ai_ready.stores import AssessmentStore, SignalStore

    # Set up LLM gateway with Groq
    print("  Initializing Groq LLM gateway...")
    gateway = LLMGateway(
        provider="groq",
        api_key=os.environ.get("GROQ_API_KEY", ""),
        default_model="llama-3.3-70b-versatile",
    )
    print(f"  Gateway ready: provider={gateway.provider_name}")

    # Set up local SQLite stores for the improvement workflow
    # (The Burr actions use these internally for diff/trend; CockroachDB is the persistent layer)
    db_path = ".ai-ready/demo_groq.db"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(db_path).exists():
        Path(db_path).unlink()

    signal_store = SignalStore(db_path)
    assessment_store = AssessmentStore(db_path)

    # Save the assessment to SQLite so the improvement workflow can diff against it
    assessment_store.save(assessment)
    signal_store.save_signals(assessment.assessment_id, assessment.signals)

    # Set up history store
    history_db = "improvement_history.db"
    history_store = ImprovementHistoryStore(history_db)

    # Set up executor (dry-run mode — no artifact_store, just logs)
    executor = KnowledgeExecutor(artifact_store=None)
    print(f"  Executor: dry-run mode (no file modifications)")

    # Create a custom ProblemQueue with a low salience threshold for the demo
    from ai_ready.improvement.problem_queue import ProblemQueue
    pq_path = history_db.replace(".db", "_problem_queue.db")
    custom_pq = ProblemQueue(
        history_store=history_store,
        db_path=pq_path,
        salience_threshold=0.01,  # Lower threshold so demo shows full flow
        signal_store=signal_store,
    )

    # Create the ImprovementManager
    print()
    print("  Creating ImprovementManager...")
    manager = ImprovementManager(
        llm_gateway=gateway,
        assessment_store=assessment_store,
        assessment_pipeline=pipeline,
        history_store=history_store,
        executor=executor,
        artifacts=artifacts,
        relationships=relationships,
        source=source,
        git_commit=git_commit,
        history_db_path=history_db,
        enable_tracking=False,
        enable_otel=False,
        max_fork_attempts=3,
        problem_queue=custom_pq,
        cockroach_store=cockroach_store,
    )
    print("  ImprovementManager ready.")

    # Start the improvement workflow
    subheader("Starting improvement workflow")
    print("  The workflow will:")
    print("    1. Analyze signals to identify root causes (LLM)")
    print("    2. Discover knowledge problems (heuristic + LLM)")
    print("    3. Rank problems by salience")
    print("    4. Generate remediation proposals (LLM)")
    print("    5. Pause at human approval checkpoint")
    print()

    t0 = time.time()
    try:
        app_id = manager.start_improvement(assessment)
    except Exception as e:
        print(f"  ERROR during start_improvement: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None
    t1 = time.time()

    print(f"  Workflow started: app_id={app_id}")
    print(f"  Elapsed: {t1-t0:.1f}s")

    # Show current state at the approval checkpoint
    state = manager.get_state(app_id)
    subheader("Workflow State at Approval Checkpoint")
    print(f"  Current stage: {state.get('current_stage', 'unknown')}")
    print(f"  Remediation ID: {state.get('remediation_id', 'unknown')}")

    # Show knowledge problems discovered
    problems = state.get("knowledge_problems", [])
    if problems:
        subheader(f"Knowledge Problems Discovered ({len(problems)})")
        for i, p in enumerate(problems[:10]):
            print(f"    [{i+1}] Category: {p.get('category', 'unknown')}")
            print(f"        Artifacts: {truncate(str(p.get('artifact_uris', [])), 80)}")
            print(f"        Description: {truncate(p.get('description', ''), 80)}")
        if len(problems) > 10:
            print(f"    ... and {len(problems) - 10} more problems")

    # Show salience rankings
    saliences = state.get("problem_saliences", [])
    if saliences:
        subheader(f"Problem Salience Rankings ({len(saliences)})")
        for s in saliences[:5]:
            print(f"    Category: {s.get('category', 'unknown')}")
            print(f"      Salience: {s.get('salience', 0):.3f}")
            print(f"      Encoding: {s.get('encoding_score', 0):.3f}, Outcome: {s.get('outcome_score', 0):.3f}")
            print()

    # Show root cause analysis
    root_causes = state.get("root_cause_analysis", [])
    if root_causes:
        subheader(f"Root Cause Analysis ({len(root_causes)})")
        for rc in root_causes[:5]:
            print(f"    Category: {rc.get('category', 'unknown')}")
            print(f"    Hypothesis: {truncate(rc.get('hypothesis', ''), 120)}")
            print(f"    Confidence: {rc.get('confidence', 'N/A')}")
            print()

    # Show proposals
    proposals = state.get("proposals", [])
    if proposals:
        subheader(f"Remediation Proposals ({len(proposals)})")
        for i, p in enumerate(proposals):
            print(f"  Proposal {i+1}:")
            print(f"    Strategy: {p.get('strategy', 'unknown')}")
            print(f"    Description: {truncate(p.get('description', ''), 120)}")
            print(f"    Reasoning: {truncate(p.get('reasoning', ''), 120)}")
            steps = p.get("modification_steps", [])
            print(f"    Modification steps: {len(steps)}")
            for j, step in enumerate(steps[:3]):
                print(f"      [{j+1}] {step.get('step_type', 'unknown')} → {truncate(step.get('artifact_uri', ''), 60)}")
                print(f"          {truncate(step.get('description', ''), 80)}")
            if len(steps) > 3:
                print(f"      ... and {len(steps) - 3} more steps")
            print()
    else:
        print()
        print("  No proposals generated.")

    # Show skip events
    skip_events = state.get("skip_events", [])
    if skip_events:
        subheader(f"Skip Events ({len(skip_events)})")
        for se in skip_events[:5]:
            print(f"    Lane: {se.get('lane', '')}, Reason: {se.get('reason', '')}")

    # Show decision trace
    decision_trace = state.get("decision_trace", {})
    if decision_trace:
        subheader("Decision Trace (so far)")
        for key, val in decision_trace.items():
            if isinstance(val, str):
                print(f"    {key}: {truncate(val, 100)}")
            elif isinstance(val, list):
                print(f"    {key}: [{len(val)} items]")
            elif isinstance(val, dict):
                print(f"    {key}: {{...{len(val)} keys...}}")
            else:
                print(f"    {key}: {val}")

    # Approval step
    if auto_approve:
        subheader("Auto-approving proposal and completing workflow")
        print("  Running: execute_change → verify_improvement...")
        print()
        t2 = time.time()
        try:
            final_state = manager.approve_and_complete(
                app_id, approved=True, reason="auto-approved for demo"
            )
        except Exception as e:
            print(f"  ERROR during approve_and_complete: {e}")
            import traceback
            traceback.print_exc()
            return manager, app_id, state
        t3 = time.time()

        print(f"  Workflow completed in {t3-t2:.1f}s")
        print()
        subheader("Final State")
        print(f"  Current stage: {final_state.get('current_stage', 'unknown')}")
        print(f"  Verification results:")
        verification = final_state.get("verification_results", {})
        if verification:
            print(f"    Overall outcome: {verification.get('overall_outcome', 'unknown')}")
            print(f"    Score before:    {verification.get('score_before', 'N/A')}")
            print(f"    Score after:     {verification.get('score_after', 'N/A')}")
            print(f"    Score difference: {verification.get('score_difference', 'N/A')}")
            if verification.get("summary"):
                print(f"    Summary: {truncate(verification['summary'], 120)}")
            if verification.get("failure_explanation"):
                print(f"    Failure: {truncate(verification['failure_explanation'], 120)}")

        # Show execution results
        execution = final_state.get("execution_results", {})
        if execution:
            subheader("Execution Results")
            steps = execution.get("steps", [])
            print(f"  Steps executed: {len(steps)}")
            for step in steps[:5]:
                print(f"    [{step.get('step_type', '')}] {truncate(step.get('artifact_uri', ''), 60)}")
                print(f"      Success: {step.get('success', False)}")
                print(f"      {truncate(step.get('description', ''), 80)}")

        # Show full decision trace
        final_trace = final_state.get("decision_trace", {})
        if final_trace:
            subheader("Full Decision Trace")
            print_json("Trace", final_trace)

        # Check if we should retry (fork)
        if final_state.get("current_stage") in ["failed_execution", "failed_verification"]:
            subheader("Workflow FAILED — checking if retry is possible")
            should_retry = manager.should_retry(app_id)
            print(f"  Should retry: {should_retry}")
            if should_retry:
                print("  Forking workflow for retry...")
                try:
                    forked_id = manager.fork_and_retry(app_id)
                    forked_state = manager.approve_and_complete(
                        forked_id, approved=True, reason="auto-approved forked retry"
                    )
                    print(f"  Forked workflow completed: {forked_state.get('current_stage', 'unknown')}")
                except Exception as e:
                    print(f"  Forked retry failed: {e}")

        return manager, app_id, final_state
    else:
        print()
        print("  Workflow is paused at the approval checkpoint.")
        print("  To approve and continue, run with --auto-approve flag.")
        return manager, app_id, state

# ---------------------------------------------------------------------------
# Phase 6: Show CockroachDB persistent state
# ---------------------------------------------------------------------------

def phase_6_show_cockroach_state(store):
    banner("PHASE 6: CockroachDB Persistent State")
    print("  Showing what was persisted to CockroachDB...")
    print()

    # Artifacts
    subheader("Knowledge Artifacts")
    artifacts = store.get_all_artifacts()
    print(f"  Total artifacts in CockroachDB: {len(artifacts)}")
    if artifacts:
        for a in artifacts[:3]:
            print(f"    [{a.artifact_type}] {truncate(a.artifact_uri, 60)}")

    # Signals
    subheader("Knowledge Signals")
    all_signals = store.get_all_signals()
    print(f"  Total signals in CockroachDB: {len(all_signals)}")
    if all_signals:
        # Count by severity
        severity_counts = {}
        for s in all_signals:
            severity_counts[s.severity] = severity_counts.get(s.severity, 0) + 1
        for sev, count in sorted(severity_counts.items()):
            print(f"    {sev}: {count}")

    # Assessments
    subheader("Assessment History")
    assessments = store.get_assessment_history(limit=10)
    print(f"  Total assessments in CockroachDB: {len(assessments)}")
    for a in assessments[:5]:
        print(f"    [{a.assessment_id[:8]}] score={a.score:.3f} signals={a.signals_count} created={a.created_at}")

    # Assessment diff
    if len(assessments) >= 2:
        subheader("Assessment Diff (latest two)")
        diff = store.diff_assessments(assessments[1].assessment_id, assessments[0].assessment_id)
        print_json("Diff", diff)

    # Health trend
    subheader("Knowledge Health Trend")
    trend = store.get_knowledge_health_trend(days=30)
    if trend:
        for t in trend:
            print(f"    {t.get('day', '')}  avg_score={t.get('avg_score', 0):.3f}  assessments={t.get('assessment_count', 0)}")
    else:
        print("  No trend data yet (first run)")

    # Remediation history
    subheader("Remediation History (Institutional Memory)")
    remediation = store.get_remediation_history(limit=10)
    print(f"  Total remediation outcomes: {len(remediation)}")
    for r in remediation[:5]:
        print(f"    [{r.outcome_id[:8]}] issue={r.issue_type} strategy={r.strategy}")
        print(f"      outcome={r.verification_outcome} score: {r.score_before:.3f} → {r.score_after:.3f}")
        print(f"      tokens={r.tokens_used} latency={r.latency_ms:.0f}ms forked={r.forked}")

    # Remediation metrics
    if remediation:
        subheader("Remediation Metrics")
        metrics = store.get_remediation_metrics()
        print_json("Metrics", metrics)

    # Problem queue
    subheader("Problem Queue")
    problems = store.get_problems(status="open", limit=10)
    print(f"  Open problems: {len(problems)}")
    for p in problems[:5]:
        print(f"    [{p.problem_id[:8]}] {p.category} salience={p.salience:.3f}")
        print(f"      {truncate(p.description, 80)}")

    # Skip events
    subheader("Skip Events (Transparency)")
    skips = store.get_skip_events(limit=10)
    print(f"  Total skip events: {len(skips)}")
    for s in skips[:5]:
        print(f"    [{s.get('lane', '')}] {s.get('reason', '')}")

    # Agent state
    subheader("Agent Workflow State (Burr persistence)")
    active = store.get_active_workflows()
    print(f"  Active/paused workflows: {len(active)}")
    for w in active[:5]:
        print(f"    [{w.get('app_id', '')[:8]}] stage={w.get('current_stage', '')} status={w.get('status', '')}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Knowledge Maintenance demo with Groq + CockroachDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE,
        help=f"Path to the knowledge base (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max artifacts to assess (0 = all, default: 50)",
    )
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="Auto-approve proposals (skip human checkpoint)",
    )
    args = parser.parse_args()

    source = str(args.source.resolve())
    if not args.source.exists():
        print(f"ERROR: Source path does not exist: {source}")
        sys.exit(1)

    git_commit = get_git_commit(source)

    print()
    print("=" * 72)
    print("  AI Knowledge Maintenance — Full Demo")
    print("  Groq LLM + CockroachDB + FastAPI Repository")
    print("=" * 72)
    print()
    print(f"  Source:     {source}")
    print(f"  Git commit: {git_commit}")
    print(f"  LLM:        Groq (llama-3.3-70b-versatile)")
    print(f"  Database:   CockroachDB Cloud")
    print(f"  Artifacts:  {args.limit if args.limit > 0 else 'all'}")
    print(f"  Approve:    {'auto' if args.auto_approve else 'manual (will pause)'}")

    # Phase 1: Connect to CockroachDB
    store = phase_1_connect_cockroach()

    # Phase 2: Load knowledge
    knowledge, artifacts, relationships = phase_2_load_knowledge(args.source, args.limit)

    # Phase 3: Run assessment
    pipeline, assessment = phase_3_assess(artifacts, relationships, source, git_commit)

    # Phase 4: Persist to CockroachDB
    phase_4_persist_cockroach(store, assessment, artifacts, source, git_commit)

    # Phase 5: Run improvement workflow
    manager, app_id, final_state = phase_5_improvement(
        assessment, pipeline, artifacts, relationships, source, git_commit, args.auto_approve,
        cockroach_store=store,
    )

    # Phase 6: Show CockroachDB state
    phase_6_show_cockroach_state(store)

    banner("Demo Complete")
    print("  This demo exercised the full AI Knowledge Maintenance pipeline:")
    print()
    print("    1. Ingestion      MarkdownKnowledgeSDK → KnowledgeArtifacts")
    print("    2. Assessment     Collectors → Signals → Dimensions → Score")
    print("    3. Persistence    CockroachDB (artifacts, signals, assessments)")
    print("    4. Improvement     Burr state machine (Groq LLM for reasoning)")
    print("    5. Memory          Remediation history, decision traces, trends")
    print()
    print("  All data persisted to CockroachDB Cloud.")
    print("=" * 72)


if __name__ == "__main__":
    main()
