"""Command-line interface for one-off jobs: kb show/index, search, eval retrieval.

- `kb index` is the INGESTION job: the only writer of the KB in Postgres (D8).
- `kb show` reads the raw pages (what the next `kb index` would write).
- `search` / `eval retrieval` are read-only, like the server.
The server is NOT started here: run `uv run python -m copilot.main`.
"""

import atexit
import json
from pathlib import Path

import typer

from copilot import telemetry
from copilot.config import get_settings
from copilot.db import open_pool
from copilot.eval import pii as pii_eval
from copilot.eval import retrieval as retrieval_eval
from copilot.eval import risks as risks_eval
from copilot.kb.ingest import build_kb
from copilot.kb.store import KBStore
from copilot.log import setup_logging
from copilot.retrieval.embeddings import get_embedder
from copilot.retrieval.index import PgVectorIndex
from copilot.retrieval.indexer import index_kb
from copilot.services import NotIndexedError, load_services
from copilot.tracing import configure_tracing, flush_traces

app = typer.Typer(help="Support-ticket co-pilot")
kb_app = typer.Typer(help="Knowledge-base commands")
keys_app = typer.Typer(help="API keys (server/auth.py)")
app.add_typer(keys_app, name="keys")
app.add_typer(kb_app, name="kb")
eval_app = typer.Typer(help="Evaluation commands")
app.add_typer(eval_app, name="eval")


@app.callback()
def _setup() -> None:
    """Runs before every command: configure logging from settings."""

    st = get_settings()
    setup_logging(st)
    if configure_tracing(st):
        atexit.register(flush_traces)  # traces are sent in the background
    if telemetry.setup_telemetry(st):
        atexit.register(telemetry.shutdown)


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


def _services(model: str | None, store: str | None):
    """Read-only services, like the server's: settings from .env, flags override them."""
    st = get_settings()
    st = st.model_copy(
        update={
            "embedding_model": model or st.embedding_model,
            "vector_store": store or st.vector_store,
        }
    )
    pool = open_pool(st) if st.vector_store == "pgvector" else None
    if pool is not None:
        atexit.register(pool.close)  # the retriever uses it until the command ends
    try:
        services = load_services(st, pool)
    except NotIndexedError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(1) from None
    return services, st.embedding_model, st.vector_store


@kb_app.command("index")
def kb_index(
    model: str = typer.Option(None, help="Embedding model (default from settings)"),
) -> None:
    """INGESTION (D8): write the KB + vectors to Postgres. Embeds only new/changed chunks.

    The only command that writes the knowledge base. Run it after the pages or
    kb_config.toml change, and once per embedding model.
    """
    st = get_settings()
    model = model or st.embedding_model
    kb, embedder = build_kb(), get_embedder(model)
    with open_pool(st) as pool:
        index = PgVectorIndex(pool, model, embedder.dim)
        d = index_kb(kb, embedder, index, KBStore(pool))
    typer.echo(
        f"kb {kb.kb_hash} · model {model} · added {len(d.added)} · changed {len(d.changed)} · "
        f"removed {len(d.removed)} · unchanged {d.unchanged} · embedded {d.embedded}"
    )


@app.command("search")
def search(
    query: str, k: int = 5, model: str = typer.Option(None), store: str = typer.Option(None)
) -> None:
    """Search the KB: top-k chunks with similarity scores."""
    services, model, store = _services(model, store)
    for h in services.retriever.search(query, k=k):
        typer.echo(f"{h.rank}. {h.score:.3f}  {h.chunk_id:30} {h.chunk.section[:60]}")


@eval_app.command("retrieval")
def eval_retrieval(
    model: str = typer.Option(None),
    store: str = typer.Option(None),
    out: str = typer.Option(None, help="Write the full JSON report here"),
) -> None:
    """Recall@k, precision@k, MRR, nDCG and abstain threshold.

    Runs eval/retrieval_queries.jsonl and the ticket texts in eval/tickets_labelled.jsonl.
    """

    services, model, store = _services(model, store)
    kb_hash, _ = services.kb.summary()
    rep = retrieval_eval.run(services.retriever, model, store, kb_hash)
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


@eval_app.command("pii")
def eval_pii(
    ner: str = typer.Option(None, help="gliner | spacy (default: PII_NER from .env)"),
    out: str = typer.Option(None, help="Write the full JSON report here"),
) -> None:
    """PII masking: value recall + keep precision on NEW cases; per-type scores on 76 tickets."""
    from copilot.pipeline.pii import Masker  # noqa: PLC0415

    st = get_settings()
    masker = Masker(ner or st.pii_ner, st.pii_gliner_model, st.pii_gliner_threshold)
    rep = pii_eval.run(masker)
    n, t = rep["new_cases"], rep["tickets"]
    typer.echo(
        f"NER {rep['ner']} · latency p50 {rep['latency_ms']['p50']} ms, "
        f"p95 {rep['latency_ms']['p95']} ms"
    )
    typer.echo(
        f"NEW CASES (n={n['n']}): value recall {n['value_recall']} "
        f"({n['leaked']}/{n['values']} leaked) · keep precision {n['keep_precision']} "
        f"({n['lost']}/{n['keeps']} wrongly masked)"
    )
    for f in n["failures"]:
        typer.echo(
            f"  {f['id']} {f['lang']}: missed {f['missed_values']} value(s), lost {f['lost_keep']}"
        )
    typer.echo(
        f"TICKETS (n={t['n']}): exact type match {t['exact_match']} · "
        f"residual-suspect tickets {t['residual_suspect_tickets']}"
    )
    for k, v in t["per_type"].items():
        typer.echo(
            f"  {k:8} precision {v['precision']}  recall {v['recall']}  "
            f"(tp {v['tp']} fp {v['fp']} fn {v['fn']})"
        )
    for f in t["failures"][:10]:
        typer.echo(f"  {f['id']}: expected {f['expected']} got {f['got']}")
    if out:
        Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        typer.echo(f"report → {out}")


@eval_app.command("safety")
def eval_safety() -> None:
    """classify_safety with the REAL model (~$0.04): recall per hard rule, LLM vs classifier."""
    from copilot.eval import safety as safety_eval  # noqa: PLC0415
    from copilot.pipeline.llm import chat_model  # noqa: PLC0415
    from copilot.pipeline.pii import Masker  # noqa: PLC0415
    from copilot.pipeline.risk_classifier import RiskClassifier  # noqa: PLC0415

    st = get_settings()
    chat = chat_model(st.llm_classify_model, st.anthropic_api_key, st.llm_timeout_s)
    clf = RiskClassifier(get_embedder(st.embedding_model), model_name=st.embedding_model)
    rep = safety_eval.run(chat, Masker.from_settings(st), clf)
    typer.echo(f"model {rep['model']} · prompt {rep['prompt_version']} · {rep['n']} · "
               f"cost ${rep['cost_usd']}")  # fmt: skip
    for split, fields in rep["report"].items():
        typer.echo(f"\n{split.upper():22} {'LLM':>16} {'classifier':>16} {'BOTH (gate)':>16}")
        for f, k in fields.items():
            cells = [f"{k[x]['recall']} p={k[x]['precision']}" for x in ("llm", "clf", "both")]
            missed = f"   missed {k['both']['missed']}" if k["both"]["missed"] else ""
            typer.echo(f"{f:22} {cells[0]:>16} {cells[1]:>16} {cells[2]:>16}{missed}")
    typer.echo(f"\nsaved {safety_eval.save(rep)}")


@eval_app.command("e2e")
def eval_e2e(
    limit: int = typer.Option(None, help="Only the first N tickets"),
    workers: int = typer.Option(4, help="Tickets in parallel"),
) -> None:
    """The whole workflow on the 76 labelled tickets with the REAL models (~$0.06)."""
    from copilot.eval import e2e  # noqa: PLC0415
    from copilot.pipeline.pii import Masker  # noqa: PLC0415
    from copilot.pipeline.workflow import build_graph, default_deps  # noqa: PLC0415

    rep = e2e.run(build_graph(), default_deps(), Masker.from_settings(get_settings()),
                  workers=workers, limit=limit)  # fmt: skip
    s = rep["summary"]
    for k, v in s.items():
        if k not in ("over_routed", "reasons", "by_suite"):
            typer.echo(f"{k:24} {v}")
    typer.echo(f"{'by_suite':24} {s['by_suite']}")
    typer.echo(f"{'reasons':24} {s['reasons']}")
    for o in s["over_routed"]:
        typer.echo(f"  OVER  {o['id']:5} {o['reasons']}")
    typer.echo(
        f"\nRELEASE GATE: {'PASS' if s['release_gate'] else 'FAIL'}   saved {e2e.save(rep)}"
    )


@eval_app.command("injection")
def eval_injection() -> None:
    """Injection gate: patterns, our bge-m3 head and the guard model, alone and combined."""
    from copilot.eval import injection as injection_eval  # noqa: PLC0415
    from copilot.pipeline.injection_guard import InjectionGuard  # noqa: PLC0415
    from copilot.pipeline.risk_classifier import (  # noqa: PLC0415
        RiskClassifier,
        load_injection_rules,
    )

    st, rules = get_settings(), load_injection_rules()
    head = RiskClassifier(get_embedder(st.embedding_model), model_name=st.embedding_model)
    token = st.huggingface_api_key.get_secret_value() if st.huggingface_api_key else None
    guard = InjectionGuard(
        st.injection_guard_model,
        {"weak": rules["guard_weak"], "strong": rules["guard_strong"]},
        token,
    )
    rep = injection_eval.run(head, guard, rules["head_detect"])
    typer.echo(f"guard {rep['guard']} · {rep['n']} cases, {rep['injections']} injections")
    for name, r in rep["results"].items():
        typer.echo(
            f"{name:44} caught {r['recall']:>6} · false alarms {r['false_alarms']:>6} "
            f"({r['false_alarm_rate']:.1%})  missed {r['missed']}  "
            f"false alarms {r['false_alarm_ids']}"
        )
    typer.echo(f"saved {injection_eval.save(rep)}")


@eval_app.command("risks")
def eval_risks(out: str = typer.Option(None, help="Write the full JSON report here")) -> None:
    """Risk classifier: thresholds from the 76 tickets, honest scores on 62 held-out cases."""
    from copilot.pipeline.risk_classifier import RiskClassifier  # noqa: PLC0415

    st = get_settings()
    clf = RiskClassifier(get_embedder(st.embedding_model), model_name=st.embedding_model)
    rep = risks_eval.run(clf)
    typer.echo(
        f"trained on {rep['examples']} examples · dev {rep['dev_n']} tickets · "
        f"test {rep['test_n']} held-out"
    )
    typer.echo(
        f"{'risk':22} {'thr':>5}  {'DEV P':>6} {'DEV R':>6}  {'TEST P':>6} {'TEST R':>6}  "
        "(test tp/fp/fn)"
    )
    for k, v in rep["per_risk"].items():
        d, t = v["dev"], v["test"]
        typer.echo(
            f"{k:22} {v['threshold']:>5}  {d['precision']:>6} {d['recall']:>6}  "
            f"{t['precision']:>6} {t['recall']:>6}  ({t['tp']}/{t['fp']}/{t['fn']})"
        )
    typer.echo(f"test recall by language: {rep['test_recall_by_language']}")
    typer.echo(
        f"category accuracy: dev {rep['category']['dev_accuracy']} · "
        f"test {rep['category']['test_accuracy']}"
    )
    typer.echo("category when sure (p ≥ cutoff): coverage · right   [thresholds.toml [category]]")
    for t, d in rep["category_when_sure"]["dev"].items():
        e = rep["category_when_sure"]["test"][t]
        typer.echo(f"  p≥{t}  dev {d['coverage']:.0%} {d['right']:>6}   "
                   f"test {e['coverage']:.0%} {e['right']:>6}")  # fmt: skip
    for m in rep["test_misses"]:
        typer.echo(f"  MISS  {m['id']} {m['lang']:3} {m['risk']:20} p={m['p']}")
    for m in rep["test_false_alarms"]:
        typer.echo(f"  FALSE {m['id']} {m['lang']:3} {m['risk']:20} p={m['p']}")
    if out:
        Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        typer.echo(f"report → {out}")


@app.command("graph")
def graph_cmd() -> None:
    """Print the ticket workflow as a Mermaid diagram (paste into the README or mermaid.live)."""
    from copilot.pipeline.workflow import graph  # noqa: PLC0415  (loads LangGraph only when used)

    typer.echo(graph.get_graph().draw_mermaid())


@app.command("logs")
def logs_cmd(
    last: int = typer.Option(30, help="Show the last N lines"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new lines"),
    grep: str = typer.Option(None, help="Only lines containing this (request_id, ticket_id...)"),
    level: str = typer.Option(None, help="Only this level: INFO, WARNING, ERROR"),
) -> None:
    """Read the app log (logs/copilot.log, JSON lines) as one readable line per event."""
    import time  # noqa: PLC0415

    path = get_settings().log_file
    if path is None or not path.exists():
        typer.echo(f"no log file ({path}); set LOG_FILE in .env", err=True)
        raise typer.Exit(1)

    def show(line: str) -> None:
        if grep and grep not in line:
            return
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return
        if level and rec.get("level") != level.upper():
            return
        core = {"ts", "level", "logger", "msg"}
        extra = " ".join(f"{k}={v}" for k, v in rec.items() if k not in core)
        typer.echo(
            f"{rec.get('ts', '')[11:23]} {rec.get('level', ''):7} {rec.get('logger', '')}: "
            f"{rec.get('msg', '')}  {extra}"
        )

    with path.open(encoding="utf-8") as f:
        for line in f.readlines()[-last:]:
            show(line)
        while follow:
            line = f.readline()
            if line:
                show(line)
            else:
                time.sleep(0.5)


@app.command("pii-report")
def pii_report(
    since: str = typer.Option(None, help="Only events after this ISO time, e.g. 2026-11-23T10:00"),
) -> None:
    """Masking monitor: summary of the masking log events (counts only, never values)."""
    path = get_settings().log_file
    if path is None or not path.exists():
        typer.echo(f"no log file ({path})", err=True)
        raise typer.Exit(1)
    tickets, ms, by_type, by_rec, suspects = 0, [], {}, {}, []
    for line in path.open(encoding="utf-8"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        # "ticket queued" since the queue (D11) carries the masking summary; "pii masked" is older.
        if rec.get("msg") not in ("ticket queued", "pii masked") or (
            since and rec.get("ts", "") < since
        ):
            continue
        tickets += 1
        ms.append(rec.get("pii_ms", 0))
        for k, v in rec.get("pii_by_type", {}).items():
            by_type[k] = by_type.get(k, 0) + v
        for k, v in rec.get("pii_by_recognizer", {}).items():
            by_rec[k] = by_rec.get(k, 0) + v
        if rec.get("pii_residual_suspect"):
            suspects.append(
                (rec.get("ticket_id"), rec.get("trace_id"), rec["pii_residual_suspect"])
            )
    if not tickets:
        typer.echo("no 'pii masked' events yet: send tickets to POST /tickets")
        return
    typer.echo(f"tickets masked: {tickets}   avg masking time: {sum(ms) / len(ms):.0f} ms")
    typer.echo("by type:       " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    typer.echo("by recognizer: " + ", ".join(f"{k}={v}" for k, v in sorted(by_rec.items())))
    typer.echo(f"possible misses (residual suspects): {len(suspects)} of {tickets} tickets")
    for tid, trace_id, kinds in suspects:
        typer.echo(f"  ⚠ ticket {tid}  kinds={kinds}  trace={trace_id}")


@app.command("replay")
def replay(
    url: str = typer.Option("http://127.0.0.1:8000", help="Running API"),
    repeat: int = typer.Option(1, help="Send the eval set this many times"),
    delay: float = typer.Option(0.2, help="Seconds between tickets"),
    key: str = typer.Option(
        ..., envvar="COPILOT_HELPDESK_KEY", help="Helpdesk API key (or env COPILOT_HELPDESK_KEY)"
    ),
) -> None:
    """Send the 76 eval tickets to POST /tickets (the worker processes them): realistic traffic
    for the Grafana dashboard and a small load test. IDs get a run suffix so they are new."""
    import time  # noqa: PLC0415

    import httpx2  # noqa: PLC0415

    root = Path(__file__).resolve().parents[2] / "eval"
    tickets = [
        json.loads(line)
        for f in ("tickets_labelled.jsonl", "tickets_adversarial.jsonl")
        for line in (root / f).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run = time.strftime("%H%M%S")
    ok = failed = 0
    t0 = time.perf_counter()
    for i in range(repeat):
        for t in tickets:
            body = {"id": f"{t['id']}-{run}-{i}", "subject": t["subject"], "body": t["body"]}
            r = httpx2.post(
                f"{url}/tickets",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=120,
            )
            ok, failed = (ok + 1, failed) if r.status_code in (200, 202) else (ok, failed + 1)
            time.sleep(delay)
    secs = time.perf_counter() - t0
    typer.echo(f"sent {ok + failed} tickets in {secs:.0f} s · ok {ok} · failed {failed}")


@app.command("worker")
def worker_cmd(
    poll: float = typer.Option(0.5, help="Seconds between queue checks when idle"),
) -> None:
    """Background worker: claims queued tickets from Postgres and runs the workflow.
    Start as many as needed; they never take the same ticket (SKIP LOCKED)."""
    from copilot import telemetry as tel  # noqa: PLC0415
    from copilot.tickets.worker import run_forever  # noqa: PLC0415

    st = get_settings()
    pool = open_pool(st) if st.vector_store == "pgvector" else None
    if pool is not None:
        atexit.register(pool.close)
    try:
        services = load_services(st, pool, warm_masker=False)
    except NotIndexedError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(1) from None
    tel.register_queue_gauge(services.tickets)
    from copilot.pipeline.workflow import build_deps, build_graph  # noqa: PLC0415

    # Checkpoints in Postgres: a retry resumes from the failed step, even after a crash.
    app_graph = None
    if pool is not None:
        from langgraph.checkpoint.postgres import PostgresSaver  # noqa: PLC0415

        saver = PostgresSaver(pool)
        saver.setup()  # creates the checkpoint tables once (idempotent)
        app_graph = build_graph(checkpointer=saver)
    run_forever(
        services.tickets,
        poll_s=poll,
        audit=services.audit,
        deps=build_deps(st, services.risk, services.guard, services.retriever),
        app=app_graph,
    )


@keys_app.command("new")
def keys_new(name: str, role: str = typer.Argument(..., help="helpdesk | agent")) -> None:
    """Create an API key. The key is shown ONCE; only its hash goes into .env (API_KEYS)."""
    from copilot.server.auth import ROLES, hash_key, new_key  # noqa: PLC0415

    if role not in ROLES:
        typer.echo(f"role must be one of {ROLES}", err=True)
        raise typer.Exit(1)
    if ":" in name or "," in name:
        typer.echo("name may not contain ':' or ','", err=True)
        raise typer.Exit(1)
    key = new_key()
    typer.echo(f"key for {name} ({role}), shown once, give it to the caller:\n  {key}")
    typer.echo(f"add to API_KEYS in .env (comma-separated):\n  {name}:{role}:{hash_key(key)}")
