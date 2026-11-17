"""Postgres tests: the same behaviour on a real database (pgvector 0.8+).

  docker compose up -d
  ATLAS_TEST_PG_URL=postgresql://atlas:atlas@127.0.0.1:5433/atlas pytest -m pg

Skipped when ATLAS_TEST_PG_URL is not set, so CI without Docker still passes.
Needs your week 3 and week 5 work (they exercise retrieval and tickets).
"""
import os
import uuid

import pytest

from atlas import embed, identity, kb, retrieval
from atlas import tickets as T

URL = os.environ.get("ATLAS_TEST_PG_URL", "")
pytestmark = [pytest.mark.pg, pytest.mark.skipif(not URL, reason="set ATLAS_TEST_PG_URL to run Postgres tests")]
USERS = identity.load_directory()


@pytest.fixture(scope="module")
def store():
    s = retrieval.PgStore(URL, 256, "test_" + uuid.uuid4().hex[:8])
    yield s
    s.conn.execute("DROP TABLE IF EXISTS " + s.table)


def test_pgvector_supports_iterative_scans(store):
    v = store.conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
    assert tuple(int(x) for x in v.split(".")[:2]) >= (0, 8), "pgvector " + v + " has no hnsw.iterative_scan"


def test_sync_is_incremental(store):
    e, chunks = embed.HashEmbedder(), kb.load()
    assert retrieval.sync(store, e, chunks)["added"] == len(chunks)
    assert retrieval.sync(store, e, chunks) == {"added": 0, "updated": 0, "deleted": 0, "unchanged": len(chunks)}
    edited = [{**c, "hash": "changed"} if c["id"] == "vpn#overview" else c for c in chunks[:-1]]
    assert retrieval.sync(store, e, edited) == {"added": 0, "updated": 1, "deleted": 1, "unchanged": len(chunks) - 2}
    retrieval.sync(store, e, chunks)
    assert store.count() == len(chunks)


def test_hybrid_search_and_permissions_in_postgres(store):
    retrieval.sync(store, embed.HashEmbedder(), kb.load())
    r = retrieval.Retriever(store, embed.HashEmbedder())
    assert "vpn#workaround-during-inc-2041" in [h["id"] for h in r.search("INC-2041 workaround", USERS["priya"].groups)]
    for h in r.search("payroll correction gateway restart", USERS["priya"].groups, k=10):
        assert h["acl"] == ["all-staff"]
    assert len(r.search("payroll correction gateway restart", USERS["priya"].groups, k=4)) == 4
    assert any(h["id"].startswith("payroll-corrections#") for h in r.search("payroll correction", USERS["dana"].groups))


def test_tickets_exactly_once_in_postgres():
    svc = T.TicketService(T.DB(URL), T.FakeTicketSystem(fail_next=3))
    p = svc.propose(USERS["priya"], "VPN drops", "pg test")
    key = "pg-" + uuid.uuid4().hex
    with pytest.raises(T.TicketSystemDown):
        svc.approve(p["id"], USERS["priya"], key)
    first = svc.approve(p["id"], USERS["priya"], key)
    again = svc.approve(p["id"], USERS["priya"], key)
    assert again["replayed"] and again["ticket_id"] == first["ticket_id"] and len(svc.system.tickets) == 1
    with pytest.raises(T.Conflict):
        svc.approve(p["id"], USERS["priya"], "pg-" + uuid.uuid4().hex)
    assert [a["action"] for a in svc.audit(p["id"])] == ["proposed", "approved", "create_failed", "created"]
