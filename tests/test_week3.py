"""Week 3 tests: hybrid retrieval, permissions, and answers with citations.

Run:  pytest -m week3
Offline: in-memory store, hash embedder, replayed or scripted model. Postgres runs are in test_pg.py.
"""
import urllib.request

import pytest
from fastapi.testclient import TestClient

from atlas import adapter as A
from atlas import ask as ask_mod
from atlas import config, embed, identity, kb, retrieval
from atlas.app import create_app
from atlas.budget import Budget, BudgetExceeded

pytestmark = pytest.mark.week3
RECORDED_QUESTION = "My VPN keeps dropping every few minutes. What should I do?"
USERS = identity.load_directory()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def settings(**over):
    return config.load({"LLM_MODE": "replay", **{k: str(v) for k, v in over.items()}})


@pytest.fixture(scope="module")
def retriever():
    e = embed.HashEmbedder()
    store = retrieval.MemoryStore()
    retrieval.sync(store, e, kb.load())
    return retrieval.Retriever(store, e)


def scripted(*texts):
    seq = list(texts)

    def live(req):
        live.calls += 1
        live.last = req
        return {"text": seq.pop(0), "tool_calls": [], "usage": {"input_tokens": 400, "output_tokens": 40}}
    live.calls = 0
    return live


def budget(s):
    return Budget(s.max_cost_usd, s.max_tokens_per_call, s.kill_switch)


# ---- 1. Reciprocal Rank Fusion -----------------------------------------------

def test_rrf_rewards_ids_both_lists_agree_on():
    assert [i for i, _ in retrieval.rrf([["a", "b", "c"], ["c", "a", "d"]])] == ["a", "c", "b", "d"]


def test_rrf_scores_use_one_over_k_plus_rank():
    (top, score), *_ = retrieval.rrf([["a"], ["a"]], k=60)
    assert top == "a" and score == pytest.approx(2 / 61)


def test_rrf_breaks_ties_by_id_and_ignores_repeats():
    assert retrieval.rrf([["b", "a"], ["a", "b"]]) == retrieval.rrf([["a", "b"], ["b", "a"]])
    assert [i for i, _ in retrieval.rrf([["b", "a"], ["a", "b"]])] == ["a", "b"]
    assert dict(retrieval.rrf([["x", "x", "y"]]))["x"] == pytest.approx(1 / 61)  # counted once, at its best rank


# ---- 2. Hybrid search ----------------------------------------------------------

def test_keyword_side_finds_exact_identifiers(retriever):
    # Embeddings blur codes like INC-2041; keyword search does not. That is why we keep both.
    ids = [h["id"] for h in retriever.search("INC-2041", USERS["priya"].groups)]
    assert "vpn#workaround-during-inc-2041" in ids


def test_results_carry_what_a_citation_needs(retriever):
    hits = retriever.search("vpn keeps dropping", USERS["priya"].groups)
    assert 1 <= len(hits) <= 4
    assert set(hits[0]) >= {"id", "title", "section", "url", "text", "acl", "score"}
    assert hits == sorted(hits, key=lambda h: -h["score"])


# ---- 3. Permissions: filter before ranking, and check again ---------------------

@pytest.mark.parametrize("query", ["payroll correction", "restarting a vpn gateway", "INC-2041 gateway notes"])
def test_priya_never_sees_hr_or_it_staff_articles(retriever, query):
    for h in retriever.search(query, USERS["priya"].groups, k=10):
        assert h["acl"] == ["all-staff"], h["id"]


def test_permitted_users_do_see_restricted_articles(retriever):
    assert any(h["id"].startswith("payroll-corrections#") for h in retriever.search("payroll correction", USERS["dana"].groups))
    assert any(h["id"].startswith("vpn-runbook#") for h in retriever.search("restarting a vpn gateway", USERS["jo"].groups))


def test_filtering_inside_the_search_still_fills_k(retriever):
    # If you took the global top k and THEN filtered, priya could get fewer than k results.
    assert len(retriever.search("payroll vpn gateway restart correction", USERS["priya"].groups, k=4)) == 4


def test_no_groups_means_no_results(retriever):
    assert retriever.search("vpn", ()) == []


def test_search_drops_a_chunk_the_store_should_not_have_returned(retriever):
    class LeakyStore:  # a store with a permission bug
        def __init__(self, inner):
            self.inner = inner

        def keyword(self, q, groups, n):
            return ["payroll-corrections#correcting-a-payment"] + self.inner.keyword(q, groups, n)

        def vector(self, v, groups, n):
            return self.inner.vector(v, groups, n)

        def get(self, ids):
            return self.inner.get(ids)
    leaky = retrieval.Retriever(LeakyStore(retriever.store), retriever.embed)
    assert all(h["acl"] == ["all-staff"] for h in leaky.search("payroll", USERS["priya"].groups))


# ---- 4. Answers with citations ----------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Use US-East [1]. Restart first [2][1].", [1, 2]),
    ("see [1, 3]", [1, 3]),
    ("no citations here", []),
    ("costs [12]", [12]),
])
def test_parse_citations(text, expected):
    assert ask_mod.parse_citations(text) == expected


def test_answer_with_valid_citations_is_returned(retriever):
    s = settings(LLM_MODE="live")
    live = scripted("Switch to the US-East gateway while INC-2041 is open [1].")
    out = ask_mod.ask("vpn keeps dropping", USERS["priya"], retriever, A.Adapter("live", live=live), budget(s), s)
    assert out["stop_reason"] == "answered" and out["grounded"] is True
    assert out["citations"][0]["n"] == 1 and out["citations"][0]["url"].startswith("kb/")
    assert live.last["tools"] == [] and "[1]" in live.last["messages"][1]["content"]


@pytest.mark.parametrize("text", ["NOT_FOUND", "Just restart it.", "See the runbook [9]."])
def test_ungrounded_answers_are_refused_not_shown(retriever, text):
    s = settings(LLM_MODE="live")
    out = ask_mod.ask("vpn keeps dropping", USERS["priya"], retriever, A.Adapter("live", live=scripted(text)), budget(s), s)
    assert out["stop_reason"] == "ungrounded" and out["answer"] == ask_mod.REFUSAL and out["citations"] == []


def test_no_sources_means_no_model_call(retriever):
    s = settings(LLM_MODE="live")
    live = scripted("should never be used")
    out = ask_mod.ask("vpn", USERS["sam"], retriever, A.Adapter("live", live=live), budget(s), s)
    assert out["stop_reason"] == "no_sources" and live.calls == 0


def test_kill_switch_stops_ask_before_the_model(retriever):
    s = settings(LLM_MODE="live", ATLAS_KILL_SWITCH="1")
    live = scripted("x [1]")
    with pytest.raises(BudgetExceeded):
        ask_mod.ask("vpn keeps dropping", USERS["priya"], retriever, A.Adapter("live", live=live), budget(s), s)
    assert live.calls == 0


# ---- 5. The API ---------------------------------------------------------------------

def test_ask_requires_a_known_user():
    c = TestClient(create_app(settings()))
    assert c.post("/v1/ask", json={"question": "vpn"}).status_code == 401
    assert c.post("/v1/ask", json={"question": "vpn"}, headers={"X-Atlas-User": "mallory"}).status_code == 401


def test_ready_includes_the_knowledge_base():
    r = TestClient(create_app(settings())).get("/v1/ready")
    assert r.status_code == 200 and r.json() == {"ready": True, "failing": []}


def test_recorded_answer_replays_with_a_vpn_citation():
    # Recorded once from a real model with:  LLM_MODE=record python -m atlas.cli ask --user priya "<question>"
    # Rerank off: the week 4 reranker reorders the sources, which changes the prompt (see test_week4.py).
    r = TestClient(create_app(settings(ATLAS_RERANK="off"))).post("/v1/ask", json={"question": RECORDED_QUESTION},
                                                headers={"X-Atlas-User": "priya"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stop_reason"] == "answered" and body["mode"] == "replay"
    assert any(c["id"].startswith("vpn#") for c in body["citations"])
