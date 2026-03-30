"""Embedding service for MUSE memory system.

Provides vector embeddings via sentence-transformers for semantic search
and similarity computation across the memory tiers.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

import numpy as np


class EmbeddingService:
    """Thread-safe embedding service using sentence-transformers.

    The model is lazily loaded on the first call to embed() or embed_batch(),
    ensuring fast import times and no unnecessary resource allocation.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model: Optional[object] = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> None:
        """Load the model if not already loaded. Thread-safe."""
        if self._model is not None:
            return
        with self._lock:
            # Double-checked locking
            if self._model is not None:
                return
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)

    def embed(self, text: str) -> list[float]:
        """Compute a 384-dimensional embedding for a single text string.

        Args:
            text: The input text to embed.

        Returns:
            A list of 384 floats representing the embedding vector.
        """
        self._ensure_model()
        vector = self._model.encode(text, normalize_embeddings=True)
        return vector.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of texts.

        Args:
            texts: A list of input texts to embed.

        Returns:
            A list of embedding vectors, one per input text.
        """
        if not texts:
            return []
        self._ensure_model()
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vectors]

    async def embed_async(self, text: str) -> list[float]:
        """Async wrapper that runs embed() in a thread to avoid blocking the event loop."""
        return await asyncio.to_thread(self.embed, text)

    async def embed_batch_async(self, texts: list[str]) -> list[list[float]]:
        """Async wrapper that runs embed_batch() in a thread to avoid blocking the event loop."""
        if not texts:
            return []
        return await asyncio.to_thread(self.embed_batch, texts)

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Uses numpy for vectorized computation instead of Python loops.

        Args:
            a: First embedding vector.
            b: Second embedding vector.

        Returns:
            Cosine similarity in the range [-1.0, 1.0].
        """
        a_arr = np.asarray(a, dtype=np.float32)
        b_arr = np.asarray(b, dtype=np.float32)

        norm_a = np.linalg.norm(a_arr)
        norm_b = np.linalg.norm(b_arr)

        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        return float(np.dot(a_arr, b_arr) / (norm_a * norm_b))
