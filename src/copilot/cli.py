"""Command-line interface for one-off jobs: kb show/index, search, eval retrieval.

- `kb index` is the INGESTION job: the only writer of the KB in Postgres (D8).
- `kb show` reads the raw pages (what the next `kb index` would write).
- `search` / `eval retrieval` are read-only, like the server.
The server is NOT started here: run `uv run python -m copilot.main`.
"""

import json
from pathlib import Path

import typer

from copilot.config import get_settings
from copilot.kb.ingest import build_kb

app = typer.Typer(help="Support-ticket co-pilot")
kb_app = typer.Typer(help="Knowledge-base commands")
app.add_typer(kb_app, name="kb")
eval_app = typer.Typer(help="Evaluation commands")
app.add_typer(eval_app, name="eval")


@app.callback()
def _setup() -> None:
    """Runs before every command: configure logging from settings."""
    from copilot.log import setup_logging

    st = get_settings()
    setup_logging(st)


@kb_app.command("show")
def kb_show(chunk_id: str = typer.Argument(None, help="Show one chunk in full")) -> None:
    """List all chunks from the raw pages, or show one."""
    kb = build_kb()
    if chunk_id:
        try:
            c = kb.get(chunk_id)
        except KeyError:
            typer.echo(f"unknown chunk {chunk_id!r}; run `copilot kb show` to list ids", err=True)
            raise typer.Exit(1) from None
        typer.echo(
            f"{c.id}  [{', '.join(c.flags) or 'no flags'}]\n"
            f"{c.section}\n{', '.join(c.page_urls)}\n\n{c.text}"
        )
        return
    for c in kb.chunks:
        flags = f" [{', '.join(c.flags)}]" if c.flags else ""
        typer.echo(f"{c.id:32} {len(c.sources)} page(s){flags}  {c.text[:70]}…")


def _retriever(model: str | None, store: str | None):
    """Read-only retriever, like the server's: settings from .env, flags override them."""
    from copilot.services import NotIndexedError, load_services

    st = get_settings()
    st = st.model_copy(
        update={
            "embedding_model": model or st.embedding_model,
            "vector_store": store or st.vector_store,
        }
    )
    try:
        r = load_services(st).retriever
    except NotIndexedError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(1) from None
    return r, st.embedding_model, st.vector_store


@kb_app.command("index")
def kb_index(
    model: str = typer.Option(None, help="Embedding model (default from settings)"),
) -> None:
    """INGESTION (D8): write the KB + vectors to Postgres. Embeds only new/changed chunks.

    The only command that writes the knowledge base. Run it after the pages or
    kb_config.toml change, and once per embedding model.
    """
    from copilot.kb.store import KBStore
    from copilot.retrieval.embeddings import get_embedder
    from copilot.retrieval.index import PgVectorIndex
    from copilot.retrieval.indexer import index_kb

    st = get_settings()
    model = model or st.embedding_model
    kb, embedder = build_kb(), get_embedder(model)
    index = PgVectorIndex(st.database_url, model, embedder.dim)
    d = index_kb(kb, embedder, index, KBStore(st.database_url))
    typer.echo(
        f"kb {kb.kb_hash} · model {model} · added {len(d.added)} · changed {len(d.changed)} · "
        f"removed {len(d.removed)} · unchanged {d.unchanged}"
    )


@app.command("search")
def search(
    query: str, k: int = 5, model: str = typer.Option(None), store: str = typer.Option(None)
) -> None:
    """Search the KB: top-k chunks with similarity scores."""
    r, model, store = _retriever(model, store)
    for h in r.search(query, k=k):
        typer.echo(f"{h.rank}. {h.score:.3f}  {h.chunk_id:30} {r.kb.get(h.chunk_id).section[:60]}")


@eval_app.command("retrieval")
def eval_retrieval(
    model: str = typer.Option(None),
    store: str = typer.Option(None),
    out: str = typer.Option(None, help="Write the full JSON report here"),
) -> None:
    """Recall@k, precision@k, MRR, nDCG and abstain threshold.

    Runs eval/retrieval_queries.jsonl and the ticket texts in eval/tickets_labelled.jsonl.
    """
    from copilot.eval.retrieval import run

    r, model, store = _retriever(model, store)
    rep = run(r, model, store)
    q, t, a = rep["queries"], rep["tickets"], rep["abstain"]
    typer.echo(f"model {model} · store {store} · kb {rep['kb_hash']}")
    typer.echo(
        f"QUERIES (n={q['n']}): R@1 {q['r1']}  R@3 {q['r3']}  R@5 {q['r5']}  P@3 {q['p3']}  "
        f"Hit@3 {q['hit3']}  MRR {q['mrr']}  nDCG@5 {q['ndcg5']}"
    )
    typer.echo(f"  Hit@3 by language: {q['hit3_by_lang']}")
    typer.echo(f"TICKETS (n={t['n']}): R@3 {t['r3']}  Hit@3 {t['hit3']}  MRR {t['mrr']}")
    typer.echo(
        f"ABSTAIN: AUROC {a['auroc']}  chosen t={a['chosen']['t']} → abstain recall "
        f"{a['chosen']['abstain_recall']:.2f}, false-abstain {a['chosen']['false_abstain']:.2f}"
    )
    typer.echo(
        f"  lowest answerable best-score {a['answerable_min_best']}; "
        f"no-answer best-scores {a['no_answer_best_scores']}"
    )
    typer.echo(f"FAILURES (R@3 < 1): {len(rep['failures'])}")
    for f in rep["failures"][:12]:
        typer.echo(f"  {f['id']:4} {f['lang']:5} R@3={f['r3']:.2f}  top3={f['top3']}")
    if out:
        Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        typer.echo(f"report → {out}")
