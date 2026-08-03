"""
AWS Lambda handler for AI Knowledge Maintenance.

Deploys the knowledge assessment and remediation workflow as a serverless
function. Supports three modes:

1. Assessment mode: Scan a knowledge base (S3 bucket), store signals and
   assessments in CockroachDB, discover problems, return assessment summary.

2. Remediation mode: Pick the top problem from the queue, run the Burr
   workflow (analyze → propose → execute → verify), store outcome in
   CockroachDB, return remediation result.

3. Status mode: Return current queue and metrics summary.

Trigger options:
- EventBridge scheduled rule (periodic assessment)
- API Gateway (on-demand assessment or remediation)
- S3 event (assessment on bucket change)

Environment variables required:
    COCKROACH_DB_URL — CockroachDB connection string
    AWS_REGION — AWS region
    S3_KNOWLEDGE_BUCKET — S3 bucket containing knowledge artifacts
    S3_KNOWLEDGE_PREFIX — S3 prefix (optional, defaults to "")
    GROQ_API_KEY — Groq API key for LLM access (required for remediation)
    LLM_PROVIDER — "groq" (default) or "bedrock"
    SALIENCE_THRESHOLD — minimum salience for problems (optional, default 0.01)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main AWS Lambda entry point.

    Routes based on the 'action' field in the event payload:
        - "assess" → run knowledge assessment
        - "remediate" → run remediation workflow
        - "status" → return current queue and metrics summary

    If no action is specified, defaults to "assess".

    Args:
        event: Lambda event payload. Supported keys:
            action: "assess" | "remediate" | "status"
            s3_bucket: Override S3_KNOWLEDGE_BUCKET env var
            s3_prefix: Override S3_KNOWLEDGE_PREFIX env var
            problem_id: Specific problem to remediate (optional)
        context: Lambda context (unused)

    Returns:
        dict with statusCode, body (JSON string), and headers.
    """
    try:
        # Fix 2e: Unwrap API Gateway event shape if present
        if "httpMethod" in event and "body" in event:
            # API Gateway REST format: {"httpMethod": "POST", "body": "..."}
            import json as _json
            body = event.get("body", "{}")
            if isinstance(body, str):
                event = _json.loads(body) if body else {}
            else:
                event = body
        elif "routeKey" in event and "body" in event:
            # API Gateway HTTP format
            body = event.get("body", "{}")
            if isinstance(body, str):
                import json as _json
                event = _json.loads(body) if body else {}
            else:
                event = body

        action = event.get("action", "assess")

        if action == "assess":
            result = run_assessment(event)
        elif action == "remediate":
            result = run_remediation(event)
        elif action == "status":
            result = run_status(event)
        else:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": f"Unknown action: {action}"}),
            }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result, default=str),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e), "type": type(e).__name__}),
        }


def run_assessment(event: dict[str, Any]) -> dict[str, Any]:
    """Run a full knowledge assessment cycle.

    1. Connect to S3 knowledge bucket
    2. Ingest artifacts via S3KnowledgeSDK
    3. Run collectors (topic_purity, heading_quality, context_independence, link_integrity)
    4. Apply interpretation policy and compute dimension scores
    5. Store signals and assessment in CockroachDB
    6. Generate embeddings via Bedrock and store in CockroachDB
    7. Discover and rank problems by salience

    Returns:
        Assessment summary dict with score, dimensions, signal counts, top problems.
    """
    from ai_ready.knowledge.s3 import S3KnowledgeSDK
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.storage.models import ProblemRecord
    from ai_ready.storage.persistence import persist_assessment
    from ai_ready.operations import CollectOperation, AssessOperation
    from ai_ready.improvement.salience import (
        discover_problems_heuristic,
        rank_problems_by_salience,
    )

    start_time = time.monotonic()

    # 1. Connect to S3
    bucket = event.get("s3_bucket", os.environ.get("S3_KNOWLEDGE_BUCKET", ""))
    prefix = event.get("s3_prefix", os.environ.get("S3_KNOWLEDGE_PREFIX", ""))

    if not bucket:
        raise ValueError(
            "S3_KNOWLEDGE_BUCKET environment variable or s3_bucket in event required"
        )

    s3_uri = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"

    # Idempotency check: if event has a dedup_key, check if we already processed it
    dedup_key = event.get("dedup_key", "")
    if dedup_key:
        store = CockroachDBStore()
        store.initialize_schema()
        existing = store._execute_fetchone(
            "SELECT assessment_id FROM knowledge_assessments WHERE metadata->>'dedup_key' = %s LIMIT 1",
            (dedup_key,),
        )
        if existing:
            return {
                "action": "assess",
                "assessment_id": existing["assessment_id"],
                "message": "Assessment already exists (idempotent skip)",
                "source": s3_uri,
            }

    sdk = S3KnowledgeSDK()
    sdk.connect(s3_uri)
    bundle = sdk.load_artifacts()

    # 2. Connect to CockroachDB (if not already connected for idempotency check)
    if not dedup_key:
        store = CockroachDBStore()
        store.initialize_schema()
    else:
        store.initialize_schema()

    try:
      # 3. Run collectors — CollectOperation.run takes a list of artifacts
      collect_op = CollectOperation()
      results, all_signals, artifact_bundle = collect_op.run(
          artifacts=bundle.artifacts,
          relationships=bundle.relationships,
          source=s3_uri,
      )

      # 4. Assess — AssessOperation.run takes results, signals, artifacts, bundle
      assess_op = AssessOperation()
      assessment = assess_op.run(
          results=results,
          signals=all_signals,
          artifacts=bundle.artifacts,
          bundle=artifact_bundle,
          source=s3_uri,
      )

      # 5. Persist assessment (P4): artifacts+embeddings, signals+lifecycle,
      #    assessment record — all via canonical persist_assessment()
      persist_result = persist_assessment(
          store=store,
          assessment=assessment,
          signals=all_signals,
          artifacts=bundle.artifacts,
          source=s3_uri,
          dedup_key=dedup_key,
      )
      assessment_record_id = persist_result["assessment_id"]
      signal_count = persist_result["signal_count"]
      signal_id_map = persist_result["signal_id_map"]

      # 8. Discover and rank problems
      # P8: Use configurable salience threshold (default 0.01)
      salience_threshold = float(os.environ.get("SALIENCE_THRESHOLD", "0.01"))
      signal_ids = list(signal_id_map.keys())
      problems, hypotheses = discover_problems_heuristic(signal_ids, assessment)
      ranked, saliences, skipped = rank_problems_by_salience(
          problems, assessment, history_store=None,
          threshold=salience_threshold,
      )

      # Store top problems in queue
      for problem in ranked[:25]:
          # Find matching salience decomposition
          salience_info = next(
              (s for s in saliences if s.problem_id == problem.problem_id), None
          )
          problem_record = ProblemRecord.new(
              category=problem.category,
              artifact_uris=problem.artifact_uris,
              salience=salience_info.total if salience_info else 0.0,
              encoding_score=salience_info.encoding if salience_info else 0.0,
              outcome_score=salience_info.outcome if salience_info else 0.0,
              retrieval_score=salience_info.retrieval if salience_info else 0.0,
              staleness_factor=salience_info.staleness_factor if salience_info else 1.0,
              description=problem.description,
          )
          store.save_problem(problem_record)

      elapsed = time.monotonic() - start_time

      return {
          "action": "assess",
          "assessment_id": assessment_record_id,
          "score": assessment.score,
          "dimensions": {name: d.score for name, d in assessment.dimensions.items()},
          "artifact_count": len(bundle.artifacts),
          "signal_count": signal_count,
          "problems_discovered": len(ranked),
          "problems_skipped": len(skipped),
          "top_problems": [
              {
                  "category": p.category,
                  "description": p.description,
                  "artifact_uris": p.artifact_uris[:3],
                  "salience": next(
                      (s.total for s in saliences if s.problem_id == p.problem_id), 0.0
                  ),
              }
              for p in ranked[:5]
          ],
          "elapsed_seconds": round(elapsed, 2),
          "source": s3_uri,
      }
    finally:
      store.close()


def run_remediation(event: dict[str, Any]) -> dict[str, Any]:
    """Run a remediation workflow for the top problem in the queue.

    Uses the Burr state machine to:
    1. Analyze the problem (LLM root cause analysis via Bedrock)
    2. Generate a proposal (with historical context from CockroachDB)
    3. Execute the change (dry-run or real)
    4. Verify improvement (re-assess and compare)
    5. Store outcome in CockroachDB for institutional memory

    Args:
        event: Lambda event. Optional 'problem_id' to remediate a specific problem.

    Returns:
        Remediation result with outcome, score delta, and decision trace.
    """
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.storage.models import RemediationRecord
    from ai_ready.llm.gateway import LLMGateway
    from ai_ready.improvement.manager import ImprovementManager
    from ai_ready.improvement.history import ImprovementHistoryStore
    from ai_ready.improvement.diagnosis_quality import DiagnosisQualityTracker

    start_time = time.monotonic()

    # 1. Connect to CockroachDB
    store = CockroachDBStore()
    store.initialize_schema()

    try:
      # 2. Set up LLM — standardized on Groq (Fix 2c)
      provider_name = os.environ.get("LLM_PROVIDER", "groq")
      gateway = LLMGateway(provider=provider_name)

      # 3. Set up history store
      history_db_path = os.environ.get("HISTORY_DB_PATH", "/tmp/improvement_history.db")
      history_store = ImprovementHistoryStore(history_db_path)
      diagnosis_tracker = DiagnosisQualityTracker()

      # 4. Get the problem to remediate from the CockroachDB queue
      problem_id = event.get("problem_id")
      if problem_id:
          problems = store.get_problems(status="open", limit=50)
          target_problem = next(
              (p for p in problems if p.problem_id == problem_id), None
          )
          if target_problem is None:
              return {"error": f"Problem {problem_id} not found or not open"}
      else:
          problems = store.get_problems(status="open", limit=1)
          if not problems:
              return {"message": "No open problems in the queue"}
          target_problem = problems[0]

      # 5. Build a minimal assessment object for the manager
      # ImprovementManager.start_improvement takes a KnowledgeAssessment
      latest_assessment = store.get_latest_assessment()
      if latest_assessment is None:
          return {"error": "No assessment found — run assessment first"}

      # Reconstruct a lightweight assessment-like object
      from ai_ready.models import KnowledgeAssessment, DimensionScore
      import uuid as uuid_mod

      dimensions_json = json.loads(latest_assessment.dimensions) if isinstance(latest_assessment.dimensions, str) else latest_assessment.dimensions
      assessment = KnowledgeAssessment(
          assessment_id=latest_assessment.assessment_id,
          score=latest_assessment.score,
          dimensions={
              k: DimensionScore(name=k, score=float(v["score"]) if isinstance(v, dict) else float(v))
              for k, v in dimensions_json.items()
          },
          signals=[],
          artifact_count=0,
      )

      # 6. Retrieve historical context from CockroachDB (closed-loop memory)
      # The agent reads past outcomes to inform its current decision — this is
      # what makes it an agentic memory, not just a data store.
      # Fix 2a: Define artifact_uris from the problem record before use
      artifact_uris = target_problem.artifact_uris if hasattr(target_problem, 'artifact_uris') else []
      remediation_context = store.get_remediation_context(target_problem.category)
      similar_problems = store.get_similar_problems(
          target_problem.category, artifact_uris
      )

      # 7. Set up the manager and start the improvement workflow
      manager = ImprovementManager(
          llm_gateway=gateway,
          history_store=history_store,
          diagnosis_quality_tracker=diagnosis_tracker,
          cockroach_store=store,  # P7: enables agent-state persistence
      )

      app_id = manager.start_improvement(
          assessment=assessment,
          artifact_uris=artifact_uris,
      )

      # 8. Agent state is now persisted by ImprovementManager (P7)
      # when cockroach_store is passed to the constructor above.

      # 9. Auto-approve and complete the workflow
      # In production this would be a human-in-the-loop checkpoint:
      #   store.update_agent_status(app_id, "paused")
      #   return {"app_id": app_id, "status": "awaiting_approval"}
      final_state = manager.approve_and_complete(
          app_id=app_id,
          approved=True,
          reason="Auto-approved by Lambda remediation handler",
      )

      # 10. Agent state at completion is also persisted by ImprovementManager (P7)

      # 11. Extract outcome from final state
      verification_outcome = final_state.get("verification_outcome", "unchanged")
      score_before = final_state.get("score_before", 0.0)
      score_after = final_state.get("score_after", 0.0)
      strategy = final_state.get("strategy", "unknown")
      proposal_reasoning = final_state.get("proposal_reasoning", "")
      tokens_used = final_state.get("total_tokens", 0)
      forked = final_state.get("forked", False)

      # 12. Store outcome in CockroachDB (institutional memory)
      record = RemediationRecord.new(
          issue_type=target_problem.category,
          strategy=strategy,
          strategy_description=final_state.get("strategy_description", ""),
          verification_outcome=verification_outcome,
          score_before=score_before,
          score_after=score_after,
          proposal_reasoning=proposal_reasoning,
          tokens_used=tokens_used,
          latency_ms=(time.monotonic() - start_time) * 1000,
          forked=forked,
      )
      store.save_remediation_outcome(record)

      # 13. Update problem status based on verification outcome
      if verification_outcome == "resolved":
          store.update_problem_status(target_problem.problem_id, "resolved")
      elif verification_outcome == "partially_resolved":
          store.update_problem_status(target_problem.problem_id, "in_progress")

      # 14. Store decision traces from the workflow
      proposals = final_state.get("proposals", [])
      if proposals:
          store.save_decision_trace(
              outcome_id=record.outcome_id,
              stage="proposal_generation",
              reasoning=json.dumps(proposals, default=str),
              llm_metadata={"proposal_count": len(proposals)},
          )

      diagnosis = final_state.get("diagnosis", {})
      if diagnosis:
          store.save_decision_trace(
              outcome_id=record.outcome_id,
              stage="root_cause_analysis",
              reasoning=json.dumps(diagnosis, default=str),
              llm_metadata={},
          )

      elapsed = time.monotonic() - start_time

      return {
          "action": "remediate",
          "problem_id": target_problem.problem_id,
          "problem_category": target_problem.category,
          "outcome_id": record.outcome_id,
          "app_id": app_id,
          "verification_outcome": verification_outcome,
          "score_before": score_before,
          "score_after": score_after,
          "score_delta": score_after - score_before,
          "strategy": strategy,
          "tokens_used": tokens_used,
          "forked": forked,
          "context_used": {
              "past_attempts": remediation_context.get("total_attempts", 0),
              "successful_strategies": len(remediation_context.get("successful_strategies", [])),
              "failed_strategies": len(remediation_context.get("failed_strategies", [])),
              "similar_problems": len(similar_problems),
          },
          "elapsed_seconds": round(elapsed, 2),
      }
    finally:
      store.close()


def run_status(event: dict[str, Any]) -> dict[str, Any]:
    """Return current system status: queue, metrics, health trend, active workflows."""
    from ai_ready.storage.cockroach import CockroachDBStore

    store = CockroachDBStore()
    store.initialize_schema()

    try:
      queue_summary = store.get_queue_summary()
      metrics = store.get_remediation_metrics()
      latest_assessment = store.get_latest_assessment()
      skip_events = store.get_skip_events(limit=10)
      health_trend = store.get_knowledge_health_trend(days=30)
      active_workflows = store.get_active_workflows()

      return {
          "action": "status",
          "latest_assessment": latest_assessment.to_dict() if latest_assessment else None,
          "problem_queue": queue_summary,
          "remediation_metrics": metrics,
          "health_trend": health_trend,
          "active_workflows": active_workflows,
          "recent_skip_events": skip_events,
      }
    finally:
      store.close()


def _artifact_to_text(artifact: Any) -> str:
    """Convert a KnowledgeArtifact's content to a flat text string for embedding."""
    parts: list[str] = []
    if hasattr(artifact, 'title') and artifact.title:
        parts.append(f"# {artifact.title}")
    if hasattr(artifact, 'content') and artifact.content:
        content = artifact.content
        if hasattr(content, 'headings'):
            for h in content.headings:
                parts.append(f"{'#' * h.level} {h.text}")
        if hasattr(content, 'paragraphs'):
            for p in content.paragraphs:
                parts.append(p.text)
    return "\n\n".join(parts)
