"""Week 5 tests: approved tickets, exactly once.

Run:  pytest -m week5
Offline: SQLite in memory and a simulated ticket system. Postgres runs are in test_pg.py.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from atlas import config, identity
from atlas import tickets as T
from atlas.app import create_app

pytestmark = pytest.mark.week5
USERS = identity.load_directory()
PRIYA, MARCUS = USERS["priya"], USERS["marcus"]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def service(fail_next=0, ttl_s=900):
    clock = Clock()
    return T.TicketService(T.DB(""), T.FakeTicketSystem(fail_next), ttl_s=ttl_s, clock=clock), clock


def draft(svc, user=PRIYA):
    return svc.propose(user, "VPN drops every few minutes", "Started after INC-2041; US-East workaround did not help.")


# ---- 1. Proposing writes a draft, never a ticket -------------------------------------

def test_propose_creates_nothing_in_the_ticket_system():
    svc, _ = service()
    p = draft(svc)
    assert p["status"] == "pending" and p["ticket_id"] is None
    assert svc.system.calls == 0
    assert [a["action"] for a in svc.audit(p["id"])] == ["proposed"]


# ---- 2. Exactly once --------------------------------------------------------------------

def test_approve_creates_one_ticket_and_records_who():
    svc, _ = service()
    p = draft(svc)
    out = svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    assert out["replayed"] is False and out["ticket_id"].startswith("TCK-")
    assert out["proposal"]["status"] == "created" and out["proposal"]["approved_by"] == "priya"
    assert [a["action"] for a in svc.audit(p["id"])] == ["proposed", "approved", "created"]


def test_double_click_with_the_same_key_returns_the_same_ticket():
    svc, _ = service()
    p = draft(svc)
    first = svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    again = svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    assert again["replayed"] is True and again["ticket_id"] == first["ticket_id"]
    assert len(svc.system.tickets) == 1 and svc.system.calls == 1


def test_a_second_approval_with_a_different_key_is_a_conflict():
    svc, _ = service()
    p = draft(svc)
    svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    with pytest.raises(T.Conflict):
        svc.approve(p["id"], PRIYA, "key-0002-abcdef")
    assert len(svc.system.tickets) == 1


def test_racing_approvals_create_one_ticket():
    svc, _ = service()
    p = draft(svc)
    results, errors = [], []

    def go(key):
        try:
            results.append(svc.approve(p["id"], PRIYA, key))
        except T.Conflict as e:
            errors.append(e)
    threads = [threading.Thread(target=go, args=("key-race-%04d" % i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and len(errors) == 7 and len(svc.system.tickets) == 1


# ---- 3. Who may approve, and for how long ----------------------------------------------

def test_only_the_requester_can_approve():
    svc, _ = service()
    p = draft(svc)
    with pytest.raises(T.Forbidden):
        svc.approve(p["id"], MARCUS, "key-0001-abcdef")
    assert svc.system.calls == 0


def test_unknown_proposal():
    svc, _ = service()
    with pytest.raises(T.NotFound):
        svc.approve("prop_nope", PRIYA, "key-0001-abcdef")


def test_stale_drafts_expire():
    svc, clock = service(ttl_s=900)
    p = draft(svc)
    clock.t += 901
    with pytest.raises(T.Conflict):
        svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    assert svc.get(p["id"])["status"] == "expired" and svc.system.calls == 0
    assert svc.audit(p["id"])[-1]["action"] == "expired"


# ---- 4. A flaky ticket system -------------------------------------------------------------

def test_transient_failures_are_retried_with_the_same_key():
    svc, _ = service(fail_next=2)
    p = draft(svc)
    out = svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    assert out["ticket_id"] and svc.system.calls == 3 and len(svc.system.tickets) == 1


def test_outage_keeps_the_approval_and_the_same_key_resumes():
    svc, _ = service(fail_next=3)
    p = draft(svc)
    with pytest.raises(T.TicketSystemDown):
        svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    assert svc.get(p["id"])["status"] == "approved" and svc.system.tickets == {}
    assert svc.audit(p["id"])[-1]["action"] == "create_failed"
    with pytest.raises(T.Conflict):  # a new click with a new key must not start a second ticket
        svc.approve(p["id"], PRIYA, "key-0002-abcdef")
    out = svc.approve(p["id"], PRIYA, "key-0001-abcdef")
    assert out["replayed"] is False and len(svc.system.tickets) == 1
    assert [a["action"] for a in svc.audit(p["id"])] == ["proposed", "approved", "create_failed", "created"]


# ---- 5. The API ------------------------------------------------------------------------------

@pytest.fixture
def client():
    svc, _ = service()
    c = TestClient(create_app(config.load({"LLM_MODE": "replay"}), tickets=svc))
    c.svc = svc
    return c


def test_api_end_to_end(client):
    h = {"X-Atlas-User": "priya"}
    r = client.post("/v1/tickets/proposals", json={"summary": "VPN drops", "details": "Every few minutes since Monday."}, headers=h)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/v1/tickets/proposals/%s/approve" % pid, headers=h).status_code == 400
    first = client.post("/v1/tickets/proposals/%s/approve" % pid, headers={**h, "Idempotency-Key": "k-123456789"})
    again = client.post("/v1/tickets/proposals/%s/approve" % pid, headers={**h, "Idempotency-Key": "k-123456789"})
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["ticket_id"] == again.json()["ticket_id"] and len(client.svc.system.tickets) == 1
    other = client.post("/v1/tickets/proposals/%s/approve" % pid, headers={**h, "Idempotency-Key": "k-987654321"})
    assert other.status_code == 409
    trail = client.get("/v1/tickets/proposals/" + pid, headers=h).json()["audit"]
    assert [a["action"] for a in trail] == ["proposed", "approved", "created"]


def test_api_permissions(client):
    pid = client.post("/v1/tickets/proposals", json={"summary": "s", "details": "d"}, headers={"X-Atlas-User": "priya"}).json()["id"]
    assert client.post("/v1/tickets/proposals", json={"summary": "s", "details": "d"}).status_code == 401
    r = client.post("/v1/tickets/proposals/%s/approve" % pid, headers={"X-Atlas-User": "marcus", "Idempotency-Key": "k-123456789"})
    assert r.status_code == 403
    assert client.get("/v1/tickets/proposals/" + pid, headers={"X-Atlas-User": "marcus"}).status_code == 404
