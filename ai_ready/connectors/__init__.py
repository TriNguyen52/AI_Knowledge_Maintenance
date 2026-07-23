"""Connector base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from ai_ready.models import Document, DocumentRelation


class Connector(ABC):
    """Base connector - reads from a source and yields normalized Documents.

    Connectors can optionally provide document relationships (navigation
    hierarchy, cross-references) via iter_relations().
    """

    name: str = "base"

    @abstractmethod
    def connect(self, source: str | Path) -> None:
        """Open connection to the source."""
        ...

    @abstractmethod
    def iter_documents(self) -> Iterator[Document]:
        """Yield normalized Document objects."""
        ...

    def iter_relations(self) -> Iterator[DocumentRelation]:
        """Yield document relationships (navigation, cross-references).

        Default implementation returns no relations. Override in
        subclasses that can extract navigation structure.
        """
        return iter(())

    def document_count(self) -> int:
        """Count documents (default: iterate)."""
        return sum(1 for _ in self.iter_documents())
