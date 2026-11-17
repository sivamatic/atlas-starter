"""Week 0 tests: Hello Atlas.

Run:  pytest -m week0
Every test runs offline: replay recordings or scripted fake providers, no keys, no network.
"""
import json
import re
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas import adapter as A
from atlas import agent, config
from atlas.app import create_app, readiness
from atlas.budget import Budget, BudgetExceeded

pytestmark = pytest.mark.week0
ROOT = Path(__file__).resolve().parents[1]
RECORDED_QUESTION = "My VPN keeps dropping every few minutes. What should I do?"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def settings(**over):
    return config.load({"LLM_MODE": "replay", **{k: str(v) for k, v in over.items()}})


def scripted(*responses):
    """A fake live provider that returns the given responses in order and counts calls."""
    seq = list(responses)

    def live(req):
        live.calls += 1
        if not seq:
            raise AssertionError("scripted provider ran out of responses")
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    live.calls = 0
    return live


def tool_call(args, name="check_status", id="c1"):
    return {"text": "", "tool_calls": [{"id": id, "name": name, "arguments": args}],
            "usage": {"input_tokens": 100, "output_tokens": 10}}


def answer(text):
    return {"text": text, "tool_calls": [], "usage": {"input_tokens": 120, "output_tokens": 30}}


# ---- 1. Health and readiness -------------------------------------------------

def test_readiness_all_green():
    assert readiness({"db": True, "llm": True}) == (200, {"ready": True, "failing": []})


def test_readiness_lists_failures_sorted():
    assert readiness({"llm": False, "db": False, "cache": True}) == (503, {"ready": False, "failing": ["db", "llm"]})


def test_health_checks_nothing_external():
    s = settings(ATLAS_KILL_SWITCH=1)
    c = TestClient(create_app(s, llm=A.Adapter("replay", tape={})))
    assert c.get("/v1/health").json() == {"status": "ok"}


def test_ready_endpoint():
    s = settings()
    assert TestClient(create_app(s)).get("/v1/ready").status_code == 200
    r = TestClient(create_app(settings(ATLAS_KILL_SWITCH=1), llm=A.Adapter("replay", tape={}))).get("/v1/ready")
    assert r.status_code == 503 and r.json()["failing"] == ["budget", "llm_adapter"]


# ---- 2. Budget guardrail ------------------------------------------------------

def test_budget_token_cap():
    with pytest.raises(BudgetExceeded, match="TOKEN_CAP"):
        Budget(1.0, 100).authorize(101)


def test_budget_ceiling():
    b = Budget(0.05, 100)
    b.authorize(100)
    b.record(0.03)
    b.record(0.02)
    assert b.spent == pytest.approx(0.05)
    with pytest.raises(BudgetExceeded, match="BUDGET_EXCEEDED"):
        b.authorize(10)


def test_kill_switch_stops_everything():
    with pytest.raises(BudgetExceeded, match="KILL_SWITCH"):
        Budget(1.0, 100, kill_switch=True).authorize(1)
    r = TestClient(create_app(settings(ATLAS_KILL_SWITCH=1))).post("/v1/chat", json={"message": RECORDED_QUESTION})
    assert r.status_code == 503 and r.json()["detail"] == "KILL_SWITCH"


# ---- 3. Adapter: record and replay ---------------------------------------------

REQ = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": [], "max_tokens": 10}


def test_record_saves_response(tmp_path):
    path = tmp_path / "tape.json"
    a = A.Adapter("record", live=scripted(answer("saved")), tape_path=path)
    assert a.complete(REQ)["text"] == "saved"
    tape = json.loads(path.read_text(encoding="utf-8"))
    assert list(tape.values())[0]["response"]["text"] == "saved"


def test_replay_never_calls_live():
    tape = {}
    A.Adapter("record", live=scripted(answer("saved")), tape=tape).complete(REQ)
    live = scripted(answer("LIVE"))
    assert A.Adapter("replay", live=live, tape=tape).complete(REQ)["text"] == "saved"
    assert live.calls == 0


def test_replay_miss_fails_loudly():
    live = scripted(answer("LIVE"))
    with pytest.raises(A.ReplayMiss):
        A.Adapter("replay", live=live, tape={}).complete(REQ)
    assert live.calls == 0


def test_no_vendor_sdk_outside_adapter():
    pattern = re.compile(r"^\s*(import|from)\s+(openai|anthropic|google\.genai|boto3|mistralai)\b", re.M)
    offenders = [p.name for p in (ROOT / "atlas").glob("*.py") if p.name != "adapter.py" and pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


# ---- 4. The bounded agent loop ----------------------------------------------------

def run_scripted(*responses, **over):
    s = settings(**over)
    live = scripted(*responses)
    out = agent.run("VPN keeps dropping", A.Adapter("live", live=live), Budget(1.0, s.max_tokens_per_call), s)
    return out, live


def test_tool_then_answer():
    out, _ = run_scripted(tool_call('{"service": "vpn"}'), answer("Use US-East. INC-2041."))
    assert out["stop_reason"] == "answered" and out["answer"] == "Use US-East. INC-2041."
    assert out["steps"] == 2 and out["tool_calls"] == [{"name": "check_status", "ok": True}]


def test_malformed_arguments_go_back_to_the_model():
    out, _ = run_scripted(tool_call("{not json"), tool_call('{"service": "printer"}', id="c2"),
                          tool_call('{"service": "vpn"}', id="c3"), answer("ok"))
    assert out["stop_reason"] == "answered"
    assert [c["ok"] for c in out["tool_calls"]] == [False, False, True]


def test_step_limit_stops_a_looping_model():
    out, live = run_scripted(*[tool_call('{"service": "vpn"}', id="c%d" % i) for i in range(10)], ATLAS_MAX_STEPS=3)
    assert out["stop_reason"] == "max_steps" and out["answer"] is None
    assert out["steps"] == 3 and live.calls == 3


def test_cost_is_recorded_from_configured_prices():
    out, _ = run_scripted(tool_call('{"service": "vpn"}'), answer("ok"),
                          LLM_PRICE_IN_PER_M=3, LLM_PRICE_OUT_PER_M=15)
    # (100 + 120) input tokens at $3/M and (10 + 30) output tokens at $15/M
    assert out["cost_usd"] == pytest.approx((220 * 3 + 40 * 15) / 1_000_000)


# ---- 5. The service end to end ----------------------------------------------------

def test_chat_replays_a_real_recording_offline():
    r = TestClient(create_app(settings())).post("/v1/chat", json={"message": RECORDED_QUESTION})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stop_reason"] == "answered" and body["mode"] == "replay"
    assert body["tool_calls"][0]["name"] == "check_status"
    assert "US-East" in body["answer"]


def test_unrecorded_question_is_a_loud_replay_miss():
    r = TestClient(create_app(settings())).post("/v1/chat", json={"message": "Something nobody recorded"})
    assert r.status_code == 500 and r.json()["error"] == "replay_miss"


def test_provider_outage_returns_502_not_a_crash():
    s = settings()
    llm = A.Adapter("live", live=scripted(A.ProviderError("provider returned HTTP 429", 429)))
    r = TestClient(create_app(s, llm=llm)).post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 502 and r.json()["error"] == "provider_unavailable"


def test_empty_message_is_rejected():
    assert TestClient(create_app(settings())).post("/v1/chat", json={"message": ""}).status_code == 422
