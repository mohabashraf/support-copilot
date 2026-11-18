"""The API serves the knowledge base."""

from fastapi.testclient import TestClient

from copilot.server.app import create_app

client = TestClient(create_app(with_retriever=False))  # no model download in unit tests


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["chunks"] == 26


def test_list_chunks_and_filter_by_flag():
    assert len(client.get("/kb/chunks").json()) == 26
    defective = client.get("/kb/chunks", params={"flag": "defective"}).json()
    assert [c["id"] for c in defective] == ["withdraw#steps"]


def test_get_one_chunk_with_hash_in_id():
    r = client.get("/kb/chunks/faq#min-deposit".replace("#", "%23"))
    assert r.status_code == 200
    assert "8 USD" in r.json()["text"]
    assert len(r.json()["sources"]) == 2


def test_unknown_chunk_is_404():
    assert client.get("/kb/chunks/nope%23nothing").status_code == 404
