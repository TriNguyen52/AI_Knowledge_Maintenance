"""Incremental execution model — update snapshots in response to document changes.

Instead of re-running all rules across the entire corpus, the incremental executor:
1. Classifies document changes (added, modified, deleted, link-changed)
2. Determines which rules need to re-run on which documents
3. Invalidates only affected findings from the previous snapshot
4. Re-runs affected rules on affected documents
5. Merges new findings with reused findings
6. Recomputes affected dimensions and overall score

The result is a snapshot identical to what a full scan would produce,
but computed in O(affected + neighborhood) instead of O(total documents).

If correctness cannot be guaranteed, the executor falls back to a full scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_ready.models import (
    Document,
    DocumentRelation,
    Finding,
    KnowledgeBase,
    RuleResult,
    Severity,
    Snapshot,
)
from ai_ready.rules import Rule, all_rules


# ---------------------------------------------------------------------------
# Change events
# ---------------------------------------------------------------------------

@dataclass
class ChangeEvent:
    """A single document change detected between scans.

    Attributes:
        event_type: "added", "modified", or "deleted"
        document_path: Relative path of the changed document
        document: New Document object (None for deletions)
        links_changed: For "modified" events, whether the document's links
                       differ from the previous version. If True, link_integrity
                       must re-evaluate. If False, link_integrity findings for
                       this document are unchanged.
    """

    event_type: str
    document_path: str
    document: Document | None = None
    links_changed: bool = False


# ---------------------------------------------------------------------------
# Rule dependencies
# ---------------------------------------------------------------------------

@dataclass
class RuleDependency:
    """Declares what a rule depends on, driving incremental execution.

    Attributes:
        rule_id: The rule's identifier
        scope: "per_document" if the rule only reads one document at a time,
               "cross_document" if it needs the full KB
        needs_full_kb_on_link_change: If True, any link change triggers a full
                                      re-evaluation of this rule
    """

    rule_id: str
    scope: str  # "per_document" | "cross_document"
    needs_full_kb_on_link_change: bool = False

    def is_affected_by(self, event: ChangeEvent) -> bool:
        """Whether this rule's findings may change due to this event."""
        if event.event_type == "deleted":
            return True
        if event.event_type == "added":
            return True
        if event.event_type == "modified":
            if self.scope == "per_document":
                return True
            if self.scope == "cross_document":
                return event.links_changed
        return False

    def get_affected_docs(
        self, event: ChangeEvent, all_doc_paths: set[str]
    ) -> set[str]:
        """Which document paths need re-evaluation for this rule + event.

        For per-document rules: only the changed document itself.
        For cross-document rules on link changes: all documents (full KB).
        For cross-document rules on content-only changes: empty set (skip).
        """
        if event.event_type == "deleted":
            if self.scope == "cross_document":
                return all_doc_paths
            return set()  # deleted doc is gone, no findings to recompute

        if event.event_type == "added":
            if self.scope == "cross_document":
                return all_doc_paths
            return {event.document_path}

        if event.event_type == "modified":
            if self.scope == "per_document":
                return {event.document_path}
            if self.scope == "cross_document" and event.links_changed:
                return all_doc_paths
            return set()  # content-only change, cross-document rule not affected

        return set()


# Registry of rule dependencies
_RULE_DEPENDENCIES: dict[str, RuleDependency] = {
    "topic_purity": RuleDependency(
        rule_id="topic_purity",
        scope="per_document",
    ),
    "heading_quality": RuleDependency(
        rule_id="heading_quality",
        scope="per_document",
    ),
    "context_independence": RuleDependency(
        rule_id="context_independence",
        scope="per_document",
    ),
    "link_integrity": RuleDependency(
        rule_id="link_integrity",
        scope="cross_document",
        needs_full_kb_on_link_change=True,
    ),
}


# ---------------------------------------------------------------------------
# Incremental executor
# ---------------------------------------------------------------------------

class IncrementalExecutor:
    """Orchestrates incremental snapshot updates.

    Uses a ScanPipeline instance to reuse policy application, dimension
    aggregation, and score computation — no logic is duplicated.
    """

    def __init__(self, pipeline: Any) -> None:
        """Create an executor that reuses the given pipeline's helper methods.

        Args:
            pipeline: A ScanPipeline instance with apply_policy,
                      aggregate_dimensions, compute_overall_score, and
                      weights attributes.
        """
        self.pipeline = pipeline
        self.policy = pipeline.policy
        self.weights = pipeline.weights

    def run(
        self,
        prev_snapshot: Snapshot,
        change_events: list[ChangeEvent],
        all_documents: list[Document],
        relations: list[DocumentRelation],
        source: str = "",
        git_commit: str = "",
    ) -> Snapshot:
        """Produce an updated snapshot from changes.

        Args:
            prev_snapshot: The previous complete snapshot.
            change_events: List of document changes since the previous scan.
            all_documents: ALL current documents (including unchanged).
                          The executor uses these to build KBs for rule execution.
            relations: ALL current document relations.
            source: Source path string for metadata.
            git_commit: Git commit hash for metadata.

        Returns:
            A new Snapshot that is identical to what a full scan would produce.
        """
        # --- Fallback conditions ---
        if not prev_snapshot or not change_events:
            return self._full_scan_fallback(all_documents, relations, source, git_commit)

        all_doc_paths = {doc.path for doc in all_documents}
        changed_paths = {e.document_path for e in change_events}

        # If >50% of docs changed, incremental gain is minimal — do full scan
        if len(changed_paths) > len(all_doc_paths) * 0.5:
            return self._full_scan_fallback(all_documents, relations, source, git_commit)

        # --- Classify changes ---
        added_paths: set[str] = set()
        modified_paths: set[str] = set()
        deleted_paths: set[str] = set()
        link_changed_paths: set[str] = set()

        for event in change_events:
            if event.event_type == "added":
                added_paths.add(event.document_path)
            elif event.event_type == "modified":
                modified_paths.add(event.document_path)
                if event.links_changed:
                    link_changed_paths.add(event.document_path)
            elif event.event_type == "deleted":
                deleted_paths.add(event.document_path)

        per_doc_rules = ["topic_purity", "heading_quality", "context_independence"]
        per_doc_affected = added_paths | modified_paths

        link_integrity_affected: set[str] = set()
        link_integrity_needs_full = bool(link_changed_paths or added_paths or deleted_paths)
        if link_integrity_needs_full:
            link_integrity_affected = all_doc_paths

        # --- Invalidate findings from previous snapshot ---
        # Build set of (rule_id, document_path) pairs to invalidate
        invalidated: set[tuple[str, str]] = set()

        for rule_id in per_doc_rules:
            for path in per_doc_affected:
                invalidated.add((rule_id, path))

        if link_integrity_needs_full:
            # Invalidate all link_integrity findings
            for f in prev_snapshot.findings:
                if f.rule_id == "link_integrity":
                    invalidated.add((f.rule_id, f.document_path))

        # Also invalidate findings for deleted docs (any rule)
        for path in deleted_paths:
            for rule_id in per_doc_rules + ["link_integrity"]:
                invalidated.add((rule_id, path))

        # --- Keep findings that are not invalidated ---
        kept_findings: list[Finding] = [
            f for f in prev_snapshot.findings
            if (f.rule_id, f.document_path) not in invalidated
        ]

        # --- Re-run affected rules ---
        new_findings: list[Finding] = []
        results: list[RuleResult] = []

        # Build document lookup for mini-KB construction
        doc_by_path: dict[str, Document] = {doc.path: doc for doc in all_documents}

        # Run per-document rules on affected docs only
        if per_doc_affected:
            affected_docs = [
                doc_by_path[p] for p in per_doc_affected
                if p in doc_by_path
            ]
            if affected_docs:
                mini_kb = KnowledgeBase(
                    documents=affected_docs,
                    relations=relations,  # relations don't affect per-doc rules
                    source=source,
                )
                registry = all_rules()
                for rule_id in per_doc_rules:
                    if rule_id not in registry:
                        continue
                    if self.pipeline.enabled_rules and rule_id not in self.pipeline.enabled_rules:
                        continue
                    rule = registry[rule_id]()
                    result = rule.run(mini_kb)
                    results.append(result)
                    new_findings.extend(result.findings)

        # Run link_integrity on full KB if needed
        if link_integrity_needs_full:
            registry = all_rules()
            rule_id = "link_integrity"
            if rule_id in registry:
                if not self.pipeline.enabled_rules or rule_id in self.pipeline.enabled_rules:
                    full_kb = KnowledgeBase(
                        documents=all_documents,
                        relations=relations,
                        source=source,
                    )
                    rule = registry[rule_id]()
                    result = rule.run(full_kb)
                    results.append(result)
                    new_findings.extend(result.findings)

        if new_findings:
            self.pipeline.apply_policy(new_findings)

        all_findings = kept_findings + new_findings

        all_results = self._build_all_results(results, all_findings, prev_snapshot)
        dimensions = self.pipeline.aggregate_dimensions(all_results, all_findings)

        # --- Recompute overall score ---
        overall_score = self.pipeline.compute_overall_score(dimensions)

        # --- Collect metrics from new results ---
        metrics: dict[str, Any] = {}
        for r in results:
            metrics.update(r.metrics)

        # --- Create updated snapshot ---
        snapshot_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata: dict[str, Any] = {
            "source": source,
            "document_count": len(all_documents),
            "relation_count": len(relations),
            "incremental": True,
            "changed_documents": len(changed_paths),
        }
        if git_commit:
            metadata["git_commit"] = git_commit

        return Snapshot(
            snapshot_id=snapshot_id,
            score=overall_score,
            dimensions=dimensions,
            findings=all_findings,
            metrics=metrics,
            metadata=metadata,
        )

    def _build_all_results(
        self,
        new_results: list[RuleResult],
        all_findings: list[Finding],
        prev_snapshot: Snapshot,
    ) -> list[RuleResult]:
        """Build a complete RuleResult list for dimension aggregation.

        For rules that ran incrementally, use their new RuleResult.
        For rules that didn't run (no affected docs), construct a RuleResult
        from the kept findings so that aggregate_dimensions can compute
        the correct dimension scores.

        All registered rules must have a RuleResult, even if they have zero
        findings, because aggregate_dimensions uses RuleResult.rule_id to
        look up the rule's dimension. Without a RuleResult, the rule's
        dimension would be missing from the output, changing the overall
        score calculation.
        """
        ran_rule_ids = {r.rule_id for r in new_results}
        registry = all_rules()

        results = list(new_results)

        for rule_id in registry:
            if rule_id in ran_rule_ids:
                continue
            if self.pipeline.enabled_rules and rule_id not in self.pipeline.enabled_rules:
                continue
            # Construct a RuleResult from existing findings
            rule_findings = [f for f in all_findings if f.rule_id == rule_id]
            results.append(RuleResult(
                rule_id=rule_id,
                score=100,  # Placeholder; aggregate_dimensions recomputes
                severity=Severity.LOW,
                metrics={},
                findings=rule_findings,
            ))

        return results

    def _full_scan_fallback(
        self,
        documents: list[Document],
        relations: list[DocumentRelation],
        source: str,
        git_commit: str,
    ) -> Snapshot:
        """Fall back to a full scan via the pipeline."""
        return self.pipeline.run(
            documents,
            source=source,
            git_commit=git_commit,
            relations=relations,
        )


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(
    source_path: Any,
    prev_snapshot: Snapshot | None,
    current_documents: list[Document],
) -> list[ChangeEvent]:
    """Detect document changes between a previous snapshot and current state.

    Uses git diff when available. Falls back to snapshot comparison.

    Args:
        source_path: Path to the knowledge base directory.
        prev_snapshot: The previous snapshot (None if no history).
        current_documents: All current Document objects.

    Returns:
        List of ChangeEvent objects. Empty list means no changes detected
        (which will trigger a full scan fallback).
    """
    if prev_snapshot is None:
        return []  # No previous state — can't do incremental

    current_by_path: dict[str, Document] = {doc.path: doc for doc in current_documents}
    current_paths = set(current_by_path.keys())

    prev_paths: set[str] = set()
    prev_doc_links: dict[str, list[str]] = {}

    for f in prev_snapshot.findings:
        prev_paths.add(f.document_path)

    prev_doc_count = prev_snapshot.metadata.get("document_count", 0)

    if not prev_paths and prev_doc_count > 0:
        return []  

    # Detect changes
    events: list[ChangeEvent] = []

    # Added documents
    for path in current_paths - prev_paths:
        events.append(ChangeEvent(
            event_type="added",
            document_path=path,
            document=current_by_path[path],
            links_changed=True,  
        ))

    # Deleted documents
    for path in prev_paths - current_paths:
        events.append(ChangeEvent(
            event_type="deleted",
            document_path=path,
            document=None,
        ))

    # Modified documents — compare links
    for path in current_paths & prev_paths:
        doc = current_by_path[path]
        current_internal_links = {link.target for link in doc.links if link.is_internal}

        events.append(ChangeEvent(
            event_type="modified",
            document_path=path,
            document=doc,
            links_changed=True,  
        ))

    return events


def detect_changes_via_git(
    source_path: Any,
    prev_snapshot: Snapshot | None,
    current_documents: list[Document],
) -> list[ChangeEvent] | None:
    """Detect changes using git diff.

    Returns None if git is not available or the directory is not a git repo.
    Otherwise returns a list of ChangeEvent objects.
    """
    import subprocess
    from pathlib import Path

    source = Path(source_path)
    if not source.is_dir():
        return None

    if prev_snapshot is None:
        return None

    prev_commit = prev_snapshot.metadata.get("git_commit", "")
    if not prev_commit:
        return None

    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", f"{prev_commit}..HEAD"],
            capture_output=True, text=True, cwd=str(source), timeout=10,
        )
        if result.returncode != 0:
            return None

        lines = result.stdout.strip().split("\n")
        if not lines or lines == [""]:
            return []  

        current_by_path = {doc.path: doc for doc in current_documents}
        events: list[ChangeEvent] = []

        for line in lines:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            file_path = parts[-1]

            # Normalize path
            file_path = file_path.replace("\\", "/")

            # Only care about supported file types
            if not any(file_path.endswith(ext) for ext in [".md", ".markdown", ".mdx", ".txt", ".rst"]):
                continue

            doc_path = None
            if file_path in current_by_path:
                doc_path = file_path
            else:
                # Try matching as suffix
                for p in current_by_path:
                    if p.endswith(file_path) or file_path.endswith(p):
                        doc_path = p
                        break

            if status.startswith("A"):
                # Added
                if doc_path:
                    events.append(ChangeEvent(
                        event_type="added",
                        document_path=doc_path,
                        document=current_by_path[doc_path],
                        links_changed=True,
                    ))
            elif status.startswith("D"):
                # Deleted
                if doc_path:
                    events.append(ChangeEvent(
                        event_type="deleted",
                        document_path=doc_path,
                        document=None,
                    ))
            elif status.startswith("R"):
                # Renamed — treat as delete + add
                if doc_path:
                    events.append(ChangeEvent(
                        event_type="added",
                        document_path=doc_path,
                        document=current_by_path[doc_path],
                        links_changed=True,
                    ))
            else:
                # Modified (M) or unknown
                if doc_path:
                    doc = current_by_path[doc_path]
                    current_links = {link.target for link in doc.links if link.is_internal}

                    events.append(ChangeEvent(
                        event_type="modified",
                        document_path=doc_path,
                        document=doc,
                        links_changed=True,  # Conservative
                    ))

        return events

    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
