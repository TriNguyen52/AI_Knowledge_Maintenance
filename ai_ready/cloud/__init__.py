"""
Cloud deployment layer for AI Knowledge Maintenance.

Supports AWS Lambda for serverless agent execution and
API Gateway for HTTP-triggered assessment workflows.
"""

from .lambda_handler import lambda_handler, run_assessment, run_remediation

__all__ = ["lambda_handler", "run_assessment", "run_remediation"]
