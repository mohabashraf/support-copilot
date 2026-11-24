"""Phase 2 KB-growth test (runs inside the app container on the test VM).

Grows the KB from the 26 real help-page chunks to 26 + N synthetic SOP chunks ON THE SAME TOPICS
(deposits, withdrawals, verification, accounts, with fees, limits, timelines), in SEPARATE tables
(scale_kb_chunks + chunk_vectors_bge_m3_scale), so the real index is untouched. Indexing is
incremental: each step embeds only the new chunks. After each step: the labelled retrieval eval
(85 queries + 28 tickets: do the right REAL chunks still come first?) and search latency.
Usage: python scale_kb.py 1000 5000 10000
"""

import json
import random
import sys
import time
from statistics import median

from copilot.config import get_settings
from copilot.db import open_pool
from copilot.eval import retrieval as reval
from copilot.kb.ingest import build_kb
from copilot.kb.models import Chunk, KnowledgeBase, Source
from copilot.kb.store import KBStore
from copilot.retrieval.embeddings import get_embedder
from copilot.retrieval.index import PgVectorIndex
from copilot.retrieval.indexer import index_kb
from copilot.retrieval.search import Retriever

rnd = random.Random(42)
TOPICS = {
    "deposit": [
        "card deposit",
        "bank transfer deposit",
        "e-wallet deposit",
        "crypto deposit",
        "deposit in a non-USD currency",
        "first deposit",
        "deposit not credited",
    ],
    "withdrawal": [
        "card withdrawal",
        "bank wire withdrawal",
        "e-wallet withdrawal",
        "withdrawal pending",
        "withdrawal rejected",
        "withdrawal cancellation",
    ],
    "verification": [
        "proof of identity",
        "proof of address",
        "selfie check",
        "document expired",
        "verification rejected",
        "enhanced due diligence",
    ],
    "account": [
        "account type change",
        "leverage change request",
        "swap-free status",
        "dormant account",
        "account closure",
        "second trading account",
    ],
}
REGIONS = [
    "GCC",
    "North Africa",
    "South-East Asia",
    "Latin America",
    "CIS",
    "West Africa",
    "South Asia",
    "East Africa",
    "Levant",
    "Central Asia",
]
METHODS = [
    "Visa",
    "Mastercard",
    "local bank",
    "Skrill",
    "Neteller",
    "USDT",
    "Perfect Money",
    "FasaPay",
    "SticPay",
    "PayPal",
]
TEAMS = ["Payments", "Back Office", "Compliance", "Risk", "Tier 2 Support", "KYC desk"]


def sop(i: int) -> Chunk:
    area = rnd.choice(list(TOPICS))
    topic, region, method, team = (
        rnd.choice(TOPICS[area]),
        rnd.choice(REGIONS),
        rnd.choice(METHODS),
        rnd.choice(TEAMS),
    )
    amount, hours, fee = (
        rnd.choice([50, 100, 500, 1000, 5000]),
        rnd.choice([2, 4, 24, 48, 72]),
        rnd.choice(["0.5%", "1%", "2%", "3 USD", "no fee"]),
    )
    text = (
        f"Internal SOP for {topic} ({method}, {region} clients). Agents check the client's "
        f"Member Area history before replying. Requests above {amount} USD go to {team} "
        f"for review; expected handling time is {hours} hours. Processing fee: {fee}. "
        f"If the client asks about the status of a {area}, confirm the request was received "
        f"and do not promise a date. Escalate repeated contacts to {team}."
    )
    return Chunk(
        id=f"sop#{area}-{i}",
        kind="faq",
        section=f"SOP {i}: {topic} - {method} - {region}",
        text=text,
        sources=[Source(page_title=f"SOP {i}", page_url=f"sop://{i}")],
    )


def main(sizes: list[int]) -> None:
    st = get_settings()
    pool = open_pool(st)
    emb = get_embedder("bge-m3")
    real = list(build_kb().chunks)
    synth = [sop(i) for i in range(max(sizes))]
    store = KBStore(pool, table="scale_kb_chunks")
    index = PgVectorIndex(pool, "bge-m3-scale", emb.dim, chunks_table="scale_kb_chunks")
    out = []
    for n in sizes:
        kb = KnowledgeBase.from_chunks(real + synth[:n])
        t0 = time.perf_counter()
        rep = index_kb(kb, emb, index, store)
        index_s = time.perf_counter() - t0
        retr = Retriever(emb, index)
        r = reval.run(retr, "bge-m3", "pgvector-scale", kb.kb_hash)
        qv = emb.embed_queries(["What is the minimum deposit?"])[0]
        lat = []
        for _ in range(200):
            t1 = time.perf_counter()
            index.search(qv, 5)
            lat.append((time.perf_counter() - t1) * 1000)
        lat.sort()
        t2 = time.perf_counter()
        for q in ["How long does verification take?", "withdrawal pending", "رسوم السحب"] * 10:
            retr.search(q, 5)
        full_ms = (time.perf_counter() - t2) * 1000 / 30
        sop_in_top3 = sum(
            any(c.startswith("sop#") for c in row["top3"]) for row in r.get("failures", [])
        )  # failures only, for the report
        row = {
            "chunks_total": len(kb.chunks),
            "synthetic": n,
            "embedded_this_step": rep.embedded,
            "index_seconds": round(index_s, 1),
            "queries": {k: r["queries"][k] for k in ("r1", "r3", "hit3", "mrr", "n")},
            "tickets": {k: r["tickets"][k] for k in ("r3", "hit3", "mrr", "n")},
            "abstain_auroc": r["abstain"]["auroc"],
            "vector_search_ms_p50": round(median(lat), 2),
            "vector_search_ms_p95": round(lat[int(len(lat) * 0.95)], 2),
            "query_embed_plus_search_ms": round(full_ms, 1),
            "failures": len(r.get("failures", [])),
            "failures_with_sop_in_top3": sop_in_top3,
        }
        out.append(row)
        print(json.dumps(row), flush=True)
    with open("/app/logs/scale_kb.json", "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1:]])
