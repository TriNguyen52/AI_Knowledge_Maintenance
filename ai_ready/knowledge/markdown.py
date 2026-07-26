"""Markdown Knowledge SDK built from separate discovery, parsing, and relation components."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ai_ready.knowledge.base import KnowledgeCapability, KnowledgeSDK
from ai_ready.knowledge.discovery import LocalFileDiscovery
from ai_ready.knowledge.markdown_parser import MarkdownDocumentParser
from ai_ready.knowledge.navigation import InlineLinkRelationExtractor, NavigationRelationExtractor
from ai_ready.knowledge.registry import register_knowledge_sdk
from ai_ready.models import Document, DocumentRelation


@register_knowledge_sdk
class MarkdownKnowledgeSDK(KnowledgeSDK):
    """Local file-backed knowledge SDK for Markdown, RST, and plain text content."""

    name = "markdown"
    supported_capabilities = frozenset({
        KnowledgeCapability.DOCUMENTS,
        KnowledgeCapability.RELATIONS,
        KnowledgeCapability.NAVIGATION,
        KnowledgeCapability.METADATA,
    })

    def __init__(self) -> None:
        super().__init__()
        self._discovery = LocalFileDiscovery()
        self._parser = MarkdownDocumentParser()
        self._inline_relation_extractor = InlineLinkRelationExtractor()
        self._navigation_relation_extractor = NavigationRelationExtractor()
        self._discovered_files: list[Path] = []
        self._document_cache: dict[str, Document] = {}
        self._relation_cache: list[DocumentRelation] | None = None

    @classmethod
    def supports(cls, source: str | Path) -> bool:
        path = Path(source)
        return path.exists() and (
            path.is_dir()
            or path.suffix.lower() in {".md", ".markdown", ".mdx", ".txt", ".rst"}
        )

    def connect(self, source: str | Path) -> None:
        self._source = Path(source)
        self._discovered_files = self._discovery.discover(self._source)
        self._document_cache = {}
        self._relation_cache = None

    def iter_documents(self) -> Iterator[Document]:
        self._ensure_documents()
        for file_path in self._discovered_files:
            relative_path = self._parser._relative_path(file_path, self._source)
            document = self._document_cache.get(relative_path)
            if document is not None:
                yield document

    def iter_relations(self) -> Iterator[DocumentRelation]:
        self._ensure_relations()
        yield from self._relation_cache or []

    def source_metadata(self) -> dict[str, object]:
        return {
            "sdk": self.name,
            "file_count": len(self._discovered_files),
        }

    def _ensure_documents(self) -> None:
        if self._document_cache:
            return

        for file_path in self._discovered_files:
            document = self._parser.parse(file_path, self._source)
            self._document_cache[document.path] = document

    def _ensure_relations(self) -> None:
        if self._relation_cache is not None:
            return

        self._ensure_documents()
        documents = list(self._document_cache.values())
        relations = list(self._inline_relation_extractor.extract(documents))
        relations.extend(
            self._navigation_relation_extractor.extract(
                self._source or Path("."),
                documents,
                self._discovered_files,
            )
        )

        deduped: list[DocumentRelation] = []
        for relation in relations:
            if not any(
                existing.source_path == relation.source_path
                and existing.target_path == relation.target_path
                and existing.relation_type == relation.relation_type
                for existing in deduped
            ):
                deduped.append(relation)

        self._relation_cache = deduped