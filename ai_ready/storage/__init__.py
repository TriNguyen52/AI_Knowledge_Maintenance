"""
Storage layer for AI Knowledge Maintenance.

Supports both SQLite (local development) and CockroachDB (production/hackathon).
"""

from .cockroach import CockroachDBStore
from .models import ArtifactRecord, SignalRecord, AssessmentRecord, ProblemRecord, RemediationRecord

__all__ = [
    "CockroachDBStore",
    "ArtifactRecord",
    "SignalRecord",
    "AssessmentRecord",
    "ProblemRecord",
    "RemediationRecord",
]