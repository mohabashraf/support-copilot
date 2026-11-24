"""PII vault: encrypted at rest, expires, idempotent. Same tests for Postgres and memory."""

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from copilot.pipeline.pii_vault import InMemoryPiiVault, PgPiiVault, display_value
from tests.pg import db_up

KEY = Fernet.generate_key().decode()
MAPPING = {"[CARD_1]": "4111 1111 1111 1111", "[NAME_1]": "محمد علي"}


@pytest.fixture(params=["memory", "pg"])
def vault(request):
    if request.param == "memory":
        return InMemoryPiiVault(KEY, retention_days=30)
    if not db_up():
        pytest.skip("Postgres not running (docker compose up -d db)")
    pool = request.getfixturevalue("pool")
    with pool.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS pii_vault_test")
    return PgPiiVault(pool, KEY, retention_days=30, table="pii_vault_test")


def test_store_and_reveal(vault):
    vault.store("T1", MAPPING)
    assert vault.reveal("T1", "[CARD_1]") == "4111 1111 1111 1111"
    assert vault.reveal("T1", "[NAME_1]") == "محمد علي"
    assert vault.reveal("T1", "[EMAIL_1]") is None
    assert vault.reveal("T2", "[CARD_1]") is None  # mappings are per ticket


def test_storing_twice_changes_nothing(vault):
    vault.store("T1", MAPPING)
    vault.store("T1", {"[CARD_1]": "something else"})  # webhook redelivered
    assert vault.reveal("T1", "[CARD_1]") == "4111 1111 1111 1111"


def test_expired_rows_are_hidden_and_purged(vault):
    vault.store("T1", MAPPING)
    if isinstance(vault, InMemoryPiiVault):
        vault.rows = {
            k: (enc, datetime(2000, 1, 1, tzinfo=UTC)) for k, (enc, _) in vault.rows.items()
        }
    else:
        with vault.pool.connection() as conn:
            conn.execute("UPDATE pii_vault_test SET expires_at = now() - interval '1 day'")
    assert vault.reveal("T1", "[CARD_1]") is None
    assert vault.purge_expired() == 2


def test_values_are_encrypted_at_rest(pool):
    if not db_up():
        pytest.skip("Postgres not running")
    with pool.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS pii_vault_test")
    v = PgPiiVault(pool, KEY, retention_days=30, table="pii_vault_test")
    v.store("T1", MAPPING)
    with pool.connection() as conn:
        raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT value_enc FROM pii_vault_test"))
    assert b"4111 1111" not in raw and "محمد".encode() not in raw


def test_cards_documents_accounts_are_never_shown_in_full():
    assert display_value("[CARD_1]", "4111 1111 1111 1111") == "**** 1111"
    assert display_value("[DOC_1]", "A12345678") == "**** 5678"
    assert display_value("[EMAIL_1]", "a@b.com") == "a@b.com"
