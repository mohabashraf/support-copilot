"""retrieve + draft helpers (code, no LLM): which queries to search, which chunks may be used,
checks on the drafter's answer, and the citations shown to the agent.

Chunks that are never given to the drafter (ARCHITECTURE §7):
    defective   broken text (withdraw steps copied from deposit)
    promo       marketing, not policy
    compliance  legal access notice: never interpreted for a customer (eligibility → compliance)
"""

from copilot.pipeline.llm import LLMOutputError
from copilot.pipeline.schemas import Classification, DraftOut

EXCLUDED_FLAGS = ("defective", "promo", "compliance")
MAX_QUERIES = 4  # the triage search query + up to 3 answerable intents


def queries(c: Classification, blocked: list[int]) -> list[str]:
    """The English search query from triage, then each answerable intent's own quote (bge-m3 is
    cross-lingual, so an Arabic quote finds the English page). Blocked parts are never searched."""
    out = [c.search_query] if c.search_query.strip() else []
    out += [it.text for i, it in enumerate(c.intents) if i not in blocked and it.text.strip()]
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq[:MAX_QUERIES]


def merge(hit_lists: list[list], k: int) -> list:
    """Best score per chunk across the queries, top k."""
    best: dict[str, object] = {}
    for hits in hit_lists:
        for h in hits:
            if h.chunk_id not in best or h.score > best[h.chunk_id].score:
                best[h.chunk_id] = h
    return sorted(best.values(), key=lambda h: -h.score)[:k]


def source(hit) -> dict:
    c = hit.chunk
    pages = [s.model_dump() for s in c.sources]
    return {"id": c.id, "section": c.section, "text": c.text, "flags": list(c.flags),
            "pages": pages, "score": round(hit.score, 4)}  # fmt: skip


def check_draft(d: DraftOut, source_ids: set[str], has_question: bool) -> DraftOut:
    """Rules the schema can't express. A violation raises → the node retries once → a person."""
    unknown = [i for i in d.cited_chunk_ids if i not in source_ids]
    if unknown:
        raise LLMOutputError(f"draft: cited ids not in the sources: {unknown}")
    if not d.abstain and not d.reply.strip():
        # Nothing to say (e.g. a ticket that is only a masked card number): that IS an
        # abstention. Retrying would cost LLM calls and end as technical_failure; a person
        # answers either way, so route it as an abstention with a note (no customer text).
        note = "No reply drafted: the message has nothing the help pages can answer."
        return d.model_copy(update={"abstain": True, "partial": False, "cited_chunk_ids": [],
                                    "note_for_agent": d.note_for_agent or note})  # fmt: skip
    if not d.abstain and has_question and source_ids and not d.cited_chunk_ids:
        raise LLMOutputError("draft: answer without any citation")
    return d


def citations(cited: list[str], sources: list[dict]) -> list[dict]:
    """One citation per (chunk, page): the agent sees which page and section each fact is from."""
    by_id = {s["id"]: s for s in sources}
    return [
        {
            "chunk_id": cid,
            "page_title": p["page_title"],
            "page_url": p["page_url"],
            "section": by_id[cid]["section"],
        }  # fmt: skip
        for cid in cited
        if cid in by_id
        for p in by_id[cid]["pages"]
    ]
