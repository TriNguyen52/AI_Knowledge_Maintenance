"""Tests for the canonical document model."""

from ai_ready.models import (
    CodeBlock,
    Document,
    Finding,
    Heading,
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


def test_snapshot_to_dict():
    s = Snapshot(
        snapshot_id="2026-07-22T10:00:00Z",
        score=91,
    )
    d = s.to_dict()
    assert d["snapshot_id"] == "2026-07-22T10:00:00Z"
    assert d["score"] == 91
    assert d["findings"] == []
