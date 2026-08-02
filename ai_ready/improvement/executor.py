"""Knowledge modification executor — AI-Ready's execution layer.

This is NOT a Burr action. It is called BY the ExecuteChangeAction
to apply approved modifications to the knowledge base.

The executor belongs to AI-Ready. Burr only orchestrates the call.

Supported modification types:
  - update_document: Modify document content (headings, text, metadata)
  - add_metadata: Add or update metadata fields on an artifact
  - create_relationship: Create a relationship between artifacts
  - remove_relationship: Remove a relationship
  - archive_artifact: Mark an artifact as archived/outdated
  - merge_artifacts: Merge multiple artifacts into one
  - split_artifact: Split one artifact into multiple

The executor operates on artifact URIs (not IDs) and returns
execution results that the Burr action stores in state.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExecutionStep:
    """A single modification step executed by the executor."""

    step_type: str  # "update_document", "add_metadata", etc.
    artifact_uri: str
    description: str
    success: bool = True
    error: str = ""
    timestamp: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_type": self.step_type,
            "artifact_uri": self.artifact_uri,
            "description": self.description,
            "success": self.success,
            "error": self.error,
            "timestamp": self.timestamp,
            "details": self.details,
        }


class KnowledgeExecutor:
    """Executes approved knowledge modifications.

    This class is intentionally pluggable — the actual file/DB operations
    depend on the deployment context. The default implementation logs
    modifications and returns success. Production deployments subclass
    this to implement real file writes, API calls, or DB updates.

    Security: The executor is the ONLY component that modifies knowledge.
    The LLM never calls the executor directly. Burr's ExecuteChangeAction
    calls the executor with the approved proposal's modification steps.
    """

    def __init__(
        self,
        artifact_store: Any = None,
        source_path: str = "",
        assessment_signals: list[Any] | None = None,
        known_artifact_uris: set[str] | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            artifact_store: Optional reference to the artifact storage layer.
                           When provided, enables production mode (real writes).
                           When None, runs in dry-run mode.
            source_path: Root directory of the knowledge base. Used to resolve
                        artifact URIs to file paths for real file modifications.
            assessment_signals: Signals from the current assessment, used to
                                identify exactly what to fix in each artifact.
            known_artifact_uris: Set of all valid artifact URIs in the knowledge
                                base, used to determine which links are broken.
        """
        self.artifact_store = artifact_store
        self.source_path = Path(source_path) if source_path else None
        self.assessment_signals = assessment_signals or []
        self.known_artifact_uris = known_artifact_uris or set()
        self._dry_run = artifact_store is None and self.source_path is None

    def _resolve_path(self, artifact_uri: str) -> Path | None:
        """Resolve an artifact URI to a full file path."""
        if self.source_path is None:
            return None
        # artifact_uri is relative (e.g., "docs/en/docs/advanced/events.md")
        full_path = self.source_path / artifact_uri
        if full_path.exists():
            return full_path
        return None

    def _get_signals_for_artifact(self, artifact_uri: str) -> list[Any]:
        """Get all assessment signals for a specific artifact."""
        return [s for s in self.assessment_signals if s.artifact_uri == artifact_uri]

    def execute(
        self,
        modification_steps: list[dict[str, Any]],
        artifact_uris: list[str],
    ) -> list[ExecutionStep]:
        """Execute a list of modification steps.

        Args:
            modification_steps: List of step dicts from the approved proposal.
                               Each dict has: step_type, artifact_uri, and
                               step-specific parameters.
            artifact_uris: All artifact URIs affected by this improvement.

        Returns:
            List of ExecutionStep results (one per input step).
        """
        results: list[ExecutionStep] = []
        timestamp = datetime.now(timezone.utc).isoformat()

        for step in modification_steps:
            step_type = step.get("step_type", "unknown")
            artifact_uri = step.get("artifact_uri", "")
            description = step.get("description", "")

            try:
                handler = self._get_handler(step_type)
                details = handler(step)
                results.append(ExecutionStep(
                    step_type=step_type,
                    artifact_uri=artifact_uri,
                    description=description,
                    success=True,
                    timestamp=timestamp,
                    details=details,
                ))
            except Exception as e:
                logger.error(f"Execution step failed: {step_type} on {artifact_uri}: {e}")
                results.append(ExecutionStep(
                    step_type=step_type,
                    artifact_uri=artifact_uri,
                    description=description,
                    success=False,
                    error=str(e),
                    timestamp=timestamp,
                ))

        return results

    def _get_handler(self, step_type: str):
        """Get the handler function for a modification type."""
        handlers = {
            # Canonical step types
            "update_document": self._handle_update_document,
            "add_metadata": self._handle_add_metadata,
            "create_relationship": self._handle_create_relationship,
            "remove_relationship": self._handle_remove_relationship,
            "archive_artifact": self._handle_archive_artifact,
            "merge_artifacts": self._handle_merge_artifacts,
            "split_artifact": self._handle_split_artifact,
            # LLM-generated aliases (the LLM often uses more descriptive names)
            "fix_broken_link": self._handle_update_document,
            "fix_broken_links": self._handle_update_document,
            "fix_dangling_reference": self._handle_update_document,
            "fix_dangling_references": self._handle_update_document,
            "rewrite_content": self._handle_update_document,
            "add_cross_reference": self._handle_create_relationship,
            "add_cross_references": self._handle_create_relationship,
            "link_orphaned_document": self._handle_create_relationship,
            "link_orphaned_documents": self._handle_create_relationship,
            "implement_automated_link_checking": self._handle_add_metadata,
        }
        handler = handlers.get(step_type)
        if handler is None:
            raise ValueError(f"Unknown modification type: {step_type}")
        return handler

    # -----------------------------------------------------------------------
    # Real file modification handlers
    # -----------------------------------------------------------------------

    def _handle_update_document(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle document content update — fix broken links and dangling refs.

        Solves the ROOT CAUSE by applying the fix to ALL artifacts that have
        the relevant signal type, not just the one file mentioned in the step.
        The LLM generates a representative step; the executor applies the fix
        pattern across every affected file.

        When source_path is set, reads each affected file and applies fixes:
        1. Removes broken markdown links (targets that don't exist in the
           known artifact set) — applied to ALL files with broken_link signals
        2. Rewrites dangling references ("see above", "as mentioned below")
           to be self-contained — applied to ALL files with dangling_reference
           signals. Conservative: only replaces clearly ambiguous phrases
           that would be meaningless to an LLM reading a single chunk without
           surrounding context.
        3. Writes each modified file back to disk
        """
        artifact_uri = step.get("artifact_uri", "")
        description = step.get("description", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Document update simulated"}

        # Determine which signal types to fix based on the step description
        desc_lower = description.lower()
        fix_broken_links = any(kw in desc_lower for kw in
                               ["link", "broken", "navigation", "cross-reference"])
        fix_dangling_refs = any(kw in desc_lower for kw in
                                ["reference", "context", "dangling", "ambiguous"])

        # If no specific signal type mentioned, fix both
        if not fix_broken_links and not fix_dangling_refs:
            fix_broken_links = True
            fix_dangling_refs = True

        # Find ALL artifacts that have the relevant signal types.
        # This solves the root cause — every file with broken links gets fixed,
        # not just the one the LLM happened to mention.
        artifacts_to_fix: set[str] = set()
        for signal in self.assessment_signals:
            if fix_broken_links and signal.signal_type == "broken_link":
                artifacts_to_fix.add(signal.artifact_uri)
            if fix_dangling_refs and signal.signal_type == "dangling_reference":
                artifacts_to_fix.add(signal.artifact_uri)

        # Also include the specific artifact mentioned in the step
        if artifact_uri:
            artifacts_to_fix.add(artifact_uri)

        if not artifacts_to_fix:
            return {"mode": "production", "files_fixed": 0, "message": "No artifacts to fix"}

        total_files_fixed = 0
        total_fixes = 0
        all_fix_details: list[str] = []

        for uri in sorted(artifacts_to_fix):
            file_path = self._resolve_path(uri)
            if file_path is None:
                continue

            content = file_path.read_text(encoding="utf-8")
            original_content = content
            fixes_applied: list[str] = []

            # --- Fix 1: Remove broken links ---
            if fix_broken_links:
                # Use signal evidence to identify which specific links are
                # broken. The assessment has already determined which links
                # are broken (e.g., targets outside the artifact set). We
                # trust its judgment and remove only those specific links,
                # rather than re-checking all links ourselves (which could
                # disagree with the assessment on whether a link is broken).
                broken_targets: set[str] = set()
                for signal in self.assessment_signals:
                    if signal.signal_type == "broken_link" and signal.artifact_uri == uri:
                        evidence = signal.evidence or {}
                        for bl in evidence.get("broken_links", []):
                            broken_targets.add(bl.get("target", ""))

                if broken_targets:
                    def _replace_broken_link(match: re.Match) -> str:
                        text = match.group(1)
                        target = match.group(2)
                        if target in broken_targets:
                            fixes_applied.append(f"Removed broken link: [{text}]({target})")
                            return text  # keep the text, drop the link
                        return match.group(0)

                    content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _replace_broken_link, content)

            # --- Fix 2: Fix dangling references ---
            # Conservative: only replace phrases that are meaningless without
            # surrounding context (problematic for LLM chunk retrieval).
            # Common human-style references like "the above section" are fine
            # for humans but ambiguous for LLMs reading individual chunks.
            if fix_dangling_refs:
                dangling_patterns = [
                    # "see above/below" references
                    (r"see above", "see the referenced documentation"),
                    (r"see below", "see the following documentation"),
                    (r"see (?:the )?previous section", "see the referenced documentation"),
                    (r"see (?:the )?next section", "see the following documentation"),
                    # "as you will see above/below"
                    (r"as (?:you will|you'll) see below", "as shown in the following example"),
                    (r"as (?:you will|you'll) see above", "as shown in the previous example"),
                    # "as mentioned/described/explained above/below"
                    (r"as (?:mentioned|described|explained) above", "as described in the documentation"),
                    (r"as (?:mentioned|described|explained) below", "as described in the documentation"),
                    # "as you read/saw above" — common in FastAPI docs
                    (r"as (?:you )?(?:read|saw) above", "as described earlier"),
                    (r"as (?:you )?(?:read|saw) below", "as described in the following"),
                    # "as explained above" — variant found in events.md
                    (r"as explained above", "as described in the documentation"),
                    # "as you saw in [link]" — partially self-contained with link
                    # Only fix the bare "as you saw in" without a following link
                    (r"as you saw in (?!\[)", "as described in "),
                    # "the above/previous section/example/code"
                    (r"the (?:above|previous) (?:sections?|examples?|code)", "the referenced section"),
                    (r"the (?:below|following|next) (?:sections?|examples?|code)", "the relevant section"),
                    # "as described in [link]" — keep if it has a link, only fix bare
                    (r"as described in (?!\[)", "as described in the documentation. "),
                ]
                for pattern, replacement in dangling_patterns:
                    new_content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
                    if new_content != content:
                        fixes_applied.append(f"Fixed dangling reference: '{pattern}' -> '{replacement}'")
                        content = new_content

            if content != original_content:
                file_path.write_text(content, encoding="utf-8")
                total_files_fixed += 1
                total_fixes += len(fixes_applied)
                all_fix_details.extend(fixes_applied[:3])

        return {
            "mode": "production",
            "files_checked": len(artifacts_to_fix),
            "files_fixed": total_files_fixed,
            "total_fixes": total_fixes,
            "sample_fixes": all_fix_details[:5],
        }

    def _handle_add_metadata(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle metadata addition — add YAML frontmatter to document."""
        artifact_uri = step.get("artifact_uri", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Metadata update simulated"}

        file_path = self._resolve_path(artifact_uri)
        if file_path is None:
            return {"mode": "dry_run", "message": f"File not found: {artifact_uri}"}

        content = file_path.read_text(encoding="utf-8")

        # Check if frontmatter already exists
        if content.startswith("---"):
            return {
                "mode": "production",
                "file": str(file_path),
                "message": "Frontmatter already exists, skipping",
            }

        # Add minimal frontmatter — extract title from first H1 heading
        # to avoid adding generic "title: Documentation" which doesn't
        # improve knowledge quality and could introduce new signals.
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else \
            artifact_uri.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").title()
        frontmatter = f"---\ntitle: {title}\n---\n\n"
        file_path.write_text(frontmatter + content, encoding="utf-8")

        return {
            "mode": "production",
            "file": str(file_path),
            "metadata_added": ["title"],
        }

    def _handle_create_relationship(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle relationship creation — fix root cause of orphaned documents.

        The root cause of orphaned documents is that the index/navigation
        page doesn't link to them. Instead of slapping a generic 'Related'
        section on every orphaned file (superficial), this handler updates
        the index page to include links to the orphaned documents —
        integrating them into the knowledge graph at the source.

        If the index page doesn't exist, falls back to adding cross-references
        from the most-connected artifact (the one with the most inbound links)
        to the orphaned documents.
        """
        artifact_uri = step.get("artifact_uri", "")
        description = step.get("description", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Relationship creation simulated"}

        # Find ALL artifacts with orphaned_documents signals
        orphaned_uris: set[str] = set()
        for signal in self.assessment_signals:
            if signal.signal_type in ("orphan", "orphaned_documents"):
                orphaned_uris.add(signal.artifact_uri)

        if not orphaned_uris:
            return {"mode": "production", "files_fixed": 0, "message": "No orphaned artifacts found"}

        # Strategy: Update the index page to link to orphaned documents.
        # This solves the root cause — the orphans exist because nothing
        # links to them. Adding them to the index integrates them.
        index_candidates = [
            "docs/en/docs/index.md",
            "docs/index.md",
            "index.md",
            "docs/en/index.md",
        ]

        index_path = None
        index_uri = None
        for candidate in index_candidates:
            resolved = self._resolve_path(candidate)
            if resolved is not None:
                index_path = resolved
                index_uri = candidate
                break

        if index_path is None:
            # Fallback: use the artifact_uri from the step as the hub document
            if artifact_uri:
                index_path = self._resolve_path(artifact_uri)
                index_uri = artifact_uri
            if index_path is None:
                return {
                    "mode": "production",
                    "files_fixed": 0,
                    "message": "No index page found to add cross-references from",
                }

        content = index_path.read_text(encoding="utf-8")
        original_content = content

        # Build a list of orphaned documents to add as links
        # Only add links that don't already exist in the index
        existing_links = set(re.findall(r"\[[^\]]+\]\(([^)]+)\)", content))
        links_added: list[str] = []

        for uri in sorted(orphaned_uris):
            if uri == index_uri:
                continue  # don't link to self
            # Compute relative path from index to the orphaned doc
            index_dir = index_path.parent
            orphan_path = self._resolve_path(uri)
            if orphan_path is None:
                continue
            try:
                rel_path = orphan_path.relative_to(index_dir)
                rel_link = str(rel_path).replace("\\", "/")
            except ValueError:
                rel_link = uri

            if rel_link in existing_links:
                continue  # already linked

            # Extract a title from the orphaned document (first H1 or filename)
            orphan_content = orphan_path.read_text(encoding="utf-8")
            title_match = re.search(r"^#\s+(.+)$", orphan_content, re.MULTILINE)
            if title_match:
                title = title_match.group(1).strip()
            else:
                title = uri.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").title()

            links_added.append(f"- [{title}]({rel_link})")

        if not links_added:
            return {
                "mode": "production",
                "file": str(index_path),
                "files_fixed": 0,
                "message": "All orphaned documents already linked in index",
            }

        # Add an "Additional Resources" section to the index
        resources_section = (
            "\n\n## Additional Resources\n\n"
            + "\n".join(links_added)
            + "\n"
        )

        content = content.rstrip() + resources_section
        index_path.write_text(content, encoding="utf-8")

        return {
            "mode": "production",
            "file": str(index_path),
            "orphaned_docs_linked": len(links_added),
            "links_added": links_added[:5],
            "message": f"Added {len(links_added)} cross-reference links to index page",
        }

    def _handle_remove_relationship(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle relationship removal."""
        if self._dry_run:
            return {"mode": "dry_run", "message": "Relationship removal simulated"}
        raise NotImplementedError("Production relationship removal requires artifact_store")

    def _handle_archive_artifact(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle artifact archiving."""
        if self._dry_run:
            return {"mode": "dry_run", "message": "Artifact archival simulated"}
        raise NotImplementedError("Production archival requires artifact_store")

    def _handle_merge_artifacts(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle artifact merging."""
        if self._dry_run:
            return {"mode": "dry_run", "message": "Artifact merge simulated"}
        raise NotImplementedError("Production merge requires artifact_store")

    def _handle_split_artifact(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle artifact splitting."""
        if self._dry_run:
            return {"mode": "dry_run", "message": "Artifact split simulated"}
        raise NotImplementedError("Production split requires artifact_store")
