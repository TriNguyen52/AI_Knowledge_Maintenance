"""Cloud-native Lambda demo: runs N closed-loop cycles entirely on AWS Lambda.

This script is **call-only** — all logic lives in :mod:`ai_ready.cloud.demo`.
It runs the full closed-loop improvement workflow (assess → remediate → verify)
as Lambda invocations, with institutional memory persisted in CockroachDB.

Each Lambda invocation is a single serverless function that:
  - Downloads artifacts from S3
  - Runs the closed-loop cycle (assess → improve → verify → rollback)
  - Uploads modified artifacts back to S3
  - Stores outcomes, signals, and decision traces in CockroachDB

Usage::

    python demo_lambda_chain.py              # run without clearing DB
    python demo_lambda_chain.py --clear       # clear DB first (fresh start)
    python clear_cockroachdb.py               # clear DB separately, then run demo

Config (override via environment variables)::

    S3_PREFIX       S3 prefix for artifact set (default: docs100)
    NUM_CYCLES      Number of remediation cycles (default: 3)
    AWS_PYTHON      Python with AWS CLI installed
    COCKROACH_DB_URL  CockroachDB connection string (from .env)
"""

import os
import sys

from ai_ready.cloud.demo import run_lambda_demo

if __name__ == "__main__":
    clear_db = "--clear" in sys.argv

    run_lambda_demo(
        endpoint="http://localhost:4566",
        function_name="ai-knowledge-assess",
        s3_bucket="knowledge-base",
        s3_prefix=os.environ.get("S3_PREFIX", "docs100"),
        num_cycles=int(os.environ.get("NUM_CYCLES", "3")),
        aws_python=os.environ.get(
            "AWS_PYTHON",
            r"C:\Users\jacks\Documents\ai-ready\.venv\Scripts\python.exe",
        ),
        clear_db=clear_db,
    )
