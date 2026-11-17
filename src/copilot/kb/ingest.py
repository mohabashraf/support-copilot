"""Build the knowledge base: parse pages → assign chunk IDs → merge duplicates → attach flags → hash."""

import hashlib
import json
import tomllib
from pathlib import Path

from copilot.kb.models import Chunk, KnowledgeBase, Source
from copilot.kb.parse import parse_page

KB_DIR = Path(__file__).resolve().parents[3] / "kb"


class IngestError(Exception):
    pass


def _chunk_id(page_slug: str, kind: str, heading: str, faq_ids: dict[str, str]) -> str:
    if kind == "faq":
        if heading not in faq_ids:
            # Fail loudly: a new FAQ on the site must be given an ID (and reviewed) on purpose.
            raise IngestError(f"FAQ not in kb_config.toml [faq_ids]: {heading!r}")
        return faq_ids[heading]
    if kind == "notice":
        return "notice#access"
    if kind == "promo":
        return "promo#devices"
    return f"{page_slug}#{kind}"  # intro / steps


def build_kb(kb_dir: Path = KB_DIR) -> KnowledgeBase:
    config = tomllib.loads((kb_dir / "kb_config.toml").read_text(encoding="utf-8"))
    faq_ids: dict[str, str] = config["faq_ids"]
    flags: dict[str, list[str]] = config.get("flags", {})

    chunks: dict[str, Chunk] = {}  # insertion order = first appearance in page order
    for page in config["pages"]:
        html = (kb_dir / "raw" / page["file"]).read_text(encoding="utf-8")
        title, blocks = parse_page(html)
        source = Source(page_title=title, page_url=page["url"])
        for block in blocks:
            cid = _chunk_id(page["slug"], block.kind, block.heading, faq_ids)
            if cid in chunks:
                existing = chunks[cid]
                # Same ID on another page: must be the same text, otherwise it's a conflict to review.
                if existing.text != block.text:
                    raise IngestError(f"{cid}: same ID, different text on {page['url']}")
                existing.sources.append(source)
            else:
                chunks[cid] = Chunk(
                    id=cid,
                    kind=block.kind,
                    section=block.heading,
                    text=block.text,
                    sources=[source],
                    flags=flags.get(cid, []),
                )

    unknown = set(flags) - set(chunks)
    if unknown:
        raise IngestError(f"flags configured for chunks that don't exist: {sorted(unknown)}")

    payload = json.dumps([c.model_dump() for c in chunks.values()], ensure_ascii=False, sort_keys=True)
    kb_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return KnowledgeBase(chunks=list(chunks.values()), kb_hash=kb_hash)


def write_kb(kb: KnowledgeBase, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(kb.model_dump_json(indent=1), encoding="utf-8")
