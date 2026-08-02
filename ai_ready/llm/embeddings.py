"""Local embedding provider with TF-IDF and HuggingFace API support.

Produces fixed-dimension (384) vectors from text for storage in CockroachDB's
VECTOR(384) column and similarity search via the <=> cosine distance operator.

Two embedding backends:
1. HuggingFace Inference API — high-quality neural embeddings (all-MiniLM-L6-v2)
   Requires HF_TOKEN env var. Free tier with rate limits.
2. TF-IDF + numpy — lightweight, dependency-free, deterministic
   Automatic fallback when HuggingFace is unavailable or rate-limited.

Usage:
    from ai_ready.llm.embeddings import EmbeddingProvider

    provider = EmbeddingProvider()
    provider.fit(corpus_texts)  # Build IDF for TF-IDF fallback
    vector = provider.embed("text to embed")  # → list[float] of 384 dims

The provider tries HuggingFace first, falls back to TF-IDF automatically.
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
                logger.warning("HuggingFace API returned %d, falling back to TF-IDF", resp.status_code)
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
    """Unified embedding provider with automatic fallback.

    Tries HuggingFace API first, falls back to TF-IDF on any failure.
    This ensures embeddings always work, even offline or rate-limited.

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

    def fit(self, corpus: list[str]) -> "EmbeddingProvider":
        """Fit the TF-IDF fallback on a corpus."""
        self._tfidf.fit(corpus)
        return self

    def embed(self, text: str) -> list[float]:
        """Embed text, trying HuggingFace first, falling back to TF-IDF."""
        result = self._hf.embed(text)
        if result is not None:
            self._hf_success_count += 1
            return result

        self._tfidf_fallback_count += 1
        return self._tfidf.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts with fallback."""
        return [self.embed(t) for t in texts]

    @property
    def stats(self) -> dict[str, int]:
        """Return embedding source statistics for observability."""
        return {
            "huggingface_calls": self._hf_success_count,
            "tfidf_fallbacks": self._tfidf_fallback_count,
            "total_embeddings": self._hf_success_count + self._tfidf_fallback_count,
        }

    @property
    def backend_status(self) -> str:
        """Human-readable status of the embedding backend."""
        if self._hf._available and self._hf_success_count > 0:
            return "huggingface (with tfidf fallback)"
        elif self._hf._available:
            return "huggingface available, tfidf used (check HF token/rate limits)"
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
