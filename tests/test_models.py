"""Tests for the canonical document model."""

from ai_ready.models import (
    AssessedSignal,
    CodeBlock,
    Document,
    Finding,
    Heading,
    KnowledgeSignal,
    Link,
    Paragraph,
    RuleResult,
    Section,
    Severity,
    Snapshot,
)


def test_heading_creation():
    h = Heading(level=1, text="Introduction", line=1)
    assert h.level == 1
    assert h.text == "Introduction"


def test_heading_invalid_level():
    try:
        Heading(level=0, text="Bad")
        assert False, "Should have raised"
    except ValueError:
        pass


def test_document_word_count():
    doc = Document(
        id="test",
        path="test.md",
        title="Test",
        sections=[Section(heading=None, text="hello world foo bar")],
    )
    assert doc.word_count == 4


def test_document_all_text():
    doc = Document(
        id="test",
        path="test.md",
        title="Test",
        sections=[
            Section(heading=None, text="First section"),
            Section(heading=None, text="Second section"),
        ],
    )
    assert "First section" in doc.all_text
    assert "Second section" in doc.all_text


def test_severity_enum():
    assert Severity.CRITICAL.value == "CRITICAL"
    assert Severity.HIGH.value == "HIGH"
    assert Severity.LOW.value == "LOW"


def test_finding_to_dict():
    f = Finding(
        rule_id="test_rule",
        severity=Severity.HIGH,
        score=50,
        document_id="doc1",
        document_path="path/to/doc.md",
        evidence={"key": "value"},
        recommendation="Fix it",
    )
    d = f.to_dict()
    assert d["rule"] == "test_rule"
    assert d["severity"] == "HIGH"
    assert d["score"] == 50


def test_rule_result_to_dict():
    r = RuleResult(
        rule_id="test",
        score=80,
        severity=Severity.LOW,
    )
    d = r.to_dict()
    assert d["rule"] == "test"
    assert d["score"] == 80
    assert d["findings"] == []


def test_finding_to_signal_roundtrip():
    """Finding.to_signal() extracts bare fact; from_signal() rebuilds with placeholders."""
    f = Finding(
        rule_id="test_rule",
        severity=Severity.HIGH,
        score=50,
        document_id="doc1",
        document_path="path/to/doc.md",
        evidence={"key": "value"},
        recommendation="Fix it",
        ai_impact="Bad impact",
    )
    sig = f.to_signal()
    assert sig.collector_id == "test_rule"
    assert sig.signal_type == f.issue_type
    assert sig.artifact_uri == "path/to/doc.md"
    assert sig.evidence == {"key": "value"}
    assert sig.signal_id == f.finding_id
    assert sig.artifact_id == f.document_id
    assert sig.line == f.line

    # Round-trip: from_signal rebuilds a Finding with placeholder interpretation
    rebuilt = Finding.from_signal(sig)
    assert rebuilt.rule_id == f.rule_id
    assert rebuilt.document_path == f.document_path
    assert rebuilt.document_id == f.document_id
    assert rebuilt.evidence == f.evidence
    assert rebuilt.issue_type == f.issue_type
    assert rebuilt.finding_id == f.finding_id
    # Interpretation fields are placeholders
    assert rebuilt.severity == Severity.LOW
    assert rebuilt.score == 100
    assert rebuilt.recommendation == ""
    assert rebuilt.ai_impact == ""


def test_assessed_signal_is_finding():
    """AssessedSignal is a type alias for Finding, not a new class."""
    assert AssessedSignal is Finding
    f = AssessedSignal(
        rule_id="r",
        severity=Severity.LOW,
        score=100,
        document_id="d",
        document_path="p",
        evidence={},
        recommendation="",
    )
    assert isinstance(f, Finding)
    assert f.to_assessed_signal() is f


def test_snapshot_to_dict():
    s = Snapshot(
        snapshot_id="2026-07-22T10:00:00Z",
        score=91,
    )
    d = s.to_dict()
    assert d["snapshot_id"] == "2026-07-22T10:00:00Z"
    assert d["score"] == 91
    assert d["findings"] == []
