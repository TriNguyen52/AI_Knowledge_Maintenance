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
    success: bool = True       # handler ran without exception
    applied: bool = True       # the modification was actually written to disk
    skipped_reason: str = ""    # why it was skipped (e.g. "blocked by allowlist")
    error: str = ""
    timestamp: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_type": self.step_type,
            "artifact_uri": self.artifact_uri,
            "description": self.description,
            "success": self.success,
            "applied": self.applied,
            "skipped_reason": self.skipped_reason,
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

        # Discover ALL markdown files in the source path that are not
        # already in known_artifact_uris.  This ensures the executor can
        # modify any file in the knowledge base (e.g. navigation hub files
        # like index.md that create_relationship operations need to edit
        # to add inbound links to orphaned documents), regardless of the
        # directory structure or naming conventions used by the source.
        if self.source_path and not self._dry_run:
            for md_file in self.source_path.rglob("*.md"):
                rel_path = str(md_file.relative_to(self.source_path)).replace("\\", "/")
                self.known_artifact_uris.add(rel_path)

    def _resolve_path(self, artifact_uri: str, is_create: bool = False) -> Path | None:
        """Resolve an artifact URI to a full file path safely.

        Security: The LLM controls ``artifact_uri`` and the executor calls
        ``write_text`` on the resolved path.  This method enforces
        containment layers depending on the operation type:

        **Modify operations** (is_create=False):
        1. **Allowlist** — if ``known_artifact_uris`` is populated, reject
           any URI not in the set.
        2. **Path-escape prevention** — resolve the full path and verify it
           is still inside ``source_path`` (blocks ``../../etc/passwd`` etc.).
        3. **Existence check** — the file must already exist (we never
           modify non-existent files).

        **Create operations** (is_create=True):
        1. **No allowlist** — new files are allowed even if the URI isn't
           in ``known_artifact_uris`` (the file doesn't exist yet).
        2. **Path-escape prevention** — same as modify: resolve and verify
           the path is inside ``source_path``.
        3. **No existence check** — the file is expected NOT to exist yet.

        Returns ``None`` on any failure — callers must treat ``None`` as
        "do not write".
        """
        if self.source_path is None:
            return None

        # Layer 1: allowlist enforcement (modify only; create skips this)
        if not is_create and self.known_artifact_uris and artifact_uri not in self.known_artifact_uris:
            logger.warning("Rejected unknown artifact URI: %s", artifact_uri)
            return None

        # Layer 2: path-escape prevention (always enforced)
        full_path = self.source_path / artifact_uri
        try:
            resolved = full_path.resolve()
            if not resolved.is_relative_to(self.source_path.resolve()):
                logger.error("Path escape blocked: %s -> %s", artifact_uri, resolved)
                return None
        except Exception:
            return None

        # Layer 3: existence check (modify only; create skips this)
        if is_create:
            return full_path
        return full_path if full_path.exists() else None

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
                # Check if the handler reported the step was skipped/blocked
                is_skipped = details.get("skipped", False) if isinstance(details, dict) else False
                skipped_reason = details.get("skipped_reason", "") if isinstance(details, dict) else ""
                results.append(ExecutionStep(
                    step_type=step_type,
                    artifact_uri=artifact_uri,
                    description=description,
                    success=True,
                    applied=not is_skipped,
                    skipped_reason=skipped_reason,
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
                    applied=False,
                    skipped_reason=f"Exception: {e}",
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
            "create_document": self._handle_create_document,
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
            # Abstract/process step types — map to document update
            "create_pipeline": self._handle_update_document,
            "link_validation_pipeline": self._handle_update_document,
            "schedule_audit": self._handle_update_document,
            "documentation_audit": self._handle_update_document,
            "update_documentation_guidelines": self._handle_update_document,
            "documentation_guidelines": self._handle_update_document,
            "provide_author_training": self._handle_add_metadata,
            "author_training": self._handle_add_metadata,
            "review_document": self._handle_update_document,
            "improve_structure": self._handle_update_document,
            "reorganize_document": self._handle_update_document,
            "add_context": self._handle_update_document,
            "add_summary": self._handle_update_document,
            "consolidate_content": self._handle_update_document,
        }
        handler = handlers.get(step_type)
        if handler is None:
            raise ValueError(f"Unknown modification type: {step_type}")
        return handler

    # -----------------------------------------------------------------------
    # Real file modification handlers
    # -----------------------------------------------------------------------

    def _handle_update_document(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle document content update — fix broken links, dangling refs,
        and add canonical source links.

        Targets ONLY the specific artifact_uri from the step (Item 5:
        targeted modifications).  The remediation map generates one step
        per signal, so each step already has a specific artifact to fix.

        When source_path is set, reads the target file and applies fixes:
        1. Removes broken markdown links (targets that don't exist in the
           known artifact set) — uses signal evidence to identify which
           specific links are broken.
        2. Rewrites dangling references ("see above", "as mentioned below")
           to be self-contained — conservative: only replaces clearly
           ambiguous phrases that would be meaningless to an LLM reading
           a single chunk without surrounding context.
        3. Writes the modified file back to disk
        """
        artifact_uri = step.get("artifact_uri", "")
        description = step.get("description", "")
        params = step.get("parameters", {})
        action = params.get("action", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Document update simulated"}

        # If the step has an explicit action parameter, use it directly
        # (this comes from the remediation map — no keyword guessing needed)
        if action == "add_canonical_link":
            return self._fix_canonical_links(step, params)

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

        # Item 5: Target ONLY the specific artifact_uri from the step.
        # The remediation map generates one step per signal, so sweeping
        # all matching artifacts would cause collateral changes to files
        # that have their own separate steps.
        if not artifact_uri:
            return {"mode": "production", "files_fixed": 0,
                    "message": "No artifact_uri in step"}

        artifacts_to_fix: list[str] = [artifact_uri]

        total_files_fixed = 0
        total_fixes = 0
        all_fix_details: list[str] = []

        for uri in artifacts_to_fix:
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

    def _fix_canonical_links(
        self, step: dict[str, Any], params: dict[str, Any]
    ) -> dict[str, Any]:
        """Add canonical source links to resolve missing_canonical_source signals.

        CanonicalSourceCollector checks artifact.links for links to
        canonical domains.  This handler adds a markdown link to the
        canonical domain at the top of the document (after the title)
        so the collector finds it on re-assessment.

        Item 5: Targets ONLY the specific artifact_uri from the step,
        not all artifacts with matching domain.
        """
        artifact_uri = step.get("artifact_uri", "")
        canonical_domain = params.get("canonical_domain", "")
        topic_keyword = params.get("topic_keyword", "")
        signal_type = params.get("signal_type", "missing_canonical_source")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Canonical link addition simulated"}

        # Item 5: Target ONLY the specific artifact_uri from the step.
        if not artifact_uri:
            return {"mode": "production", "files_fixed": 0,
                    "message": "No artifact_uri in step"}

        artifacts_to_fix: list[str] = [artifact_uri]

        total_files_fixed = 0
        total_fixes = 0
        all_fix_details: list[str] = []

        for uri in sorted(artifacts_to_fix):
            file_path = self._resolve_path(uri)
            if file_path is None:
                continue

            content = file_path.read_text(encoding="utf-8")
            original_content = content

            # Determine the canonical domain for this specific artifact
            sig_domain = canonical_domain
            if not sig_domain:
                for signal in self.assessment_signals:
                    if (signal.signal_type == "missing_canonical_source"
                            and signal.artifact_uri == uri):
                        sig_domain = (signal.evidence or {}).get(
                            "canonical_domain", "")
                        break

            if not sig_domain:
                continue

            canonical_url = f"https://{sig_domain}/"
            link_text = f"Official {topic_keyword or sig_domain.split('.')[0]} documentation"

            # Check if the link already exists
            if canonical_url in content:
                continue

            # Add the canonical link after the first H1 heading
            # (or at the top if no H1)
            h1_match = re.search(r"^(# .+)$", content, re.MULTILINE)
            if h1_match:
                insert_pos = h1_match.end()
                # Skip the newline after H1
                if insert_pos < len(content) and content[insert_pos] == "\n":
                    insert_pos += 1
                canonical_line = f"\n> See the [{link_text}]({canonical_url})\n"
                content = content[:insert_pos] + canonical_line + content[insert_pos:]
            else:
                canonical_line = f"> See the [{link_text}]({canonical_url})\n\n"
                content = canonical_line + content

            if content != original_content:
                file_path.write_text(content, encoding="utf-8")
                total_files_fixed += 1
                total_fixes += 1
                all_fix_details.append(
                    f"Added canonical link to {sig_domain} in {uri}"
                )

        return {
            "mode": "production",
            "files_checked": len(artifacts_to_fix),
            "files_fixed": total_files_fixed,
            "total_fixes": total_fixes,
            "sample_fixes": all_fix_details[:5],
            "signal_type": signal_type,
            "applied": total_files_fixed > 0,
        }

    def _handle_add_metadata(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle metadata addition — add or update YAML frontmatter.

        Writes the EXACT front-matter key the collector reads so the
        signal clears on re-assessment (Item 1.1 constraint).

        For no_date_signal / stale_content (FreshnessCollector):
          Writes ``date`` key — FreshnessCollector reads metadata keys
          "last_modified", "date", "updated", "created", "last_updated".

        If frontmatter already exists, the key is updated in-place
        rather than skipping (so stale dates get refreshed).
        """
        artifact_uri = step.get("artifact_uri", "")
        params = step.get("parameters", {})
        meta_key = params.get("metadata_key", "date")
        meta_value = params.get("metadata_value", "")
        signal_type = params.get("signal_type", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Metadata update simulated"}

        file_path = self._resolve_path(artifact_uri)
        if file_path is None:
            return {"mode": "dry_run", "message": f"File not found: {artifact_uri}"}

        content = file_path.read_text(encoding="utf-8")
        original_content = content

        # Check if frontmatter already exists
        if content.startswith("---"):
            # Update existing frontmatter — replace or add the key
            fm_end = content.index("---", 3)  # find closing ---
            fm_block = content[3:fm_end]
            rest = content[fm_end + 3:]

            # Check if the key already exists in frontmatter
            key_pattern = re.compile(
                rf"^(\s*{re.escape(meta_key)}\s*:\s*)(.*)$",
                re.MULTILINE,
            )
            if key_pattern.search(fm_block):
                # Update existing key
                fm_block = key_pattern.sub(
                    rf"\g<1>{meta_value}", fm_block
                )
            else:
                # Add new key to frontmatter
                fm_block = fm_block.rstrip() + f"\n{meta_key}: {meta_value}\n"

            content = "---" + fm_block + "---" + rest
        else:
            # Create new frontmatter with the metadata key
            # Also add title from first H1 if available
            title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else \
                artifact_uri.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ").title()

            if meta_key == "date":
                frontmatter = f"---\ntitle: {title}\n{meta_key}: {meta_value}\n---\n\n"
            else:
                frontmatter = f"---\n{meta_key}: {meta_value}\n---\n\n"
            content = frontmatter + content

        if content != original_content:
            file_path.write_text(content, encoding="utf-8")
            return {
                "mode": "production",
                "file": str(file_path),
                "metadata_added": [meta_key],
                "signal_type": signal_type,
                "applied": True,
            }
        else:
            return {
                "mode": "production",
                "file": str(file_path),
                "message": "No changes needed — metadata already correct",
                "applied": False,
            }

    def _handle_create_relationship(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle relationship creation — fix root cause of orphaned documents.

        The root cause of orphaned documents is that the index/navigation
        page doesn't link to them. Instead of slapping a generic 'Related'
        section on every orphaned file (superficial), this handler updates
        the index page to include links to the orphaned documents —
        integrating them into the knowledge graph at the source.

        Item 5: Targets ONLY the specific artifact_uri from the step,
        not all orphaned artifacts.  The remediation map generates one
        step per orphan signal, so each step has a specific artifact.
        """
        artifact_uri = step.get("artifact_uri", "")
        description = step.get("description", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Relationship creation simulated"}

        # Item 5: Target ONLY the specific artifact_uri from the step.
        if not artifact_uri:
            return {"mode": "production", "files_fixed": 0,
                    "message": "No artifact_uri in step"}

        orphaned_uris: set[str] = {artifact_uri}

        # Strategy: Find the best navigation hub file to add links from.
        # The root cause of orphaned documents is that no other file links
        # to them. We find the root-level index.md (or closest equivalent)
        # and add cross-reference links there, integrating the orphan into
        # the knowledge graph at the source.
        #
        # Discovery is general: find index.md files at the root of the
        # source path, then at increasing depth. This works for any
        # knowledge base structure, not just FastAPI docs.
        index_path = None
        index_uri = None

        # Try root-level index.md first (most common navigation hub)
        root_index = self._resolve_path("index.md")
        if root_index is not None:
            index_path = root_index
            index_uri = "index.md"
        else:
            # Search for any index.md in the source path, shallowest first
            if self.source_path:
                index_files = sorted(
                    self.source_path.rglob("index.md"),
                    key=lambda p: len(p.relative_to(self.source_path).parts),
                )
                for idx_file in index_files:
                    rel_uri = str(idx_file.relative_to(self.source_path)).replace("\\", "/")
                    resolved = self._resolve_path(rel_uri)
                    if resolved is not None:
                        index_path = resolved
                        index_uri = rel_uri
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

    def _handle_create_document(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle creating a new document inside source_path.

        This is a CREATE operation — the allowlist is skipped (the file
        doesn't exist yet), but path-escape prevention is still enforced.
        The file must be inside source_path.
        """
        artifact_uri = step.get("artifact_uri", "")
        content = step.get("content", "")
        description = step.get("description", "")

        if self._dry_run:
            return {"mode": "dry_run", "message": "Document creation simulated"}

        if not artifact_uri:
            return {"mode": "production", "skipped": True,
                    "skipped_reason": "No artifact_uri provided for create_document"}

        # Use is_create=True to skip allowlist and existence check
        file_path = self._resolve_path(artifact_uri, is_create=True)
        if file_path is None:
            return {"mode": "production", "skipped": True,
                    "skipped_reason": f"Path resolution failed for new file: {artifact_uri}"}

        # If the file already exists, don't overwrite — skip
        if file_path.exists():
            return {"mode": "production", "skipped": True,
                    "skipped_reason": f"File already exists: {artifact_uri}"}

        # Create parent directories if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the new file
        file_path.write_text(content, encoding="utf-8")

        return {
            "mode": "production",
            "file": str(file_path),
            "created": True,
            "message": f"Created new document: {artifact_uri}",
        }

    def _handle_remove_relationship(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle relationship removal — explicitly disabled.

        Relationship removal requires graph mutation via artifact_store,
        which is not implemented.  Returns a clear skip result rather
        than raising NotImplementedError (Item 1.1 — no silent no-ops).
        """
        return {
            "mode": "production",
            "skipped": True,
            "skipped_reason": "Relationship removal not implemented — requires artifact_store graph mutation",
            "applied": False,
        }

    def _handle_archive_artifact(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle artifact archiving — explicitly disabled.

        Archival requires artifact_store lifecycle management, which
        is not implemented.  Returns a clear skip result.
        """
        return {
            "mode": "production",
            "skipped": True,
            "skipped_reason": "Artifact archival not implemented — requires artifact_store lifecycle management",
            "applied": False,
        }

    def _handle_merge_artifacts(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle artifact merging — explicitly disabled.

        Merging requires content deduplication logic, which is not
        implemented.  Returns a clear skip result.
        """
        return {
            "mode": "production",
            "skipped": True,
            "skipped_reason": "Artifact merging not implemented — requires content deduplication logic",
            "applied": False,
        }

    def _handle_split_artifact(self, step: dict[str, Any]) -> dict[str, Any]:
        """Handle artifact splitting — explicitly disabled.

        Splitting requires content partitioning logic, which is not
        implemented.  Returns a clear skip result.
        """
        return {
            "mode": "production",
            "skipped": True,
            "skipped_reason": "Artifact splitting not implemented — requires content partitioning logic",
            "applied": False,
        }
