"""Tests for the scan pipeline and output formats."""

import json
import tempfile
from pathlib import Path

from ai_ready.connectors.markdown import MarkdownConnector
from ai_ready.output import format_json, format_sarif, format_terminal
from ai_ready.pipeline import ScanPipeline

FIXTURES = Path(__file__).parent / "fixtures"


def _load_kb(source=FIXTURES):
    """Load documents and relations from source."""
    conn = MarkdownConnector()
    conn.connect(source)
    docs = list(conn.iter_documents())
    relations = list(conn.iter_relations())
    return docs, relations


def test_pipeline_runs_full_scan():
    docs, relations = _load_kb()
    pipeline = ScanPipeline()
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)

    assert snapshot.score >= 0
    assert snapshot.score <= 100
    assert len(snapshot.dimensions) > 0
    assert snapshot.metadata["document_count"] >= 4


def test_pipeline_produces_findings():
    docs, relations = _load_kb()
    pipeline = ScanPipeline()
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)

    # We expect heading quality issues, context independence issues, and broken links
    assert len(snapshot.findings) > 0


def test_pipeline_exit_code_pass():
    docs, relations = _load_kb()
    pipeline = ScanPipeline(thresholds={"overall_score": 0}, fail_on=[])
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)
    assert pipeline.get_exit_code(snapshot) == 0


def test_pipeline_exit_code_threshold_fail():
    docs, relations = _load_kb()
    pipeline = ScanPipeline(thresholds={"overall_score": 200}, fail_on=[])
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)
    assert pipeline.get_exit_code(snapshot) == 1


def test_pipeline_exit_code_high_findings():
    docs, relations = _load_kb()
    pipeline = ScanPipeline(thresholds={"overall_score": 0}, fail_on=["HIGH"])
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)
    # We should have HIGH findings from topic_purity or heading_quality
    exit_code = pipeline.get_exit_code(snapshot)
    assert exit_code in (0, 2)


def test_pipeline_relations_metadata():
    docs, relations = _load_kb()
    pipeline = ScanPipeline()
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)
    assert "relation_count" in snapshot.metadata


def test_format_terminal():
    docs, relations = _load_kb()
    pipeline = ScanPipeline()
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)

    output = format_terminal(snapshot)
    assert "AI Readiness Scan" in output
    assert "Overall Score" in output


def test_format_json():
    docs, relations = _load_kb()
    pipeline = ScanPipeline()
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)

    output = format_json(snapshot)
    data = json.loads(output)
    assert "snapshot_id" in data
    assert "score" in data
    assert "dimensions" in data


def test_format_sarif():
    docs, relations = _load_kb()
    pipeline = ScanPipeline()
    snapshot = pipeline.run(docs, source=str(FIXTURES), relations=relations)

    output = format_sarif(snapshot)
    data = json.loads(output)
    assert data["version"] == "2.1.0"
    assert len(data["runs"]) == 1
    assert data["runs"][0]["tool"]["driver"]["name"] == "ai-ready"


def test_config_loading():
    from ai_ready.config import Config

    config = Config.default()
    assert config.weights["retrieval"] == 0.25
    assert "CRITICAL" in config.fail_on


def test_config_from_file():
    from ai_ready.config import Config

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write("""
version: 1
thresholds:
  overall_score: 85
fail_on:
  - HIGH
weights:
  retrieval: 0.30
  consistency: 0.15
  trust: 0.20
  connectivity: 0.15
  workflow: 0.10
  context: 0.10
rules:
  topic_purity:
    enabled: true
  context_independence:
    enabled: false
""")
        f.flush()

        config = Config.from_file(f.name)
        assert config.thresholds["overall_score"] == 85
        assert config.weights["retrieval"] == 0.30
        assert "topic_purity" in config.enabled_rule_ids
        assert "context_independence" not in config.enabled_rule_ids
