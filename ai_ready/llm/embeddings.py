"""Local embedding provider with TF-IDF and HuggingFace API support.

Produces fixed-dimension (384) vectors from text for storage in CockroachDB's
VECTOR(384) column and similarity search via the <=> cosine distance operator.

Two embedding backends:
1. HuggingFace Inference API — high-quality neural embeddings (all-MiniLM-L6-v2)
   Requires HF_TOKEN env var. Free tier with rate limits.
2. TF-IDF + numpy — lightweight, dependency-free, deterministic
   **This is the default fallback** when HuggingFace is unavailable, rate-limited,
   or returns 403. Without a valid HF_TOKEN, TF-IDF is the active backend.

The provider tries HuggingFace first (if HF_TOKEN is set), falls back to
TF-IDF automatically on the first failure. Once a backend succeeds, it
becomes "sticky" for the process lifetime.

Usage:
    from ai_ready.llm.embeddings import EmbeddingProvider

    provider = EmbeddingProvider()
    provider.fit(corpus_texts)  # Build IDF for TF-IDF fallback
    vector = provider.embed("text to embed")  # → list[float] of 384 dims

To check which backend is active:
    provider.active_backend  # → "huggingface" or "tfidf" or "uninitialized"
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import logging
from collections import Counter
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384
HF_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HF_API_URL = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}/pipeline/feature-extraction"


def _tokenize(text: str) -> list[str]:
    """Simple tokenizer: lowercase, split on non-alphanumeric, filter stop words."""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]{2,}", text)
    stop = {
        "the", "is", "at", "on", "in", "to", "of", "a", "an", "and", "or",
        "for", "it", "as", "be", "by", "this", "that", "with", "from",
        "are", "was", "were", "will", "can", "has", "have", "had", "not",
        "but", "if", "then", "else", "when", "all", "any", "each", "such",
        "do", "does", "did", "how", "what", "which", "who", "whom",
    }
    return [t for t in tokens if t not in stop and len(t) >= 2]


class TFIDFEmbedder:
    """TF-IDF embedding with signed hashing to fixed dimension."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self._idf: dict[str, float] = {}
        self._fitted = False

    def fit(self, corpus: list[str]) -> "TFIDFEmbedder":
        """Build IDF weights from a corpus of documents."""
        doc_count = len(corpus)
        if doc_count == 0:
            self._fitted = True
            return self

        df: Counter[str] = Counter()
        for text in corpus:
            tokens = set(_tokenize(text))
            for token in tokens:
                df[token] += 1

        self._idf = {
            term: math.log((1 + doc_count) / (1 + freq)) + 1.0
            for term, freq in df.items()
        }
        self._fitted = True
        return self

    def embed(self, text: str) -> list[float]:
        """Embed text into a fixed-dimension vector via TF-IDF + signed hashing."""
        tokens = _tokenize(text)
        if not tokens:
            return [0.0] * self.dim

        tf = Counter(tokens)
        vector = np.zeros(self.dim, dtype=np.float32)

        for term, count in tf.items():
            idf = self._idf.get(term, 1.0) if self._fitted else 1.0
            weight = count * idf

            h1 = int(hashlib.md5(f"{term}_1".encode()).hexdigest(), 16) % self.dim
            h2 = int(hashlib.md5(f"{term}_2".encode()).hexdigest(), 16) % self.dim

            sign1 = 1.0 if (h1 % 2 == 0) else -1.0
            sign2 = 1.0 if (h2 % 2 == 0) else -1.0

            vector[h1] += sign1 * weight
            vector[h2] += sign2 * weight

        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm

        return vector.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class HuggingFaceEmbedder:
    """HuggingFace Inference API embedding provider.

    Uses the free HuggingFace Inference API to generate high-quality
    384-dimension embeddings via sentence-transformers/all-MiniLM-L6-v2.

    Requires HF_TOKEN environment variable.
    Falls back to TF-IDF if the API is unavailable or rate-limited.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self._token = os.environ.get("HF_TOKEN", "")
        self._available = bool(self._token)
        if not self._available:
            logger.info("HF_TOKEN not set — HuggingFace embeddings disabled, using TF-IDF fallback")

    def embed(self, text: str) -> list[float] | None:
        """Embed text via HuggingFace API. Returns None on failure."""
        if not self._available:
            return None

        try:
            import requests
        except ImportError:
            return None

        try:
            resp = requests.post(
                HF_API_URL,
                headers={"Authorization": f"Bearer {self._token}"},
                json={"inputs": text, "options": {"wait_for_model": True}},
                timeout=30,
            )
            if resp.status_code == 503:
                # Model loading, retry once after brief wait
                time.sleep(3)
                resp = requests.post(
                    HF_API_URL,
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={"inputs": text, "options": {"wait_for_model": True}},
                    timeout=30,
                )
            if resp.status_code != 200:
                if not hasattr(self, '_warned'):
                    logger.warning("HuggingFace API returned %d, falling back to TF-IDF", resp.status_code)
                    self._warned = True
                return None

            data = resp.json()
            # HuggingFace returns a list of token embeddings; mean-pool to get sentence embedding
            if isinstance(data, list) and len(data) > 0:
                if isinstance(data[0], list) and isinstance(data[0][0], list):
                    # Token-level embeddings: [batch][tokens][dims] → mean pool
                    token_embs = np.array(data[0], dtype=np.float32)
                    sentence_emb = token_embs.mean(axis=0)
                elif isinstance(data[0], list):
                    # Already a sentence embedding
                    sentence_emb = np.array(data[0], dtype=np.float32)
                else:
                    sentence_emb = np.array(data, dtype=np.float32)

                # Ensure 384 dimensions (pad or truncate)
                if len(sentence_emb) > self.dim:
                    sentence_emb = sentence_emb[:self.dim]
                elif len(sentence_emb) < self.dim:
                    padded = np.zeros(self.dim, dtype=np.float32)
                    padded[:len(sentence_emb)] = sentence_emb
                    sentence_emb = padded

                # L2 normalize
                norm = np.linalg.norm(sentence_emb)
                if norm > 0:
                    sentence_emb = sentence_emb / norm

                return sentence_emb.tolist()

            return None

        except Exception as e:
            logger.warning("HuggingFace embedding failed: %s, falling back to TF-IDF", e)
            return None

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Embed multiple texts. Returns None for failed embeddings."""
        return [self.embed(t) for t in texts]


class EmbeddingProvider:
    """Unified embedding provider with backend stickiness.

    Tries HuggingFace API first, falls back to TF-IDF on the *first*
    failure.  Once a backend succeeds, it becomes the *sticky* backend
    for the process lifetime — subsequent failures do NOT silently
    switch backends.  Instead, they log loudly and raise, so operators
    notice the degradation rather than getting silently inconsistent
    embeddings.

    Usage:
        provider = EmbeddingProvider()
        provider.fit(corpus_texts)  # Build TF-IDF IDF from corpus
        vector = provider.embed("text")  # → list[float] of 384 dims

    The provider tracks which backend was used for each embedding,
    enabling observability of the embedding pipeline.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self._tfidf = TFIDFEmbedder(dim=dim)
        self._hf = HuggingFaceEmbedder(dim=dim)
        self._hf_success_count = 0
        self._tfidf_fallback_count = 0
        # Backend stickiness (Fix 6): once a backend succeeds, lock it.
        self._sticky_backend: str | None = None  # "huggingface" or "tfidf"

    def fit(self, corpus: list[str]) -> "EmbeddingProvider":
        """Fit the TF-IDF fallback on a corpus."""
        self._tfidf.fit(corpus)
        return self

    @property
    def active_backend(self) -> str:
        """Return the name of the currently sticky backend, or 'uninitialized'."""
        return self._sticky_backend or "uninitialized"

    def embed(self, text: str) -> list[float]:
        """Embed text with backend stickiness.

        If no backend has succeeded yet, try HuggingFace first, then
        TF-IDF.  Once a backend succeeds, it becomes sticky — subsequent
        calls use the same backend.  If the sticky backend fails, log
        loudly and raise rather than silently switching.
        """
        # First embedding ever — try HF, fall back to TF-IDF
        if self._sticky_backend is None:
            result = self._hf.embed(text)
            if result is not None:
                self._sticky_backend = "huggingface"
                self._hf_success_count += 1
                return result
            # HF failed on first try — use TF-IDF and stick with it
            self._sticky_backend = "tfidf"
            self._tfidf_fallback_count += 1
            logger.warning(
                "Embedding backend: HuggingFace unavailable on first call, "
                "sticking with TF-IDF for process lifetime."
            )
            return self._tfidf.embed(text)

        # Sticky backend is set — use it exclusively
        if self._sticky_backend == "huggingface":
            result = self._hf.embed(text)
            if result is not None:
                self._hf_success_count += 1
                return result
            # Sticky backend failed — do NOT silently switch
            logger.error(
                "Embedding backend STICKINESS VIOLATION: HuggingFace was "
                "the sticky backend but failed. Raising to alert operators. "
                "If you need to switch backends, restart the process."
            )
            raise RuntimeError(
                "Sticky embedding backend 'huggingface' failed. "
                "Restart the process to re-select a backend."
            )
        else:
            # TF-IDF is sticky — it's local and should never fail
            self._tfidf_fallback_count += 1
            return self._tfidf.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts with the sticky backend."""
        return [self.embed(t) for t in texts]

    @property
    def stats(self) -> dict[str, int]:
        """Return embedding source statistics for observability."""
        return {
            "huggingface_calls": self._hf_success_count,
            "tfidf_fallbacks": self._tfidf_fallback_count,
            "total_embeddings": self._hf_success_count + self._tfidf_fallback_count,
            "sticky_backend": self._sticky_backend or "none",
        }

    @property
    def backend_status(self) -> str:
        """Human-readable status of the embedding backend."""
        if self._sticky_backend == "huggingface":
            return "huggingface (sticky)"
        elif self._sticky_backend == "tfidf":
            return "tfidf (sticky — HF unavailable or failed)"
        elif self._hf._available:
            return "huggingface available, not yet used (will try HF first)"
        else:
            return "tfidf only (set HF_TOKEN for neural embeddings)"


# Convenience functions
_default_provider: EmbeddingProvider | None = None


def get_embedder() -> EmbeddingProvider:
    """Get the default embedding provider instance."""
    global _default_provider
    if _default_provider is None:
        _default_provider = EmbeddingProvider()
    return _default_provider


def embed_text(text: str, corpus: list[str] | None = None) -> list[float]:
    """Embed a single text string.

    Args:
        text: Text to embed.
        corpus: Optional corpus for TF-IDF fitting.

    Returns:
        List of 384 floats for CockroachDB VECTOR column.
    """
    global _default_provider
    if _default_provider is None:
        _default_provider = EmbeddingProvider()
    if corpus and not _default_provider._tfidf._fitted:
        _default_provider.fit(corpus)
    return _default_provider.embed(text)


def embed_artifacts(
    artifacts: list[Any],
    corpus_texts: list[str] | None = None,
) -> list[list[float]]:
    """Embed a batch of artifacts using a shared, fit-once embedder.

    Promotes demo behavior P3: the embedder is fitted once on the full
    corpus (all artifact texts), then reused for every artifact embedding.
    This ensures IDF weights are consistent across the corpus and avoids
    re-fitting per artifact.

    Args:
        artifacts: List of objects with a ``content`` attribute or a
            ``_artifact_to_text``-compatible structure.  Each is converted
            to text and embedded.
        corpus_texts: Optional pre-extracted corpus texts.  If not
            provided, texts are extracted from ``artifacts``.

    Returns:
        List of 384-dim embedding vectors, one per artifact (same order).
    """
    provider = get_embedder()

    # Extract texts from artifacts
    texts: list[str] = []
    for art in artifacts:
        if isinstance(art, str):
            texts.append(art)
        elif hasattr(art, "content") and hasattr(art.content, "headings"):
            # KnowledgeArtifact-like object
            parts: list[str] = []
            if hasattr(art, "title") and art.title:
                parts.append(f"# {art.title}")
            for h in art.content.headings:
                parts.append(f"{'#' * h.level} {h.text}")
            for p in getattr(art.content, "paragraphs", []):
                parts.append(p.text)
            texts.append("\n\n".join(parts))
        elif hasattr(art, "content") and isinstance(art.content, str):
            texts.append(art.content)
        else:
            texts.append(str(art))

    # Fit-once on the full corpus
    if not provider._tfidf._fitted:
        provider.fit(corpus_texts or texts)

    # Batch embed
    return provider.embed_batch(texts)
