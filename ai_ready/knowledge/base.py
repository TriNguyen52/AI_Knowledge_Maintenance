"""Knowledge SDK base types and normalized ingestion results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from ai_ready.models import (
    ArtifactBundle,
    Document,
    DocumentRelation,
    KnowledgeArtifact,
    Relationship,
)


class KnowledgeCapability(str, Enum):
    """Capabilities an SDK may expose."""

    DOCUMENTS = "documents"
    RELATIONS = "relations"
    NAVIGATION = "navigation"
    METADATA = "metadata"
    EMBEDDINGS = "embeddings"
    ARTIFACTS = "artifacts"


@dataclass
class KnowledgeSource:
    """Materialized normalized ingestion output from an SDK."""

    documents: list[Document] = field(default_factory=list)
    relations: list[DocumentRelation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    capabilities: frozenset[KnowledgeCapability] = frozenset()
    source: str = ""

    def to_artifact_bundle(self) -> ArtifactBundle:
        """Convert this document-centric source to an artifact-centric bundle.

        Documents are converted to KnowledgeArtifacts via ``to_artifact()``
        and DocumentRelations to Relationships via ``to_relationship()``.
        """
        return ArtifactBundle(
            artifacts=[d.to_artifact() for d in self.documents],
            relationships=[r.to_relationship() for r in self.relations],
            source=self.source,
            metadata=dict(self.metadata),
        )


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

    def iter_artifacts(self) -> Iterator[KnowledgeArtifact]:
        """Yield normalized knowledge artifacts.

        Default implementation delegates to ``iter_documents()`` and
        converts each document via ``Document.to_artifact()``. SDKs that
        natively produce non-document artifacts should override this.
        """
        for doc in self.iter_documents():
            yield doc.to_artifact()

    def iter_relationships(self) -> Iterator[Relationship]:
        """Yield normalized relationships between artifacts.

        Default implementation delegates to ``iter_relations()`` and
        converts each DocumentRelation via ``to_relationship()``.
        """
        for rel in self.iter_relations():
            yield rel.to_relationship()

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

    def load_artifacts(self) -> ArtifactBundle:
        """Materialize the source as an ArtifactBundle.

        Default implementation calls ``iter_artifacts()`` and
        ``iter_relationships()``. SDKs may override for custom behavior.
        """
        return ArtifactBundle(
            artifacts=list(self.iter_artifacts()),
            relationships=list(self.iter_relationships()),
            source=str(self._source) if self._source is not None else "",
            metadata=self.source_metadata(),
        )