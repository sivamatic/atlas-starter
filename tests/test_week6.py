"""Week 6 tests: traces, metrics and no personal data in telemetry.

Run:  pytest -m week6
Offline: the built-in MemoryTracer and the stub model. The OpenTelemetry test runs only when
opentelemetry-sdk is installed (pip install -r requirements-otel.txt).
"""
import json
import urllib.request

import pytest
from fastapi.testclient import TestClient

from atlas import config, telemetry, tools
from atlas.adapter import Adapter
from atlas.app import create_app

pytestmark = pytest.mark.week6
QUESTION = "My VPN disconnects every few minutes. What should I do?"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("tests must not touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def settings(**over):
    return config.load({"LLM_MODE": "stub", **{k: str(v) for k, v in over.items()}})


def client(tracer=None, metrics=None, **over):
    tracer = tracer or telemetry.MemoryTracer()
    metrics = metrics or telemetry.Metrics()
    return TestClient(create_app(settings(**over), tracer=tracer, metrics=metrics)), tracer, metrics


def ask(c, question=QUESTION, user="priya"):
    return c.post("/v1/ask", json={"question": question}, headers={"X-Atlas-User": user})


def tree(spans):
    by_id = {s["span_id"]: s for s in spans}
    return {s["name"]: (by_id[s["parent_id"]]["name"] if s["parent_id"] else None) for s in spans}


# ---- 1. One request, one trace -----------------------------------------------------------

def test_ask_produces_a_span_tree_and_returns_its_trace_id():
    c, tracer, _ = client()
    r = ask(c)
    assert r.status_code == 200, r.text
    trace_id = r.headers["X-Trace-Id"]
    assert trace_id and r.json()["trace_id"] == trace_id
    spans = tracer.trace(trace_id)
    assert tree(spans) == {"atlas.request": None, "retrieval": "atlas.request",
                           "rerank": "atlas.request", "model.call": "atlas.request"}
    assert {s["trace_id"] for s in spans} == {trace_id}


def test_spans_carry_the_attributes_on_call_needs():
    c, tracer, _ = client()
    spans = {s["name"]: s["attributes"] for s in tracer.trace(ask(c).headers["X-Trace-Id"])}
    req = spans["atlas.request"]
    assert req["http.route"] == "/v1/ask" and req["http.response.status_code"] == 200
    assert req["atlas.tenant"] == "meridian" and req["atlas.user.role"] == "Field sales engineer"
    assert req["atlas.user.groups"] == "all-staff" and req["atlas.stop_reason"] == "answered"
    assert req["atlas.latency_ms"] >= 0 and "atlas.cost_usd" in req and "atlas.refusal_reason" not in req
    assert spans["retrieval"]["atlas.retrieval.hits"] > 0
    assert spans["rerank"]["atlas.rerank.name"] == "overlap" and spans["rerank"]["atlas.rerank.kept"] == 4
    model = spans["model.call"]
    assert model["gen_ai.request.model"] == "llama3.2:3b" and model["gen_ai.operation.name"] == "chat"
    assert model["gen_ai.usage.input_tokens"] > 0 and model["gen_ai.usage.output_tokens"] > 0
    assert model["atlas.cache_hit"] is False and model["atlas.latency_ms"] >= 0 and "atlas.cost_usd" in model


def test_a_refusal_records_its_reason():
    c, tracer, _ = client()
    r = ask(c, "What is on the cafeteria lunch menu this Friday?")
    req = next(s for s in tracer.trace(r.headers["X-Trace-Id"]) if s["name"] == "atlas.request")
    assert r.json()["stop_reason"] != "answered"
    assert req["attributes"]["atlas.refusal_reason"] == r.json()["stop_reason"]


def test_a_tool_call_is_a_child_of_the_request():
    service = next(iter(tools.STATUS))
    script = [{"text": "", "tool_calls": [{"id": "c1", "name": "check_status", "arguments": json.dumps({"service": service})}],
               "usage": {"input_tokens": 100, "output_tokens": 10}},
              {"text": "It is fine.", "tool_calls": [], "usage": {"input_tokens": 120, "output_tokens": 30}}]
    tracer = telemetry.MemoryTracer()
    app = create_app(settings(), llm=Adapter("live", live=lambda req: script.pop(0)), tracer=tracer)
    r = TestClient(app).post("/v1/chat", json={"message": "Is %s up?" % service})
    spans = tracer.trace(r.headers["X-Trace-Id"])
    assert [s["name"] for s in spans].count("model.call") == 2
    tool = next(s for s in spans if s["name"] == "tool.call")
    assert tree(spans)["tool.call"] == "atlas.request"
    assert tool["attributes"]["gen_ai.tool.name"] == "check_status" and tool["attributes"]["atlas.tool.ok"] is True


def test_probes_are_not_traced_or_counted():
    c, tracer, metrics = client()
    assert c.get("/v1/health").status_code == 200
    assert "X-Trace-Id" not in c.get("/v1/health").headers
    assert len(tracer.spans) == 0 and metrics.requests == 0


def test_spans_are_appended_to_a_jsonl_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    c, _, _ = client(tracer=telemetry.MemoryTracer(path))
    trace_id = ask(c).headers["X-Trace-Id"]
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {s["name"] for s in lines} == {"atlas.request", "retrieval", "rerank", "model.call"}
    assert all(s["trace_id"] == trace_id for s in lines)


# ---- 2. No personal data by default --------------------------------------------------------

def test_question_and_answer_text_never_reach_a_span_by_default():
    c, tracer, _ = client()
    question = "My VPN disconnects every few minutes. Call me on +1 512 555 0199 or priya@example.com"
    ask(c, question)
    dump = json.dumps(list(tracer.spans))
    for secret in ("disconnects every few minutes", "555 0199", "priya@example.com", "Priya Nair"):
        assert secret not in dump
    req = next(s for s in tracer.spans if s["name"] == "atlas.request")["attributes"]
    assert req["atlas.question.chars"] == len(question)


def test_scrub_drops_content_unless_asked_and_masks_it_when_kept():
    attrs = {"atlas.question": "Mail jo@example.com, call 0151 555 0199, account 12345678", "atlas.tenant": "meridian",
             "atlas.refusal_reason": None}
    assert telemetry.scrub(attrs) == {"atlas.tenant": "meridian"}
    kept = telemetry.scrub(attrs, capture_content=True)
    assert kept["atlas.tenant"] == "meridian" and "atlas.refusal_reason" not in kept
    assert "jo@example.com" not in kept["atlas.question"] and "[email]" in kept["atlas.question"]
    assert "555 0199" not in kept["atlas.question"] and "12345678" not in kept["atlas.question"]


def test_content_capture_is_an_explicit_opt_in():
    c, tracer, _ = client(tracer=telemetry.MemoryTracer(capture_content=True))
    ask(c)
    req = next(s for s in tracer.spans if s["name"] == "atlas.request")["attributes"]
    assert req["atlas.question"] == QUESTION


# ---- 3. Metrics ----------------------------------------------------------------------------

def test_percentile_is_nearest_rank():
    assert telemetry.percentile([], 50) is None
    assert telemetry.percentile([5, 1, 3, 2, 4], 50) == 3
    assert telemetry.percentile([5, 1, 3, 2, 4], 95) == 5
    assert telemetry.percentile(list(range(1, 101)), 95) == 95
    assert telemetry.percentile([7], 1) == 7


def test_metrics_snapshot_math():
    m = telemetry.Metrics()
    assert m.snapshot()["refusal_rate"] is None and m.snapshot()["cost_per_resolved_usd"] is None
    m.observe("/v1/ask", 200, 10.0, {"kind": "ask", "answered": True, "cost_usd": 0.002})
    m.observe("/v1/ask", 200, 30.0, {"kind": "ask", "answered": False, "cost_usd": 0.001})
    m.observe("/v1/chat", 200, 20.0, {"kind": "chat", "answered": True, "cost_usd": 0.003})
    m.observe("/v1/ask", 500, 40.0)
    s = m.snapshot()
    assert s["requests"] == 4 and s["errors"] == 1 and s["asks"] == 2 and s["refusals"] == 1
    assert s["refusal_rate"] == 0.5 and s["resolved"] == 2
    assert s["cost_usd_total"] == 0.006 and s["cost_per_resolved_usd"] == 0.003
    assert s["latency_ms"] == {"p50": 20.0, "p95": 40.0}
    assert s["by_route"] == {"/v1/ask": 3, "/v1/chat": 1}


def test_metrics_endpoint_counts_real_requests():
    c, _, _ = client()
    ask(c)
    ask(c, "What is on the cafeteria lunch menu this Friday?")
    ask(c, user="nobody")
    s = c.get("/metrics").json()
    assert s["requests"] == 3 and s["asks"] == 2 and s["refusals"] == 1 and s["refusal_rate"] == 0.5
    assert s["resolved"] == 1 and s["by_route"] == {"/v1/ask": 3}
    assert s["latency_ms"]["p50"] is not None and s["errors"] == 0
    text = c.get("/metrics", params={"format": "prometheus"}).text
    assert 'atlas_requests_total{route="/v1/ask"} 3' in text and "atlas_refusals_total 1" in text


# ---- 4. OpenTelemetry (optional) -----------------------------------------------------------

def test_otel_tracer_builds_the_same_tree():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    c, _, _ = client(tracer=telemetry.OtelTracer(provider))
    r = ask(c)
    spans = exporter.get_finished_spans()
    by_id = {s.context.span_id: s.name for s in spans}
    parents = {s.name: (by_id.get(s.parent.span_id) if s.parent else None) for s in spans}
    assert parents == {"atlas.request": None, "retrieval": "atlas.request", "rerank": "atlas.request",
                       "model.call": "atlas.request"}
    assert {format(s.context.trace_id, "032x") for s in spans} == {r.headers["X-Trace-Id"]}
    model = next(s for s in spans if s.name == "model.call")
    assert model.attributes["gen_ai.usage.input_tokens"] > 0
    assert all("atlas.question" not in s.attributes for s in spans)
