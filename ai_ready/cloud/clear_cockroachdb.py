"""Standalone script to clear all CockroachDB tables for a fresh demo.

Usage:
    python clear_cockroachdb.py

This drops all views and tables, then re-initializes the schema.
Run this BEFORE demo_lambda_chain.py when you want a clean start.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(Path.cwd() / ".env"), override=True)

from ai_ready.storage.cockroach import CockroachDBStore


def clear_cockroachdb() -> None:
    db_url = os.environ.get("COCKROACH_DB_URL", "")
    if not db_url:
        print("ERROR: COCKROACH_DB_URL not set in .env")
        sys.exit(1)

    print(f"Connecting to CockroachDB ({db_url[:60]}...)...")
    store = CockroachDBStore(connection_string=db_url)
    store.connect()
    print("Connected. Initializing schema...")
    store.initialize_schema()

    print("Dropping views...")
    for view in [
        "mcp_active_workflows", "mcp_decision_traces",
        "mcp_health_trend", "mcp_knowledge_health",
        "mcp_problem_queue", "mcp_remediation_history",
        "mcp_remediation_metrics", "mcp_signals",
    ]:
        try:
            store._execute(f"DROP VIEW IF EXISTS {view} CASCADE")
        except Exception:
            pass

    print("Dropping tables...")
    for table in [
        "decision_traces", "remediation_history", "skip_events",
        "wont_fix_signatures", "problem_queue",
        "assessment_signals", "signal_lifecycle",
        "knowledge_signals", "knowledge_assessments",
        "knowledge_artifacts", "agent_state",
        "modified_artifacts", "embeddings",
    ]:
        try:
            store._execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:
            pass

    print("Re-initializing schema...")
    store._initialized = False
    store.initialize_schema()
    print("Done. CockroachDB is now clean.")
    store.close()


if __name__ == "__main__":
    clear_cockroachdb()
