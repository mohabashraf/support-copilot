"""Command-line interface: kb build/show/index, search, eval retrieval, web."""

from pathlib import Path

import typer

import json

from copilot.config import get_settings
from copilot.kb.ingest import KB_DIR, build_kb, write_kb

app = typer.Typer(help="Support-ticket co-pilot")
kb_app = typer.Typer(help="Knowledge-base commands")
app.add_typer(kb_app, name="kb")
eval_app = typer.Typer(help="Evaluation commands")
app.add_typer(eval_app, name="eval")

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


@app.command("web")
def web(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the web app (open http://127.0.0.1:8000/docs)."""
    import uvicorn

    uvicorn.run("copilot.api.app:create_app", factory=True, host=host, port=port, reload=reload)


def _retriever(model: str | None, store: str | None):
    from copilot.retrieval.search import make_retriever

    st = get_settings()
    model, store = model or st.embedding_model, store or st.vector_store
    return make_retriever(build_kb(), store, model, st.database_url), model, store


@kb_app.command("index")
def kb_index(model: str = typer.Option(None, help="Embedding model (default from settings)"),
             store: str = typer.Option(None, help="pgvector | memory")) -> None:
    """Embed all chunks and store them in the vector index."""
    r, model, store = _retriever(model, store)
    r.build_index()
    typer.echo(f"indexed {len(r.kb.chunks)} chunks · model {model} · store {store} · kb {r.kb.kb_hash}")


@app.command("search")
def search(query: str, k: int = 5, model: str = typer.Option(None), store: str = typer.Option(None)) -> None:
    """Search the KB: top-k chunks with similarity scores."""
    r, model, store = _retriever(model, store)
    for h in r.search(query, k=k):
        typer.echo(f"{h.rank}. {h.score:.3f}  {h.chunk_id:30} {r.kb.get(h.chunk_id).section[:60]}")


@eval_app.command("retrieval")
def eval_retrieval(model: str = typer.Option(None), store: str = typer.Option(None),
                   out: str = typer.Option(None, help="Write the full JSON report here")) -> None:
    """Recall@k, precision@k, MRR, nDCG, abstain threshold on eval/retrieval_queries.jsonl + ticket texts."""
    from copilot.eval.retrieval import run

    r, model, store = _retriever(model, store)
    rep = run(r, model, store)
    q, t, a = rep["queries"], rep["tickets"], rep["abstain"]
    typer.echo(f"model {model} · store {store} · kb {rep['kb_hash']}")
    typer.echo(f"QUERIES (n={q['n']}): R@1 {q['r1']}  R@3 {q['r3']}  R@5 {q['r5']}  P@3 {q['p3']}  "
               f"Hit@3 {q['hit3']}  MRR {q['mrr']}  nDCG@5 {q['ndcg5']}")
    typer.echo(f"  Hit@3 by language: {q['hit3_by_lang']}")
    typer.echo(f"TICKETS (n={t['n']}): R@3 {t['r3']}  Hit@3 {t['hit3']}  MRR {t['mrr']}")
    typer.echo(f"ABSTAIN: AUROC {a['auroc']}  chosen t={a['chosen']['t']} → abstain recall "
               f"{a['chosen']['abstain_recall']:.2f}, false-abstain {a['chosen']['false_abstain']:.2f}")
    typer.echo(f"  lowest answerable best-score {a['answerable_min_best']}; no-answer best-scores {a['no_answer_best_scores']}")
    typer.echo(f"FAILURES (R@3 < 1): {len(rep['failures'])}")
    for f in rep["failures"][:12]:
        typer.echo(f"  {f['id']:4} {f['lang']:5} R@3={f['r3']:.2f}  top3={f['top3']}")
    if out:
        Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        typer.echo(f"report → {out}")
