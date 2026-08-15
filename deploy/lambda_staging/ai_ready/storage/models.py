"""
Data models for the CockroachDB storage layer.

These models map to CockroachDB tables with vector indexing support.
They serve as the persistent memory layer for the agentic knowledge maintenance system.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SignalStatus(str, Enum):
    """Lifecycle status of a knowledge signal."""
    NEW = "new"
    PERSISTENT = "persistent"
    RESOLVED = "resolved"
    RECURRING = "recurring"


class ProblemStatus(str, Enum):
    """Status of a discovered knowledge problem."""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


class VerificationOutcome(str, Enum):
    """Outcome of a remediation verification."""
    RESOLVED = "resolved"
    PARTIALLY_RESOLVED = "partially_resolved"
    UNCHANGED = "unchanged"
    REGRESSED = "regressed"
    MISDIAGNOSED = "misdiagnosed"


@dataclass
class ArtifactRecord:
    """A knowledge artifact stored in CockroachDB with vector embedding."""
    artifact_id: str
    artifact_uri: str
    artifact_type: str  # document, memory, schema, api_spec, example, embedding
    source: str
    content: str
    metadata: str  # JSON string
    embedding: list[float] | None = None  # VECTOR(384) in CockroachDB
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, artifact_uri: str, artifact_type: str, source: str,
            content: str, metadata: dict[str, Any] | None = None,
            embedding: list[float] | None = None) -> ArtifactRecord:
        return cls(
            artifact_id=str(uuid.uuid4()),
            artifact_uri=artifact_uri,
            artifact_type=artifact_type,
            source=source,
            content=content,
            metadata=json.dumps(metadata or {}),
            embedding=embedding,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_uri": self.artifact_uri,
            "artifact_type": self.artifact_type,
            "source": self.source,
            "content": self.content,
            "metadata": self.metadata,
            "embedding": self.embedding,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class SignalRecord:
    """A knowledge signal stored in CockroachDB."""
    signal_id: str
    artifact_uri: str
    collector_id: str
    signal_type: str
    severity: str  # LOW, MEDIUM, HIGH, CRITICAL
    score: float
    recommendation: str
    ai_impact: str
    evidence: str  # JSON string
    status: str = SignalStatus.NEW.value
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    access_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, artifact_uri: str, collector_id: str, signal_type: str,
            severity: str, score: float, recommendation: str,
            ai_impact: str, evidence: dict[str, Any]) -> SignalRecord:
        return cls(
            signal_id=str(uuid.uuid4()),
            artifact_uri=artifact_uri,
            collector_id=collector_id,
            signal_type=signal_type,
            severity=severity,
            score=score,
            recommendation=recommendation,
            ai_impact=ai_impact,
            evidence=json.dumps(evidence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "artifact_uri": self.artifact_uri,
            "collector_id": self.collector_id,
            "signal_type": self.signal_type,
            "severity": self.severity,
            "score": self.score,
            "recommendation": self.recommendation,
            "ai_impact": self.ai_impact,
            "evidence": self.evidence,
            "status": self.status,
            "last_seen": self.last_seen.isoformat(),
            "access_count": self.access_count,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class AssessmentRecord:
    """A knowledge assessment stored in CockroachDB."""
    assessment_id: str
    score: float
    dimensions: str  # JSON string
    signals_count: int
    metadata: str  # JSON string (git_commit, source, etc.)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, score: float, dimensions: dict[str, Any],
            signals_count: int, metadata: dict[str, Any] | None = None,
            assessment_id: str | None = None) -> AssessmentRecord:
        return cls(
            assessment_id=assessment_id or str(uuid.uuid4()),
            score=score,
            dimensions=json.dumps(dimensions),
            signals_count=signals_count,
            metadata=json.dumps(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "score": self.score,
            "dimensions": self.dimensions,
            "signals_count": self.signals_count,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ProblemRecord:
    """A knowledge problem in the remediation queue."""
    problem_id: str
    category: str
    artifact_uris: str  # JSON string (sorted list)
    salience: float
    encoding_score: float
    outcome_score: float
    retrieval_score: float
    staleness_factor: float
    status: str = ProblemStatus.OPEN.value
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, category: str, artifact_uris: list[str],
            salience: float, encoding_score: float, outcome_score: float,
            retrieval_score: float, staleness_factor: float,
            description: str = "") -> ProblemRecord:
        return cls(
            problem_id=str(uuid.uuid4()),
            category=category,
            artifact_uris=json.dumps(sorted(artifact_uris)),
            salience=salience,
            encoding_score=encoding_score,
            outcome_score=outcome_score,
            retrieval_score=retrieval_score,
            staleness_factor=staleness_factor,
            description=description,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "category": self.category,
            "artifact_uris": self.artifact_uris,
            "salience": self.salience,
            "encoding_score": self.encoding_score,
            "outcome_score": self.outcome_score,
            "retrieval_score": self.retrieval_score,
            "staleness_factor": self.staleness_factor,
            "status": self.status,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class RemediationRecord:
    """A remediation outcome stored in CockroachDB for institutional memory."""
    outcome_id: str
    issue_type: str
    strategy: str
    strategy_description: str
    verification_outcome: str
    score_before: float
    score_after: float
    proposal_reasoning: str
    tokens_used: int
    latency_ms: float
    forked: bool = False
    modification_steps: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, issue_type: str, strategy: str, strategy_description: str,
            verification_outcome: str, score_before: float, score_after: float,
            proposal_reasoning: str = "", tokens_used: int = 0,
            latency_ms: float = 0.0, forked: bool = False,
            modification_steps: list[dict[str, Any]] | None = None) -> RemediationRecord:
        return cls(
            outcome_id=str(uuid.uuid4()),
            issue_type=issue_type,
            strategy=strategy,
            strategy_description=strategy_description,
            verification_outcome=verification_outcome,
            score_before=score_before,
            score_after=score_after,
            proposal_reasoning=proposal_reasoning,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            forked=forked,
            modification_steps=modification_steps if modification_steps is not None else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "issue_type": self.issue_type,
            "strategy": self.strategy,
            "strategy_description": self.strategy_description,
            "verification_outcome": self.verification_outcome,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "proposal_reasoning": self.proposal_reasoning,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "forked": self.forked,
            "modification_steps": self.modification_steps,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class WontFixSignature:
    """A wont-fix signature to suppress re-discovery of resolved problems."""
    signature_id: str
    category: str
    artifact_uris: str  # JSON string (sorted list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def new(cls, category: str, artifact_uris: list[str]) -> WontFixSignature:
        return cls(
            signature_id=str(uuid.uuid4()),
            category=category,
            artifact_uris=json.dumps(sorted(artifact_uris)),
        )
