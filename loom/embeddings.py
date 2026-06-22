"""Embedding service with safe fallback behavior.

``embed()`` never raises on runtime provider failures; it returns ``None`` so
callers fall back to text search. Identical normalized inputs are cached with an
in-process LRU keyed by SHA-256.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import structlog

from loom.errors import EmbeddingError
from loom.utils import normalize_whitespace

if TYPE_CHECKING:
    from loom.config import LoomConfig

logger = structlog.get_logger("loom.embeddings")


class EmbeddingService:
    """Generates vector embeddings with safe fallback behavior."""

    def __init__(self, config: LoomConfig):
        # Programmer-error guard: config must be a LoomConfig-like object.
        if not hasattr(config, "embedding_provider"):
            raise EmbeddingError("EmbeddingService requires a LoomConfig instance.")
        self.config = config
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._disabled_logged = False

    def _log_disabled_once(self, reason: str, level: str = "warning") -> None:
        if self._disabled_logged:
            return
        self._disabled_logged = True
        getattr(logger, level)("embeddings_disabled", reason=reason)

    def _cache_get(self, key: str) -> list[float] | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, key: str, value: list[float]) -> None:
        max_size = self.config.embedding_cache_size
        if max_size <= 0:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > max_size:
            self._cache.popitem(last=False)

    async def embed(self, text: str) -> list[float] | None:
        if self.config.embedding_provider == "none":
            self._log_disabled_once("embedding_provider is none", level="info")
            return None
        if not self.config.embedding_api_key:
            self._log_disabled_once("embedding api key missing; using text search")
            return None

        if not text or not text.strip():
            return None

        normalized = normalize_whitespace(text)[: self.config.embedding_input_max_chars]
        if not normalized:
            return None

        cache_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            import litellm

            litellm.suppress_debug_info = True

            kwargs: dict[str, Any] = {
                "model": self.config.embedding_model,
                "input": [normalized],
                "api_key": self.config.embedding_api_key,
            }
            # OpenAI embedding models can emit a chosen output size; request the
            # configured dimension so vectors fit the fixed VECTOR(n) column.
            if self.config.embedding_provider == "openai":
                kwargs["dimensions"] = self.config.embedding_dimension

            resp = await litellm.aembedding(**kwargs)
            vector = resp["data"][0]["embedding"]
        except Exception as exc:  # noqa: BLE001 - runtime failures fall back to text
            logger.warning(
                "embedding_failed",
                error_type=type(exc).__name__,
                text_length=len(normalized),
            )
            return None

        if len(vector) != self.config.embedding_dimension:
            logger.warning(
                "embedding_dimension_mismatch",
                got=len(vector),
                expected=self.config.embedding_dimension,
            )
            return None

        vector = [float(v) for v in vector]
        self._cache_put(cache_key, vector)
        return vector


def vector_to_pg(vector: list[float] | None) -> str | None:
    """Render a vector as a pgvector-compatible string literal, or None."""
    if vector is None:
        return None
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def pg_to_vector(value) -> list[float] | None:
    """Parse a pgvector string/sequence back into a list of floats."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    text = str(value).strip()
    if not text:
        return None
    text = text.strip("[]")
    if not text:
        return []
    return [float(part) for part in text.split(",")]
