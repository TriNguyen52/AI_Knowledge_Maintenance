"""
MCP (Model Context Protocol) server interface for AI Knowledge Maintenance.

Exposes the knowledge assessment and remediation system to external AI agents
via the CockroachDB Cloud MCP Server. External agents (Claude Code, Cursor,
VS Code) can query the knowledge health status, retrieve signals, and
trigger assessments through MCP.

This module provides:
1. A lightweight MCP server that wraps CockroachDB queries
2. Tool definitions for agent-callable operations
3. Read-only access by default (safe for external agents)
4. Full audit logging via CockroachDB's built-in audit

The MCP server connects to CockroachDB Cloud's managed MCP endpoint:
    https://cockroachlabs.cloud/mcp

Usage:
    # As a standalone server
    python -m ai_ready.cloud.mcp_server

    # Or register with CockroachDB Cloud MCP
    # Add this config snippet to your MCP client:
    # {
    #   "mcpServers": {
    #     "ai-knowledge-maintenance": {
    #       "url": "https://cockroachlabs.cloud/mcp",
    #       "headers": {
    #         "Authorization": "Bearer <your-crdb-token>"
    #       }
    #     }
    #   }
    # }
"""

from __future__ import annotations

import json
import os
from typing import Any


# Tool definitions for MCP exposure
MCP_TOOLS = [
    {
        "name": "get_knowledge_health",
        "description": "Get the latest knowledge health assessment score, dimensions, and signal summary.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_signals",
        "description": "Retrieve knowledge signals (findings) filtered by severity, collector, or artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    "description": "Filter by severity level",
                },
                "collector_id": {
                    "type": "string",
                    "description": "Filter by collector (topic_purity, heading_quality, context_independence, link_integrity)",
                },
                "artifact_uri": {
                    "type": "string",
                    "description": "Filter by artifact URI",
                },
                "status": {
                    "type": "string",
                    "enum": ["new", "persistent", "resolved", "recurring"],
                    "description": "Filter by lifecycle status",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50)",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "get_problem_queue",
        "description": "Get the current knowledge problem queue ranked by salience. Problems are clusters of related signals that need remediation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "in_progress", "resolved", "wont_fix"],
                    "description": "Filter by status (default: open)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "get_remediation_history",
        "description": "Get past remediation outcomes for institutional memory. Shows which strategies worked and which failed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_type": {
                    "type": "string",
                    "description": "Filter by issue type/category",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "get_remediation_metrics",
        "description": "Get aggregate metrics for all remediation workflows: success rate, avg improvement, token usage, fork count.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "search_artifacts_semantic",
        "description": "Semantic similarity search across knowledge artifacts using CockroachDB distributed vector indexing. Find artifacts related to a query concept.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query to find related artifacts",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_assessment_history",
        "description": "Get historical assessment snapshots to track knowledge health over time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "get_decision_trace",
        "description": "Get the full reasoning chain for a remediation outcome. Shows why the agent made each decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outcome_id": {
                    "type": "string",
                    "description": "Remediation outcome ID",
                },
            },
            "required": ["outcome_id"],
        },
    },
    {
        "name": "get_health_trend",
        "description": "Get knowledge health score trend over time (daily aggregates). Shows whether knowledge quality is improving, degrading, or stable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback period in days (default 30)",
                    "default": 30,
                },
            },
        },
    },
    {
        "name": "get_active_workflows",
        "description": "Get all active or paused agent workflows. Shows in-progress remediation work that can be resumed.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_remediation_context",
        "description": "Get historical context for a problem type: which strategies worked, which failed, and past decision traces. Used by the agent to inform future remediation decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_type": {
                    "type": "string",
                    "description": "Problem category/type to get context for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max recent outcomes (default 5)",
                    "default": 5,
                },
            },
            "required": ["issue_type"],
        },
    },
]


def handle_tool_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle an MCP tool call by routing to the appropriate CockroachDB query.

    This function is called by the MCP server when an external agent
    invokes a tool. All queries are read-only (safe by default).
    """
    from ai_ready.storage.cockroach import CockroachDBStore
    from ai_ready.llm.bedrock import BedrockProvider

    store = CockroachDBStore()

    if tool_name == "get_knowledge_health":
        latest = store.get_latest_assessment()
        if latest is None:
            return {"message": "No assessments found. Run an assessment first."}
        return latest.to_dict()

    elif tool_name == "get_signals":
        severity = arguments.get("severity")
        collector_id = arguments.get("collector_id")
        artifact_uri = arguments.get("artifact_uri")
        status = arguments.get("status")
        limit = arguments.get("limit", 50)

        if artifact_uri:
            signals = store.get_signals_for_artifact(artifact_uri)
        elif status:
            signals = store.get_signals_by_status(status)
        else:
            signals = store.get_all_signals()

        # Apply filters
        if severity:
            signals = [s for s in signals if s.severity == severity]
        if collector_id:
            signals = [s for s in signals if s.collector_id == collector_id]

        return {"signals": [s.to_dict() for s in signals[:limit]], "count": len(signals)}

    elif tool_name == "get_problem_queue":
        status = arguments.get("status", "open")
        limit = arguments.get("limit", 10)
        problems = store.get_problems(status=status, limit=limit)
        return {"problems": [p.to_dict() for p in problems], "count": len(problems)}

    elif tool_name == "get_remediation_history":
        issue_type = arguments.get("issue_type")
        limit = arguments.get("limit", 20)
        history = store.get_remediation_history(issue_type=issue_type, limit=limit)
        return {"outcomes": [r.to_dict() for r in history], "count": len(history)}

    elif tool_name == "get_remediation_metrics":
        return store.get_remediation_metrics()

    elif tool_name == "search_artifacts_semantic":
        query = arguments.get("query", "")
        limit = arguments.get("limit", 10)

        # Generate embedding via Bedrock
        bedrock = BedrockProvider()
        query_embedding = bedrock.embed_text(query)

        results = store.search_artifacts_semantic(query_embedding, limit=limit)
        return {
            "results": [
                {
                    "artifact_uri": artifact.artifact_uri,
                    "artifact_type": artifact.artifact_type,
                    "similarity": round(similarity, 4),
                    "title": json.loads(artifact.metadata).get("title", ""),
                }
                for artifact, similarity in results
            ],
            "query": query,
        }

    elif tool_name == "get_assessment_history":
        limit = arguments.get("limit", 10)
        history = store.get_assessment_history(limit=limit)
        return {"assessments": [a.to_dict() for a in history], "count": len(history)}

    elif tool_name == "get_decision_trace":
        outcome_id = arguments.get("outcome_id", "")
        traces = store.get_decision_traces(outcome_id)
        return {"traces": traces, "count": len(traces)}

    elif tool_name == "get_health_trend":
        days = arguments.get("days", 30)
        trend = store.get_knowledge_health_trend(days=days)
        return {"trend": trend, "days": days}

    elif tool_name == "get_active_workflows":
        workflows = store.get_active_workflows()
        return {"workflows": workflows, "count": len(workflows)}

    elif tool_name == "get_remediation_context":
        issue_type = arguments.get("issue_type", "")
        limit = arguments.get("limit", 5)
        context = store.get_remediation_context(issue_type, limit=limit)
        return context

    else:
        return {"error": f"Unknown tool: {tool_name}"}


def get_mcp_manifest() -> dict[str, Any]:
    """Return the MCP server manifest with tool definitions.

    This manifest is registered with the CockroachDB Cloud MCP Server
    so external agents can discover and call these tools.
    """
    return {
        "server_name": "ai-knowledge-maintenance",
        "description": "Agentic knowledge maintenance system — assesses knowledge base health, discovers problems, and tracks remediation outcomes using CockroachDB as persistent memory.",
        "version": "0.1.0",
        "tools": MCP_TOOLS,
        "default_mode": "read-only",
        "audit_logging": True,
    }


if __name__ == "__main__":
    # Print the MCP manifest for registration
    manifest = get_mcp_manifest()
    print(json.dumps(manifest, indent=2))
    print("\n--- Tool Count ---")
    print(f"{len(MCP_TOOLS)} tools available for external agents")
