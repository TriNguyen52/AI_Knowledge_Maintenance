"""Tests for the Markdown connector."""

from pathlib import Path

from ai_ready.connectors.markdown import MarkdownConnector

FIXTURES = Path(__file__).parent / "fixtures"


def test_connector_finds_files():
    conn = MarkdownConnector()
    conn.connect(FIXTURES)
    docs = list(conn.iter_documents())
    assert len(docs) >= 4
    paths = [d.path for d in docs]
    assert any("auth_guide" in p for p in paths)
    assert any("billing" in p for p in paths)


def test_connector_parses_headings():
    conn = MarkdownConnector()
    conn.connect(FIXTURES / "auth_guide.md")
    docs = list(conn.iter_documents())
    assert len(docs) == 1
    doc = docs[0]
    heading_texts = [h.text for h in doc.headings]
    assert "Authentication Guide" in heading_texts
    assert "Configure OAuth Authentication" in heading_texts
    assert "Password Policy" in heading_texts


def test_connector_parses_links():
    conn = MarkdownConnector()
    conn.connect(FIXTURES / "auth_guide.md")
    doc = list(conn.iter_documents())[0]
    assert len(doc.links) > 0
    internal_links = [l for l in doc.links if l.is_internal]
    assert any(l.target == "./configuration.md" for l in internal_links)


def test_connector_parses_sections():
    conn = MarkdownConnector()
    conn.connect(FIXTURES / "configuration.md")
    doc = list(conn.iter_documents())[0]
    assert len(doc.sections) >= 3
    # Sections should have text
    for s in doc.sections:
        assert isinstance(s.text, str)


def test_connector_parses_paragraphs():
    conn = MarkdownConnector()
    conn.connect(FIXTURES / "auth_guide.md")
    doc = list(conn.iter_documents())[0]
    assert len(doc.paragraphs) > 0
    # Check that paragraphs have text
    for p in doc.paragraphs:
        assert len(p.text) > 0


def test_connector_extracts_title():
    conn = MarkdownConnector()
    conn.connect(FIXTURES / "auth_guide.md")
    doc = list(conn.iter_documents())[0]
    assert doc.title == "Authentication Guide"


def test_connector_extracts_frontmatter():
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("---\ntitle: Custom Title\ndescription: A test doc\n---\n\n# Content\n\nBody text.")
        f.flush()
        f.close()

        conn = MarkdownConnector()
        conn.connect(f.name)
        doc = list(conn.iter_documents())[0]
        assert doc.metadata.get("title") == "Custom Title"
        assert doc.metadata.get("description") == "A test doc"
    try:
        os.unlink(f.name)
    except PermissionError:
        pass


def test_connector_document_id_is_consistent():
    conn = MarkdownConnector()
    conn.connect(FIXTURES / "auth_guide.md")
    doc1 = list(conn.iter_documents())[0]
    conn2 = MarkdownConnector()
    conn2.connect(FIXTURES / "auth_guide.md")
    doc2 = list(conn2.iter_documents())[0]
    assert doc1.id == doc2.id
