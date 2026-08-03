"""Workflow Completeness collector — detect incomplete procedural guides.

Flags artifacts that describe a multi-step workflow (tutorial, how-to,
setup guide) but are missing expected structural elements:
1. Prerequisites section
2. Step numbering (ordered steps)
3. Verification/testing step
4. Troubleshooting or error handling section

This is a deterministic, LLM-free collector using heading and content analysis.
"""

from __future__ import annotations

import re
from typing import Any

from ai_ready.models import (
    ArtifactBundle,
    CollectorResult,
    KnowledgeSignal,
    Severity,
)
from ai_ready.rules import SignalCollector, register_collector

# Keywords that identify workflow/how-to artifacts
_WORKFLOW_KEYWORDS = [
    "tutorial", "how to", "getting started", "setup guide",
    "quickstart", "quick start", "step by step", "walkthrough",
    "installation guide", "configuration guide",
]

# Heading patterns for expected sections
_PREREQUISITES_PATTERNS = [
    r"prerequis", r"requirement", r"before you start", r"what you need",
]
_STEPS_PATTERN = r"(?:^|\n)\s*(?:#{1,6}\s+)?(?:\d+\.|step\s+\d+|step\s*\d)"
_VERIFY_PATTERNS = [
    r"verif", r"test", r"check", r"confirm", r"validate", r"troubleshoot",
    r"error", r"debug", r"common issue", r"faq",
]


@register_collector
class WorkflowCompletenessCollector(SignalCollector):
    """Flag workflow artifacts missing expected structural elements."""

    id = "workflow_completeness"
    severity_default = Severity.LOW
    dimension = "workflow"
    scope = "per_artifact"

    def collect(self, bundle: ArtifactBundle) -> dict[str, Any]:
        """Identify workflow artifacts and check for structural completeness."""
        workflow_artifacts: list[dict[str, Any]] = []

        for artifact in bundle.artifacts:
            text = artifact.all_text if hasattr(artifact, "all_text") else ""
            if not text:
                text = " ".join(p.text for p in artifact.paragraphs)
            text_lower = text.lower()

            # Check if this artifact is a workflow/how-to guide
            is_workflow = any(kw in text_lower for kw in _WORKFLOW_KEYWORDS)
            # Also check title
            title_lower = (artifact.title or "").lower()
            is_workflow = is_workflow or any(kw in title_lower for kw in _WORKFLOW_KEYWORDS)

            if not is_workflow:
                continue

            # Check for prerequisites section
            has_prereqs = any(
                re.search(pat, text_lower)
                for pat in _PREREQUISITES_PATTERNS
            )

            # Check for numbered steps
            step_matches = re.findall(_STEPS_PATTERN, text, re.MULTILINE | re.IGNORECASE)
            has_steps = len(step_matches) >= 2  # At least 2 numbered steps

            # Check for verification/troubleshooting
            has_verify = any(
                re.search(pat, text_lower)
                for pat in _VERIFY_PATTERNS
            )

            # Check for code blocks (expected in technical tutorials)
            has_code = len(artifact.code_blocks) > 0 if hasattr(artifact, "code_blocks") else False

            missing: list[str] = []
            if not has_prereqs:
                missing.append("prerequisites")
            if not has_steps:
                missing.append("numbered_steps")
            if not has_verify:
                missing.append("verification_step")
            if not has_code:
                missing.append("code_examples")

            workflow_artifacts.append({
                "artifact_id": artifact.id,
                "artifact_uri": artifact.uri,
                "title": artifact.title,
                "has_prerequisites": has_prereqs,
                "has_numbered_steps": has_steps,
                "has_verification": has_verify,
                "has_code_examples": has_code,
                "step_count": len(step_matches),
                "missing_elements": missing,
            })

        return {
            "workflow_artifacts": workflow_artifacts,
            "total_artifacts": len(bundle.artifacts),
        }

    def measure(self, metrics: dict[str, Any]) -> dict[str, Any]:
        """Count incomplete workflows."""
        incomplete = [w for w in metrics["workflow_artifacts"] if w["missing_elements"]]
        complete = [w for w in metrics["workflow_artifacts"] if not w["missing_elements"]]

        return {
            "incomplete_workflows": incomplete,
            "complete_workflows": complete,
            "incomplete_count": len(incomplete),
            "complete_count": len(complete),
            "total_workflows": len(metrics["workflow_artifacts"]),
            "total_artifacts": metrics["total_artifacts"],
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[KnowledgeSignal]:
        """Emit signals for incomplete workflow artifacts."""
        signals: list[KnowledgeSignal] = []

        for workflow in metrics["incomplete_workflows"]:
            signals.append(KnowledgeSignal(
                collector_id=self.id,
                signal_type="incomplete_workflow",
                severity=Severity.LOW,
                score=0,
                artifact_id=workflow["artifact_id"],
                artifact_uri=workflow["artifact_uri"],
                evidence={
                    "missing_elements": workflow["missing_elements"],
                    "has_prerequisites": workflow["has_prerequisites"],
                    "has_numbered_steps": workflow["has_numbered_steps"],
                    "has_verification": workflow["has_verification"],
                    "has_code_examples": workflow["has_code_examples"],
                    "step_count": workflow["step_count"],
                },
                recommendation="",
            ))

        return signals

    def report(self) -> CollectorResult:
        incomplete = self._metrics.get("incomplete_count", 0)
        total_workflows = self._metrics.get("total_workflows", 0)
        total = self._metrics.get("total_artifacts", 1)
        if total_workflows == 0:
            score = 100
        else:
            score = max(0, int(100 * (total_workflows - incomplete) / max(total_workflows, 1)))
        severity = (
            Severity.HIGH if incomplete > total_workflows * 0.5
            else Severity.MEDIUM if incomplete > 0
            else Severity.LOW
        )
        return CollectorResult(
            collector_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "incomplete_workflows": incomplete,
                "complete_workflows": self._metrics.get("complete_count", 0),
                "total_workflows": total_workflows,
                "total_artifacts": total,
            },
            signals=self._findings,
            recommendation=(
                f"{incomplete}/{total_workflows} workflow(s) missing structural elements"
                if incomplete
                else f"All {total_workflows} workflow(s) are complete"
                if total_workflows
                else "No workflow artifacts found"
            ),
        )
