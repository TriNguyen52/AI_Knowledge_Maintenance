"""Knowledge SDK base types and normalized ingestion results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from ai_ready.models import Document, DocumentRelation


class KnowledgeCapability(str, Enum):
    """Capabilities an SDK may expose."""

    DOCUMENTS = "documents"
    RELATIONS = "relations"
    NAVIGATION = "navigation"
    METADATA = "metadata"
    EMBEDDINGS = "embeddings"


@dataclass
class KnowledgeSource:
    """Materialized normalized ingestion output from an SDK."""

    documents: list[Document] = field(default_factory=list)
    relations: list[DocumentRelation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    capabilities: frozenset[KnowledgeCapability] = frozenset()
    source: str = ""


class KnowledgeSDK(ABC):
    """Source-specific ingestion layer that normalizes knowledge into domain models."""

    name: str = "base"
    supported_capabilities: frozenset[KnowledgeCapability] = frozenset({
        KnowledgeCapability.DOCUMENTS,
    })

    def __init__(self) -> None:
        self._source: Path | None = None

    @property
    def capabilities(self) -> frozenset[KnowledgeCapability]:
        return self.supported_capabilities

    @classmethod
    def supports(cls, source: str | Path) -> bool:
        """Return True when this SDK can ingest the provided source."""
        return False

    @abstractmethod
    def connect(self, source: str | Path) -> None:
        """Open or prepare the source for ingestion."""

    @abstractmethod
    def iter_documents(self) -> Iterator[Document]:
        """Yield normalized documents."""

    def iter_relations(self) -> Iterator[DocumentRelation]:
        """Yield normalized relationships when the source can provide them."""
        return iter(())

    def source_metadata(self) -> dict[str, Any]:
        """Return source-level metadata to carry into the knowledge base."""
        return {}

    def load(self) -> KnowledgeSource:
        """Materialize the normalized source into an in-memory knowledge bundle."""
        return KnowledgeSource(
            documents=list(self.iter_documents()),
            relations=list(self.iter_relations()),
            metadata=self.source_metadata(),
            capabilities=frozenset(self.capabilities),
            source=str(self._source) if self._source is not None else "",
        )