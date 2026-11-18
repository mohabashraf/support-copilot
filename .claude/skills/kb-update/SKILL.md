---
name: kb-update
description: Add or change knowledge-base content in the support co-pilot - a new or updated help page, a new FAQ, a changed answer, or a new/removed flag (conflict, defective, gap_link, rule_sensitive, route, compliance, promo). Use whenever files in knowledge_base/ change or the user asks to add a page, FAQ or flag.
---

# Updating the knowledge base

The KB is used **as it is**: never edit or rewrite page text. Problems are recorded as flags and
findings. Only `copilot kb index` writes the KB to Postgres (D8).

## Steps

1. **Save the page unchanged.** Put the HTML in `knowledge_base/raw/<file>.html`
   (a new snapshot replaces the old file; don't hand-edit it).

2. **Register a new page** in `knowledge_base/kb_config.toml`:
   ```toml
   [[pages]]
   slug = "fees"                      # short name → chunk IDs fees#intro, fees#steps
   file = "how-to-fees.html"
   url = "https://valetax.com/..."    # goes into every citation: must be the public URL
   ```

3. **Give every new FAQ question a stable ID** in `[faq_ids]`. The key is the question text exactly
   as on the page (after whitespace normalisation). Build fails loudly on an unknown FAQ:
   `IngestError: FAQ not in kb_config.toml [faq_ids]`. A question that appears on several pages
   maps to one ID (one chunk, all page URLs). If two pages give the same question **different**
   answers the build fails on purpose: that is a conflict for a human to decide.

4. **Look at what was built:** `uv run copilot kb show` (all chunks) and
   `uv run copilot kb show <chunk_id>` (one chunk with sources and flags). Check the text reads
   correctly; if a block is missing or garbled, the fix belongs in `kb/parse.py` (with a test in
   `tests/test_kb_ingest.py`), never in the page.

5. **Flag problems** in `[flags]` (allowed values are the `Flag` Literal in `kb/models.py`):
   conflict (pages contradict) · defective (broken/copied text) · gap_link ("see here" to a page we
   don't have) · rule_sensitive (touches a hard rule) · route (the KB itself says contact support) ·
   compliance (jurisdiction/regulatory) · promo (marketing, not policy).
   Record **why** in `knowledge_base/KB_FINDINGS.md` (what it says, where, how the co-pilot handles it).

6. **Index (incremental):** `uv run copilot kb index` (Docker: `docker compose run --rm indexer`).
   Expect only the new/changed chunks under `added`/`changed`; everything else `unchanged`.

7. **Update the eval data.** For new content, add queries to `eval/retrieval_queries.jsonl` with
   graded labels (`required`, `optional`, `answerable`), including at least one non-English query.
   If an old "not in KB" query is now answerable, relabel it.

8. **Measure:** `uv run copilot eval retrieval --out eval/results/retrieval_<model>.json` and compare
   with the previous numbers in `docs/BENCHMARK.md`. A drop in R@3 / MRR / Hit@3 by language must be
   explained before committing.

9. **Test and commit:** `uv run pytest -q`. `knowledge_base/chunk_plan.json` (the expected chunk
   list, checked by `tests/test_kb_ingest.py`) and `knowledge_base/CHUNKS.md` must list the new
   chunks. Tests that pin the chunk count (26 today: `tests/test_kb_ingest.py`,
   `tests/test_server.py`, `tests/test_ingestion.py`) are updated on purpose, with the new count in
   the commit message.

## Done when
`kb show` reads correctly · flags + KB_FINDINGS updated · `kb index` reports only the expected
changes · eval re-run and BENCHMARK updated · tests pass.
