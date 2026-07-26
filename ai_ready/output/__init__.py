"""Output formatters - terminal, JSON, SARIF."""

from __future__ import annotations

import json
import sys
from typing import Any

from ai_ready.models import RegressionReport, Snapshot


def format_terminal(snapshot: Snapshot) -> str:
    """Format snapshot as human-readable terminal output."""
    lines: list[str] = []
    lines.append("")
    lines.append("AI Readiness Scan")
    lines.append("=" * 60)
    lines.append(f"  Snapshot ID:  {snapshot.snapshot_id}")
    lines.append(f"  Source:        {snapshot.metadata.get('source', 'N/A')}")
    lines.append(f"  Documents:     {snapshot.metadata.get('document_count', 0)}")
    if snapshot.metadata.get("relation_count"):
        lines.append(f"  Relations:     {snapshot.metadata.get('relation_count', 0)}")
    lines.append(f"  Git Commit:   {snapshot.metadata.get('git_commit', 'N/A')}")
    lines.append("")
    lines.append(f"  Overall Score: {snapshot.score}/100")
    lines.append("")
    lines.append("  Dimensions:")
    for name, dim in sorted(snapshot.dimensions.items()):
        lines.append(f"    {name:20s} {dim.score:>3}/100  ({dim.findings_count} findings)")
    lines.append("")

    explanation = snapshot.explain_score()
    if snapshot.findings:
        lines.append("  Why this score:")
        lines.append(f"    {explanation.summary}")
        for contributor in explanation.dominant_contributors[:5]:
            gain = contributor.estimated_score_gain
            lines.append(
                f"    - {contributor.cause} [{contributor.dimension}/{contributor.rule_id}] "
                f"{contributor.finding_count} finding(s), "
                f"{contributor.document_count} doc(s), up to +{gain:.1f} score"
            )
            if contributor.sample_findings:
                sample = contributor.sample_findings[0]
                lines.append(
                    f"      Example: {sample['document_path']} "
                    f"({sample['finding_id']})"
                )
        lines.append("")

    if snapshot.findings:
        lines.append(f"  Findings ({len(snapshot.findings)}):")
        lines.append("")

        # Group by severity
        severity_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
        for sev in severity_order:
            sev_findings = [f for f in snapshot.findings if f.severity.value == sev]
            if not sev_findings:
                continue
            lines.append(f"  [{sev}] ({len(sev_findings)})")
            for f in sev_findings[:20]:  # Limit output
                lines.append(f"    {f.document_path}:{f.line} - {f.rule_id}")
                lines.append(f"      {f.recommendation}")
                if f.evidence:
                    for k, v in list(f.evidence.items())[:3]:
                        lines.append(f"      {k}: {v}")
            if len(sev_findings) > 20:
                lines.append(f"    ... and {len(sev_findings) - 20} more")
            lines.append("")
    else:
        lines.append("  No findings. Knowledge base is AI-ready.")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_json(snapshot: Snapshot) -> str:
    """Format snapshot as JSON."""
    return json.dumps(snapshot.to_dict(), indent=2, default=str)


def format_sarif(snapshot: Snapshot) -> str:
    """Format snapshot as SARIF (Static Analysis Results Interchange Format)."""
    sarif: dict[str, Any] = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/SARIF/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ai-ready",
                        "version": "0.2.0",
                        "informationUri": "https://github.com/jacks/ai-ready",
                        "rules": [
                            {
                                "id": f.rule_id,
                                "name": f.rule_id.replace("_", " ").title(),
                                "shortDescription": {"text": f.recommendation[:200]},
                            }
                            for f in snapshot.findings
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": f.rule_id,
                        "level": _severity_to_sarif_level(f.severity.value),
                        "message": {"text": f.recommendation},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {
                                        "uri": f.document_path,
                                    },
                                    "region": {"startLine": f.line} if f.line else {},
                                }
                            }
                        ],
                        "partialFingerprints": {
                            "primaryLocationLineHash": f.finding_id,
                        },
                    }
                    for f in snapshot.findings
                ],
            }
        ],
    }
    return json.dumps(sarif, indent=2)


def format_regression_terminal(report: RegressionReport) -> str:
    """Format regression report as human-readable terminal output."""
    lines: list[str] = []
    lines.append("")
    lines.append("AI Readiness Diff")
    lines.append("=" * 60)
    lines.append(f"  Previous: {report.prev_snapshot_id} (score: {report.prev_score})")
    lines.append(f"  Current:  {report.curr_snapshot_id} (score: {report.curr_score})")
    lines.append(f"  Score:    {report.prev_score} -> {report.curr_score} ({report.score_delta:+d})")
    lines.append("")

    if report.explanation:
        lines.append(f"  Summary: {report.explanation}")
        lines.append("")

    if report.dimension_deltas:
        lines.append("  Dimension Changes:")
        for name, delta in sorted(report.dimension_deltas.items()):
            indicator = "[+]" if delta > 0 else "[!]" if delta < 0 else "   "
            lines.append(f"    {indicator} {name:20s} {delta:+d}")
        lines.append("")

    if report.score_change_explanation:
        regressions = report.score_change_explanation.get("top_regressions", [])
        improvements = report.score_change_explanation.get("top_improvements", [])
        if regressions or improvements:
            lines.append("  Why the score changed:")
            for change in regressions[:3]:
                lines.append(
                    f"    [!] {change['cause']} "
                    f"({change['prev_findings']} -> {change['curr_findings']} findings, "
                    f"{change['delta_estimated_score_gain']:+.1f} score pressure)"
                )
            for change in improvements[:3]:
                lines.append(
                    f"    [+] {change['cause']} "
                    f"({change['prev_findings']} -> {change['curr_findings']} findings, "
                    f"{change['delta_estimated_score_gain']:+.1f} score pressure)"
                )
            lines.append("")

    if report.new_findings:
        lines.append(f"  New Issues ({len(report.new_findings)}):")
        for f in report.new_findings[:20]:
            lines.append(f"    * {f.rule_id}: {f.document_path}")
        if len(report.new_findings) > 20:
            lines.append(f"    ... and {len(report.new_findings) - 20} more")
        lines.append("")

    if report.resolved_findings:
        lines.append(f"  Resolved Issues ({len(report.resolved_findings)}):")
        for f in report.resolved_findings[:20]:
            lines.append(f"    [OK] {f.rule_id}: {f.document_path}")
        if len(report.resolved_findings) > 20:
            lines.append(f"    ... and {len(report.resolved_findings) - 20} more")
        lines.append("")

    if report.persistent_findings:
        lines.append(f"  Persistent Issues: {len(report.persistent_findings)}")
        lines.append("")

    if report.severity_changes:
        lines.append(f"  Severity Changes ({len(report.severity_changes)}):")
        for sc in report.severity_changes[:10]:
            direction = "[!]" if sc["direction"] == "worse" else "[+]"
            lines.append(f"    {direction} {sc['rule_id']}: {sc['document_path']} "
                         f"({sc['prev_severity']} -> {sc['curr_severity']})")
        lines.append("")

    if report.new_high_count:
        lines.append(f"  [!] {report.new_high_count} new high-severity findings")

    if report.increased_contradictions:
        lines.append(f"  [!] Contradictions increased by {report.increased_contradictions}")
    if report.increased_topic_entropy:
        lines.append(f"  [!] Topic entropy increased by {report.increased_topic_entropy}")
    if report.new_orphan_documents:
        lines.append(f"  [!] {report.new_orphan_documents} new orphan documents")
    if report.new_duplicate_clusters:
        lines.append(f"  [!] {report.new_duplicate_clusters} new duplicate clusters")

    lines.append("")
    if report.recommendation:
        lines.append(f"  Recommendation: {report.recommendation}")
    lines.append("=" * 60)
    return "\n".join(lines)


def format_regression_json(report: RegressionReport) -> str:
    """Format regression report as JSON."""
    return json.dumps(report.to_dict(), indent=2, default=str)


def _severity_to_sarif_level(severity: str) -> str:
    """Map severity to SARIF level."""
    mapping = {
        "CRITICAL": "error",
        "HIGH": "error",
        "MEDIUM": "warning",
        "LOW": "note",
        "INFO": "none",
    }
    return mapping.get(severity, "warning")
