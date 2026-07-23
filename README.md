# AI-Ready

AI knowledge observability platform — continuously evaluate whether a knowledge base is AI-ready before deployment and as it evolves over time.

## What It Does

AI-Ready analyzes your documentation knowledge base and detects structural properties that cause AI failures:

- **Topic Purity** — documents mixing unrelated concepts
- **Context Independence** — chunks that can't stand alone when retrieved
- **Heading Quality** — vague headings that provide weak retrieval signals
- **Link Integrity** — broken links and orphan documents

## Quick Start

```bash
# Install
pip install -e .

# Scan a knowledge base
ai-ready scan docs/

# JSON output for CI/CD
ai-ready scan docs/ --json

# SARIF output for GitHub code scanning
ai-ready scan docs/ --sarif

# View snapshot history
ai-ready history

# Diff two snapshots for regression detection
ai-ready diff baseline.db current.db

# Continuous monitoring
ai-ready monitor --path docs/ --interval 300
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Scan successful, thresholds passed |
| 1 | Readiness threshold failed |
| 2 | High severity findings present |
| 3 | Internal analyzer error |

## Configuration

Create `.ai-ready.yml` in your docs directory:

```yaml
version: 1

thresholds:
  overall_score: 85

fail_on:
  - CRITICAL

weights:
  retrieval: 0.25
  context: 0.15
  consistency: 0.20
  trust: 0.20
  connectivity: 0.10
  workflow: 0.10

rules:
  topic_purity:
    enabled: true
  context_independence:
    enabled: true
  heading_quality:
    enabled: true
  link_integrity:
    enabled: true
```

## AI Readiness Dimensions

| Dimension | Signals |
|-----------|---------|
| Retrieval Readiness | Topic Purity, Heading Quality |
| Context Readiness | Context Independence |
| Consistency | Terminology, Contradictions |
| Trustworthiness | Canonical Sources, Freshness |
| Connectivity | Knowledge Connectivity, Links |
| Task Completion | Workflow Completeness |

## GitHub Actions Integration

```yaml
- name: AI readiness scan
  run: ai-ready scan docs/ --json > report.json

- name: Fail on regression
  run: ai-ready diff baseline.db current.db
```

## Architecture

```
Connectors (Markdown, Git, GitBook*, Notion*, Confluence*)
    ↓
Knowledge Representation Layer (Canonical Document Model)
    ↓
AI Readiness Rule Engine (collect → measure → evaluate → report)
    ↓
Snapshot Store (SQLite)
    ↓
Diff / Regression Engine
    ↓
CLI / JSON / SARIF / Prometheus* / GitHub Checks
```

*Future connectors and output formats

## Design Principles

- **Infrastructure-first** — runs as CLI, CI step, daemon, or container
- **Deterministic** — no LLM-based scoring, every finding has evidence
- **Machine-readable** — JSON, SARIF, Prometheus metrics
- **CI/CD native** — exit codes for pipeline integration
- **Never modifies the KB** — read-only analysis

## License

MIT
