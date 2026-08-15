"""
MCP query catalog for AI Knowledge Maintenance.

This module is NOT an MCP server. It maps each external-agent capability
to a ``select_query`` on a dedicated ``mcp_*`` view (see
``ai_ready/storage/mcp_views.sql``). The CockroachDB Cloud managed MCP
server (https://cockroachlabs.cloud/mcp) exposes ``select_query`` as a
named tool; external agents call it with the view name and optional
WHERE/LIMIT clauses.

All access is read-only. A dedicated ``mcp_reader`` role is granted
SELECT only on the ``mcp_*`` views — not on base tables.

Usage:
    # Print the tool catalog (for documentation or display)
    python -m ai_ready.cloud.mcp_server

    # In code — look up the view for a capability
    from ai_ready.cloud.mcp_server import MCP_QUERY_CATALOG
    entry = MCP_QUERY_CATALOG["get_knowledge_health"]
    # entry["view"] == "mcp_knowledge_health"
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Query catalog: maps capability name → MCP view + description + example
# ---------------------------------------------------------------------------
# External agents call the managed MCP's select_query tool with:
#   select_query(sql="SELECT * FROM mcp_problem_queue LIMIT 10")
# The mcp_reader role can only SELECT from mcp_* views.

MCP_QUERY_CATALOG: list[dict[str, Any]] = [
    {
        "name": "get_knowledge_health",
        "description": (
            "Get the latest knowledge health assessment score, "
            "dimensions, and signal summary."
        ),
        "view": "mcp_knowledge_health",
        "example_query": "SELECT * FROM mcp_knowledge_health",
    },
    {
        "name": "get_signals",
        "description": (
            "Retrieve knowledge signals (findings) filtered by "
            "severity, collector, artifact, or lifecycle status."
        ),
        "view": "mcp_signals",
        "example_query": (
            "SELECT * FROM mcp_signals WHERE severity = 'HIGH' "
            "ORDER BY created_at DESC LIMIT 50"
        ),
    },
    {
        "name": "get_problem_queue",
        "description": (
            "Get the current knowledge problem queue ranked by salience. "
            "Problems are clusters of related signals that need remediation."
        ),
        "view": "mcp_problem_queue",
        "example_query": (
            "SELECT * FROM mcp_problem_queue WHERE status = 'open' LIMIT 10"
        ),
    },
    {
        "name": "get_remediation_history",
        "description": (
            "Get past remediation outcomes for institutional memory. "
            "Shows which strategies worked and which failed."
        ),
        "view": "mcp_remediation_history",
        "example_query": (
            "SELECT * FROM mcp_remediation_history "
            "WHERE issue_type = 'broken_link' LIMIT 20"
        ),
    },
    {
        "name": "get_remediation_metrics",
        "description": (
            "Get aggregate metrics: success rate, avg improvement, "
            "token usage, fork count."
        ),
        "view": "mcp_remediation_metrics",
        "example_query": "SELECT * FROM mcp_remediation_metrics",
    },
    {
        "name": "get_assessment_history",
        "description": "Get historical assessment snapshots over time.",
        "view": "mcp_assessment_history",
        "example_query": (
            "SELECT * FROM mcp_assessment_history ORDER BY created_at DESC "
            "LIMIT 10"
        ),
    },
    {
        "name": "get_decision_trace",
        "description": (
            "Get the full reasoning chain for a remediation outcome. "
            "Shows why the agent made each decision."
        ),
        "view": "mcp_decision_traces",
        "example_query": (
            "SELECT * FROM mcp_decision_traces "
            "WHERE outcome_id = '<outcome_id>' ORDER BY created_at"
        ),
    },
    {
        "name": "get_health_trend",
        "description": (
            "Get daily score aggregates for monitoring knowledge health "
            "over time."
        ),
        "view": "mcp_health_trend",
        "example_query": "SELECT * FROM mcp_health_trend",
    },
    {
        "name": "get_active_workflows",
        "description": (
            "Get all active or paused agent workflows. Shows in-progress "
            "remediation work that can be resumed."
        ),
        "view": "mcp_active_workflows",
        "example_query": "SELECT * FROM mcp_active_workflows",
    },
    {
        "name": "get_remediation_context",
        "description": (
            "Get historical context for a problem type: which strategies "
            "worked, which failed, and past decision traces."
        ),
        "view": "mcp_remediation_context",
        "example_query": (
            "SELECT * FROM mcp_remediation_context "
            "WHERE issue_type = '<issue_type>' ORDER BY created_at DESC LIMIT 5"
        ),
    },
    {
        "name": "search_artifacts_semantic",
        "description": (
            "Vector similarity search via HuggingFace embeddings + "
            "CockroachDB VECTOR(384) cosine distance."
        ),
        "view": None,
        "note": (
            "Not a SQL view â€” uses CockroachDB <=> vector distance operator "
            "on knowledge_artifacts.embedding column"
        ),
        "example_query": (
            "SELECT artifact_uri, 1 - (embedding <=> '<query_vector>') "
            "AS similarity FROM knowledge_artifacts "
            "ORDER BY embedding <=> '<query_vector>' LIMIT 5"
        ),
    },
]

# Backward-compatible alias (old code may reference MCP_TOOLS)
MCP_TOOLS = MCP_QUERY_CATALOG

# Module-level store reference for handle_tool_call
_store: Any = None


def set_store(store: Any) -> None:
    """Set the CockroachDBStore instance for handle_tool_call queries."""
    global _store
    _store = store


def handle_tool_call(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Handle an MCP tool call by querying the CockroachDB store directly.

    This provides a programmatic interface mirroring what external agents
    would do via the CockroachDB Cloud managed MCP server's select_query tool.
    """
    if _store is None:
        return {"message": "No store connected. Call set_store() first."}

    limit = params.get("limit", 10)

    if tool_name == "get_knowledge_health":
        try:
            row = _store._execute_fetchone(
                "SELECT score, signals_count FROM knowledge_assessments ORDER BY created_at DESC LIMIT 1"
            )
            if row:
                return {"score": row.get("score", 0), "signals_count": row.get("signals_count", 0)}
            return {"message": "No assessments found."}
        except Exception as e:
            return {"message": f"Error: {e}"}

    elif tool_name == "get_signals":
        try:
            rows = _store._execute_fetchall(
                "SELECT signal_id, collector_id, signal_type, severity, artifact_uri, recommendation "
                "FROM knowledge_signals ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            # Increment access_count for retrieved signals (Item 4 — LTP)
            if rows:
                signal_ids = [r["signal_id"] for r in rows if r.get("signal_id")]
                if signal_ids:
                    _store.increment_signal_access_count(signal_ids)
            return {"count": len(rows), "signals": rows}
        except Exception as e:
            return {"count": 0, "signals": [], "error": str(e)}

    elif tool_name == "search_artifacts_semantic":
        try:
            from ai_ready.llm.embeddings import get_embedder
            embedder = get_embedder()
            q_emb = embedder.embed(params.get("query", ""))
            results = _store.search_artifacts_semantic(q_emb, limit=limit)
            return {"results": [{"artifact_uri": r[0].artifact_uri, "similarity": r[1]} for r in results]}
        except Exception as e:
            return {"results": [], "error": str(e)}

    elif tool_name == "get_active_workflows":
        try:
            workflows = _store.get_all_workflows(limit=limit)
            return {"count": len(workflows), "workflows": workflows}
        except Exception as e:
            return {"count": 0, "workflows": [], "error": str(e)}

    elif tool_name == "get_remediation_history":
        try:
            history = _store.get_remediation_history(limit=limit)
            return {"count": len(history), "outcomes": [h.__dict__ if hasattr(h, '__dict__') else h for h in history]}
        except Exception as e:
            return {"count": 0, "outcomes": [], "error": str(e)}

    elif tool_name == "get_decision_trace":
        try:
            outcome_id = params.get("outcome_id", "")
            traces = _store.get_decision_traces(outcome_id)
            return {"count": len(traces), "traces": traces}
        except Exception as e:
            return {"count": 0, "traces": [], "error": str(e)}

    elif tool_name == "get_remediation_context":
        try:
            issue_type = params.get("issue_type", "")
            context = _store.get_remediation_context(issue_type=issue_type, limit=limit)
            return context
        except Exception as e:
            return {"error": str(e)}

    elif tool_name == "get_remediation_metrics":
        try:
            history = _store.get_remediation_history(limit=100)
            total = len(history)
            resolved = sum(1 for h in history if getattr(h, 'verification_outcome', '') == 'resolved')
            partially = sum(1 for h in history if getattr(h, 'verification_outcome', '') == 'partially_resolved')
            improvements = [getattr(h, 'score_after', 0) - getattr(h, 'score_before', 0) for h in history]
            avg_improvement = sum(improvements) / len(improvements) if improvements else 0
            return {
                "total_workflows": total,
                "resolved": resolved,
                "partially_resolved": partially,
                "verification_success_rate": (resolved + partially) / total if total else 0,
                "avg_score_improvement": avg_improvement,
            }
        except Exception as e:
            return {"error": str(e)}

    elif tool_name == "get_problem_queue":
        try:
            return _store.get_problems(limit=limit)
        except Exception as e:
            return {"error": str(e)}

    elif tool_name == "get_assessment_history":
        try:
            rows = _store.get_assessment_history(limit=limit)
            return [
                {
                    "assessment_id": getattr(r, "assessment_id", ""),
                    "score": getattr(r, "score", 0),
                    "signals_count": getattr(r, "signals_count", 0),
                    "source": getattr(r, "source", ""),
                    "created_at": str(getattr(r, "created_at", "")),
                }
                for r in rows
            ]
        except Exception as e:
            return {"error": str(e)}

    elif tool_name == "get_health_trend":
        try:
            days = int(params.get("days", 30))
            return _store.get_knowledge_health_trend(days=days)
        except Exception as e:
            return {"error": str(e)}

    return {"message": f"Unknown tool: {tool_name}"}


def get_mcp_manifest() -> dict[str, Any]:
    """Return the MCP tool catalog for documentation/display.

    This is NOT a server manifest — it documents which select_query
    calls an external agent should make against the mcp_* views via
    the CockroachDB Cloud managed MCP server.
    """
    return {
        "system": "ai-knowledge-maintenance",
        "description": (
            "Agentic knowledge maintenance system — assesses knowledge "
            "base health, discovers problems, and tracks remediation "
            "outcomes using CockroachDB as persistent memory."
        ),
        "access_model": "read-only via mcp_* views",
        "managed_mcp_endpoint": "https://cockroachlabs.cloud/mcp",
        "tools": [
            {
                "name": entry["name"],
                "description": entry["description"],
                "view": entry["view"],
                "example_select_query": entry["example_query"],
            }
            for entry in MCP_QUERY_CATALOG
        ],
    }


if __name__ == "__main__":
    manifest = get_mcp_manifest()
    print(json.dumps(manifest, indent=2))
    print(f"\n--- Tool Count ---")
    print(f"{len(MCP_QUERY_CATALOG)} tools in the query catalog")
