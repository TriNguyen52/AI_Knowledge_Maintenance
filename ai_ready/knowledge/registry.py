"""SDK registry and loader helpers."""

from __future__ import annotations

from pathlib import Path

from ai_ready.knowledge.base import KnowledgeSDK, KnowledgeSource

_REGISTERED_SDKS: list[type[KnowledgeSDK]] = []
_BUILTINS_LOADED = False


def register_knowledge_sdk(cls: type[KnowledgeSDK]) -> type[KnowledgeSDK]:
    """Register a Knowledge SDK for source discovery."""
    if cls not in _REGISTERED_SDKS:
        _REGISTERED_SDKS.append(cls)
    return cls


def registered_knowledge_sdks() -> list[type[KnowledgeSDK]]:
    """Return the currently registered SDK classes."""
    return list(_REGISTERED_SDKS)


def _ensure_builtin_sdks() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return

    # Importing SDKs registers them via the @register_knowledge_sdk decorator.
    from ai_ready.knowledge import markdown  # noqa: F401
    from ai_ready.knowledge import s3  # noqa: F401

    _BUILTINS_LOADED = True


def get_knowledge_sdk(source: str | Path) -> KnowledgeSDK:
    """Find, instantiate, and connect the first SDK that supports a source."""
    _ensure_builtin_sdks()
    for sdk_cls in _REGISTERED_SDKS:
        if sdk_cls.supports(source):
            sdk = sdk_cls()
            sdk.connect(source)
            return sdk
    raise ValueError(f"No Knowledge SDK is registered for source: {source}")


def load_knowledge_source(
    source: str | Path,
    exclude_patterns: list[str] | None = None,
) -> KnowledgeSource:
    """Load a knowledge source, optionally filtering artifacts by path patterns.

    Convenience wrapper that returns a normalized in-memory knowledge
    bundle.  When ``exclude_patterns`` is provided, any artifact whose
    URI contains one of the patterns is removed from the bundle, and
    relationships referencing dropped artifacts are pruned so they don't
    produce false ``link_integrity`` breakage signals.

    Args:
        source: Path or URI to the knowledge base.
        exclude_patterns: Optional list of substrings to exclude.
            Typical usage: ``["/de/", "/es/", "/fr/", "/ja/", "/ko/",
            "/pt/", "/ru/", "/tr/", "/zh/", "/uk/", "/az/", "/it/"]``
            to skip translated copies that create false
            consistency/duplication signals.

    Returns:
        KnowledgeSource with (optionally filtered) artifacts and
        pruned relationships.
    """
    ks = get_knowledge_sdk(source).load()

    if exclude_patterns:
        original_count = len(ks.artifacts)
        ks.artifacts = [
            a for a in ks.artifacts
            if not any(pat in a.uri for pat in exclude_patterns)
        ]
        # Prune relationships to only reference retained artifacts
        retained_uris = {a.uri for a in ks.artifacts}
        ks.relationships = [
            r for r in ks.relationships
            if r.source_uri in retained_uris and r.target_uri in retained_uris
        ]
        logger = __import__("logging").getLogger(__name__)
        logger.info(
            "load_knowledge_source: filtered %d -> %d artifacts (%d relationships pruned)",
            original_count, len(ks.artifacts), len(ks.relationships),
        )

    return ks