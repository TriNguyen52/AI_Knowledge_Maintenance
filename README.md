<div align="center">

# AI-Ready

**Deterministic knowledge observability and continuous improvement for AI-ready documentation.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Framework: Burr](https://img.shields.io/badge/workflow-Apache%20Brr-orange.svg)](https://github.com/DAGWorks-Inc/burr)

</div>

---

> **AI systems are only as reliable as the knowledge they retrieve.**
>
> Even the strongest language models hallucinate when documentation has missing context, disconnected concepts, broken links, ambiguous headings, or inconsistent terminology. These issues silently degrade retrieval quality — and no one notices until an AI gives the wrong answer.

AI-Ready is a deterministic knowledge observability platform that evaluates knowledge **before** it reaches an AI system. Rather than measuring language models or retrieval algorithms, AI-Ready measures the quality of the knowledge itself.

## Features

- **10 Deterministic Signal Collectors** across 6 dimensions — retrieval, context, consistency, trust, connectivity, and workflow. No LLM required for assessment.
- **Knowledge Problem Discovery** — clusters signals into root-cause problems instead of treating each symptom in isolation.
- **Salience-Based Prioritization** — ranks problems by their expected impact on AI reasoning, so the most consequential issues are addressed first.
- **Continuous Improvement Loop** — generates remediation proposals via LLM, applies changes after human approval, and verifies whether the knowledge actually improved.
- **Institutional Memory** — tracks which remediation strategies worked and which failed (EMA-scored), so the agent gets progressively better at resolving recurring problem types.
- **Workflow Orchestration** — Apache Burr state machine with resumable approval checkpoints, workflow forking after failed attempts, and automatic regression test generation.
- **Persistent Agent Memory** — CockroachDB stores artifacts with vector embeddings, signals, assessments, agent workflow state, and remediation history. The agent reads its own past outcomes before making new decisions.
- **MCP Server** — 11 tools that let external AI agents (Claude Code, Cursor, VS Code Copilot) query the knowledge maintenance agent's memory.
- **Multiple Output Formats** — terminal tables, JSON, and SARIF for CI/CD integration.
- **Incremental Assessment** — only re-runs collectors affected by changes, with git-based change detection.

## Quick Start

```bash
# Install
pip install -e .

# Scan a knowledge base
ai-ready scan ./knowledge

# View the current assessment
ai-ready status

# Compare historical assessments
ai-ready diff

# Show health trend over time
ai-ready trend

# List signals with lifecycle information
ai-ready signals

# Start continuous monitoring
ai-ready monitor
```

## Why?

Every team using AI coding assistants — Claude Code, Cursor, Copilot — depends on documentation quality. But documentation degrades over time: broken links accumulate, content goes stale, headings become ambiguous, and context goes missing.

There's no **Sentry for knowledge bases**. No system that continuously monitors documentation health, diagnoses structural problems, and fixes them before they cause AI hallucinations.

AI-Ready fills that gap. It treats knowledge as infrastructure and applies observability principles — signals, assessments, regression detection, decision traces — to the knowledge layer itself.

## How It Works

```
Knowledge Sources (Markdown, .mdx, .txt, .rst, S3)
        │
        ▼
Knowledge Artifact Model (normalized representation)
        │
        ▼
Deterministic Signal Collection (10 collectors, 6 dimensions)
        │
        ▼
Knowledge Problem Discovery (cluster signals → root causes)
        │
        ▼
Knowledge Assessment (versioned, scored, stored)
        │
        ▼
Problem Prioritization (salience ranking)
        │
        ▼
LLM Proposal Generation (with institutional memory context)
        │
        ▼
Human Approval (checkpoint)
        │
        ▼
Knowledge Modification (executed by sealed executor)
        │
        ▼
Verification (re-assess, measure improvement)
        │
        ▼
Historical Learning (EMA strategy scoring, decision traces)
```

### Signal Collectors

| Dimension | Collector | What It Detects |
|-----------|----------|----------------|
| **Retrieval** | Topic Purity | Documents covering too many unrelated topics |
| **Retrieval** | Context Independence | Content that requires external context to understand |
| **Context** | Heading Quality | Generic, numbered-only, placeholder, or too-short headings |
| **Consistency** | Terminology Consistency | Inconsistent use of terms across documents |
| **Consistency** | Contradiction Detection | Conflicting factual claims between documents |
| **Trust** | Canonical Source | Missing canonical versions of duplicated content |
| **Trust** | Freshness | Stale or outdated content |
| **Connectivity** | Link Integrity | Broken internal links |
| **Connectivity** | Knowledge Connectivity | Orphaned documents with no inbound links |
| **Workflow** | Workflow Completeness | Missing steps in documented workflows |

### Improvement Workflow

The improvement pipeline is orchestrated by an Apache Burr state machine:

```
analyze_issue → generate_proposal → review_approval → execute_change → verify_improvement
       │                │                  │                  │                 │
       ↓ fail           ↓ fail             ↓ rejected         ↓ fail            ↓ fail
  handle_failure   handle_failure      (terminal)        handle_failure    handle_failure
                                                                         │
                                                                    can fork & retry
```

- **Signal-delta gate**: skips LLM analysis when no new signals have appeared since the last assessment — saving tokens on steady-state repos.
- **Heuristic pre-analysis**: deterministic problem discovery without LLM, reducing token consumption.
- **Edit budget**: rejects proposals that modify more than 20% of any artifact.
- **Rejected-fix memory**: feeds previously rejected edits into the next proposal round to prevent repetition.
- **Forking**: failed workflows can fork and retry with prior failure context.

## Getting Started

### Prerequisites

- Python 3.10 or higher
- A knowledge base directory with `.md`, `.mdx`, `.txt`, or `.rst` files

### Installation

```bash
git clone https://github.com/TriNguyen52/ai-knowledge-maintenance.git
cd ai-knowledge-maintenance
pip install -e .
```

### Basic Usage — Local Assessment

```bash
# Scan a local documentation directory
ai-ready scan ./docs

# View the assessment report (dimensions, scores, signals)
ai-ready status

# Compare against a previous assessment
ai-ready diff

# Track health trend over time
ai-ready trend
```

### Cloud Deployment (CockroachDB + AWS)

For persistent agent memory and serverless deployment:

1. **Set up CockroachDB Cloud:**

```bash
python setup_cockroachdb.py
```

2. **Configure environment:**

```bash
cp .env.example .env
# Edit .env with your CockroachDB connection string, S3 bucket, and LLM provider
```

3. **Deploy to AWS Lambda:**

```bash
sam deploy --guided
```

4. **Trigger an assessment:**

```bash
curl -X POST https://<api-endpoint>/assess \
  -d '{"action": "assess", "s3_bucket": "my-knowledge-base"}'
```

### LLM Providers

AI-Ready supports multiple LLM providers for the improvement workflow:

| Provider | Models | Use Case |
|----------|--------|----------|
| **Groq** | llama-3.3-70b-versatile | Free-tier demos, fast inference |
| **AWS Bedrock** | Claude 3.5 Sonnet, Titan Embeddings | Production deployment |
| **OpenAI** | GPT-4o | Alternative provider |
| **Anthropic** | Claude 3.5 Sonnet | Direct API access |
| **Ollama** | Local models | Fully offline |

Set `LLM_PROVIDER` in `.env` to switch providers. Assessment (signal collection) is always deterministic and does not require any LLM.

## Design Principles

- **Deterministic before AI.** Every assessment is reproducible regardless of which language model is available. Signal collection never calls an LLM.
- **Knowledge is infrastructure.** Documentation is treated as an engineering artifact, not application data. It deserves the same observability as code.
- **Evidence is never discarded.** Every assessment, proposed modification, verification result, and remediation outcome becomes part of the historical record.
- **Conservative bias throughout.** No-CI paths give cautious verdicts. Underpowered assessments produce no conclusion rather than a false one. Fail closed on reuse.

## License

This project is licensed under the MIT License.
