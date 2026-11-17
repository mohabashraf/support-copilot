"""Command-line interface: `copilot kb build`, `copilot kb show`."""

from pathlib import Path

import typer

from copilot.kb.ingest import KB_DIR, build_kb, write_kb

app = typer.Typer(help="Support-ticket co-pilot")
kb_app = typer.Typer(help="Knowledge-base commands")
app.add_typer(kb_app, name="kb")

BUILD_PATH = KB_DIR / "build" / "chunks.json"


@kb_app.command("build")
def kb_build(out: Path = BUILD_PATH) -> None:
    """Parse the 4 pages and write the chunks."""
    kb = build_kb()
    write_kb(kb, out)
    typer.echo(f"{len(kb.chunks)} chunks · hash {kb.kb_hash} → {out}")


@kb_app.command("show")
def kb_show(chunk_id: str = typer.Argument(None, help="Show one chunk in full")) -> None:
    """List all chunks, or show one."""
    kb = build_kb()
    if chunk_id:
        c = kb.get(chunk_id)
        typer.echo(f"{c.id}  [{', '.join(c.flags) or 'no flags'}]\n{c.section}\n{', '.join(c.page_urls)}\n\n{c.text}")
        return
    for c in kb.chunks:
        flags = f" [{', '.join(c.flags)}]" if c.flags else ""
        typer.echo(f"{c.id:32} {len(c.sources)} page(s){flags}  {c.text[:70]}…")
