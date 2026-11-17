"""Evals: run a golden set through the ask pipeline and decide if a change may ship (Unit 4).

A golden case (data/golden.json, or <dataset>/golden.json) says what a correct run looks like:

  {"id": "vpn-drops", "user": "priya", "question": "...",
   "expect_docs": ["vpn"],          answerable: these articles must be retrieved and cited
   "expect_refusal": false,         or true: Atlas must refuse (out of scope, no permission, injection)
   "forbidden_docs": ["vpn-runbook"],   optional: never retrieve or cite these for this user
   "must_not_contain": ["vpn-usw-01"]}  optional: strings that must never appear in the answer

run() sends every case through the same ask() the API uses, as that user, and records what was
retrieved, what was cited and whether Atlas refused. score_case() and summarize() turn that into
metrics; gate() compares them with thresholds (evals.thresholds.json) and says pass or fail.

Offline (the default) the model is stub_model: deterministic, no network, so CI gives the same
numbers every time. It tests retrieval, permissions, rerank, grounding and refusals, not the
model's writing. For the model, run the same set with --live, several times (--repeat).
"""
import json
from pathlib import Path

from . import ask as ask_mod
from .budget import Budget


def load_golden(path) -> list[dict]:
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("golden case ids must be unique")
    for c in cases:
        if not c.get("expect_refusal") and not c.get("expect_docs"):
            raise ValueError(c["id"] + ": an answerable case needs expect_docs")
    return cases


class _Recorder:
    """Wraps a retriever and remembers what the last search returned."""

    def __init__(self, retriever):
        self.retriever, self.hits = retriever, []

    def search(self, query, groups, k=4):
        self.hits = self.retriever.search(query, groups, k=k)
        return self.hits


def run_case(case: dict, users: dict, retriever, llm, settings) -> dict:
    user = users.get(case["user"])
    if user is None:
        raise ValueError("%s: unknown user %r" % (case["id"], case["user"]))
    rec = _Recorder(retriever)
    budget = Budget(float("inf"), settings.max_tokens_per_call)
    out = ask_mod.ask(case["question"], user, rec, llm, budget, settings)
    by_id = {h["id"]: h for h in rec.hits}
    return {"id": case["id"], "user": user.id, "groups": list(user.groups),
            "expect_refusal": bool(case.get("expect_refusal")), "expect_docs": case.get("expect_docs", []),
            "forbidden_docs": case.get("forbidden_docs", []), "must_not_contain": case.get("must_not_contain", []),
            "retrieved": [{"id": h["id"], "doc_id": h["doc_id"], "acl": list(h["acl"])} for h in rec.hits],
            "cited": [{"id": c["id"], "doc_id": by_id[c["id"]]["doc_id"] if c["id"] in by_id else c["id"].split("#")[0],
                       "acl": list(by_id[c["id"]]["acl"]) if c["id"] in by_id else []} for c in out["citations"]],
            "refused": out["stop_reason"] != "answered", "stop_reason": out["stop_reason"],
            "answer": out["answer"], "cost_usd": out["cost_usd"]}


def run(cases: list[dict], users: dict, retriever, llm, settings, repeat: int = 1) -> list[dict]:
    return [dict(run_case(c, users, retriever, llm, settings), attempt=i + 1) for i in range(repeat) for c in cases]


def evaluate(settings, golden_path=None, llm=None, repeat: int = 1) -> tuple[list[dict], dict]:
    """Build Atlas from settings (dataset, rerank, model mode) exactly as the API does, run the
    golden set and return (results, summarize(results)). Used by tools/eval_gate.py and the tests."""
    from . import adapter, embed, identity, rerank, retrieval
    users = identity.load_directory(settings.data_dir / "users.json")
    e = embed.from_settings(settings)
    retriever = rerank.wrap(retrieval.Retriever(retrieval.build(settings, e), e), settings)
    cases = load_golden(golden_path or settings.data_dir / "golden.json")
    results = run(cases, users, retriever, llm or adapter.from_settings(settings), settings, repeat)
    return results, summarize(results)


def score_case(r: dict) -> dict:
    """Score one result from run_case(). Return:

    recall     answerable cases only (expect_refusal False), else None: the share of expect_docs
               that appear among the doc_ids in r["retrieved"]
    rr         answerable only, else None: 1 / (1-based position of the first retrieved chunk whose
               doc_id is in expect_docs); 0.0 if none
    cited_ok   answerable only, else 0: how many cited chunks have a doc_id in expect_docs
    cited      answerable only, else 0: how many chunks were cited
    refusal_ok r["refused"] == r["expect_refusal"]
    leak       True if ANY of these: a retrieved or cited chunk whose acl shares no group with
               r["groups"]; a retrieved or cited doc_id in forbidden_docs; an answer that contains a
               must_not_contain string (case-insensitive) while Atlas did not refuse
    """
    # TODO(week4): the six fields above. A leak is the one number that must always be zero.
    raise NotImplementedError("TODO(week4): score_case")


def summarize(results: list[dict]) -> dict:
    """Metrics over all results (score each with score_case):

    cases               number of results
    recall_at_k         mean recall over answerable cases (1.0 if there are none)
    mrr                 mean rr over answerable cases (1.0 if there are none)
    citation_precision  sum(cited_ok) / sum(cited) over all cases (1.0 if nothing was cited)
    refusal_accuracy    share of cases with refusal_ok
    answer_rate         share of answerable cases that were answered (not refused) (1.0 if none)
    permission_leaks    number of cases with leak
    overall             mean of recall_at_k, citation_precision and refusal_accuracy; 0.0 if any leak
    Round every float to 4 decimals.
    """
    # TODO(week4): aggregate score_case() over the results.
    raise NotImplementedError("TODO(week4): summarize")


def gate(metrics: dict, thresholds: dict) -> tuple[bool, list[str]]:
    """thresholds: {"min": {metric: lowest allowed}, "max": {metric: highest allowed}}.
    Return (passed, reasons), one reason per broken or missing metric, in the order of thresholds,
    "min" first. Reason text: "<metric> 0.8 < min 0.9", "<metric> 2 > max 0", "<metric> missing".
    """
    # TODO(week4): compare every threshold; a missing metric is a failure, not a pass.
    raise NotImplementedError("TODO(week4): gate")
