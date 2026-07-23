"""Local Markdown connector - reads .md/.mdx/.txt/.rst files and normalizes to Documents.

Also parses navigation config files (mkdocs.yml, sidebars.js/cjs, docusaurus.config.js)
to extract document relationships (parent/child navigation hierarchy).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Iterator

from ai_ready.connectors import Connector
from ai_ready.models import CodeBlock, Document, DocumentRelation, Heading, Link, Paragraph, Section

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".mdx", ".txt", ".rst"}

# Frontmatter parser
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Heading parser
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Link parser - markdown links [text](target) and reference links
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
MD_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")

# Code block parser
CODE_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

# RST headings (===, ---, ~~~ underlines)
RST_HEADING_RE = re.compile(r"^([^\n]+)\n([=\-~`'\"^_*+#])\1+\s*$", re.MULTILINE)


class MarkdownConnector(Connector):
    """Reads local markdown/text files and yields normalized Documents.

    Also extracts document relationships from navigation config files.
    """

    name = "markdown"

    def __init__(self) -> None:
        self._source_path: Path | None = None
        self._files: list[Path] = []
        self._relations: list[DocumentRelation] = []
        self._relations_parsed = False

    def connect(self, source: str | Path) -> None:
        self._source_path = Path(source)
        if self._source_path.is_file():
            self._files = [self._source_path]
        else:
            self._files = sorted(
                f for f in self._source_path.rglob("*")
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        self._relations = []
        self._relations_parsed = False

    def iter_documents(self) -> Iterator[Document]:
        for filepath in self._files:
            yield self._parse_file(filepath)

    def iter_relations(self) -> Iterator[DocumentRelation]:
        """Yield document relationships extracted from nav configs and inline links."""
        if not self._relations_parsed:
            self._parse_relations()
            self._relations_parsed = True
        yield from self._relations

    def _parse_relations(self) -> None:
        """Parse navigation config files and inline links to build relationships."""
        if not self._source_path or not self._source_path.is_dir():
            return

        # Parse mkdocs.yml - look in source dir, parent dirs, and subdirs
        for mkdocs_path in [self._source_path / "mkdocs.yml",
                           self._source_path.parent / "mkdocs.yml"]:
            if mkdocs_path.exists():
                self._parse_mkdocs_nav(mkdocs_path)

        # Also look for mkdocs.yml in subdirectories
        for yml in self._source_path.rglob("mkdocs.yml"):
            if yml != self._source_path / "mkdocs.yml" and yml != self._source_path.parent / "mkdocs.yml":
                self._parse_mkdocs_nav(yml)

        # Parse Docusaurus sidebars - look in source dir and parent dirs
        for sidebar_file in list(self._source_path.rglob("sidebars.*")) + list(self._source_path.parent.rglob("sidebars.*", ))[:3]:
            self._parse_docusaurus_sidebars(sidebar_file)

        # Parse Astro starlight config - look in source dir and parent
        for config_file in list(self._source_path.rglob("astro.config.*")) + [self._source_path.parent / f for f in ["astro.config.mjs", "astro.config.js", "astro.config.ts"] if (self._source_path.parent / f).exists()]:
            self._parse_astro_config(config_file)

        # Also look for _toc.yml (Jekyll) and mkdocs.yml in parent's parent
        for parent_level in [self._source_path.parent, self._source_path.parent.parent]:
            mkdocs = parent_level / "mkdocs.yml"
            if mkdocs.exists() and mkdocs not in [self._source_path / "mkdocs.yml", self._source_path.parent / "mkdocs.yml"]:
                self._parse_mkdocs_nav(mkdocs)

        # Also extract cross-reference relations from inline links
        self._parse_inline_link_relations()

    def _parse_mkdocs_nav(self, mkdocs_path: Path) -> None:
        """Parse mkdocs.yml nav section for parent/child relationships."""
        try:
            content = mkdocs_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        # Find the docs_dir (defaults to "docs" relative to mkdocs.yml location)
        docs_dir_match = re.search(r"docs_dir:\s*(.+)", content)
        docs_dir = docs_dir_match.group(1).strip() if docs_dir_match else "docs"
        docs_path = mkdocs_path.parent / docs_dir

        # If docs_path doesn't exist but our source_path is a subdirectory, use source_path
        if not docs_path.exists() and self._source_path and self._source_path.is_dir():
            # Check if source_path is under docs_path or vice versa
            try:
                self._source_path.relative_to(docs_path)
                docs_path = self._source_path
            except ValueError:
                # Try: maybe source_path IS the docs dir
                if self._source_path.name == "docs" or self._source_path.name == "content":
                    docs_path = self._source_path

        # Find nav section
        nav_match = re.search(r"^nav:\s*$", content, re.MULTILINE)
        if not nav_match:
            return

        nav_start = nav_match.end()
        nav_lines = content[nav_start:].split("\n")

        # Parse the indented nav structure
        self._parse_mkdocs_nav_lines(nav_lines, docs_path, parent_path="")

    def _parse_mkdocs_nav_lines(
        self, lines: list[str], docs_path: Path, parent_path: str, indent: int = 0
    ) -> None:
        """Recursively parse mkdocs nav lines for parent/child relationships.

        mkdocs.yml nav structure uses `- ` prefix at each level:
        nav:
        - index.md
        - Section:
          - page1.md
          - page2.md
        """
        current_section = parent_path
        section_index_path: str | None = None
        section_files: list[str] = []

        for i, line in enumerate(lines):
            if not line.strip():
                continue
            stripped = line.strip()
            if not stripped.startswith("-"):
                break

            line_indent = len(line) - len(line.lstrip())
            if line_indent < indent:
                break
            if line_indent > indent:
                continue

            item = stripped[1:].strip()
            if item.startswith("- "):
                item = item[2:].strip()

            # Section header: "Section Name: file.md" or "Section Name:"
            if ":" in item:
                colon_idx = item.index(":")
                section_name = item[:colon_idx].strip().strip('"').strip("'")
                remainder = item[colon_idx + 1:].strip()

                if remainder and remainder.endswith(".md"):
                    file_path = self._resolve_doc_path(remainder, docs_path)
                    if file_path:
                        section_files.append(file_path)
                        current_section = section_name
                        section_index_path = file_path
                        # Mark as in-navigation
                        self._add_relation("__nav__", file_path, "navigation")
                else:
                    current_section = section_name
                    section_index_path = None

                # Recurse into children, passing this section's index
                child_lines = lines[i + 1:]
                child_indent = line_indent + 2
                saved = getattr(self, '_child_section_index', None)
                self._child_section_index = section_index_path
                self._parse_mkdocs_nav_lines(child_lines, docs_path, current_section, child_indent)
                self._child_section_index = saved
                continue

            # Plain file reference: "- file.md"
            if item.endswith(".md"):
                file_path = self._resolve_doc_path(item, docs_path)
                if file_path:
                    section_files.append(file_path)
                    # Mark as in-navigation (prevents orphan false positive)
                    self._add_relation("__nav__", file_path, "navigation")
                    # Link to parent section's index if available
                    parent_idx = getattr(self, '_child_section_index', None)
                    if parent_idx and parent_idx != file_path:
                        self._add_relation(parent_idx, file_path, "parent_child")

        # Link top-level files to this section's index
        if section_files and section_index_path:
            for fp in section_files:
                if fp != section_index_path:
                    self._add_relation(section_index_path, fp, "parent_child")

    def _find_section_index(self, section_name: str, docs_path: Path) -> str | None:
        """Try to find an index.md or section landing page for a nav section."""
        # Try section_name/index.md
        for index_file in ["index.md", "index.mdx", "_index.md"]:
            candidate = docs_path / section_name / index_file
            if candidate.exists():
                return self._relative_path(candidate, self._source_path)
        # Try relative to source path
        if self._source_path:
            for index_file in ["index.md", "index.mdx", "_index.md"]:
                candidate = self._source_path / section_name / index_file
                if candidate.exists():
                    return self._relative_path(candidate, self._source_path)
        return None

    def _resolve_doc_path(self, ref: str, docs_path: Path) -> str | None:
        """Resolve a markdown file reference to a relative path."""
        candidate = docs_path / ref
        if candidate.exists():
            return self._relative_path(candidate, self._source_path)
        # Try relative to source path directly
        if self._source_path:
            candidate2 = self._source_path / ref
            if candidate2.exists():
                return self._relative_path(candidate2, self._source_path)
        return None

    def _relative_path(self, path: Path, base: Path) -> str:
        """Get path relative to source, normalized."""
        try:
            return str(path.relative_to(self._source_path)).replace("\\", "/")
        except (ValueError, TypeError):
            return str(path).replace("\\", "/")

    def _parse_docusaurus_sidebars(self, sidebar_path: Path) -> None:
        """Parse Docusaurus sidebars.js/cjs for parent/child relationships."""
        try:
            content = sidebar_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        # Docusaurus sidebar structure: { docId: [childDoc1, childDoc2, ...] }
        # or nested: { docId: { label: "...", items: [...] } }
        # Extract doc IDs and their children
        doc_id_re = re.compile(r"['\"]([a-f0-9\-_/]+)['\"]")
        items_match = re.finditer(r"items:\s*\[([^\]]+)\]", content, re.DOTALL)

        for match in items_match:
            items_content = match.group(1)
            doc_ids = doc_id_re.findall(items_content)
            if len(doc_ids) >= 2:
                # First doc is parent, rest are children
                parent = doc_ids[0]
                for child in doc_ids[1:]:
                    parent_path = self._find_doc_by_id(parent)
                    child_path = self._find_doc_by_id(child)
                    if parent_path and child_path:
                        self._add_relation(parent_path, child_path, "parent_child")
                    # Mark all as in-navigation
                    if child_path:
                        self._add_relation("__nav__", child_path, "navigation")
                if parent_path:
                    self._add_relation("__nav__", parent_path, "navigation")
            elif len(doc_ids) == 1:
                # Single doc in sidebar - still in navigation
                doc_path = self._find_doc_by_id(doc_ids[0])
                if doc_path:
                    self._add_relation("__nav__", doc_path, "navigation")

    def _parse_astro_config(self, config_path: Path) -> None:
        """Parse Astro Starlight config for sidebar structure."""
        try:
            content = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        # Starlight sidebar: { label: "...", items: ["doc1", "doc2", ...] }
        # or { label: "...", autogenerate: { directory: "..." } }
        items_match = re.finditer(r"items:\s*\[([^\]]+)\]", content, re.DOTALL)
        for match in items_match:
            items_content = match.group(1)
            doc_ids = re.findall(r"['\"]([^'\"]+)['\"]", items_content)
            if len(doc_ids) >= 2:
                parent = doc_ids[0]
                for child in doc_ids[1:]:
                    parent_path = self._find_doc_by_id(parent)
                    child_path = self._find_doc_by_id(child)
                    if parent_path and child_path:
                        self._add_relation(parent_path, child_path, "parent_child")
                    if child_path:
                        self._add_relation("__nav__", child_path, "navigation")
                if parent_path:
                    self._add_relation("__nav__", parent_path, "navigation")

    def _find_doc_by_id(self, doc_id: str) -> str | None:
        """Find a document path by its ID (slug or filename)."""
        # Build stem-to-path cache if not already built
        if not hasattr(self, '_stem_cache'):
            self._stem_cache: dict[str, str] = {}
            for f in self._files:
                relative = self._relative_path(f, self._source_path)
                self._stem_cache[f.stem] = relative
                self._stem_cache[f.stem.replace("/", "_")] = relative

        if doc_id in self._stem_cache:
            return self._stem_cache[doc_id]
        # Fallback: check if doc_id is in any path
        for path in self._stem_cache.values():
            if doc_id in path:
                return path
        return None

    def _parse_inline_link_relations(self) -> None:
        """Extract cross_reference relations from inline markdown links.

        Uses the already-parsed Document objects' links instead of re-reading files.
        """
        # Build a set of known document paths for fast lookup
        doc_paths: set[str] = set()
        for f in self._files:
            relative = self._relative_path(f, self._source_path)
            doc_paths.add(relative)

        # Parse documents to get their links (we need link info)
        if not hasattr(self, '_parsed_cache'):
            self._parsed_cache: dict[str, Document] = {}
            for f in self._files:
                relative = self._relative_path(f, self._source_path)
                self._parsed_cache[relative] = self._parse_file(f)

        for source_path, doc in self._parsed_cache.items():
            doc_dir = source_path.rsplit("/", 1)[0] if "/" in source_path else ""
            for link in doc.links:
                if not link.is_internal:
                    continue
                target = link.target.split("#")[0].split("?")[0]
                if not target:
                    continue
                target_path = self._resolve_link_from_paths(target, doc_dir, doc_paths)
                if target_path:
                    self._add_relation(source_path, target_path, "cross_reference")

    def _resolve_link_from_paths(self, target: str, doc_dir: str, known_paths: set[str]) -> str | None:
        """Resolve a link target using a set of known document paths."""
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

    def _add_relation(self, source: str, target: str, rel_type: str) -> None:
        """Add a document relation, avoiding duplicates."""
        for r in self._relations:
            if r.source_path == source and r.target_path == target and r.relation_type == rel_type:
                return
        self._relations.append(DocumentRelation(
            source_path=source, target_path=target, relation_type=rel_type,
        ))

    def _parse_file(self, filepath: Path) -> Document:
        text = filepath.read_text(encoding="utf-8", errors="replace")
        relative_path = str(filepath)
        if self._source_path and self._source_path.is_dir():
            try:
                relative_path = str(filepath.relative_to(self._source_path))
            except ValueError:
                pass

        # Normalize path separators
        relative_path = relative_path.replace("\\", "/")

        doc_id = self._make_id(relative_path)
        metadata = self._extract_frontmatter(text)
        body = self._strip_frontmatter(text)

        headings = self._extract_headings(body, filepath.suffix.lower())
        sections = self._extract_sections(body, headings)
        paragraphs = self._extract_paragraphs(sections)
        links = self._extract_links(body)
        code_blocks = self._extract_code_blocks(body)

        title = self._extract_title(filepath, metadata, headings)

        return Document(
            id=doc_id,
            path=relative_path,
            title=title,
            headings=headings,
            sections=sections,
            paragraphs=paragraphs,
            links=links,
            code_blocks=code_blocks,
            metadata=metadata,
        )

    def _make_id(self, path: str) -> str:
        return hashlib.sha256(path.encode()).hexdigest()[:16]

    def _extract_frontmatter(self, text: str) -> dict:
        match = FRONTMATTER_RE.match(text)
        if not match:
            return {}
        raw = match.group(1)
        result: dict = {}
        for line in raw.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                result[key.strip()] = val.strip()
        return result

    def _strip_frontmatter(self, text: str) -> str:
        match = FRONTMATTER_RE.match(text)
        if match:
            return text[match.end():]
        return text

    def _extract_headings(self, body: str, ext: str) -> list[Heading]:
        headings: list[Heading] = []
        if ext in {".md", ".markdown", ".mdx"}:
            for i, line in enumerate(body.split("\n"), 1):
                m = re.match(r"^(#{1,6})\s+(.+)$", line)
                if m:
                    headings.append(Heading(level=len(m.group(1)), text=m.group(2).strip(), line=i))
        elif ext == ".rst":
            for m in RST_HEADING_RE.finditer(body):
                line_num = body[: m.start()].count("\n") + 1
                underline_char = m.group(2)[0]
                level = {"=": 1, "-": 2, "~": 3, "`": 4, "'": 5, '"': 6}.get(underline_char, 2)
                headings.append(Heading(level=level, text=m.group(1).strip(), line=line_num))
        return headings

    def _extract_sections(self, body: str, headings: list[Heading]) -> list[Section]:
        if not headings:
            return [Section(heading=None, text=body.strip(), line_start=1)]

        sections: list[Section] = []
        lines = body.split("\n")

        for i, heading in enumerate(headings):
            start = heading.line
            if i + 1 < len(headings):
                end = headings[i + 1].line - 1
            else:
                end = len(lines)

            section_lines = lines[start:end]  # skip the heading line itself
            text = "\n".join(section_lines).strip()
            sections.append(Section(
                heading=heading,
                text=text,
                line_start=start,
                line_end=end,
            ))

        # Also capture content before the first heading
        if headings[0].line > 1:
            pre_text = "\n".join(lines[: headings[0].line - 1]).strip()
            if pre_text:
                sections.insert(0, Section(heading=None, text=pre_text, line_start=1, line_end=headings[0].line - 1))

        return sections

    def _extract_paragraphs(self, sections: list[Section]) -> list[Paragraph]:
        paragraphs: list[Paragraph] = []
        for section in sections:
            heading_text = section.heading.text if section.heading else ""
            for para in section.text.split("\n\n"):
                para = para.strip()
                if para and not para.startswith("```") and not para.startswith("|"):
                    paragraphs.append(Paragraph(
                        text=para,
                        section_heading=heading_text,
                        line=section.line_start,
                    ))
        return paragraphs

    def _extract_links(self, body: str) -> list[Link]:
        links: list[Link] = []
        for i, line in enumerate(body.split("\n"), 1):
            for m in MD_LINK_RE.finditer(line):
                target = m.group(2).strip()
                is_internal = not target.startswith("http://") and not target.startswith("https://")
                links.append(Link(text=m.group(1), target=target, line=i, is_internal=is_internal))
        return links

    def _extract_code_blocks(self, body: str) -> list[CodeBlock]:
        blocks: list[CodeBlock] = []
        for m in CODE_FENCE_RE.finditer(body):
            line = body[: m.start()].count("\n") + 1
            blocks.append(CodeBlock(language=m.group(1) or "text", text=m.group(2), line=line))
        return blocks

    def _extract_title(self, filepath: Path, metadata: dict, headings: list[Heading]) -> str:
        if "title" in metadata:
            return metadata["title"]
        if headings:
            return headings[0].text
        return filepath.stem
