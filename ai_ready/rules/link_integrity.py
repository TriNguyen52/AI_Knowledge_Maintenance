"""Link Integrity rule - detect broken, redirect, and orphan links.

Uses document relationships from the KnowledgeBase to avoid false-positive
orphan detection. A document is only an orphan if it is not linked from any
other document AND not present in the navigation hierarchy.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ai_ready.models import Finding, KnowledgeBase, RuleResult, Severity
from ai_ready.rules import Rule, register_rule


@register_rule
class LinkIntegrityRule(Rule):
    """Check internal links for broken references and detect orphan documents."""

    id = "link_integrity"
    severity_default = Severity.MEDIUM
    dimension = "connectivity"

    def __init__(self) -> None:
        self._source_path: str | None = None

    def collect(self, kb: KnowledgeBase) -> dict[str, Any]:
        """Collect all links and check their targets. Use relations for orphan detection."""
        all_links: list[dict[str, Any]] = []
        doc_paths = {doc.path for doc in kb.documents}
        doc_dirs = {os.path.dirname(doc.path) for doc in kb.documents}

        for doc in kb.documents:
            for link in doc.links:
                if not link.is_internal:
                    continue

                target = link.target
                # Strip anchors
                anchor = ""
                if "#" in target:
                    target, _, anchor = target.partition("#")

                # Strip query params
                if "?" in target:
                    target = target.split("?")[0]

                if not target:
                    # Pure anchor link - skip
                    continue

                # Check if target exists relative to doc's directory and source root
                doc_dir = os.path.dirname(doc.path)
                resolved = self._resolve_link(target, doc_dir, doc_paths)

                all_links.append({
                    "doc_id": doc.id,
                    "doc_path": doc.path,
                    "link_text": link.text,
                    "link_target": link.target,
                    "clean_target": target,
                    "anchor": anchor,
                    "line": link.line,
                    "exists": resolved is not None,
                    "resolved_path": resolved,
                    "is_orphan": False,
                })

        # Check if the link target matches a document path
        for al in all_links:
            if al["clean_target"] in doc_paths:
                al["resolved_path"] = al["clean_target"]

        # Compute orphan documents using BOTH inline links AND relations
        # A document is orphan if no other document links to it AND it has no
        # parent in the navigation hierarchy
        linked_via_links = {al["resolved_path"] for al in all_links if al["resolved_path"]}
        linked_via_relations = {r.target_path for r in kb.relations if r.relation_type in ("parent_child", "cross_reference", "navigation")}

        all_linked = linked_via_links | linked_via_relations

        orphan_docs = []
        for doc in kb.documents:
            is_linked = doc.path in all_linked
            # Also check if any relation has this doc as a source (it's a parent, so it's reachable)
            is_parent = any(r.source_path == doc.path for r in kb.relations if r.relation_type in ("parent_child", "navigation"))
            if not is_linked and not is_parent and len(kb.documents) > 1:
                orphan_docs.append({"doc_id": doc.id, "doc_path": doc.path})

        return {
            "all_links": all_links,
            "orphan_docs": orphan_docs,
            "total_docs": len(kb.documents),
            "total_relations": len(kb.relations),
        }

    def measure(self, signals: dict[str, Any]) -> dict[str, Any]:
        """Count broken links and orphan documents."""
        broken = [l for l in signals["all_links"] if not l["exists"]]
        orphans = signals["orphan_docs"]

        return {
            "broken_links": broken,
            "orphan_documents": orphans,
            "total_links": len(signals["all_links"]),
            "total_docs": signals["total_docs"],
            "total_relations": signals["total_relations"],
            "broken_count": len(broken),
            "orphan_count": len(orphans),
        }

    def evaluate(self, metrics: dict[str, Any]) -> list[Finding]:
        """Create findings for broken links and orphan documents.

        Emits bare findings with issue_type only. Severity, score, and
        recommendation are assigned by the EvaluationPolicy in the pipeline.
        """
        findings: list[Finding] = []

        # Group broken links by document
        broken_by_doc: dict[str, list[dict[str, Any]]] = {}
        for link in metrics["broken_links"]:
            broken_by_doc.setdefault(link["doc_id"], []).append(link)

        for doc_id, links in broken_by_doc.items():
            doc_path = links[0]["doc_path"]
            findings.append(Finding(
                rule_id=self.id,
                issue_type="broken_link",
                severity=Severity.LOW,  # Placeholder; overridden by policy
                score=0,  # Placeholder; overridden by policy
                document_id=doc_id,
                document_path=doc_path,
                evidence={
                    "broken_links": [
                        {"target": l["link_target"], "line": l["line"], "text": l["link_text"]}
                        for l in links
                    ],
                    "broken_link_count": len(links),
                },
                recommendation="",  # Filled by policy
            ))

        # Orphan documents
        for orphan in metrics["orphan_documents"]:
            findings.append(Finding(
                rule_id=self.id,
                issue_type="orphan",
                severity=Severity.LOW,  # Placeholder; overridden by policy
                score=0,  # Placeholder; overridden by policy
                document_id=orphan["doc_id"],
                document_path=orphan["doc_path"],
                evidence={"orphan": True},
                recommendation="",  # Filled by policy
            ))

        return findings

    def report(self) -> RuleResult:
        findings = self._findings
        broken_count = self._metrics.get("broken_count", 0)
        orphan_count = self._metrics.get("orphan_count", 0)
        total_links = self._metrics.get("total_links", 0)
        total_relations = self._metrics.get("total_relations", 0)

        score = max(0, 100 - broken_count * 5 - orphan_count * 2)
        severity = Severity.HIGH if broken_count > 5 else (Severity.MEDIUM if findings else Severity.LOW)

        return RuleResult(
            rule_id=self.id,
            score=score,
            severity=severity,
            metrics={
                "broken_links": broken_count,
                "orphan_documents": orphan_count,
                "total_links": total_links,
                "total_relations": total_relations,
            },
            findings=findings,
            recommendation=f"{broken_count} broken links, {orphan_count} orphan documents" if findings else "All links resolve correctly",
        )

    def _resolve_link(self, target: str, doc_dir: str, known_paths: set[str]) -> str | None:
        """Try to resolve a relative link target."""
        # Normalize path separators
        target = target.replace("\\", "/")
        doc_dir = doc_dir.replace("\\", "/")

        # Try relative to document directory
        candidate = os.path.normpath(os.path.join(doc_dir, target)).replace("\\", "/")
        if candidate in known_paths:
            return candidate

        # Try as-is
        if target in known_paths:
            return target

        # Try without leading ./
        if target.startswith("./"):
            clean = target[2:]
            if clean in known_paths:
                return clean

        # Try with .md extension
        if not target.endswith((".md", ".mdx", ".txt", ".rst")):
            for ext in [".md", ".mdx", ".txt", ".rst"]:
                candidate = target + ext
                if candidate in known_paths:
                    return candidate
                candidate = os.path.normpath(os.path.join(doc_dir, target + ext)).replace("\\", "/")
                if candidate in known_paths:
                    return candidate

        return None
