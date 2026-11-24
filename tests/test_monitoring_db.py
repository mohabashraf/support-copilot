"""Monitoring reads Postgres as `grafana_ro`: it may read the monitoring views and NOTHING else
(no ticket text, no vault), and every dashboard SQL query must run with exactly those rights."""

import json
import re

import psycopg
import pytest

from copilot.config import ROOT
from copilot.pipeline.pii import Masker
from copilot.pipeline.pii_vault import PgPiiVault
from copilot.pipeline.schemas import Ticket
from copilot.tickets import service
from copilot.tickets.store import PgTicketStore
from copilot.tickets.worker import run_once
from tests.pg import DB, db_up

pytestmark = pytest.mark.skipif(not db_up(), reason="Postgres not running")
DASHBOARD = ROOT / "observability/grafana/dashboards/support-copilot.json"
GRAFANA_DB = DB.replace("copilot:copilot@", "grafana_ro:grafana_ro@")


@pytest.fixture
def ticket(pool):
    """A processed test ticket in tickets_test (+ views), with grafana_ro set up as in Docker."""
    sql = (ROOT / "observability/postgres/grafana_readonly.sql").read_text()
    with pool.connection() as conn:
        # "anything else": a table the test owns, so it doesn't depend on `copilot kb index`
        # having created kb_chunks (CI never runs it). Created before the role SQL.
        conn.execute("CREATE TABLE IF NOT EXISTS monitoring_test_other (secret text)")
        conn.execute(sql)
        conn.execute("DROP TABLE IF EXISTS tickets_test CASCADE")
    store = PgTicketStore(pool, table="tickets_test")  # creates the views + grants grafana_ro
    vault = PgPiiVault(pool, "x" * 43 + "=", retention_days=1, table="pii_vault_test")
    rec, _ = service.intake(
        Ticket(
            id="M1", subject="Deposit", body="Hi, this is Ahmed Hassan, card 4111 1111 1111 1111"
        ),
        Masker(ner="spacy"),
        None,
        store,
    )
    run_once(store)
    return rec.number, vault


def _as_grafana(sql: str):
    with psycopg.connect(GRAFANA_DB) as conn:
        return conn.execute(sql).fetchall()


def test_grafana_role_reads_the_views(ticket):
    n, _ = ticket
    assert _as_grafana(f"SELECT status FROM tickets_test_monitoring WHERE number = {n}") == [
        ("done",)
    ]
    assert (
        _as_grafana(f"SELECT count(*) FROM tickets_test_pii_detections WHERE number = {n}")[0][0]
        >= 1
    )
    assert _as_grafana(f"SELECT count(*) FROM tickets_test_steps WHERE number = {n}")[0][0] == 14


@pytest.mark.parametrize(
    "query",
    [
        "SELECT masked FROM tickets_test",  # the masked message
        "SELECT result FROM tickets_test",  # the draft
        "SELECT * FROM pii_vault_test",  # encrypted real values
        "SELECT * FROM monitoring_test_other",  # anything else (e.g. kb_chunks)
    ],
)
def test_grafana_role_cannot_read_text_or_the_vault(ticket, query):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as_grafana(query)


def test_monitoring_views_hold_no_text(ticket):
    n, _ = ticket
    with psycopg.connect(GRAFANA_DB) as conn:
        for view in (
            "tickets_test_monitoring",
            "tickets_test_pii_detections",
            "tickets_test_steps",
            "tickets_test_risk_signals",
        ):
            row = conn.execute(f"SELECT * FROM {view} WHERE number = {n}").fetchall()
            assert "4111 1111" not in str(row) and "Ahmed" not in str(row)


def test_every_dashboard_sql_query_runs_as_grafana(ticket):
    """A renamed column must fail here, not silently break a Grafana panel."""
    n, _ = ticket
    dash = json.loads(DASHBOARD.read_text())
    queries = [
        (p["title"], t["rawSql"])
        for p in dash["panels"]
        for t in p.get("targets", [])
        if t.get("datasource", {}).get("uid") == "tickets-db"
    ]
    queries += [
        ("variable ticket", v["query"])
        for v in dash["templating"]["list"]
        if v["name"] == "ticket"
    ]
    assert len(queries) >= 15
    for title, sql in queries:
        sql = sql.replace("$ticket", str(n))
        sql = re.sub(
            r"\btickets_(monitoring|steps|pii_detections|risk_signals)\b", r"tickets_test_\1", sql
        )
        try:
            _as_grafana(sql)
        except psycopg.Error as err:
            pytest.fail(f"dashboard panel {title!r}: {err}")


def test_the_readonly_script_keeps_every_live_monitoring_view_readable(ticket):
    """The script REVOKEs everything, then must grant all four views back. It once re-granted
    only tickets_monitoring, so running it (tests do) broke the journey and risk panels."""
    PgTicketStore(ticket[1].pool)  # the app's own views exist (as in Docker)
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute((ROOT / "observability/postgres/grafana_readonly.sql").read_text())
    for view in (
        "tickets_monitoring",
        "tickets_pii_detections",
        "tickets_steps",
        "tickets_risk_signals",
    ):
        _as_grafana(f"SELECT count(*) FROM {view}")  # raises if not granted
