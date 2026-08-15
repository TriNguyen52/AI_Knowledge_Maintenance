"""Streamlit dashboard for AI Knowledge Maintenance.

All data is read live from CockroachDB Cloud. Improvement cycles are
triggered via AWS Lambda (S3-backed knowledge base). The dashboard is
call-only — no business logic lives here.

Architecture:
    S3 Bucket -> EventBridge -> Lambda -> CockroachDB -> Groq LLM

Tabs:
    1. Dashboard        — health score, trend, actions, inline results, problems, signals
    2. Artifact Explorer — inline content view with deduplicated signal annotations
    3. Remediation      — propose -> review -> approve -> execute -> verify
    4. Memory           — A/B test, EMA scores, pause/resume
    5. Technical        — MCP tools, cross-check diagnostics, AWS event proof

Deployment:
    1. Push to GitHub
    2. Go to share.streamlit.io
    3. Connect repo, set main file to ai_ready/cloud/dashboard.py
    4. Add secrets: COCKROACH_DB_URL, GROQ_API_KEY, HF_TOKEN, LLM_PROVIDER
    5. Deploy
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

import streamlit as st

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Connection helpers (cached across reruns)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_store() -> Any:
    from ai_ready.storage.cockroach import CockroachDBStore

    db_url = None
    try:
        db_url = st.secrets["COCKROACH_DB_URL"]
    except (KeyError, FileNotFoundError):
        pass

    if not db_url:
        db_url = os.environ.get("COCKROACH_DB_URL", "")

    if not db_url:
        st.error("No COCKROACH_DB_URL found. Set it in Streamlit secrets or env.")
        st.stop()

    store = CockroachDBStore(db_url)
    try:
        store.initialize_schema()
    except Exception:
        pass
    return store


@st.cache_resource
def get_llm_gateway() -> Any:
    groq_key = None
    try:
        groq_key = st.secrets.get("GROQ_API_KEY", "")
    except (KeyError, FileNotFoundError):
        pass

    if not groq_key:
        groq_key = os.environ.get("GROQ_API_KEY", "")

    if not groq_key:
        return None

    try:
        from ai_ready.llm import LLMGateway
        return LLMGateway(provider="groq")
    except Exception:
        return None


def get_mcp_store() -> Any:
    from ai_ready.cloud.mcp_server import set_store
    store = get_store()
    set_store(store)
    return store


# ---------------------------------------------------------------------------
# Data extraction helpers (reuse CockroachDBStore methods — no business logic)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def fetch_overview(store_id: str) -> dict[str, Any]:
    store = get_store()
    latest = {}
    try:
        rows = store.get_assessment_history(limit=1)
        if rows:
            row = rows[0]
            latest = {
                "assessment_id": getattr(row, "assessment_id", ""),
                "score": getattr(row, "score", 0),
                "signals_count": getattr(row, "signals_count", 0),
                "source": getattr(row, "source", ""),
                "created_at": str(getattr(row, "created_at", "")),
            }
    except Exception:
        pass

    artifact_count = 0
    try:
        artifact_count = len(store.get_all_artifacts())
    except Exception:
        pass

    signal_count = 0
    try:
        signal_count = len(store.get_all_signals())
    except Exception:
        pass

    assessment_count = 0
    try:
        assessment_count = len(store.get_assessment_history(limit=100))
    except Exception:
        pass

    return {
        "latest": latest,
        "artifact_count": artifact_count,
        "signal_count": signal_count,
        "assessment_count": assessment_count,
    }


@st.cache_data(ttl=30)
def fetch_signals(store_id: str) -> list[dict[str, Any]]:
    store = get_store()
    try:
        signals = store.get_all_signals()
        return [
            {
                "signal_id": s.signal_id,
                "collector_id": s.collector_id,
                "artifact_uri": s.artifact_uri,
                "signal_type": s.signal_type,
                "severity": getattr(s, "severity", ""),
                "recommendation": getattr(s, "recommendation", ""),
                "status": getattr(s, "status", "new"),
            }
            for s in signals
        ]
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_lifecycle_counts(store_id: str) -> dict[str, int]:
    store = get_store()
    counts = {"new": 0, "persistent": 0, "resolved": 0, "recurring": 0}
    try:
        for status in counts:
            sigs = store.get_signals_by_status(status)
            counts[status] = len(sigs)
    except Exception:
        pass
    return counts


@st.cache_data(ttl=30)
def fetch_outcomes(store_id: str, limit: int = 50) -> list[dict[str, Any]]:
    store = get_store()
    try:
        history = store.get_remediation_history(limit=limit)
        return [
            {
                "outcome_id": getattr(h, "outcome_id", ""),
                "issue_type": getattr(h, "issue_type", ""),
                "strategy": getattr(h, "strategy", ""),
                "verification_outcome": getattr(h, "verification_outcome", ""),
                "score_before": getattr(h, "score_before", 0),
                "score_after": getattr(h, "score_after", 0),
                "created_at": str(getattr(h, "created_at", "")),
            }
            for h in history
        ]
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_workflows(store_id: str) -> list[dict[str, Any]]:
    store = get_store()
    try:
        return store.get_all_workflows(limit=50)
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_trend(store_id: str, days: int = 30) -> list[dict[str, Any]]:
    store = get_store()
    try:
        return store.get_knowledge_health_trend(days=days)
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_problems(store_id: str) -> list[dict[str, Any]]:
    store = get_store()
    try:
        records = store.get_problems(limit=50)
        result = []
        for p in records:
            if isinstance(p, dict):
                result.append(p)
            else:
                result.append({
                    "problem_id": getattr(p, "problem_id", ""),
                    "category": getattr(p, "category", "unknown"),
                    "description": getattr(p, "description", ""),
                    "artifact_uris": getattr(p, "artifact_uris", []),
                    "salience": getattr(p, "salience", 0.0),
                    "encoding_score": getattr(p, "encoding_score", 0.0),
                    "outcome_score": getattr(p, "outcome_score", 0.0),
                    "retrieval_score": getattr(p, "retrieval_score", 0.0),
                    "status": getattr(p, "status", "open"),
                    "created_at": str(getattr(p, "created_at", "")),
                })
        return result
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_modified_artifacts(store_id: str) -> list[dict[str, Any]]:
    store = get_store()
    try:
        return store.get_modified_artifact_uris(source="")
    except Exception:
        return []


@st.cache_data(ttl=30)
def fetch_artifacts_with_signals(store_id: str) -> list[dict[str, Any]]:
    """Fetch all artifacts that have signals, with their content and signals.

    Reuses store.get_all_signals() and store.get_artifact() — no new DB queries.
    Returns list of dicts sorted by signal count (most signals first):
        artifact_uri, signal_count, signals, content
    """
    import json as _json

    store = get_store()
    try:
        all_signals = store.get_all_signals()
    except Exception:
        return []

    # Group signals by artifact_uri
    by_uri: dict[str, list[dict[str, Any]]] = {}
    for s in all_signals:
        uri = s.artifact_uri
        if uri not in by_uri:
            by_uri[uri] = []
        evidence = {}
        try:
            evidence = _json.loads(s.evidence) if s.evidence else {}
        except Exception:
            pass
        by_uri[uri].append({
            "signal_id": s.signal_id,
            "signal_type": s.signal_type,
            "collector_id": s.collector_id,
            "severity": getattr(s, "severity", ""),
            "recommendation": getattr(s, "recommendation", ""),
            "ai_impact": getattr(s, "ai_impact", ""),
            "evidence": evidence,
            "status": getattr(s, "status", "new"),
        })

    # Fetch content for each artifact
    result = []
    for uri, signals in by_uri.items():
        content = ""
        try:
            artifact = store.get_artifact(uri)
            if artifact:
                content = artifact.content or ""
        except Exception:
            pass
        result.append({
            "artifact_uri": uri,
            "signal_count": len(signals),
            "signals": signals,
            "content": content,
        })

    result.sort(key=lambda x: x["signal_count"], reverse=True)
    return result


# Signal-type-to-line mapping — determines which lines in artifact content
# are affected by each signal type.  Returns 1-indexed line numbers, or [0]
# for document-level signals (orphan, mixed_topics, etc.).
#
# This is display-only logic — it does NOT modify content.  The executor
# (executor.py) handles actual content modification using similar evidence
# parsing, but for a different purpose (fixing vs annotating).

_DANGLING_PATTERNS = [
    "see above", "see below", "as mentioned above", "as described above",
    "as explained above", "as you saw above", "the above section",
    "the previous section", "the below section", "as you read above",
    "as you saw in",
]


def _map_signal_to_lines(signal: dict[str, Any], content: str) -> list[int]:
    """Map a signal to specific 1-indexed line numbers in artifact content.

    Returns [0] for document-level signals that apply to the whole artifact.
    """
    signal_type = signal.get("signal_type", "")
    evidence = signal.get("evidence", {})
    lines = content.split("\n")

    if signal_type in ("no_date_signal", "stale_content"):
        fm_lines = []
        for i, line in enumerate(lines[:15], 1):
            if line.strip().startswith("---") or ":" in line:
                fm_lines.append(i)
        return fm_lines[:5] if fm_lines else [1]

    elif signal_type == "broken_link":
        broken_targets = set()
        for bl in evidence.get("broken_links", []):
            broken_targets.add(bl.get("target", ""))
        if not broken_targets:
            return []
        result = []
        for i, line in enumerate(lines, 1):
            for target in broken_targets:
                if target in line:
                    result.append(i)
                    break
        return result

    elif signal_type == "dangling_reference":
        phrases = evidence.get("phrases", _DANGLING_PATTERNS)
        result = []
        for i, line in enumerate(lines, 1):
            line_lower = line.lower()
            for phrase in phrases:
                if isinstance(phrase, str) and phrase.lower() in line_lower:
                    result.append(i)
                    break
        return result

    elif signal_type in (
        "generic_heading", "placeholder_heading", "too_short_heading",
        "numbered_only_heading",
    ):
        heading_text = evidence.get("heading", evidence.get("heading_text", ""))
        heading_pattern = evidence.get("pattern", "")
        result = []
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                if heading_text and heading_text in line:
                    result.append(i)
                elif heading_pattern and heading_pattern.lower() in line.lower():
                    result.append(i)
        return result

    elif signal_type == "missing_canonical_source":
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("# "):
                return [i, i + 1]
        return [1, 2]

    elif signal_type in (
        "orphan_artifact", "mixed_topics", "duplicate_content",
        "incomplete_workflow",
    ):
        return [0]

    elif signal_type == "terminology_variant":
        term = evidence.get("term", evidence.get("variant", ""))
        if not term:
            return [0]
        result = []
        for i, line in enumerate(lines, 1):
            if term in line:
                result.append(i)
        return result or [0]

    return [0]


def _deduplicate_signals_by_type(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group signals by signal_type, returning one entry per type with count.

    Instead of showing 6 identical 'missing_canonical_source' signals,
    show one with a count of 6.
    """
    by_type: dict[str, dict[str, Any]] = {}
    for s in signals:
        sig_type = s.get("signal_type", "unknown")
        if sig_type not in by_type:
            by_type[sig_type] = {
                **s,
                "count": 1,
                "signal_ids": [s.get("signal_id", "")],
            }
        else:
            by_type[sig_type]["count"] += 1
            by_type[sig_type]["signal_ids"].append(s.get("signal_id", ""))
    return list(by_type.values())


def _render_annotated_content(content: str, signals: list[dict[str, Any]]) -> str:
    """Render artifact content as HTML with inline signal annotations.

    Produces a code-editor-like view with highlighted lines and inline
    annotation boxes showing signal type (deduplicated), severity, and recommendation.
    """
    import html as _html

    lines = content.split("\n")

    # Map line numbers to annotations; collect document-level separately
    line_annotations: dict[int, list[dict[str, Any]]] = {}
    doc_annotations: list[dict[str, Any]] = []

    for signal in signals:
        affected = _map_signal_to_lines(signal, content)
        if affected == [0]:
            doc_annotations.append(signal)
        else:
            for ln in affected:
                if 1 <= ln <= len(lines):
                    line_annotations.setdefault(ln, []).append(signal)

    # Deduplicate annotations per line by signal_type
    for ln, anns in line_annotations.items():
        line_annotations[ln] = _deduplicate_signals_by_type(anns)

    # Also deduplicate doc-level annotations
    doc_annotations = _deduplicate_signals_by_type(doc_annotations)

    sev_colors = {
        "CRITICAL": "#dc2626",
        "HIGH": "#ea580c",
        "MEDIUM": "#ca8a04",
        "LOW": "#2563eb",
    }
    sev_icons = {
        "CRITICAL": "🔴",
        "HIGH": "🟠",
        "MEDIUM": "🟡",
        "LOW": "🔵",
    }

    parts = [
        '<div style="font-family: ui-monospace, monospace; font-size: 13px; '
        'line-height: 1.6; white-space: pre-wrap; background: #f8fafc; '
        'border: 1px solid #e2e8f0; border-radius: 4px; padding: 12px; '
        'max-height: 600px; overflow-y: auto;">'
    ]

    for i, line in enumerate(lines, 1):
        anns = line_annotations.get(i, [])
        if anns:
            top_sev = anns[0].get("severity", "MEDIUM")
            border_color = sev_colors.get(top_sev, "#ca8a04")
            parts.append(
                f'<div style="background: #fef3c7; border-left: 3px solid '
                f'{border_color}; padding: 2px 8px; margin: 2px 0;">'
            )
            parts.append(
                f'<span style="color: #94a3b8; user-select: none; '
                f'margin-right: 8px;">{i:4d}</span>'
            )
            parts.append(f'<span>{_html.escape(line)}</span>')

            for ann in anns:
                sev = ann.get("severity", "MEDIUM")
                color = sev_colors.get(sev, "#ca8a04")
                icon = sev_icons.get(sev, "🟡")
                sig_type = ann.get("signal_type", "?")
                rec = ann.get("recommendation", "")
                count = ann.get("count", 1)
                count_str = f" <span style='color: #94a3b8; font-size: 11px;'>(×{count})</span>" if count > 1 else ""
                parts.append(
                    f'<div style="margin-top: 4px; padding: 4px 8px; '
                    f'background: {color}15; border-radius: 3px; '
                    f'font-size: 12px;">'
                    f'<span style="color: {color}; font-weight: 600;">'
                    f'{icon} {sig_type}</span>{count_str}'
                    f'<span style="color: #475569; margin-left: 8px;">'
                    f'{_html.escape(rec)}</span>'
                    f'</div>'
                )
            parts.append('</div>')
        else:
            parts.append(
                f'<div style="padding: 2px 8px;">'
                f'<span style="color: #94a3b8; user-select: none; '
                f'margin-right: 8px;">{i:4d}</span>'
                f'<span>{_html.escape(line)}</span>'
                f'</div>'
            )

    if doc_annotations:
        parts.append(
            '<div style="margin-top: 12px; padding-top: 8px; '
            'border-top: 1px solid #e2e8f0;">'
        )
        parts.append(
            '<div style="font-weight: 600; color: #475569; '
            'margin-bottom: 4px;">Document-level signals:</div>'
        )
        for ann in doc_annotations:
            sev = ann.get("severity", "MEDIUM")
            color = sev_colors.get(sev, "#ca8a04")
            icon = sev_icons.get(sev, "🟡")
            sig_type = ann.get("signal_type", "?")
            rec = ann.get("recommendation", "")
            count = ann.get("count", 1)
            count_str = f" (×{count})" if count > 1 else ""
            parts.append(
                f'<div style="padding: 4px 8px; background: {color}15; '
                f'border-radius: 3px; font-size: 12px; margin-bottom: 4px;">'
                f'<span style="color: {color}; font-weight: 600;">'
                f'{icon} {sig_type}</span>{count_str}'
                f'<span style="color: #475569; margin-left: 8px;">'
                f'{_html.escape(rec)}</span>'
                f'</div>'
            )
        parts.append('</div>')

    parts.append('</div>')
    return "\n".join(parts)


def _render_suggested_edit(signal: dict[str, Any], content: str) -> dict[str, str] | None:
    """Generate a Cursor-like inline edit suggestion for a signal.

    Returns a dict with 'line', 'original', 'suggested' keys, or None.
    """
    signal_type = signal.get("signal_type", "")
    evidence = signal.get("evidence", {})
    lines = content.split("\n")
    affected = _map_signal_to_lines(signal, content)

    if signal_type == "missing_canonical_source":
        # Suggest adding a canonical source link after the first heading
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("# "):
                heading = line.strip().lstrip("# ").strip()
                suggested = f"> **Source**: Official documentation for {heading}"
                return {
                    "line": i + 1,
                    "original": "(insert after heading)",
                    "suggested": suggested,
                    "signal_type": signal_type,
                    "recommendation": signal.get("recommendation", ""),
                }
        return None

    elif signal_type == "no_date_signal":
        # Suggest adding date to frontmatter
        return {
            "line": 1,
            "original": "(add to frontmatter)",
            "suggested": "last_updated: 2026-08-14",
            "signal_type": signal_type,
            "recommendation": signal.get("recommendation", ""),
        }

    elif signal_type == "broken_link":
        broken_targets = set()
        for bl in evidence.get("broken_links", []):
            broken_targets.add(bl.get("target", ""))
        if not broken_targets or not affected:
            return None
        ln = affected[0]
        original_line = lines[ln - 1] if 1 <= ln <= len(lines) else ""
        # Suggest removing the broken link
        for target in broken_targets:
            if target in original_line:
                suggested_line = original_line.replace(f"]({target})", "](#)")
                return {
                    "line": ln,
                    "original": original_line,
                    "suggested": suggested_line,
                    "signal_type": signal_type,
                    "recommendation": signal.get("recommendation", ""),
                }
        return None

    elif signal_type == "dangling_reference":
        if not affected:
            return None
        ln = affected[0]
        original_line = lines[ln - 1] if 1 <= ln <= len(lines) else ""
        # Suggest replacing dangling reference with specific section link
        for phrase in _DANGLING_PATTERNS:
            if phrase in original_line.lower():
                suggested_line = original_line.replace(phrase, "the relevant section")
                return {
                    "line": ln,
                    "original": original_line,
                    "suggested": suggested_line,
                    "signal_type": signal_type,
                    "recommendation": signal.get("recommendation", ""),
                }
        return None

    elif signal_type in ("generic_heading", "numbered_only_heading", "too_short_heading"):
        if not affected:
            return None
        ln = affected[0]
        original_line = lines[ln - 1] if 1 <= ln <= len(lines) else ""
        heading_text = evidence.get("heading", evidence.get("heading_text", ""))
        # Suggest a more descriptive heading
        if heading_text:
            suggested_line = original_line.replace(heading_text, f"Overview of {heading_text}")
            return {
                "line": ln,
                "original": original_line,
                "suggested": suggested_line,
                "signal_type": signal_type,
                "recommendation": signal.get("recommendation", ""),
            }
        return None

    elif signal_type == "stale_content":
        return {
            "line": 1,
            "original": "(update frontmatter date)",
            "suggested": "last_updated: 2026-08-14",
            "signal_type": signal_type,
            "recommendation": signal.get("recommendation", ""),
        }

    return None


def _render_edit_suggestions(content: str, signals: list[dict[str, Any]]) -> str:
    """Render Cursor-like inline edit suggestions as HTML."""
    import html as _html

    # Deduplicate signals by type first
    deduped = _deduplicate_signals_by_type(signals)

    suggestions = []
    for signal in deduped:
        suggestion = _render_suggested_edit(signal, content)
        if suggestion:
            suggestions.append(suggestion)

    if not suggestions:
        return ""

    parts = [
        '<div style="font-family: ui-monospace, monospace; font-size: 13px; '
        'line-height: 1.6; background: #f0fdf4; border: 1px solid #bbf7d0; '
        'border-radius: 4px; padding: 12px; margin-top: 12px;">'
    ]
    parts.append(
        '<div style="font-weight: 600; color: #166534; margin-bottom: 8px;">'
        '💡 Suggested Edits (Cursor-like inline suggestions)</div>'
    )

    for s in suggestions:
        sig_type = s["signal_type"]
        ln = s["line"]
        original = s["original"]
        suggested = s["suggested"]
        rec = s.get("recommendation", "")

        parts.append(
            f'<div style="margin-bottom: 12px; padding: 8px; background: white; '
            f'border-radius: 4px; border: 1px solid #d1d5db;">'
            f'<div style="font-size: 12px; color: #6b7280; margin-bottom: 4px;">'
            f'Line {ln} — <span style="color: #2563eb; font-weight: 600;">{sig_type}</span>'
            f'</div>'
            f'<div style="color: #dc2626; text-decoration: line-through; '
            f'padding: 2px 8px; background: #fef2f2; border-radius: 3px; '
            f'margin-bottom: 4px;">- {_html.escape(original)}</div>'
            f'<div style="color: #16a34a; padding: 2px 8px; background: #f0fdf4; '
            f'border-radius: 3px;">+ {_html.escape(suggested)}</div>'
            f'{f"<div style=\"font-size: 11px; color: #6b7280; margin-top: 4px;\">{_html.escape(rec)}</div>" if rec else ""}'
            f'</div>'
        )

    parts.append('</div>')
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Inline CSS — calm surface, one accent, minimal chrome
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="AI Knowledge Maintenance",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    /* Accent: one blue for actions and active state */
    :root {
        --accent: #2563eb;
        --accent-light: #dbeafe;
        --text: #1e293b;
        --text-muted: #64748b;
        --surface: #ffffff;
        --border: #e2e8f0;
    }

    /* Calm typography */
    .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {
        color: var(--text);
        font-weight: 600;
        letter-spacing: -0.02em;
    }

    /* Section headers: utility copy, not marketing */
    .stMarkdown h2 {
        font-size: 1.1rem;
        padding-top: 0.5rem;
    }

    /* Captions: muted, one line */
    .stMarkdown p, .stCaption {
        color: var(--text-muted);
    }

    /* Metrics: clean, no boxes */
    [data-testid="stMetric"] {
        background: transparent;
        border: none;
        padding: 0.5rem 0;
    }

    /* Buttons: accent for primary, ghost for secondary */
    .stButton > button[kind="primary"] {
        background: var(--accent);
        border-color: var(--accent);
    }
    .stButton > button[kind="secondary"] {
        background: transparent;
        border-color: var(--border);
        color: var(--text);
    }

    /* Dataframes: thin borders, no heavy chrome */
    .stDataFrame {
        border: 1px solid var(--border);
        border-radius: 4px;
    }

    /* Expanders: minimal */
    .stExpander {
        border: 1px solid var(--border);
        border-radius: 4px;
    }

    /* Dividers: subtle */
    hr {
        border-color: var(--border);
        margin: 1.5rem 0;
    }

    /* Sidebar: clean */
    .stSidebar {
        border-right: 1px solid var(--border);
    }

    /* Tabs: underline style, no pills */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        border-bottom: 1px solid var(--border);
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 0;
        padding: 0.5rem 1rem;
        border-bottom: 2px solid transparent;
    }
    .stTabs [aria-selected="true"] {
        border-bottom-color: var(--accent);
    }

    /* Signal type cards */
    .signal-card {
        padding: 8px 12px;
        margin: 4px 0;
        border-left: 3px solid var(--accent);
        background: var(--accent-light);
        border-radius: 0 4px 4px 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Sidebar — connection status + AWS config
# ---------------------------------------------------------------------------

st.sidebar.markdown("## Connection")
store = get_store()
gateway = get_llm_gateway()

if gateway:
    st.sidebar.markdown("Groq LLM connected")
else:
    st.sidebar.markdown("No Groq LLM (heuristic mode)")

st.sidebar.divider()
st.sidebar.markdown("## AWS Configuration")
s3_bucket = st.sidebar.text_input(
    "S3 Bucket",
    value=os.environ.get("S3_KNOWLEDGE_BUCKET", "knowledge-base"),
)
s3_prefix = st.sidebar.text_input(
    "S3 Prefix",
    value=os.environ.get("S3_KNOWLEDGE_PREFIX", "docs100"),
)
aws_endpoint = st.sidebar.text_input(
    "Floci Endpoint",
    value=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
)
lambda_function = st.sidebar.text_input(
    "Lambda Function",
    value=os.environ.get("LAMBDA_FUNCTION_NAME", "ai-knowledge-assess"),
)

st.sidebar.divider()
st.sidebar.markdown("AI Knowledge Maintenance")
st.sidebar.markdown("CockroachDB + Groq LLM + AWS Lambda")
st.sidebar.markdown("[GitHub](https://github.com/TriNguyen52/AI_Knowledge_Maintenance)")

_store_id = "cockroach"


# ---------------------------------------------------------------------------
# Tabs — 5 tabs (merged Overview+Diagnosis into Dashboard)
# ---------------------------------------------------------------------------

tab_dashboard, tab_explorer, tab_remediation, tab_memory, tab_tech = st.tabs([
    "Dashboard",
    "Artifact Explorer",
    "Remediation",
    "Memory",
    "Technical",
])


# ---------------------------------------------------------------------------
# Tab 1: Dashboard (merged Overview + Diagnosis)
# ---------------------------------------------------------------------------

with tab_dashboard:
    data = fetch_overview(_store_id)
    latest = data["latest"]
    score = latest.get("score", 0)

    # KPIs — no boxes, just numbers
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Health Score", f"{score}/100")
    with m2:
        st.metric("Artifacts", data["artifact_count"])
    with m3:
        st.metric("Signals", data["signal_count"])
    with m4:
        st.metric("Assessments", data["assessment_count"])

    st.divider()

    # Score + trend
    col_score, col_trend = st.columns([1, 2])
    with col_score:
        st.markdown("### Score")
        st.progress(score / 100, text=f"{score} / 100")
        if latest:
            st.caption(f"Source: {latest.get('source', 'N/A')}")
            st.caption(f"Assessed: {latest.get('created_at', 'N/A')}")

    with col_trend:
        trend = fetch_trend(_store_id, days=30)
        if trend:
            st.markdown("### Score Trend (30 days)")
            try:
                import pandas as pd
                df = pd.DataFrame(trend)
                if "day" in df.columns and "avg_score" in df.columns:
                    st.line_chart(df.set_index("day")["avg_score"], height=200)
            except Exception:
                st.dataframe(trend, use_container_width=True)
        else:
            st.caption("No trend data available.")

    st.divider()

    # Actions — Run Assessment (with human approval) and Run Full Cycle (with progress)
    st.markdown("### Actions")

    col_a1, col_a2 = st.columns(2)
    with col_a1:
        if st.button("Run Assessment", type="primary",
                      help="Runs assessment + generates proposal. Halts at approval checkpoint for human review."):
            with st.status("Running assessment via Lambda...", expanded=True) as status:
                try:
                    from ai_ready.cloud.demo import invoke_lambda

                    st.write("📥 Invoking Lambda assessment (assess action)...")
                    payload = {
                        "action": "assess",
                        "s3_bucket": s3_bucket,
                        "s3_prefix": s3_prefix,
                    }
                    result = invoke_lambda(
                        payload=payload,
                        endpoint=aws_endpoint,
                        function_name=lambda_function,
                    )
                    if "error" in result:
                        st.error(f"Assessment failed: {result['error']}")
                        status.update(label="Assessment failed", state="error")
                    else:
                        st.write(f"✅ Assessment complete: score {result.get('score', '?')}, "
                                 f"{result.get('signal_count', 0)} signals, "
                                 f"{result.get('problems_discovered', 0)} problems")

                        st.write("🔍 Generating proposal via Lambda (propose action)...")
                        propose_payload = {
                            "action": "propose",
                            "s3_bucket": s3_bucket,
                            "s3_prefix": s3_prefix,
                        }
                        propose_result = invoke_lambda(
                            payload=propose_payload,
                            endpoint=aws_endpoint,
                            function_name=lambda_function,
                        )

                        if "error" in propose_result:
                            st.error(f"Proposal generation failed: {propose_result['error']}")
                            status.update(label="Proposal failed", state="error")
                        else:
                            st.session_state.proposal_data = propose_result
                            st.session_state.execution_result = None
                            status.update(label="Proposal ready for review", state="complete")
                            st.success("Assessment complete. Proposal ready for review below.")
                            st.cache_data.clear()
                            st.rerun()
                except Exception as e:
                    st.error(f"Lambda invocation failed: {e}")
                    st.info("Make sure Floci is running locally or AWS is configured.")
                    status.update(label="Failed", state="error")

    with col_a2:
        if st.button("Run Full Cycle (auto-approve)",
                      help="Runs full cycle: assess → analyze → propose → execute → verify. Auto-approves."):
            with st.status("Running full improvement cycle...", expanded=True) as status:
                try:
                    from ai_ready.cloud.demo import invoke_lambda

                    st.write("📥 Step 1: Invoking Lambda remediation (assess → improve → verify)...")
                    payload = {"action": "remediate"}
                    result = invoke_lambda(
                        payload=payload,
                        endpoint=aws_endpoint,
                        function_name=lambda_function,
                    )

                    if "error" in result:
                        st.error(f"Remediation failed: {result['error']}")
                        status.update(label="Remediation failed", state="error")
                    else:
                        delta = result.get("score_delta", 0)
                        outcome = result.get("verification_outcome", "unknown")
                        score_before = result.get("score_before", 0)
                        score_after = result.get("score_after", 0)
                        signals_resolved = result.get("signals_resolved", 0)

                        st.write(f"✅ Assessment complete: score {score_before}")
                        st.write(f"🔍 Analysis & proposal generated")
                        st.write(f"⚡ Execution applied {len(result.get('modified_uris', []))} modifications")
                        st.write(f"📊 Verification: {score_before} → {score_after} "
                                 f"({'+' if delta >= 0 else ''}{delta}), {outcome}")
                        st.write(f"🔧 Signals resolved: {signals_resolved}")

                        status.update(
                            label=f"Cycle complete: {score_before} → {score_after} ({'+' if delta >= 0 else ''}{delta})",
                            state="complete",
                        )
                        st.success(
                            f"{score_before} -> {score_after} "
                            f"({'+' if delta >= 0 else ''}{delta}), {outcome}"
                        )
                        with st.expander("Full Details"):
                            st.json(result)
                        st.cache_data.clear()
                        st.rerun()
                except Exception as e:
                    st.error(f"Lambda invocation failed: {e}")
                    st.info("Make sure Floci is running locally or AWS is configured.")
                    status.update(label="Failed", state="error")

    # --- Human Approval Section (shown when proposal_data is in session state) ---
    if st.session_state.get("proposal_data"):
        st.divider()
        st.markdown("### Human Approval Required")
        st.caption("Review the proposal below. Approve or reject to continue.")

        proposal = st.session_state.proposal_data
        app_id = proposal.get("app_id", "")

        col_p1, col_p2 = st.columns([2, 1])
        with col_p1:
            st.markdown(f"**Workflow ID**: `{app_id}`")
            score_before = proposal.get("score_before", 0)
            st.metric("Score Before", score_before)

            root_causes = proposal.get("root_causes", [])
            if root_causes:
                st.markdown("**Root Causes**")
                for rc in root_causes:
                    st.markdown(f"- {rc}")

            proposals = proposal.get("proposals", [])
            if proposals:
                st.markdown("**Proposed Remediation Steps**")
                for i, p in enumerate(proposals):
                    strategy = p.get("strategy", "unknown")
                    confidence = p.get("confidence", 0)
                    steps = p.get("modification_steps", [])
                    st.markdown(f"**Proposal {i+1}: {strategy}** (confidence: {confidence:.0%})")
                    if steps:
                        for step in steps:
                            step_type = step.get("step_type", "?")
                            artifact = step.get("artifact_uri", "?")
                            st.markdown(f"  - `{step_type}` on `{artifact}`")
                    reasoning = p.get("proposal_reasoning", "")
                    if reasoning:
                        with st.expander("Reasoning"):
                            st.markdown(reasoning)

        with col_p2:
            st.markdown("**Decision**")
            approved = st.checkbox("Approve proposal", value=True)
            reason = st.text_input("Reason", value="approved by user" if approved else "rejected by user")

            if st.button("Execute Decision", type="primary", key="approve_assessment"):
                with st.spinner("Executing approved proposal via Lambda..."):
                    try:
                        from ai_ready.cloud.demo import invoke_lambda
                        payload = {
                            "action": "execute",
                            "app_id": app_id,
                            "s3_bucket": s3_bucket,
                            "s3_prefix": s3_prefix,
                            "approved": approved,
                            "reason": reason,
                        }
                        result = invoke_lambda(
                            payload=payload,
                            endpoint=aws_endpoint,
                            function_name=lambda_function,
                        )
                        if "error" in result:
                            st.error(f"Execution failed: {result['error']}")
                        else:
                            st.session_state.execution_result = result
                            st.session_state.proposal_data = None
                            st.success("Execution complete.")
                            st.cache_data.clear()
                            st.rerun()
                    except Exception as e:
                        st.error(f"Lambda invocation failed: {e}")
                        st.info("Make sure Floci is running locally or AWS is configured.")

    # --- Verification Result (shown after execution) ---
    if st.session_state.get("execution_result"):
        st.divider()
        st.markdown("### Verification Result")

        result = st.session_state.execution_result
        score_before = result.get("score_before", 0)
        score_after = result.get("score_after", 0)
        delta = score_after - score_before
        outcome = result.get("verification_outcome", "unknown")
        signals_resolved = result.get("signals_resolved", 0)
        signals_new = result.get("new_signals", 0)

        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.metric("Score Before", score_before)
        with r2:
            st.metric("Score After", score_after, delta=delta)
        with r3:
            st.metric("Signals Resolved", signals_resolved)
        with r4:
            st.metric("Outcome", outcome)

        with st.expander("Full Result"):
            st.json(result)

        if st.button("Clear Result"):
            st.session_state.execution_result = None
            st.rerun()

    st.divider()

    # --- Problems (expandable cards, not dense tables) ---
    st.markdown("### Problems")
    problems = fetch_problems(_store_id)
    if problems:
        # Group by category
        by_category: dict[str, list[dict[str, Any]]] = {}
        for p in problems:
            cat = p.get("category", "unknown")
            by_category.setdefault(cat, []).append(p)

        for cat, cat_problems in sorted(by_category.items()):
            count = len(cat_problems)
            with st.expander(f"{cat} ({count} problem{'s' if count != 1 else ''})"):
                for p in cat_problems:
                    salience = p.get("salience", 0)
                    desc = p.get("description", "")
                    artifacts = p.get("artifact_uris", [])
                    st.markdown(
                        f"**Salience**: {salience:.3f} | "
                        f"**Artifacts**: {len(artifacts)}"
                    )
                    if desc:
                        st.caption(desc)
                    if artifacts:
                        st.caption(f"Files: {', '.join(artifacts[:3])}"
                                   + (f" +{len(artifacts)-3} more" if len(artifacts) > 3 else ""))
                    st.caption("---")
    else:
        st.caption("No problems discovered. Run an assessment first.")

    st.divider()

    # --- Signal Lifecycle ---
    st.markdown("### Signal Lifecycle")
    counts = fetch_lifecycle_counts(_store_id)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("New", counts["new"])
    with c2:
        st.metric("Persistent", counts["persistent"])
    with c3:
        st.metric("Resolved", counts["resolved"])
    with c4:
        st.metric("Recurring", counts["recurring"])

    st.divider()

    # --- Signals (grouped by type, expandable) ---
    signal_list = fetch_signals(_store_id)
    if signal_list:
        # Group by signal_type for compact display
        by_type: dict[str, list[dict[str, Any]]] = {}
        for s in signal_list:
            sig_type = s.get("signal_type", "unknown")
            by_type.setdefault(sig_type, []).append(s)

        st.markdown(f"### Signals ({len(signal_list)} total, {len(by_type)} types)")

        # Filters
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            sel_type = st.selectbox("Filter by type", ["All"] + sorted(by_type.keys()), key="sig_type")
        with col_f2:
            statuses = sorted(set(s.get("status", "new") for s in signal_list))
            sel_status = st.selectbox("Filter by status", ["All"] + statuses, key="sig_status")

        # Show grouped signal types as expandable cards
        types_to_show = sorted(by_type.keys()) if sel_type == "All" else [sel_type]
        for sig_type in types_to_show:
            type_sigs = by_type[sig_type]
            if sel_status != "All":
                type_sigs = [s for s in type_sigs if s.get("status") == sel_status]
            if not type_sigs:
                continue

            # Get unique artifacts for this signal type
            unique_artifacts = sorted(set(s.get("artifact_uri", "") for s in type_sigs))
            sample_rec = type_sigs[0].get("recommendation", "")

            with st.expander(f"🟡 {sig_type} ({len(type_sigs)} signals, {len(unique_artifacts)} artifacts)"):
                st.caption(sample_rec)
                if unique_artifacts:
                    st.markdown("**Affected artifacts:**")
                    for uri in unique_artifacts[:10]:
                        count_for_uri = sum(1 for s in type_sigs if s.get("artifact_uri") == uri)
                        st.markdown(f"- `{uri}` ({count_for_uri})")
                    if len(unique_artifacts) > 10:
                        st.caption(f"+{len(unique_artifacts) - 10} more artifacts")
    else:
        st.caption("No signals found.")

    st.divider()

    # --- Modified Artifacts ---
    st.markdown("### Modified Artifacts")
    modified = fetch_modified_artifacts(_store_id)
    if modified:
        st.dataframe(modified, use_container_width=True)
    else:
        st.caption("No modified artifacts tracked.")


# ---------------------------------------------------------------------------
# Tab 2: Artifact Explorer (Cursor-like inline view with deduplicated signals)
# ---------------------------------------------------------------------------

with tab_explorer:
    artifacts = fetch_artifacts_with_signals(_store_id)

    if not artifacts:
        st.caption("No artifacts with signals found. Run an assessment first.")
    else:
        col_list, col_content = st.columns([1, 3])

        with col_list:
            st.markdown("### Artifacts")
            st.caption(f"{len(artifacts)} artifacts with signals")

            # Severity filter
            all_severities = set()
            for a in artifacts:
                for s in a["signals"]:
                    all_severities.add(s.get("severity", ""))
            sel_severity = st.selectbox(
                "Filter by severity",
                ["All"] + sorted(all_severities),
                key="explorer_severity",
            )

            # Signal type filter
            all_types = set()
            for a in artifacts:
                for s in a["signals"]:
                    all_types.add(s.get("signal_type", ""))
            sel_sig_type = st.selectbox(
                "Filter by signal type",
                ["All"] + sorted(all_types),
                key="explorer_sig_type",
            )

            # Apply filters
            filtered_artifacts = []
            for a in artifacts:
                sigs = a["signals"]
                if sel_severity != "All":
                    sigs = [s for s in sigs if s.get("severity") == sel_severity]
                if sel_sig_type != "All":
                    sigs = [s for s in sigs if s.get("signal_type") == sel_sig_type]
                if sigs:
                    filtered_artifacts.append({
                        **a, "signals": sigs, "signal_count": len(sigs),
                    })

            # Show deduplicated signal count in label
            artifact_labels = []
            for a in filtered_artifacts:
                deduped = _deduplicate_signals_by_type(a["signals"])
                unique_types = len(deduped)
                artifact_labels.append(
                    f"{a['artifact_uri']} ({unique_types} types, {a['signal_count']} total)"
                )

            if not artifact_labels:
                st.caption("No artifacts match the filter.")
            else:
                sel_idx = st.selectbox(
                    "Select artifact",
                    range(len(artifact_labels)),
                    format_func=lambda i: artifact_labels[i],
                    key="explorer_artifact",
                )

                selected = filtered_artifacts[sel_idx]
                deduped_sigs = _deduplicate_signals_by_type(selected["signals"])
                st.markdown(f"**{len(deduped_sigs)} signal types on this artifact:**")
                for s in deduped_sigs:
                    sev = s.get("severity", "MEDIUM")
                    icon = {"CRITICAL": "🔴", "HIGH": "🟠",
                            "MEDIUM": "🟡", "LOW": "🔵"}.get(sev, "⚪")
                    count = s.get("count", 1)
                    count_str = f" (×{count})" if count > 1 else ""
                    st.markdown(
                        f"- {icon} `{s['signal_type']}`{count_str} — "
                        f"{s.get('recommendation', '')[:80]}"
                    )

        with col_content:
            if not artifact_labels:
                st.caption("Select an artifact to view.")
            else:
                selected = filtered_artifacts[sel_idx]
                uri = selected["artifact_uri"]
                content = selected["content"]
                signals = selected["signals"]

                st.markdown(f"### `{uri}`")
                deduped = _deduplicate_signals_by_type(signals)
                st.caption(f"{len(signals)} signals ({len(deduped)} unique types)")

                view_mode = st.radio(
                    "View mode",
                    ["Annotated Content", "Suggested Edits", "Manual Edit"],
                    horizontal=True,
                    key="explorer_mode",
                )

                if view_mode == "Annotated Content":
                    if content:
                        html = _render_annotated_content(content, signals)
                        st.markdown(html, unsafe_allow_html=True)
                    else:
                        st.warning("No content available for this artifact.")
                        st.info(
                            "The artifact may not have been persisted to "
                            "CockroachDB. Run an assessment first."
                        )

                elif view_mode == "Suggested Edits":
                    if content:
                        edit_html = _render_edit_suggestions(content, signals)
                        if edit_html:
                            st.markdown(edit_html, unsafe_allow_html=True)
                        else:
                            st.info("No automated edit suggestions available for these signal types.")
                        # Also show the annotated content below for context
                        st.markdown("---")
                        st.markdown("**Annotated Content:**")
                        html = _render_annotated_content(content, signals)
                        st.markdown(html, unsafe_allow_html=True)
                    else:
                        st.warning("No content available for this artifact.")

                elif view_mode == "Manual Edit":
                    if content:
                        edited = st.text_area(
                            "Edit content",
                            value=content,
                            height=500,
                            key=f"edit_{uri}",
                        )

                        col_save, col_download = st.columns(2)
                        with col_save:
                            if st.button(
                                "Save Changes",
                                type="primary",
                                key="save_edit",
                            ):
                                st.session_state[f"edited_{uri}"] = edited
                                st.success("Content saved to session state.")
                        with col_download:
                            st.download_button(
                                "Download",
                                data=edited,
                                file_name=uri.split("/")[-1],
                                mime="text/markdown",
                                key=f"download_{uri}",
                            )
                    else:
                        st.warning("No content available for editing.")


# ---------------------------------------------------------------------------
# Tab 3: Remediation (Approval Workflow)
# ---------------------------------------------------------------------------

with tab_remediation:
    # Session state
    if "proposal_data" not in st.session_state:
        st.session_state.proposal_data = None
    if "execution_result" not in st.session_state:
        st.session_state.execution_result = None

    # --- Step 1: Generate Proposal ---
    st.markdown("### Step 1 — Generate Proposal")
    st.caption("Runs assessment + LLM analysis + proposal generation. Halts at approval checkpoint.")

    if st.button("Generate Proposal", type="primary"):
        with st.status("Running assessment and generating proposal via Lambda...", expanded=True) as status:
            try:
                from ai_ready.cloud.demo import invoke_lambda
                payload = {
                    "action": "propose",
                    "s3_bucket": s3_bucket,
                    "s3_prefix": s3_prefix,
                }
                st.write("📥 Invoking Lambda (propose action)...")
                result = invoke_lambda(
                    payload=payload,
                    endpoint=aws_endpoint,
                    function_name=lambda_function,
                )
                if "error" in result:
                    st.error(f"Proposal generation failed: {result['error']}")
                    if "Unknown action" in str(result.get("error", "")):
                        st.info("The deployed Lambda function may be stale. Redeploy with the latest code.")
                    status.update(label="Proposal failed", state="error")
                else:
                    st.write(f"✅ Proposal generated: {len(result.get('proposals', []))} proposals")
                    st.write(f"📊 Score before: {result.get('score_before', 0)}")
                    st.write(f"🔍 Root causes: {len(result.get('root_causes', []))}")
                    st.session_state.proposal_data = result
                    st.session_state.execution_result = None
                    status.update(label="Proposal ready for review", state="complete")
                    st.success("Proposal generated. Review below.")
                    st.rerun()
            except Exception as e:
                st.error(f"Lambda invocation failed: {e}")
                st.info("Make sure Floci is running locally or AWS is configured.")
                status.update(label="Failed", state="error")

    # --- Step 2: Review Proposal ---
    if st.session_state.proposal_data:
        st.divider()
        st.markdown("### Step 2 — Review Proposal")

        proposal = st.session_state.proposal_data
        app_id = proposal.get("app_id", "")

        col_p1, col_p2 = st.columns([2, 1])
        with col_p1:
            st.markdown(f"**Workflow ID**: `{app_id}`")
            score_before = proposal.get("score_before", 0)
            st.metric("Score Before", score_before)

            root_causes = proposal.get("root_causes", [])
            if root_causes:
                st.markdown("**Root Causes**")
                for rc in root_causes:
                    st.markdown(f"- {rc}")

            proposals = proposal.get("proposals", [])
            if proposals:
                st.markdown("**Proposed Remediation Steps**")
                for i, p in enumerate(proposals):
                    strategy = p.get("strategy", "unknown")
                    confidence = p.get("confidence", 0)
                    steps = p.get("modification_steps", [])
                    st.markdown(f"**Proposal {i+1}: {strategy}** (confidence: {confidence:.0%})")
                    if steps:
                        for step in steps:
                            step_type = step.get("step_type", "?")
                            artifact = step.get("artifact_uri", "?")
                            st.markdown(f"  - `{step_type}` on `{artifact}`")
                    reasoning = p.get("proposal_reasoning", "")
                    if reasoning:
                        with st.expander("Reasoning"):
                            st.markdown(reasoning)

        with col_p2:
            st.markdown("**Decision**")
            approved = st.checkbox("Approve proposal", value=True)
            reason = st.text_input("Reason", value="approved by user" if approved else "rejected by user")

            if st.button("Execute Decision", type="primary"):
                with st.spinner("Executing approved proposal via Lambda..."):
                    try:
                        from ai_ready.cloud.demo import invoke_lambda
                        payload = {
                            "action": "execute",
                            "app_id": app_id,
                            "s3_bucket": s3_bucket,
                            "s3_prefix": s3_prefix,
                            "approved": approved,
                            "reason": reason,
                        }
                        result = invoke_lambda(
                            payload=payload,
                            endpoint=aws_endpoint,
                            function_name=lambda_function,
                        )
                        if "error" in result:
                            st.error(f"Execution failed: {result['error']}")
                        else:
                            st.session_state.execution_result = result
                            st.session_state.proposal_data = None
                            st.success("Execution complete.")
                            st.cache_data.clear()
                            st.rerun()
                    except Exception as e:
                        st.error(f"Lambda invocation failed: {e}")
                        st.info("Make sure Floci is running locally or AWS is configured.")

    # --- Step 3: Verification Result ---
    if st.session_state.execution_result:
        st.divider()
        st.markdown("### Step 3 — Verification Result")

        result = st.session_state.execution_result
        score_before = result.get("score_before", 0)
        score_after = result.get("score_after", 0)
        delta = score_after - score_before
        outcome = result.get("verification_outcome", "unknown")
        signals_resolved = result.get("signals_resolved", 0)
        signals_new = result.get("new_signals", 0)

        r1, r2, r3, r4 = st.columns(4)
        with r1:
            st.metric("Score Before", score_before)
        with r2:
            st.metric("Score After", score_after, delta=delta)
        with r3:
            st.metric("Signals Resolved", signals_resolved)
        with r4:
            st.metric("Outcome", outcome)

        with st.expander("Full Result"):
            st.json(result)

        if st.button("Clear Result"):
            st.session_state.execution_result = None
            st.rerun()

    # --- Remediation History ---
    st.divider()
    st.markdown("### Remediation History")
    outcomes = fetch_outcomes(_store_id, limit=50)
    if outcomes:
        try:
            import pandas as pd
            df = pd.DataFrame(outcomes)
            if "score_before" in df.columns and "score_after" in df.columns:
                st.line_chart(df[["score_before", "score_after"]], height=200)
        except Exception:
            pass

        st.dataframe(outcomes, use_container_width=True)

        total = len(outcomes)
        resolved = sum(1 for o in outcomes if o.get("verification_outcome") == "resolved")
        partial = sum(1 for o in outcomes if o.get("verification_outcome") == "partially_resolved")
        s1, s2, s3 = st.columns(3)
        with s1:
            st.metric("Total Outcomes", total)
        with s2:
            st.metric("Resolved", f"{resolved} ({resolved/total*100:.0f}%)" if total else "N/A")
        with s3:
            st.metric("Partially Resolved", f"{partial} ({partial/total*100:.0f}%)" if total else "N/A")
    else:
        st.caption("No remediation history. Run a cycle to populate.")

    # --- Workflow States ---
    st.divider()
    st.markdown("### Workflow States")
    workflows = fetch_workflows(_store_id)
    if workflows:
        status_counts: dict[str, int] = {}
        for w in workflows:
            status = w.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        w1, w2, w3 = st.columns(3)
        with w1:
            st.metric("Total Workflows", len(workflows))
        with w2:
            st.metric("Completed", status_counts.get("completed", 0))
        with w3:
            st.metric("Active/Paused", status_counts.get("active", 0) + status_counts.get("paused", 0))

        st.dataframe(workflows, use_container_width=True)
    else:
        st.caption("No workflows found.")


# ---------------------------------------------------------------------------
# Tab 4: Memory
# ---------------------------------------------------------------------------

with tab_memory:
    # A/B Memory Test
    st.markdown("### A/B Memory Test")
    st.caption("Compares LLM proposal generation WITH vs WITHOUT institutional memory from CockroachDB.")

    if st.button("Run A/B Memory Test", key="ab_test"):
        if not gateway:
            st.warning("No Groq LLM available. A/B test will use heuristic path only.")

        with st.spinner("Running A/B test..."):
            try:
                from ai_ready.improvement.actions import run_memory_ab_test
                from ai_ready.models import KnowledgeSignal
                from ai_ready.storage.adapters import CockroachHistoryStore
                from burr.core import State

                ab_store = CockroachHistoryStore(store)

                signals = fetch_signals(_store_id)[:5]
                ks_signals = [
                    KnowledgeSignal(
                        signal_id=s["signal_id"],
                        collector_id=s["collector_id"],
                        artifact_uri=s["artifact_uri"],
                        signal_type=s["signal_type"],
                        evidence={},
                    )
                    for s in signals
                ]

                ab_state = State({
                    "root_cause_analysis": [
                        {"category": "missing_metadata", "hypothesis": "test", "confidence": 0.8},
                    ],
                    "knowledge_problems": [],
                    "affected_artifact_uris": [s["artifact_uri"] for s in signals[:3]],
                    "prior_failures": [],
                    "assessment_id": "",
                    "prior_diagnosis": {},
                    "prior_strategy": {},
                    "prior_verification": {},
                    "decision_trace": {},
                    "assessment_summary": "A/B test from dashboard.",
                    "cumulative_tokens": 0,
                    "problem_saliences": [],
                    "prior_modification_steps": [],
                    "systemic_clusters": [],
                    "assessment_signals": ks_signals,
                })

                ab_result = run_memory_ab_test(
                    ab_state,
                    llm_gateway=gateway,
                    history_store=ab_store,
                    assessment_store=None,
                )

                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**With Memory**")
                    st.json(ab_result.get("with_memory", {}))
                with col2:
                    st.markdown("**Without Memory**")
                    st.json(ab_result.get("without_memory", {}))

                st.metric("Differs", str(ab_result.get("differs", "N/A")))
                if ab_result.get("explanation"):
                    st.info(ab_result["explanation"])
            except Exception as e:
                st.error(f"A/B test failed: {e}")

    st.divider()

    # EMA Strategy Scores
    st.markdown("### EMA Strategy Scores")
    try:
        from ai_ready.storage.adapters import CockroachHistoryStore
        chs = CockroachHistoryStore(store)
        metrics = chs.get_remediation_metrics()
        st.json(metrics)
    except Exception as e:
        st.caption(f"Metrics unavailable: {e}")

    st.divider()

    # Memory Influence
    st.markdown("### Memory Influence (Recent Runs)")
    outcomes = fetch_outcomes(_store_id, limit=10)
    if outcomes:
        for o in outcomes:
            label = f"{o.get('issue_type', '?')} -> {o.get('verification_outcome', '?')} ({o.get('strategy', '?')})"
            with st.expander(label):
                st.json(o)
    else:
        st.caption("No remediation outcomes found.")

    st.divider()

    # Pause/Resume
    st.markdown("### Pause/Resume")
    st.caption(
        "Workflow state survives process restart via CockroachDB. "
        "Each row represents a workflow that can be resumed from any stage."
    )

    workflows = fetch_workflows(_store_id)
    if workflows:
        st.dataframe(workflows, use_container_width=True)
    else:
        st.caption("No workflows found. Run an assessment or improvement cycle first.")

    if st.button("Run Pause/Resume via Lambda", key="pause_resume"):
        with st.status("Running pause/resume demo via Lambda...", expanded=True) as status:
            try:
                from ai_ready.cloud.demo import invoke_lambda

                st.write("Step 1: Invoking Lambda assessment (start workflow)...")
                assess_payload = {
                    "action": "assess",
                    "s3_bucket": s3_bucket,
                    "s3_prefix": s3_prefix,
                }
                assess_result = invoke_lambda(
                    payload=assess_payload,
                    endpoint=aws_endpoint,
                    function_name=lambda_function,
                )
                if "error" in assess_result:
                    st.error(f"Assessment failed: {assess_result['error']}")
                    status.update(label="Assessment failed", state="error")
                else:
                    st.write(f"✅ Assessment: score {assess_result.get('score', '?')}, "
                             f"{assess_result.get('signal_count', 0)} signals")

                    st.write("Step 2: Invoking Lambda remediation (resume & complete)...")
                    remediate_payload = {"action": "remediate"}
                    remediate_result = invoke_lambda(
                        payload=remediate_payload,
                        endpoint=aws_endpoint,
                        function_name=lambda_function,
                    )
                    if "error" in remediate_result:
                        st.error(f"Remediation failed: {remediate_result['error']}")
                        status.update(label="Remediation failed", state="error")
                    else:
                        delta = remediate_result.get("score_delta", 0)
                        outcome = remediate_result.get("verification_outcome", "unknown")
                        st.write(f"✅ Remediation: {remediate_result.get('score_before', 0)} -> "
                                 f"{remediate_result.get('score_after', 0)} "
                                 f"({'+' if delta >= 0 else ''}{delta}), {outcome}")
                        status.update(label="Pause/Resume complete", state="complete")
                        st.success(
                            f"Remediation complete: {remediate_result.get('score_before', 0)} -> "
                            f"{remediate_result.get('score_after', 0)} "
                            f"({'+' if delta >= 0 else ''}{delta}), {outcome}"
                        )

                    st.cache_data.clear()
                    st.rerun()
            except Exception as e:
                st.error(f"Lambda pause/resume failed: {e}")
                st.info("Make sure Floci is running locally or AWS is configured.")
                status.update(label="Failed", state="error")


# ---------------------------------------------------------------------------
# Tab 5: Technical
# ---------------------------------------------------------------------------

with tab_tech:
    # MCP Tools
    st.markdown("### MCP Tools (Live CockroachDB Views)")
    get_mcp_store()
    from ai_ready.cloud.mcp_server import MCP_QUERY_CATALOG, handle_tool_call

    tool_names = [t["name"] for t in MCP_QUERY_CATALOG]
    sel_tool = st.selectbox("Select MCP Tool", tool_names, key="mcp_tool")

    tool_entry = next((t for t in MCP_QUERY_CATALOG if t["name"] == sel_tool), None)
    if tool_entry:
        st.caption(tool_entry["description"])
        if tool_entry.get("view"):
            st.code(f"View: {tool_entry['view']}")

    params: dict[str, Any] = {}
    if sel_tool == "search_artifacts_semantic":
        query = st.text_input("Search Query", "FastAPI tutorial", key="mcp_query")
        params["query"] = query
    elif sel_tool == "get_decision_trace":
        outcome_id = st.text_input("Outcome ID", "", key="mcp_outcome")
        params["outcome_id"] = outcome_id
    elif sel_tool == "get_remediation_context":
        issue_type = st.text_input("Issue Type", "", key="mcp_issue")
        params["issue_type"] = issue_type
    elif sel_tool == "get_health_trend":
        days = st.number_input("Days", 1, 365, 30, key="mcp_days")
        params["days"] = days

    limit = st.slider("Limit", 1, 100, 10, key="mcp_limit")
    params["limit"] = limit

    if st.button("Execute Query", key="mcp_exec"):
        with st.spinner("Querying CockroachDB..."):
            result = handle_tool_call(sel_tool, params)
            st.json(result)

    st.divider()

    # Cross-Check Diagnostics
    st.markdown("### Cross-Check Diagnostics")
    st.caption("Deterministic cross-collector pattern detection (5 rules).")

    try:
        from ai_ready.improvement.cross_checks import run_cross_checks
        from ai_ready.models import KnowledgeSignal, KnowledgeAssessment, DimensionScore

        signal_list = fetch_signals(_store_id)
        if signal_list:
            ks_list = [
                KnowledgeSignal(
                    signal_id=s["signal_id"],
                    collector_id=s["collector_id"],
                    artifact_uri=s["artifact_uri"],
                    signal_type=s["signal_type"],
                    evidence={},
                )
                for s in signal_list
            ]

            data = fetch_overview(_store_id)
            latest = data["latest"]
            score = latest.get("score", 50)
            dimensions = {}
            for dim_name in ["retrieval", "context", "consistency", "trust", "connectivity", "workflow"]:
                dimensions[dim_name] = DimensionScore(name=dim_name, score=score)

            assessment = KnowledgeAssessment(
                assessment_id=latest.get("assessment_id", "dashboard"),
                score=score,
                signals=ks_list,
                dimensions=dimensions,
            )

            results = run_cross_checks(assessment)
            if results:
                for r in results:
                    with st.expander(f"{r.name} — Severity: {r.severity}"):
                        st.markdown(f"**Description**: {r.description}")
                        st.markdown(f"**AI Impact**: {r.ai_impact}")
                        st.markdown(f"**Recommendation**: {r.recommendation}")
                        st.markdown(f"**Affected Artifacts**: {len(r.affected_artifact_uris)}")
                        if hasattr(r, "to_dict"):
                            st.json(r.to_dict())
            else:
                st.caption("No cross-check patterns detected.")
        else:
            st.caption("No signals to analyze.")
    except Exception as e:
        st.caption(f"Cross-check diagnostics unavailable: {e}")

    st.divider()

    # AWS Event Chain Proof
    st.markdown("### AWS Event Chain Proof")
    st.caption("Proves the full event chain: S3 upload -> EventBridge -> Lambda -> CockroachDB.")

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        ev_bucket = st.text_input("S3 Bucket", "knowledge-base", key="ev_bucket")
    with col_s2:
        ev_key = st.text_input("S3 Key", "test-trigger/test.md", key="ev_key")

    ev_endpoint = st.text_input("Floci/AWS Endpoint", "http://localhost:4566", key="ev_endpoint")
    ev_function = st.text_input("Lambda Function", "ai-knowledge-assess", key="ev_function")

    if st.button("Trigger S3 Assessment", key="s3_trigger"):
        with st.status("Running AWS event chain test...", expanded=True) as status:
            try:
                from ai_ready.cloud.demo import trigger_s3_assessment
                st.write("📤 Uploading test file to S3...")
                result = trigger_s3_assessment(
                    bucket=ev_bucket,
                    key=ev_key,
                    endpoint=ev_endpoint,
                    function_name=ev_function,
                    cockroach_store=store,
                )

                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("S3 Upload", "Pass" if result.get("s3_upload") else "Fail")
                with col2:
                    lambda_ok = "error" not in result.get("lambda_response", {})
                    st.metric("Lambda", "Pass" if lambda_ok else "Fail")
                with col3:
                    st.metric("CockroachDB", "Pass" if result.get("cockroach_verified") else "Fail")
                with col4:
                    st.metric("Chain Complete", "Pass" if result.get("chain_complete") else "Fail")

                if result.get("chain_complete"):
                    st.write("✅ Full AWS event chain proven: S3 → Lambda → CockroachDB")
                    status.update(label="AWS event chain complete", state="complete")
                else:
                    st.write("⚠️ Chain incomplete. See details below.")
                    status.update(label="Chain incomplete", state="error")

                st.json(result)
            except Exception as e:
                st.error(f"AWS event chain failed: {e}")
                st.info("This is expected when Floci/AWS is not running locally.")
                status.update(label="Failed", state="error")

    st.divider()

    # Architecture
    st.markdown("### Architecture")
    st.code("""S3 Bucket (docs) -> EventBridge (trigger) -> Lambda (assess/remediate) -> CockroachDB (memory)
                                                            -> Groq LLM (proposal)""")

    st.markdown("| Component | Technology | Purpose |")
    st.markdown("|---|---|---|")
    st.markdown("| Knowledge Source | S3 Bucket | Markdown documentation storage |")
    st.markdown("| Event Trigger | EventBridge | S3 upload -> Lambda trigger |")
    st.markdown("| Execution | AWS Lambda | Assessment + remediation cycles |")
    st.markdown("| LLM | Groq (llama-3.3-70b) | Proposal reasoning |")
    st.markdown("| Persistent Memory | CockroachDB Cloud | Signals, assessments, outcomes, vectors |")
    st.markdown("| MCP | CockroachDB Managed MCP | External agent access (read-only views) |")
    st.markdown("| Dashboard | Streamlit Cloud | Live monitoring and demo URL |")

    try:
        db_url = os.environ.get("COCKROACH_DB_URL", "")
        if db_url:
            safe_url = db_url.split("@")[-1] if "@" in db_url else "configured"
            st.caption(f"Connected to: {safe_url}")
    except Exception:
        pass
