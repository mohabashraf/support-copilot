"""Embedding models (D2): text → vector. Multilingual models are required: tickets come in many
languages, the KB is English, so an Arabic question must land near the English answer."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class ModelSpec:
    hf_name: str  # name on Hugging Face
    dim: int  # vector length
    query_prefix: str = ""  # some models are trained with prefixes and work worse without them
    passage_prefix: str = ""


MODELS: dict[str, ModelSpec] = {
    # English-only BASELINE: expected to fail cross-lingual queries (the point of the comparison).
    "all-minilm-l6": ModelSpec("sentence-transformers/all-MiniLM-L6-v2", 384),
    # Multilingual, small (~0.5 GB). e5 models need "query: " / "passage: " prefixes.
    "multilingual-e5-small": ModelSpec(
        "intfloat/multilingual-e5-small", 384, "query: ", "passage: "
    ),
    # Multilingual, large (~2 GB). No prefixes.
    "bge-m3": ModelSpec("BAAI/bge-m3", 1024),
}


class Embedder(Protocol):
    name: str
    dim: int

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...
    def embed_queries(self, texts: list[str]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    """Runs locally: no ticket text leaves the machine for embeddings."""

    def __init__(self, name: str):
        if name not in MODELS:
            raise ValueError(f"unknown embedding model {name!r}; choose one of {sorted(MODELS)}")
        from sentence_transformers import SentenceTransformer  # heavy import, only when used

        self.name = name
        self.spec = MODELS[name]
        self.dim = self.spec.dim
        self._model = SentenceTransformer(self.spec.hf_name)

    def _embed(self, texts: list[str], prefix: str) -> np.ndarray:
        # normalize_embeddings=True → vectors have length 1, so dot product == cosine similarity.
        vecs = self._model.encode([prefix + t for t in texts], normalize_embeddings=True)
        return np.asarray(vecs, dtype=np.float32)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, self.spec.passage_prefix)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, self.spec.query_prefix)


@lru_cache(maxsize=4)
def get_embedder(name: str) -> SentenceTransformerEmbedder:
    """Load each model once per process (loading takes seconds)."""
    return SentenceTransformerEmbedder(name)
