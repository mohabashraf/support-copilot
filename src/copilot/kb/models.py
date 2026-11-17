"""Data models for the knowledge base."""

from typing import Literal

from pydantic import BaseModel, Field

BlockKind = Literal["intro", "steps", "faq", "promo", "notice"]
Flag = Literal["conflict", "defective", "rule_sensitive", "compliance", "gap_link", "route", "promo"]


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


class KnowledgeBase(BaseModel):
    chunks: list[Chunk]
    kb_hash: str  # changes only when the source pages or the config change

    def get(self, chunk_id: str) -> Chunk:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        raise KeyError(chunk_id)
