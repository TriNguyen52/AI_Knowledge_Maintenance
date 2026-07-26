"""Backward-compatible connector base class."""

from __future__ import annotations

from abc import ABC
from pathlib import Path
from typing import Iterator

from ai_ready.knowledge.base import KnowledgeSDK
from ai_ready.models import Document, DocumentRelation


class Connector(KnowledgeSDK, ABC):
    """Backward-compatible alias for the Knowledge SDK base.

    New source integrations should subclass KnowledgeSDK directly. Existing
    connector imports keep working through this compatibility layer.
    """

    name: str = "base"

    def document_count(self) -> int:
        """Count documents by iterating the normalized stream."""
        return sum(1 for _ in self.iter_documents())
