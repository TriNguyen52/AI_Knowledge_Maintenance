"""Examples of additional Knowledge SDKs that can be added without pipeline changes."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ai_ready.knowledge.base import KnowledgeCapability, KnowledgeSDK
from ai_ready.models import Document, DocumentRelation, Heading, Paragraph, Section


class PineconeKnowledgeSDK(KnowledgeSDK):
    """Example SDK for a vector-store-backed knowledge source."""

    name = "pinecone"
    supported_capabilities = frozenset({KnowledgeCapability.DOCUMENTS})

    @classmethod
    def supports(cls, source: str | Path) -> bool:
        return str(source).startswith("pinecone://")

    def connect(self, source: str | Path) -> None:
        self._source = Path(str(source))

    def iter_documents(self) -> Iterator[Document]:
        return iter(())

    def iter_relations(self) -> Iterator[DocumentRelation]:
        return iter(())


class ConfluenceKnowledgeSDK(KnowledgeSDK):
    """Example SDK for Confluence-style pages."""

    name = "confluence"
    supported_capabilities = frozenset({
        KnowledgeCapability.DOCUMENTS,
        KnowledgeCapability.RELATIONS,
        KnowledgeCapability.METADATA,
    })

    @classmethod
    def supports(cls, source: str | Path) -> bool:
        return str(source).startswith("confluence://")

    def connect(self, source: str | Path) -> None:
        self._source = Path(str(source))

    def iter_documents(self) -> Iterator[Document]:
        example = Document(
            id="confluence-example",
            path="spaces/example/page",
            title="Example Confluence Page",
            headings=[Heading(level=1, text="Example Confluence Page", line=1)],
            sections=[Section(heading=None, text="Example content from Confluence.", line_start=1, line_end=1)],
            paragraphs=[Paragraph(text="Example content from Confluence.", section_heading="", line=1)],
            metadata={"source": "confluence"},
        )
        return iter([example])

    def iter_relations(self) -> Iterator[DocumentRelation]:
        return iter(())