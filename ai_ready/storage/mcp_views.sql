-- ============================================================
-- MCP Views — Read-only surfaces for CockroachDB Cloud managed MCP
--
-- These views expose a safe, read-only subset of the AI Knowledge
-- Maintenance schema to external AI agents via the CockroachDB
-- Cloud managed MCP server (https://cockroachlabs.cloud/mcp).
--
-- The managed MCP server exposes named tools (list_databases,
-- get_table_schema, select_query) that run read-only SQL against
-- the cluster.  External agents call select_query on these views.
--
-- A dedicated read-only role (mcp_reader) is granted SELECT only on
-- these views — not on the base tables.  This prevents accidental
-- writes or access to sensitive columns.
-- ============================================================

-- View 1: Knowledge health (latest assessment)
CREATE OR REPLACE VIEW mcp_knowledge_health AS
SELECT
    assessment_id,
    score,
    dimensions,
    signals_count,
    created_at
FROM knowledge_assessments
ORDER BY created_at DESC
LIMIT 1;

-- View 2: Knowledge signals (all, with lifecycle status)
CREATE OR REPLACE VIEW mcp_signals AS
SELECT
    signal_id,
    artifact_uri,
    collector_id,
    signal_type,
    severity,
    score,
    recommendation,
    ai_impact,
    status,
    last_seen,
    access_count,
    created_at
FROM knowledge_signals
ORDER BY created_at DESC;

-- View 3: Problem queue (ranked by salience)
CREATE OR REPLACE VIEW mcp_problem_queue AS
SELECT
    problem_id,
    category,
    artifact_uris,
    salience,
    encoding_score,
    outcome_score,
    retrieval_score,
    staleness_factor,
    status,
    description,
    created_at,
    updated_at
FROM problem_queue
WHERE salience > 0
ORDER BY salience DESC;

-- View 4: Remediation history (institutional memory)
CREATE OR REPLACE VIEW mcp_remediation_history AS
SELECT
    outcome_id,
    issue_type,
    strategy,
    strategy_description,
    verification_outcome,
    score_before,
    score_after,
    proposal_reasoning,
    tokens_used,
    latency_ms,
    forked,
    created_at
FROM remediation_history
ORDER BY created_at DESC;

-- View 5: Active workflows (in-progress agent state)
CREATE OR REPLACE VIEW mcp_active_workflows AS
SELECT
    app_id,
    current_stage,
    status,
    assessment_id,
    remediation_id,
    updated_at
FROM agent_state
WHERE status IN ('active', 'paused')
ORDER BY updated_at DESC;

-- View 6: Health trend (daily aggregates)
CREATE OR REPLACE VIEW mcp_health_trend AS
SELECT
    date_trunc('day', created_at) AS day,
    avg(score) AS avg_score,
    count(*) AS assessment_count,
    avg(signals_count) AS avg_signals
FROM knowledge_assessments
WHERE created_at > now() - INTERVAL '30 days'
GROUP BY date_trunc('day', created_at)
ORDER BY day;

-- View 7: Decision traces (reasoning observability)
CREATE OR REPLACE VIEW mcp_decision_traces AS
SELECT
    dt.trace_id,
    dt.outcome_id,
    dt.stage,
    dt.reasoning,
    dt.llm_metadata,
    dt.created_at,
    rh.issue_type,
    rh.strategy
FROM decision_traces dt
LEFT JOIN remediation_history rh ON dt.outcome_id = rh.outcome_id
ORDER BY dt.created_at DESC;

-- View 8: Remediation metrics (aggregate)
CREATE OR REPLACE VIEW mcp_remediation_metrics AS
SELECT
    count(*) AS total_workflows,
    count(*) FILTER (WHERE verification_outcome = 'resolved') AS problems_resolved,
    count(*) FILTER (WHERE verification_outcome = 'partially_resolved') AS problems_partially_resolved,
    avg(score_after - score_before) AS avg_score_improvement,
    avg(tokens_used) AS avg_tokens_per_workflow
FROM remediation_history;

-- View 9: Assessment history (longitudinal)
CREATE OR REPLACE VIEW mcp_assessment_history AS
SELECT
    assessment_id,
    score,
    dimensions,
    signals_count,
    source,
    git_commit,
    created_at
FROM knowledge_assessments
ORDER BY created_at DESC;

-- View 10: Remediation context (per-issue-type)
CREATE OR REPLACE VIEW mcp_remediation_context AS
SELECT
    outcome_id,
    issue_type,
    strategy,
    strategy_description,
    verification_outcome,
    score_before,
    score_after,
    proposal_reasoning,
    modification_steps,
    tokens_used,
    created_at
FROM remediation_history
ORDER BY created_at DESC;

-- ============================================================
-- Read-only role for MCP access
-- ============================================================
-- Create the role and grant SELECT only on the mcp_* views.
-- Run this after the views are created.
--
-- CREATE ROLE IF NOT EXISTS mcp_reader;
-- GRANT SELECT ON mcp_knowledge_health TO mcp_reader;
-- GRANT SELECT ON mcp_signals TO mcp_reader;
-- GRANT SELECT ON mcp_problem_queue TO mcp_reader;
-- GRANT SELECT ON mcp_remediation_history TO mcp_reader;
-- GRANT SELECT ON mcp_active_workflows TO mcp_reader;
-- GRANT SELECT ON mcp_health_trend TO mcp_reader;
-- GRANT SELECT ON mcp_decision_traces TO mcp_reader;
-- GRANT SELECT ON mcp_remediation_metrics TO mcp_reader;
-- GRANT SELECT ON mcp_assessment_history TO mcp_reader;
-- GRANT SELECT ON mcp_remediation_context TO mcp_reader;
--
-- Revoke access to base tables (defense in depth):
-- REVOKE ALL ON knowledge_artifacts FROM mcp_reader;
-- REVOKE ALL ON knowledge_signals FROM mcp_reader;
-- REVOKE ALL ON knowledge_assessments FROM mcp_reader;
-- REVOKE ALL ON problem_queue FROM mcp_reader;
-- REVOKE ALL ON remediation_history FROM mcp_reader;
-- REVOKE ALL ON agent_state FROM mcp_reader;
-- REVOKE ALL ON decision_traces FROM mcp_reader;
-- REVOKE ALL ON signal_lifecycle FROM mcp_reader;
-- REVOKE ALL ON skip_events FROM mcp_reader;
-- REVOKE ALL ON wont_fix_signatures FROM mcp_reader;
-- REVOKE ALL ON assessment_signals FROM mcp_reader;
