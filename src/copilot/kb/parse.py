"""Parse one help page (HTML) into blocks. No IDs or flags here: that is ingest's job."""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from copilot.kb.models import BlockKind


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    heading: str
    text: str


def normalise(s: str) -> str:
    """Collapse whitespace and tidy spaces before punctuation. Nothing else is changed."""
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s+([.,!?;:])", r"\1", s)


def _sentence(s: str) -> str:
    """End with punctuation so joined steps stay readable. Adds a full stop only, never words."""
    return s if s.endswith((".", "!", "?")) else f"{s}."


def _text(tag: Tag | None) -> str:
    return normalise(tag.get_text(" ", strip=True)) if tag else ""


def parse_page(html: str) -> tuple[str, list[Block]]:
    """Return (page title, blocks in page order)."""
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[Block] = []

    # Intro: page title + the paragraph right after it.
    h1 = soup.select_one("h1.page__title")
    title = _text(h1)
    intro_p = h1.find_next_sibling("p") if h1 else None
    blocks.append(Block("intro", title, f"{title}. {_text(intro_p)}"))

    # Steps: "step N" label + heading + optional description, joined into one block.
    steps = []
    for wrap in soup.select(".seps-list__content-wrap"):
        label = _text(wrap.select_one(".seps-list__label"))
        heading = _text(wrap.find("h2"))
        desc = " ".join(_text(p) for p in wrap.find_all("p") if not p.find_parent(class_="seps-list__label"))
        steps.append(_sentence(f"{label.capitalize()}: {heading}") + (f" {_sentence(desc)}" if desc else ""))
    if steps:
        blocks.append(Block("steps", f"{title}: steps", " ".join(steps)))

    # FAQ: one block per question.
    for item in soup.select(".accordion__item"):
        q = _text(item.find("h4"))
        a = _text(item.select_one(".accordion__text"))
        blocks.append(Block("faq", q, f"{q} {a}"))

    # Promo block ("Trade from every device").
    promo = soup.select_one("section.trade-device")
    if promo:
        h2 = _text(promo.find("h2"))
        p = _text(promo.find("p"))
        blocks.append(Block("promo", h2, f"{h2}. {p}"))

    # Access notice (modal present on every page).
    notice_title = soup.select_one("#uk-modal-title")
    if notice_title:
        body = soup.select_one(".uk-modal__body")
        paras = [_text(p) for p in body.find_all("p")] if body else []
        blocks.append(Block("notice", _text(notice_title), " ".join([_text(notice_title) + "."] + paras)))

    return title, blocks
