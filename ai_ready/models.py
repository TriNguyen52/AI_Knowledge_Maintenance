"""Canonical document model - normalized representation of any source document."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


_SEVERITY_ORDER = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


def _severity_rank(s: Severity) -> int:
    """Return numeric rank for severity comparison (higher = worse)."""
    return _SEVERITY_ORDER.get(s, 0)


def make_signal_id(collector_id: str, artifact_uri: str, signal_type: str) -> str:
    """Generate a stable signal ID that survives line shifts and minor edits.

    The ID is derived from (collector_id, artifact_uri, signal_type) where
    signal_type identifies the *what* of the signal, not the *where*.
    Line numbers are deliberately excluded.
    """
    raw = f"{collector_id}:{artifact_uri}:{signal_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# Backward-compatible alias so existing code using make_finding_id continues to work.
make_finding_id = make_signal_id


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

    def to_artifact(self) -> "KnowledgeArtifact":
        """Convert this Document to a KnowledgeArtifact.

        The artifact wraps the document's structured content in a
        DocumentContent object and sets artifact_type to "document".
        """
        return KnowledgeArtifact(
            id=self.id,
            uri=self.path,
            title=self.title,
            artifact_type="document",
            content=DocumentContent(
                headings=self.headings,
                sections=self.sections,
                paragraphs=self.paragraphs,
                links=self.links,
                code_blocks=self.code_blocks,
            ),
            metadata=dict(self.metadata),
        )


@dataclass
class DocumentRelation:
    """A relationship between two documents in the knowledge base."""
    source_path: str
    target_path: str
    relation_type: str  # "parent_child", "cross_reference", "navigation"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_relationship(self) -> "Relationship":
        """Convert this DocumentRelation to a Relationship.

        Maps source_path -> source_uri, target_path -> target_uri.
        """
        return Relationship(
            source_uri=self.source_path,
            target_uri=self.target_path,
            relation_type=self.relation_type,
            metadata=dict(self.metadata),
        )


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

    def to_artifact_bundle(self) -> "ArtifactBundle":
        """Convert this KnowledgeBase to an ArtifactBundle.

        Documents are converted to KnowledgeArtifacts via ``to_artifact()``
        and DocumentRelations to Relationships via ``to_relationship()``.
        """
        return ArtifactBundle(
            artifacts=[d.to_artifact() for d in self.documents],
            relationships=[r.to_relationship() for r in self.relations],
            source=self.source,
            metadata=dict(self.metadata),
        )


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

    def to_signal(self) -> KnowledgeSignal:
        """Extract the bare-fact KnowledgeSignal from this assessed finding.

        An AssessedSignal (Finding) carries both bare facts (signal_id,
        collector_id, signal_type, artifact_uri, evidence, line) and
        interpretation (severity, score, recommendation, ai_impact). This
        method extracts just the bare fact portion as a KnowledgeSignal.
        """
        return KnowledgeSignal(
            collector_id=self.rule_id,
            signal_type=self.issue_type,
            artifact_uri=self.document_path,
            evidence=self.evidence,
            signal_id=self.finding_id,
            artifact_id=self.document_id,
            line=self.line,
        )

    @classmethod
    def from_signal(
        cls,
        signal: KnowledgeSignal,
        severity: Severity = Severity.LOW,
        score: int = 100,
        recommendation: str = "",
        ai_impact: str = "",
    ) -> Finding:
        """Create an AssessedSignal (Finding) from a bare KnowledgeSignal.

        The interpretation fields (severity, score, recommendation, ai_impact)
        are set to placeholder values. The assessment layer enriches them
        via InterpretationPolicy.
        """
        return cls(
            rule_id=signal.collector_id,
            severity=severity,
            score=score,
            document_id=signal.artifact_id,
            document_path=signal.artifact_uri,
            evidence=signal.evidence,
            recommendation=recommendation,
            finding_id=signal.signal_id,
            issue_type=signal.signal_type,
            ai_impact=ai_impact,
            line=signal.line,
        )

    def to_assessed_signal(self) -> Finding:
        """Return self as an AssessedSignal (Finding).

        This is a no-op since Finding IS AssessedSignal. Provided for API
        symmetry with to_signal() and for code that works with both
        KnowledgeSignal and AssessedSignal uniformly.
        """
        return self


# AssessedSignal is the new canonical name for Finding.
# A Finding IS an AssessedSignal — a KnowledgeSignal enriched with
# interpretation (severity, score, recommendation, ai_impact).
AssessedSignal = Finding


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
class ScoreContributor:
    """Derived explanation for a repeated score contributor.

    This is intentionally not persisted as its own entity. It is computed from
    findings that were already interpreted by the EvaluationPolicy.
    """

    key: str
    dimension: str
    rule_id: str
    issue_type: str
    cause: str
    finding_count: int
    document_count: int
    severity: Severity
    total_penalty: int
    estimated_rule_gain: int
    estimated_dimension_gain: float
    estimated_score_gain: float
    recommendation: str
    ai_impact: str
    finding_ids: list[str] = field(default_factory=list)
    sample_findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "dimension": self.dimension,
            "rule": self.rule_id,
            "issue_type": self.issue_type,
            "cause": self.cause,
            "finding_count": self.finding_count,
            "document_count": self.document_count,
            "severity": self.severity.value,
            "total_penalty": self.total_penalty,
            "estimated_rule_gain": self.estimated_rule_gain,
            "estimated_dimension_gain": round(self.estimated_dimension_gain, 2),
            "estimated_score_gain": round(self.estimated_score_gain, 2),
            "recommendation": self.recommendation,
            "ai_impact": self.ai_impact,
            "finding_ids": self.finding_ids,
            "sample_findings": self.sample_findings,
        }


@dataclass
class ScoreExplanation:
    """Derived explanation of why a snapshot received its score."""

    score: int
    possible_score: int = 100
    lost_score_points: int = 0
    summary: str = ""
    dominant_dimensions: list[dict[str, Any]] = field(default_factory=list)
    dominant_contributors: list[ScoreContributor] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "possible_score": self.possible_score,
            "lost_score_points": self.lost_score_points,
            "summary": self.summary,
            "dominant_dimensions": self.dominant_dimensions,
            "dominant_contributors": [c.to_dict() for c in self.dominant_contributors],
        }


@dataclass
class Snapshot:
    """A complete scan snapshot."""

    snapshot_id: str
    score: int
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def explain_score(self, max_contributors: int = 5) -> ScoreExplanation:
        """Explain the snapshot score using existing dimensions and findings.

        The calculation estimates how much score could recover if a contributor's
        findings were fixed. It does not re-score policy; it only removes the
        penalties already present on findings.
        """
        contributors = self._score_contributors(max_contributors=max_contributors)
        dominant_dimensions = self._dominant_dimensions()

        if not self.findings:
            summary = "No findings contributed to score degradation."
        elif contributors:
            top = contributors[0]
            summary = (
                f"{top.cause} is the largest contributor: {top.finding_count} "
                f"finding(s) across {top.document_count} document(s), worth up to "
                f"{top.estimated_score_gain:.1f} overall point(s)."
            )
        else:
            summary = "Findings exist, but no score contributor could be estimated."

        return ScoreExplanation(
            score=self.score,
            lost_score_points=max(0, 100 - self.score),
            summary=summary,
            dominant_dimensions=dominant_dimensions,
            dominant_contributors=contributors,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "score": self.score,
            "dimensions": {
                name: {"name": d.name, "score": d.score, "rule_ids": d.rule_ids, "findings_count": d.findings_count}
                for name, d in self.dimensions.items()
            },
            "score_explanation": self.explain_score().to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "metrics": self.metrics,
            "metadata": self.metadata,
        }

    def to_assessment(self) -> "KnowledgeAssessment":
        """Convert this Snapshot to a KnowledgeAssessment.

        Maps snapshot_id -> assessment_id, findings -> signals.
        All other fields map 1:1.
        """
        return KnowledgeAssessment(
            assessment_id=self.snapshot_id,
            score=self.score,
            dimensions=dict(self.dimensions),
            signals=list(self.findings),
            metrics=dict(self.metrics),
            metadata=dict(self.metadata),
        )

    @classmethod
    def from_assessment(cls, assessment: "KnowledgeAssessment") -> "Snapshot":
        """Create a Snapshot from a KnowledgeAssessment.

        Maps assessment_id -> snapshot_id, signals -> findings.
        """
        return cls(
            snapshot_id=assessment.assessment_id,
            score=assessment.score,
            dimensions=dict(assessment.dimensions),
            findings=list(assessment.signals),
            metrics=dict(assessment.metrics),
            metadata=dict(assessment.metadata),
        )

    def _dominant_dimensions(self) -> list[dict[str, Any]]:
        weights = self._effective_weights()
        total_weight = sum(weights.get(name, 0) for name in self.dimensions)
        if total_weight <= 0:
            total_weight = len(self.dimensions) or 1
            weights = {name: 1 for name in self.dimensions}

        dimensions: list[dict[str, Any]] = []
        for name, dim in self.dimensions.items():
            normalized_weight = weights.get(name, 0) / total_weight
            lost_points = (100 - dim.score) * normalized_weight
            dimensions.append({
                "dimension": name,
                "score": dim.score,
                "findings_count": dim.findings_count,
                "weight": round(normalized_weight, 3),
                "estimated_lost_score_points": round(lost_points, 2),
            })
        return sorted(
            dimensions,
            key=lambda d: (d["estimated_lost_score_points"], d["findings_count"]),
            reverse=True,
        )

    def _score_contributors(self, max_contributors: int = 5) -> list[ScoreContributor]:
        rule_to_dimension = self._rule_to_dimension()
        dimension_rule_counts = {
            name: max(1, len(dim.rule_ids)) for name, dim in self.dimensions.items()
        }
        weights = self._effective_weights()
        total_weight = sum(weights.get(name, 0) for name in self.dimensions)
        if total_weight <= 0:
            total_weight = len(self.dimensions) or 1
            weights = {name: 1 for name in self.dimensions}

        findings_by_rule: dict[str, list[Finding]] = {}
        for finding in self.findings:
            findings_by_rule.setdefault(finding.rule_id, []).append(finding)

        rule_total_penalties = {
            rule_id: sum(self._finding_penalty(f) for f in findings)
            for rule_id, findings in findings_by_rule.items()
        }

        groups: dict[str, list[Finding]] = {}
        for finding in self.findings:
            dimension = rule_to_dimension.get(finding.rule_id, "unknown")
            issue_type = self._effective_issue_type(finding)
            cause_key, _ = self._finding_cause(finding, issue_type)
            key = f"{dimension}:{finding.rule_id}:{issue_type}:{cause_key}"
            groups.setdefault(key, []).append(finding)

        contributors: list[ScoreContributor] = []
        for key, group_findings in groups.items():
            first = group_findings[0]
            dimension = rule_to_dimension.get(first.rule_id, "unknown")
            issue_type = self._effective_issue_type(first)
            _, cause = self._finding_cause(first, issue_type)

            group_penalty = sum(self._finding_penalty(f) for f in group_findings)
            rule_total_penalty = rule_total_penalties.get(first.rule_id, 0)
            current_rule_score = max(0, 100 - rule_total_penalty)
            fixed_rule_score = max(0, 100 - max(0, rule_total_penalty - group_penalty))
            estimated_rule_gain = fixed_rule_score - current_rule_score

            rule_count = dimension_rule_counts.get(dimension, 1)
            dimension_gain = estimated_rule_gain / rule_count
            normalized_weight = weights.get(dimension, 0) / total_weight
            score_gain = dimension_gain * normalized_weight

            docs = sorted({f.document_path for f in group_findings})
            severities = [f.severity for f in group_findings]
            highest_severity = max(severities, key=lambda s: _severity_rank(s)) if severities else Severity.INFO

            contributors.append(ScoreContributor(
                key=key,
                dimension=dimension,
                rule_id=first.rule_id,
                issue_type=issue_type,
                cause=cause,
                finding_count=len(group_findings),
                document_count=len(docs),
                severity=highest_severity,
                total_penalty=group_penalty,
                estimated_rule_gain=estimated_rule_gain,
                estimated_dimension_gain=dimension_gain,
                estimated_score_gain=score_gain,
                recommendation=first.recommendation,
                ai_impact=first.ai_impact,
                finding_ids=[f.finding_id for f in group_findings],
                sample_findings=[
                    {
                        "finding_id": f.finding_id,
                        "document_path": f.document_path,
                        "line": f.line,
                    }
                    for f in group_findings[:5]
                ],
            ))

        return sorted(
            contributors,
            key=lambda c: (
                c.estimated_score_gain,
                c.finding_count,
                _severity_rank(c.severity),
            ),
            reverse=True,
        )[:max_contributors]

    def _effective_weights(self) -> dict[str, float]:
        raw = self.metadata.get("weights", {})
        if not isinstance(raw, dict):
            return {}
        weights: dict[str, float] = {}
        for key, value in raw.items():
            try:
                weights[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return weights

    def _rule_to_dimension(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for dim_name, dim in self.dimensions.items():
            for rule_id in dim.rule_ids:
                mapping[rule_id] = dim_name
        return mapping

    def _effective_issue_type(self, finding: Finding) -> str:
        if finding.issue_type:
            return finding.issue_type
        if isinstance(finding.evidence, dict):
            if finding.evidence.get("issue_type"):
                return str(finding.evidence["issue_type"])
            if finding.evidence.get("orphan"):
                return "orphan"
            if finding.evidence.get("broken_links"):
                return "broken_link"
            if finding.evidence.get("clusters"):
                return "mixed_topics"
            if finding.evidence.get("dangling_reference_count") or finding.evidence.get("issues"):
                return "dangling_reference"
        return "unknown"

    def _finding_cause(self, finding: Finding, issue_type: str) -> tuple[str, str]:
        evidence = finding.evidence if isinstance(finding.evidence, dict) else {}
        if finding.rule_id == "heading_quality":
            heading = str(evidence.get("heading", "")).strip()
            if heading:
                return self._safe_key(heading), f"{issue_type.replace('_', ' ')} heading '{heading}'"
        if finding.rule_id == "context_independence":
            refs: list[str] = []
            for issue in evidence.get("issues", []):
                if isinstance(issue, dict):
                    refs.extend(str(ref) for ref in issue.get("references", []) if ref)
            if refs:
                ref = Counter(refs).most_common(1)[0][0]
                return self._safe_key(ref), f"dangling reference '{ref}'"
        if finding.rule_id == "link_integrity":
            if issue_type == "broken_link":
                targets = [
                    str(link.get("target"))
                    for link in evidence.get("broken_links", [])
                    if isinstance(link, dict) and link.get("target")
                ]
                if targets:
                    target = Counter(targets).most_common(1)[0][0]
                    return self._safe_key(target), f"broken link target '{target}'"
            if issue_type == "orphan":
                return "orphan", "orphan documents"
        if finding.rule_id == "topic_purity":
            return "mixed_topics", "mixed-topic documents"
        return issue_type or "unknown", (issue_type or finding.rule_id).replace("_", " ")

    def _finding_penalty(self, finding: Finding) -> int:
        return max(0, 100 - finding.score)

    def _safe_key(self, value: str) -> str:
        return value.lower().strip()[:120]


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
    score_change_explanation: dict[str, Any] = field(default_factory=dict)
    contributor_changes: list[dict[str, Any]] = field(default_factory=list)
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
            "score_change_explanation": self.score_change_explanation,
            "contributor_changes": self.contributor_changes,
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


# ---------------------------------------------------------------------------
# New domain model — Knowledge Adequacy architecture
#
# These types are introduced alongside the existing document-centric types.
# They are separate objects (not aliases). Converters (to_artifact / to_document)
# bridge the old and new worlds. Nothing in the existing codebase is modified
# to use these types yet — they exist for future phases of the migration.
# ---------------------------------------------------------------------------


@dataclass
class DocumentContent:
    """Structured content of a document artifact.

    Wraps the heading, section, paragraph, link, and code-block lists
    that a Document carries directly as fields. This gives KnowledgeArtifact
    a single ``content`` attribute instead of a flat dict.
    """

    headings: list[Heading] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)


@dataclass
class KnowledgeArtifact:
    """Any external context that contributes to AI reasoning.

    A document is one type of artifact (``artifact_type="document"``).
    Future types may include schemas, API specs, examples, embeddings, etc.

    Use ``to_document()`` to obtain a backward-compatible ``Document``.
    Use ``Document.to_artifact()`` to create a ``KnowledgeArtifact`` from a
    ``Document``.
    """

    id: str
    uri: str
    title: str
    artifact_type: str = "document"
    content: DocumentContent | dict[str, Any] = field(default_factory=dict)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> Document:
        """Convert this artifact to a Document.

        Raises ValueError if artifact_type is not "document".
        """
        if self.artifact_type != "document":
            raise ValueError(
                f"Cannot convert artifact_type={self.artifact_type!r} to Document"
            )
        if isinstance(self.content, DocumentContent):
            return Document(
                id=self.id,
                path=self.uri,
                title=self.title,
                headings=list(self.content.headings),
                sections=list(self.content.sections),
                paragraphs=list(self.content.paragraphs),
                links=list(self.content.links),
                code_blocks=list(self.content.code_blocks),
                metadata=dict(self.metadata),
            )
        if isinstance(self.content, dict):
            return Document(
                id=self.id,
                path=self.uri,
                title=self.title,
                headings=self.content.get("headings", []),
                sections=self.content.get("sections", []),
                paragraphs=self.content.get("paragraphs", []),
                links=self.content.get("links", []),
                code_blocks=self.content.get("code_blocks", []),
                metadata=dict(self.metadata),
            )
        return Document(id=self.id, path=self.uri, title=self.title, metadata=dict(self.metadata))


@dataclass
class Relationship:
    """A relationship between two artifacts in the knowledge ecosystem.

    Generalizes DocumentRelation from document-specific paths to
    artifact URIs, so the same model can express relationships between
    any artifact types (documents, schemas, APIs, etc.).
    """

    source_uri: str
    target_uri: str
    relation_type: str  # "parent_child", "cross_reference", "navigation"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactBundle:
    """A collection of artifacts and their relationships.

    Generalizes KnowledgeBase from documents to arbitrary artifacts.
    """

    artifacts: list[KnowledgeArtifact] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def artifact_count(self) -> int:
        return len(self.artifacts)

    def get_artifact(self, uri: str) -> KnowledgeArtifact | None:
        for artifact in self.artifacts:
            if artifact.uri == uri:
                return artifact
        return None

    def get_children(self, uri: str) -> list[str]:
        return [
            r.target_uri for r in self.relationships
            if r.source_uri == uri and r.relation_type == "parent_child"
        ]

    def get_parents(self, uri: str) -> list[str]:
        return [
            r.source_uri for r in self.relationships
            if r.target_uri == uri and r.relation_type == "parent_child"
        ]

    def is_linked(self, uri: str) -> bool:
        for r in self.relationships:
            if r.target_uri == uri and r.source_uri != uri:
                return True
        return False


@dataclass
class KnowledgeSignal:
    """A single observable fact about a knowledge artifact.

    Signals are bare facts produced by SignalCollectors. They contain no
    interpretation — no severity, score, or recommendation. Interpretation
    is applied by the Assessment layer via InterpretationPolicy.

    This is a separate object from Finding. Finding carries interpretation
    fields (severity, score, recommendation, ai_impact); KnowledgeSignal
    does not.
    """

    collector_id: str
    signal_type: str
    artifact_uri: str
    evidence: dict[str, Any]
    signal_id: str = ""
    artifact_id: str = ""
    line: int = 0

    def __post_init__(self) -> None:
        if not self.signal_id:
            self.signal_id = make_signal_id(
                self.collector_id, self.artifact_uri, self.signal_type
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "collector_id": self.collector_id,
            "signal_type": self.signal_type,
            "artifact_uri": self.artifact_uri,
            "artifact_id": self.artifact_id,
            "evidence": self.evidence,
            "line": self.line,
        }


class SignalStatus(str, Enum):
    """Lifecycle status for a signal across assessments.

    Mirrors FindingStatus with identical values so the two can be used
    interchangeably during migration.
    """

    NEW = "new"
    PERSISTENT = "persistent"
    RESOLVED = "resolved"
    RECURRING = "recurring"


@dataclass
class SignalLifecycle:
    """Lifecycle tracking for a single signal across assessments.

    Mirrors FindingLifecycle with signal-centric field names.
    """

    signal_id: str
    collector_id: str
    artifact_uri: str
    first_seen: str = ""
    last_seen: str = ""
    status: SignalStatus = SignalStatus.NEW
    severity_history: list[dict[str, Any]] = field(default_factory=list)
    assessment_ids: list[str] = field(default_factory=list)
    resolved_assessment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "collector_id": self.collector_id,
            "artifact_uri": self.artifact_uri,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "status": self.status.value,
            "severity_history": self.severity_history,
            "assessment_ids": self.assessment_ids,
            "resolved_assessment": self.resolved_assessment,
            "age_assessments": len(self.assessment_ids),
        }

    def to_finding_lifecycle(self) -> "FindingLifecycle":
        """Convert this SignalLifecycle to a FindingLifecycle.

        Maps signal-centric fields to finding-centric fields for backward
        compatibility with SnapshotStore consumers.
        """
        return FindingLifecycle(
            finding_id=self.signal_id,
            rule_id=self.collector_id,
            document_path=self.artifact_uri,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            status=FindingStatus(self.status.value),
            severity_history=self.severity_history,
            snapshot_ids=list(self.assessment_ids),
            resolved_snapshot=self.resolved_assessment,
        )


@dataclass
class CollectorResult:
    """Output from a single SignalCollector evaluation.

    Generalizes RuleResult from findings to signals.
    """

    collector_id: str
    score: int
    severity: Severity
    metrics: dict[str, Any] = field(default_factory=dict)
    signals: list[KnowledgeSignal] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "collector_id": self.collector_id,
            "score": self.score,
            "severity": self.severity.value,
            "metrics": self.metrics,
            "signals": [s.to_dict() for s in self.signals],
            "recommendation": self.recommendation,
        }


@dataclass
class KnowledgeAssessment:
    """Interpretation of signals into dimensions and scores.

    Replaces Snapshot in the new architecture. An assessment is the
    interpreted view of collected signals, with severity, scores, and
    recommendations applied via InterpretationPolicy.
    """

    assessment_id: str
    score: int
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    signals: list[KnowledgeSignal] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "score": self.score,
            "dimensions": {
                name: {
                    "name": d.name,
                    "score": d.score,
                    "rule_ids": d.rule_ids,
                    "findings_count": d.findings_count,
                }
                for name, d in self.dimensions.items()
            },
            "signals": [s.to_dict() for s in self.signals],
            "metrics": self.metrics,
            "metadata": self.metadata,
        }

    def to_snapshot(self) -> "Snapshot":
        """Convert this KnowledgeAssessment to a Snapshot.

        Maps assessment_id -> snapshot_id, signals -> findings.
        """
        return Snapshot(
            snapshot_id=self.assessment_id,
            score=self.score,
            dimensions=dict(self.dimensions),
            findings=list(self.signals),
            metrics=dict(self.metrics),
            metadata=dict(self.metadata),
        )

    @classmethod
    def from_snapshot(cls, snapshot: "Snapshot") -> "KnowledgeAssessment":
        """Create a KnowledgeAssessment from a Snapshot.

        Maps snapshot_id -> assessment_id, findings -> signals.
        """
        return cls(
            assessment_id=snapshot.snapshot_id,
            score=snapshot.score,
            dimensions=dict(snapshot.dimensions),
            signals=list(snapshot.findings),
            metrics=dict(snapshot.metrics),
            metadata=dict(snapshot.metadata),
        )


@dataclass
class AssessmentDiff:
    """Diff between two assessments.

    Generalizes RegressionReport from findings to signals.
    """

    prev_assessment_id: str = ""
    curr_assessment_id: str = ""
    prev_score: int = 0
    curr_score: int = 0
    score_delta: int = 0
    new_signals: list[KnowledgeSignal] = field(default_factory=list)
    resolved_signals: list[KnowledgeSignal] = field(default_factory=list)
    persistent_signals: list[KnowledgeSignal] = field(default_factory=list)
    recurring_signals: list[KnowledgeSignal] = field(default_factory=list)
    severity_changes: list[dict[str, Any]] = field(default_factory=list)
    dimension_deltas: dict[str, int] = field(default_factory=dict)
    new_high_count: int = 0
    score_change_explanation: dict[str, Any] = field(default_factory=dict)
    contributor_changes: list[dict[str, Any]] = field(default_factory=list)
    recommendation: str = ""
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "prev_assessment_id": self.prev_assessment_id,
            "curr_assessment_id": self.curr_assessment_id,
            "prev_score": self.prev_score,
            "curr_score": self.curr_score,
            "score_delta": self.score_delta,
            "new_signals": [s.to_dict() for s in self.new_signals],
            "resolved_signals": [s.to_dict() for s in self.resolved_signals],
            "persistent_signals": [s.to_dict() for s in self.persistent_signals],
            "recurring_signals": [s.to_dict() for s in self.recurring_signals],
            "severity_changes": self.severity_changes,
            "dimension_deltas": self.dimension_deltas,
            "new_high_count": self.new_high_count,
            "score_change_explanation": self.score_change_explanation,
            "contributor_changes": self.contributor_changes,
            "recommendation": self.recommendation,
            "explanation": self.explanation,
        }

    def to_regression_report(self) -> "RegressionReport":
        """Convert this AssessmentDiff to a RegressionReport.

        Maps assessment-centric fields back to snapshot-centric fields
        for backward compatibility with SnapshotStore consumers.
        """
        return RegressionReport(
            prev_snapshot_id=self.prev_assessment_id,
            curr_snapshot_id=self.curr_assessment_id,
            prev_score=self.prev_score,
            curr_score=self.curr_score,
            score_delta=self.score_delta,
            new_findings=list(self.new_signals),
            resolved_findings=list(self.resolved_signals),
            persistent_findings=list(self.persistent_signals),
            recurring_findings=list(self.recurring_signals),
            severity_changes=self.severity_changes,
            dimension_deltas=self.dimension_deltas,
            new_high_count=self.new_high_count,
            score_change_explanation=self.score_change_explanation,
            contributor_changes=self.contributor_changes,
            recommendation=self.recommendation,
            explanation=self.explanation,
        )


@dataclass
class EvolutionView:
    """Health evolution across multiple assessments.

    Generalizes TrendReport from findings to signals.
    """

    assessments: list[dict[str, Any]] = field(default_factory=list)
    score_trend: list[dict[str, Any]] = field(default_factory=list)
    signal_trend: list[dict[str, Any]] = field(default_factory=list)
    dimension_trends: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    trajectory: str = ""  # "improving", "worsening", "stable"
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessments": self.assessments,
            "score_trend": self.score_trend,
            "signal_trend": self.signal_trend,
            "dimension_trends": self.dimension_trends,
            "trajectory": self.trajectory,
            "summary": self.summary,
        }

    def to_trend_report(self) -> "TrendReport":
        """Convert this EvolutionView to a TrendReport.

        Maps assessment-centric fields to snapshot-centric fields for
        backward compatibility with SnapshotStore consumers.
        """
        # Convert score_trend entries from assessment_id to snapshot_id
        score_trend = [
            {**entry, "snapshot_id": entry.get("assessment_id", "")}
            for entry in self.score_trend
        ]
        # Convert signal_trend to finding_trend with snapshot_id keys
        finding_trend = [
            {
                "snapshot_id": entry.get("assessment_id", ""),
                "total_findings": entry.get("total_signals", 0),
                "high_findings": entry.get("high_signals", 0),
            }
            for entry in self.signal_trend
        ]
        # Convert assessments list to snapshots list
        snapshots = [
            {"snapshot_id": a.get("assessment_id", ""), "score": a.get("score", 0)}
            for a in self.assessments
        ]
        return TrendReport(
            snapshots=snapshots,
            score_trend=score_trend,
            finding_trend=finding_trend,
            dimension_trends=self.dimension_trends,
            trajectory=self.trajectory,
            summary=self.summary,
        )
