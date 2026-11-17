"""Week 4 tests: evals, a reranker and a merge gate.

Run:  pytest -m week4
Offline: the stub model (atlas/stub_model.py) and the hash embedder. No network, no model.
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas import config, embed, evals, identity, rerank, retrieval
from atlas.app import create_app

pytestmark = pytest.mark.week4
ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted(p.name for p in (ROOT / "examples").glob("*") if (p / "golden.json").exists())
THRESHOLDS = json.loads((ROOT / "evals.thresholds.json").read_text(encoding="utf-8"))
RECORDED_QUESTION = "My VPN keeps dropping every few minutes. What should I do?"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("tests must not touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def settings(**over):
    return config.load({"LLM_MODE": "stub", **{k: str(v) for k, v in over.items()}})


def chunk(id, text, title="T", section="S", score=0.5, acl=("all-staff",)):
    return {"id": id, "doc_id": id.split("#")[0], "title": title, "section": section, "text": text,
            "score": score, "acl": list(acl)}


# ---- 1. The reranker -----------------------------------------------------------------

def test_overlap_score_rewards_coverage_heading_and_proximity():
    q = "reset outlook account"
    none = chunk("a#1", "Laptops are replaced every four years.")
    far = chunk("b#1", "Reset your laptop. Many unrelated words sit here before the outlook app and the account.")
    near = chunk("c#1", "Open Outlook and select reset account.")
    heading = chunk("d#1", "Open Outlook and select reset account.", title="Outlook", section="Reset account")
    scores = [rerank.overlap_score(q, c) for c in (none, far, near, heading)]
    assert scores[0] == 0.0
    assert scores[0] < scores[1] < scores[2] < scores[3] <= 1.0
    assert rerank.overlap_score("", near) == 0.0


def test_overlap_score_matches_word_forms():
    assert rerank.overlap_score("vpn dropping", chunk("a#1", "The VPN drops often.")) > 0.6


def test_rerank_moves_the_best_match_up_and_keeps_the_fused_score():
    hits = [chunk("wifi#1", "Use the Meridian-Corp network.", score=0.033),
            chunk("mail#1", "Install Outlook. Other mail apps are blocked.", title="Work email on your phone", score=0.032)]
    out = rerank.OverlapReranker().rerank("Can I use another mail app for work email on my phone?", hits)
    assert [h["id"] for h in out] == ["mail#1", "wifi#1"]
    assert out[0]["score"] == 0.032 and "rerank_score" in out[0]
    assert "rerank_score" not in hits[0], "rerank must not change its input"
    assert rerank.OverlapReranker().rerank("anything", []) == []


def test_reranking_retriever_reads_a_bigger_pool_and_returns_k():
    class Fake:
        store, embed = None, None

        def __init__(self):
            self.asked = []

        def search(self, query, groups, k=4):
            self.asked.append(k)
            return [chunk("d%d#1" % i, "filler words" if i else "wifi network password", score=1 / (i + 1)) for i in range(k)][::-1]

    fake = Fake()
    out = rerank.RerankingRetriever(fake, rerank.OverlapReranker(), pool=12).search("wifi password", ["all-staff"], k=3)
    assert fake.asked == [12] and len(out) == 3 and out[0]["id"] == "d0#1"
    fake = Fake()
    assert len(rerank.RerankingRetriever(fake, None, pool=12).search("wifi", ["all-staff"], k=3)) == 3
    assert fake.asked == [3], "without a reranker, retrieval must not fetch a bigger pool"


def test_rerank_is_on_by_default_and_can_be_switched_off():
    assert isinstance(rerank.from_settings(settings()), rerank.OverlapReranker)
    assert rerank.from_settings(settings(ATLAS_RERANK="off")) is None
    with pytest.raises(ValueError):
        rerank.from_settings(settings(ATLAS_RERANK="magic"))


def test_reranking_never_shows_an_article_the_user_cannot_read():
    s = settings()
    e = embed.from_settings(s)
    r = rerank.wrap(retrieval.Retriever(retrieval.build(s, e), e), s)
    hits = r.search("restart the VPN gateway concentrator vpn-usw-01", ["all-staff"], k=4)
    assert hits and all("all-staff" in h["acl"] for h in hits)


# ---- 2. Scoring and the gate ---------------------------------------------------------

def result(**over):
    base = {"id": "c", "user": "priya", "groups": ["all-staff"], "expect_refusal": False, "expect_docs": ["vpn"],
            "forbidden_docs": [], "must_not_contain": [], "refused": False, "answer": "Switch to US-East [1].",
            "retrieved": [{"id": "wifi#1", "doc_id": "wifi", "acl": ["all-staff"]},
                          {"id": "vpn#1", "doc_id": "vpn", "acl": ["all-staff"]}],
            "cited": [{"id": "vpn#1", "doc_id": "vpn", "acl": ["all-staff"]},
                      {"id": "wifi#1", "doc_id": "wifi", "acl": ["all-staff"]}]}
    return {**base, **over}


def test_score_case_for_an_answerable_question():
    s = evals.score_case(result())
    assert s == {"recall": 1.0, "rr": 0.5, "cited_ok": 1, "cited": 2, "refusal_ok": True, "leak": False}


def test_score_case_for_a_refusal():
    s = evals.score_case(result(expect_refusal=True, refused=True, cited=[], answer="NOT_FOUND"))
    assert s["recall"] is None and s["rr"] is None and s["cited"] == 0 and s["refusal_ok"] and not s["leak"]
    assert not evals.score_case(result(expect_refusal=True))["refusal_ok"]


@pytest.mark.parametrize("over", [
    {"retrieved": [{"id": "pay#1", "doc_id": "pay", "acl": ["hr"]}]},                # a chunk the user may not read
    {"cited": [{"id": "pay#1", "doc_id": "pay", "acl": ["hr"]}]},
    {"forbidden_docs": ["wifi"]},                                                     # a doc this case must never see
    {"must_not_contain": ["us-east"]},                                                # a secret string in the answer
])
def test_score_case_counts_every_kind_of_leak(over):
    assert evals.score_case(result(**over))["leak"] is True


def test_a_secret_string_in_a_refusal_is_not_a_leak():
    assert not evals.score_case(result(refused=True, must_not_contain=["us-east"]))["leak"]


def test_summarize_and_a_single_leak_zeroes_the_overall_score():
    ok = result()
    refusal = result(id="r", expect_refusal=True, refused=True, cited=[], answer="NOT_FOUND")
    miss = result(id="m", expect_docs=["mfa-reset"], cited=[])
    m = evals.summarize([ok, refusal, miss])
    assert m == {"cases": 3, "recall_at_k": 0.5, "mrr": 0.25, "citation_precision": 0.5,
                 "refusal_accuracy": 1.0, "answer_rate": 1.0, "permission_leaks": 0,
                 "overall": round((0.5 + 0.5 + 1.0) / 3, 4)}
    leaky = evals.summarize([ok, result(id="l", forbidden_docs=["vpn"])])
    assert leaky["permission_leaks"] == 1 and leaky["overall"] == 0.0


def test_gate_reports_every_broken_or_missing_metric():
    th = {"min": {"recall_at_k": 0.9, "mrr": 0.8, "overall": 0.5}, "max": {"permission_leaks": 0}}
    assert evals.gate({"recall_at_k": 0.95, "mrr": 0.8, "overall": 0.9, "permission_leaks": 0}, th) == (True, [])
    ok, reasons = evals.gate({"recall_at_k": 0.8, "mrr": 0.9, "permission_leaks": 2}, th)
    assert not ok
    assert reasons == ["recall_at_k 0.8 < min 0.9", "overall missing", "permission_leaks 2 > max 0"]


# ---- 3. The golden set, the gate and the examples --------------------------------------

def test_golden_set_passes_the_gate_with_no_leaks():
    results, m = evals.evaluate(settings())
    assert m["cases"] >= 15 and m["permission_leaks"] == 0
    assert any(r["expect_refusal"] for r in results) and any(not r["expect_refusal"] for r in results)
    assert evals.gate(m, THRESHOLDS) == (True, [])


def test_rerank_measurably_improves_the_golden_set():
    _, off = evals.evaluate(settings(ATLAS_RERANK="off"))
    _, on = evals.evaluate(settings(ATLAS_RERANK="overlap"))
    for name in ("recall_at_k", "mrr", "citation_precision", "refusal_accuracy"):
        assert on[name] >= off[name], name
    assert on["mrr"] > off["mrr"] and on["overall"] > off["overall"]


@pytest.mark.parametrize("example", EXAMPLES)
def test_each_industry_example_passes_its_gate(example):
    _, m = evals.evaluate(settings(ATLAS_DATA_DIR="examples/" + example))
    assert m["cases"] >= 10
    assert evals.gate(m, THRESHOLDS) == (True, []), m


def test_eval_gate_cli_writes_a_report_and_fails_below_threshold(tmp_path):
    report = tmp_path / "report.json"
    run = [sys.executable, str(ROOT / "tools" / "eval_gate.py"), "--report", str(report)]
    ok = subprocess.run(run + ["--compare"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "GATE: PASS" in ok.stdout
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["passed"] and set(body["runs"]) == {"rerank-off", "rerank-overlap"}
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"min": {"overall": 1.01}}), encoding="utf-8")
    bad = subprocess.run(run + ["--thresholds", str(strict)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert bad.returncode == 1 and "GATE: FAIL" in bad.stdout
    # --live with no model configured is a setup error (2), not a quality failure (1)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "ATLAS_"))}
    missing = subprocess.run(run + ["--live", "--data-dir", "examples/bank"], cwd=ROOT, env=dict(env, LLM_MODE="replay"),
                             capture_output=True, text=True, timeout=300)
    assert missing.returncode == 2 and "no recording" in missing.stderr, missing.stdout + missing.stderr


# ---- 4. The model is still real: a recording of the reranked prompt replays ------------

def test_reranked_recorded_answer_replays_with_a_vpn_citation():
    # Recorded once from a real model (Ollama llama3.2:3b) with rerank on:
    #   LLM_MODE=record python -m atlas.cli ask --user priya "<question>"
    r = TestClient(create_app(config.load({"LLM_MODE": "replay"}))).post(
        "/v1/ask", json={"question": RECORDED_QUESTION}, headers={"X-Atlas-User": "priya"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stop_reason"] == "answered" and body["mode"] == "replay"
    assert body["citations"] and all(c["id"].startswith("vpn#") for c in body["citations"])

