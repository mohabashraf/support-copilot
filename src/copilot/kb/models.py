"""Data models for the knowledge base."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

BlockKind = Literal["intro", "steps", "faq", "promo", "notice"]
Flag = Literal[
    "conflict", "defective", "rule_sensitive", "compliance", "gap_link", "route", "promo"
]


class Source(BaseModel):
    """A page a chunk comes from. A chunk can have several (e.g. the shared FAQ)."""

    page_title: str
    page_url: str


class Chunk(BaseModel):
    """One citable piece of the KB: a section, a step list, or one FAQ entry."""

    id: str  # e.g. "faq#min-deposit"; stable across runs
    kind: BlockKind
    section: str  # heading as written on the page (FAQ question, page title, ...)
    text: str  # source text, whitespace-normalised only, never rewritten
    sources: list[Source] = Field(min_length=1)
    flags: list[Flag] = []

    @property
    def page_urls(self) -> list[str]:
        return [s.page_url for s in self.sources]

    @property
    def embed_text(self) -> str:
        """What the embedding model sees: heading + text."""
        return f"{self.section}\n{self.text}"

    @property
    def content_hash(self) -> str:
        """Fingerprint of this chunk. Changes when its text, sources or flags change, so
        ingestion re-embeds only the chunks whose fingerprint changed."""
        return _hash(self.model_dump())


class KnowledgeBase(BaseModel):
    chunks: list[Chunk]
    kb_hash: str  # changes only when the source pages or the config change

    @classmethod
    def from_chunks(cls, chunks: list[Chunk]) -> "KnowledgeBase":
        return cls(chunks=chunks, kb_hash=_hash([c.model_dump() for c in chunks]))

    @property
    def chunk_hashes(self) -> dict[str, str]:
        """chunk_id → content_hash: what an up-to-date index must contain."""
        return {c.id: c.content_hash for c in self.chunks}

    def get(self, chunk_id: str) -> Chunk:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        raise KeyError(chunk_id)


def _hash(obj: object) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Diff:
    """What changed between the KB we want and what a store currently holds."""

    added: list[str]
    changed: list[str]
    removed: list[str]
    unchanged: int

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def diff(wanted: dict[str, str], stored: dict[str, str]) -> Diff:
    """Compare chunk_id → content_hash maps."""
    return Diff(
        added=[i for i in wanted if i not in stored],
        changed=[i for i in wanted if i in stored and stored[i] != wanted[i]],
        removed=[i for i in stored if i not in wanted],
        unchanged=sum(1 for i in wanted if stored.get(i) == wanted[i]),
    )
