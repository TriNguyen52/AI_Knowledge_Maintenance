"""Heading Quality rule — evaluate whether headings provide strong retrieval signals."""

from __future__ import annotations

import re
from typing import Any

from ai_ready.models import Finding, KnowledgeBase, RuleResult, Severity
from ai_ready.rules import Rule, register_rule

# Generic/vague headings that provide weak retrieval signals
GENERIC_HEADINGS = {
    "overview", "introduction", "background", "summary", "conclusion",
    "details", "miscellaneous", "other", "notes", "additional",
    "appendix", "reference", "references", "resources", "help",
    "getting started", "setup", "configuration", "settings",
    "general", "info", "information", "about", "more",
}

# Placeholder headings
PLACEHOLDER_HEADINGS = {"todo", "tbd", "placeholder", "coming soon", "wip", "n/a", "none"}

# Single-word headings that are too short to be useful (excluding common good ones)
GOOD_SINGLE_WORDS = {"api", "sdk", "cli", "faq", "changelog", "graphql", "rest", "grpc", "css", "html", "sql"}


@register_rule
class HeadingQualityRule(Rule):
    """Detect vague, numbered-only, placeholder, or too-short headings."""

    id = "heading_quality"
    severity_default = Severity.MEDIUM
    dimension = "retrieval"

    def collect(self, kb: KnowledgeBase) -> dict[str, Any]:
        """Extract all headings with quality signals."""
        all_headings: list[dict[str, Any]] = []
        for doc in kb.documents:
            for heading in doc.headings:
                text = heading.text.strip()
                text_lower = text.lower()

                issue_type = None
                severity = Severity.LOW

                # Check placeholder
                if text_lower in PLACEHOLDER_HEADINGS:
                    issue_type = "placeholder"
                    severity = Severity.CRITICAL

                # Check numbered-only (e.g., "Step 1", "1.", "2. Setup" is ok)
                elif re.match(r"^(step\s+)?\d+$", text_lower):
                    issue_type = "numbered_only"
                    severity = Severity.CRITICAL

                # Check generic
                elif text_lower in GENERIC_HEADINGS:
                    issue_type = "generic"
                    severity = Severity.MEDIUM

                # Check too short (single word, not in good list)
                elif len(text.split()) == 1 and text_lower not in GOOD_SINGLE_WORDS and len(text) < 4:
                    issue_type = "too_short"
                    severity = Severity.MEDIUM

                if issue_type:
                    all_headings.append({
                        "doc_id": doc.id,
                        "doc_path": doc.path,
                        "heading_text": text,
                        "heading_level": heading.level,
                        "heading_line": heading.line,
                        "issue_type": issue_type,
                        "severity": severity,
                    })

        return {"bad_headings": all_headings}

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Count heading quality issues by type."""
        by_type: dict[str, int] = {}
        for h in signals["bad_headings"]:
            by_type[h["issue_type"]] = by_type.get(h["issue_type"], 0) + 1

        return {
            "bad_headings": signals["bad_headings"],
            "by_type": by_type,
            "total_issues": len(signals["bad_headings"]),
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[Finding]:
        """Create findings for each bad heading.

        Emits bare findings with issue_type only. Severity, score, and
        recommendation are assigned by the EvaluationPolicy in the pipeline.
        """
        findings: list[Finding] = []
        for h in metrics["bad_headings"]:
            findings.append(Finding(
                rule_id=self.id,
                issue_type=h["issue_type"],
                severity=Severity.LOW,  # Placeholder; overridden by policy
                score=0,  # Placeholder; overridden by policy
                document_id=h["doc_id"],
                document_path=h["doc_path"],
                evidence={
                    "heading": h["heading_text"],
                    "level": h["heading_level"],
                    "issue_type": h["issue_type"],
                },
                recommendation="",  # Filled by policy
                line=h["heading_line"],
            ))
        return findings

    def report(self) -> RuleResult:
        findings = self._findings
        by_type = self._metrics.get("by_type", {})
        total = self._metrics.get("total_issues", 0)

        # Placeholder score; pipeline recomputes from assessed findings
        score = max(0, 100 - total * 5)
        severity = Severity.HIGH if any(f.issue_type in ("placeholder", "numbered_only") for f in findings) else (
            Severity.MEDIUM if findings else Severity.LOW
        )

        return RuleResult(
            rule_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "total_heading_issues": total,
                "by_type": by_type,
            },
            findings=findings,
            recommendation=f"{total} heading quality issues ({', '.join(f'{k}: {v}' for k, v in by_type.items())})" if findings else "All headings provide strong retrieval signals",
        )

    def _make_recommendation(self, issue_type: str, heading: str) -> str:
        """Generate a specific recommendation for the heading issue."""
        if issue_type == "generic":
            return f"Replace generic heading '{heading}' with a specific, descriptive heading (e.g., 'Configure OAuth Authentication' instead of 'Configuration')."
        elif issue_type == "numbered_only":
            return f"Replace numbered-only heading '{heading}' with a descriptive heading that includes the action or topic."
        elif issue_type == "placeholder":
            return f"Replace placeholder heading '{heading}' with actual content heading."
        elif issue_type == "too_short":
            return f"Heading '{heading}' is too short. Use a more descriptive heading (2+ words recommended)."
        return f"Improve heading '{heading}' for better AI retrieval."


# Collector alias (Phase 11 naming migration)
HeadingQualityCollector = HeadingQualityRule
