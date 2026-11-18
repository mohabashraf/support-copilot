"""Hot reload: the server swaps in a KB written by `copilot kb index`, without a restart.

KBWatcher is built from two functions, so these tests use fakes: no database, no model.
"""

import time

from fastapi.testclient import TestClient

from copilot.kb.ingest import build_kb
from copilot.kb.models import KnowledgeBase
from copilot.server.app import create_app
from copilot.services import KBWatcher, NotIndexedError, Services

KB = build_kb()
KB2 = KnowledgeBase.from_chunks(
    [c.model_copy(update={"text": "edited"}) if c.id == "faq#history" else c for c in KB.chunks]
)


def test_no_change_no_reload():
    loads = []
    w = KBWatcher(lambda: KB.chunk_hashes, lambda: loads.append(1) or Services(KB))
    assert w.check(Services(KB)) is None
    assert loads == []  # unchanged KB: not even a load


def test_changed_and_ready_returns_new_services():
    w = KBWatcher(lambda: KB2.chunk_hashes, lambda: Services(KB2))
    new = w.check(Services(KB))
    assert new is not None and new.kb.kb_hash == KB2.kb_hash


def test_changed_but_vectors_not_ready_keeps_current():
    def not_ready():
        raise NotIndexedError("vectors do not match")

    w = KBWatcher(lambda: KB2.chunk_hashes, not_ready)
    assert w.check(Services(KB)) is None  # keep serving the old KB, retry next tick


def test_running_app_swaps_kb_without_restart():
    stored = {"hashes": KB.chunk_hashes}
    w = KBWatcher(lambda: stored["hashes"], lambda: Services(KB2))
    with TestClient(create_app(Services(KB), watcher=w, reload_seconds=0.05)) as client:
        assert client.get("/health").json()["kb_hash"] == KB.kb_hash
        stored["hashes"] = KB2.chunk_hashes  # `copilot kb index` wrote a new KB
        for _ in range(40):
            if client.get("/health").json()["kb_hash"] == KB2.kb_hash:
                break
            time.sleep(0.05)
        assert client.get("/health").json()["kb_hash"] == KB2.kb_hash
