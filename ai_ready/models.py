"""Canonical document model - normalized representation of any source document."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


def make_finding_id(rule_id: str, document_path: str, key_evidence: str) -> str:
    """Generate a stable finding ID that survives line shifts and minor edits.

    The ID is derived from (rule_id, document_path, key_evidence) where
    key_evidence is a rule-specific string that identifies the *what* of
    the finding, not the *where*. Line numbers are deliberately excluded.
    """
    raw = f"{rule_id}:{document_path}:{key_evidence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Heading:
    level: int
    text: str
    line: int = 0

    def __post_init__(self) -> None:
        if self.level < 1:
            raise ValueError(f"Heading level must be >= 1, got {self.level}")


@dataclass
class Section:
    heading: Heading | None
    text: str
    line_start: int = 0
    line_end: int = 0


@dataclass
class Paragraph:
    text: str
    section_heading: str = ""
    line: int = 0


@dataclass
class Link:
    text: str
    target: str
    line: int = 0
    is_internal: bool = True


@dataclass
class CodeBlock:
    language: str
    text: str
    line: int = 0


@dataclass
class Document:
    id: str
    path: str
    title: str
    headings: list[Heading] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        """Approximate word count across all sections."""
        total = 0
        for section in self.sections:
            total += len(section.text.split())
        return total

    @property
    def all_text(self) -> str:
        """Concatenated text of all sections."""
        return "\n\n".join(s.text for s in self.sections)


@dataclass
class DocumentRelation:
    """A relationship between two documents in the knowledge base."""
    source_path: str
    target_path: str
    relation_type: str  # "parent_child", "cross_reference", "navigation"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeBase:
    """Rich internal representation: documents + their relationships."""
    documents: list[Document] = field(default_factory=list)
    relations: list[DocumentRelation] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def document_count(self) -> int:
        return len(self.documents)

    def get_document(self, path: str) -> Document | None:
        for doc in self.documents:
            if doc.path == path:
                return doc
        return None

    def get_children(self, path: str) -> list[str]:
        """Get child document paths for a given parent path."""
        return [
            r.target_path for r in self.relations
            if r.source_path == path and r.relation_type == "parent_child"
        ]

    def get_parents(self, path: str) -> list[str]:
        """Get parent document paths for a given child path."""
        return [
            r.source_path for r in self.relations
            if r.target_path == path and r.relation_type == "parent_child"
        ]

    def get_cross_references(self, path: str) -> list[str]:
        """Get documents that this document cross-references."""
        return [
            r.target_path for r in self.relations
            if r.source_path == path and r.relation_type == "cross_reference"
        ]

    def is_linked(self, path: str) -> bool:
        """Check if a document is linked from any other document
        (either via parent_child or cross_reference)."""
        for r in self.relations:
            if r.target_path == path and r.source_path != path:
                return True
        return False


class FindingStatus(str, Enum):
    NEW = "new"
    PERSISTENT = "persistent"
    RESOLVED = "resolved"
    RECURRING = "recurring"


@dataclass
class Finding:
    """A single issue found by a rule.

    Rules emit bare findings with issue_type and evidence only. The pipeline
    enriches them with severity, score, ai_impact, and recommendation via the
    EvaluationPolicy registry.
    """

    rule_id: str
    severity: Severity
    score: int
    document_id: str
    document_path: str
    evidence: dict[str, Any]
    recommendation: str
    finding_id: str = ""
    issue_type: str = ""
    ai_impact: str = ""
    line: int = 0

    def __post_init__(self) -> None:
        if not self.finding_id:
            self.finding_id = self._generate_id()

    def _generate_id(self) -> str:
        """Generate stable ID from rule + document + key evidence."""
        key_evidence = self.issue_type if self.issue_type else self._key_evidence()
        return make_finding_id(self.rule_id, self.document_path, key_evidence)

    def _key_evidence(self) -> str:
        """Fallback for old code that doesn't set issue_type.

        When issue_type is set, _generate_id uses it directly and this method
        is never called. It remains for backward compatibility with findings
        created before the policy registry was introduced.
        """
        if self.rule_id == "topic_purity":
            clusters = self.evidence.get("clusters", [])
            return "|".join(sorted(str(c) for c in clusters))
        elif self.rule_id == "heading_quality":
            return f"{self.evidence.get('heading', '')}:{self.evidence.get('issue_type', '')}"
        elif self.rule_id == "context_independence":
            issues = self.evidence.get("issues", [])
            sections = sorted(set(i.get("section", "") for i in issues))
            return "|".join(sections)
        elif self.rule_id == "link_integrity":
            if self.evidence.get("orphan"):
                return "orphan"
            broken = self.evidence.get("broken_links", [])
            targets = sorted(l.get("target", "") for l in broken)
            return "|".join(targets)
        return str(sorted(self.evidence.keys()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule": self.rule_id,
            "issue_type": self.issue_type,
            "severity": self.severity.value,
            "score": self.score,
            "document_id": self.document_id,
            "document_path": self.document_path,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "ai_impact": self.ai_impact,
            "line": self.line,
        }


@dataclass
class RuleResult:
    """Output from a single rule evaluation."""

    rule_id: str
    score: int
    severity: Severity
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "score": self.score,
            "severity": self.severity.value,
            "metrics": self.metrics,
            "findings": [f.to_dict() for f in self.findings],
            "recommendation": self.recommendation,
        }


@dataclass
class DimensionScore:
    """Score for a single AI readiness dimension."""

    name: str
    score: int
    rule_ids: list[str] = field(default_factory=list)
    findings_count: int = 0


@dataclass
class Snapshot:
    """A complete scan snapshot."""

    snapshot_id: str
    score: int
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "score": self.score,
            "dimensions": {
                name: {"name": d.name, "score": d.score, "rule_ids": d.rule_ids, "findings_count": d.findings_count}
                for name, d in self.dimensions.items()
            },
            "findings": [f.to_dict() for f in self.findings],
            "metrics": self.metrics,
            "metadata": self.metadata,
        }


@dataclass
class RegressionReport:
    """Diff between two snapshots."""

    prev_snapshot_id: str = ""
    curr_snapshot_id: str = ""
    prev_score: int = 0
    curr_score: int = 0
    score_delta: int = 0
    new_findings: list[Finding] = field(default_factory=list)
    resolved_findings: list[Finding] = field(default_factory=list)
    persistent_findings: list[Finding] = field(default_factory=list)
    recurring_findings: list[Finding] = field(default_factory=list)
    severity_changes: list[dict[str, Any]] = field(default_factory=list)
    dimension_deltas: dict[str, int] = field(default_factory=dict)
    new_high_count: int = 0
    increased_contradictions: int = 0
    increased_topic_entropy: int = 0
    new_orphan_documents: int = 0
    new_duplicate_clusters: int = 0
    recommendation: str = ""
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "prev_snapshot_id": self.prev_snapshot_id,
            "curr_snapshot_id": self.curr_snapshot_id,
            "prev_score": self.prev_score,
            "curr_score": self.curr_score,
            "score_delta": self.score_delta,
            "new_findings": [f.to_dict() for f in self.new_findings],
            "resolved_findings": [f.to_dict() for f in self.resolved_findings],
            "persistent_findings": [f.to_dict() for f in self.persistent_findings],
            "recurring_findings": [f.to_dict() for f in self.recurring_findings],
            "severity_changes": self.severity_changes,
            "dimension_deltas": self.dimension_deltas,
            "new_high_count": self.new_high_count,
            "increased_contradictions": self.increased_contradictions,
            "increased_topic_entropy": self.increased_topic_entropy,
            "new_orphan_documents": self.new_orphan_documents,
            "new_duplicate_clusters": self.new_duplicate_clusters,
            "recommendation": self.recommendation,
            "explanation": self.explanation,
        }


@dataclass
class FindingLifecycle:
    """Lifecycle tracking for a single finding across scans."""
    finding_id: str
    rule_id: str
    document_path: str
    first_seen: str = ""
    last_seen: str = ""
    status: FindingStatus = FindingStatus.NEW
    severity_history: list[dict[str, Any]] = field(default_factory=list)
    snapshot_ids: list[str] = field(default_factory=list)
    resolved_snapshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "document_path": self.document_path,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "status": self.status.value,
            "severity_history": self.severity_history,
            "snapshot_ids": self.snapshot_ids,
            "resolved_snapshot": self.resolved_snapshot,
            "age_scans": len(self.snapshot_ids),
        }


@dataclass
class TrendReport:
    """Health trend across multiple snapshots."""
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    score_trend: list[dict[str, Any]] = field(default_factory=list)
    finding_trend: list[dict[str, Any]] = field(default_factory=list)
    dimension_trends: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    trajectory: str = ""  # "improving", "worsening", "stable"
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "score_trend": self.score_trend,
            "finding_trend": self.finding_trend,
            "dimension_trends": self.dimension_trends,
            "trajectory": self.trajectory,
            "summary": self.summary,
        }
