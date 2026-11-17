"""Tracing and operational metrics (Unit 6).

One request becomes one trace, a tree of spans:

  atlas.request              the HTTP request (middleware in app.py)
    retrieval                keyword + vector search, fused (rerank.RerankingRetriever)
    rerank                   the second look at the candidates, when a reranker is on
    model.call               one call through the provider adapter (TracedAdapter)
    tool.call                one tool the agent ran (tools.run_tool)

Two tracers, same interface:
  MemoryTracer  built in. Keeps recent spans in memory and, with ATLAS_TRACE_FILE, appends each
                finished span to a JSON Lines file. Good for tests, dev and "what just happened?".
  OtelTracer    the OpenTelemetry API. With the optional packages in requirements-otel.txt and
                OTEL_EXPORTER_OTLP_ENDPOINT set, spans go to any OTLP backend: Langfuse, Arize
                Phoenix, Jaeger, Grafana Tempo. See the README for the exact variables.

Traces are not a place for personal data. scrub() runs on every attribute before it is stored,
and question or answer text is dropped unless ATLAS_TRACE_CONTENT=1.

Metrics answer the on-call questions: how many requests, how slow (p50, p95), how often Atlas
refuses, what a resolved request costs, how many errors. GET /metrics serves them.
"""
import contextvars
import json
import math
import re
import threading
import time
import uuid
from collections import Counter, deque
from contextlib import contextmanager
from pathlib import Path

from .budget import cost_usd

# Span attributes that can hold what a person typed or what the model said.
CONTENT_KEYS = {"atlas.question", "atlas.answer", "gen_ai.prompt", "gen_ai.completion"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+?\d[\d ().-]{6,}\d")
LONG_NUMBER = re.compile(r"\b\d{6,}\b")


def scrub(attributes: dict, capture_content: bool = False) -> dict:
    """Return the attributes that are safe to store on a span.

    - Drop keys whose value is None.
    - Drop CONTENT_KEYS unless capture_content is True.
    - When content is kept, mask it: emails -> "[email]", phone numbers -> "[phone]",
      other runs of 6+ digits (account, member or record numbers) -> "[number]".
    - Everything else passes through unchanged.
    """
    out = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if key in CONTENT_KEYS:
            if not capture_content:
                continue
            value = LONG_NUMBER.sub("[number]", PHONE.sub("[phone]", EMAIL.sub("[email]", str(value))))
        out[key] = value
    return out


# ---------------------------------------------------------------- the built-in tracer

_span = contextvars.ContextVar("atlas_span", default=None)
_tracer = contextvars.ContextVar("atlas_tracer", default=None)
_outcome = contextvars.ContextVar("atlas_outcome", default=None)


class Span:
    def __init__(self, name, trace_id, parent_id, scrubber):
        self.name, self.trace_id, self.parent_id = name, trace_id, parent_id
        self.span_id = uuid.uuid4().hex[:16]
        self.start, self.end = time.time(), None
        self.status, self.attributes, self._scrub = "ok", {}, scrubber

    def set(self, key, value):
        self.attributes.update(self._scrub({key: value}))

    def update(self, attributes: dict):
        self.attributes.update(self._scrub(attributes))

    def to_dict(self):
        return {"name": self.name, "trace_id": self.trace_id, "span_id": self.span_id, "parent_id": self.parent_id,
                "start": self.start, "duration_ms": round(((self.end or time.time()) - self.start) * 1000, 3),
                "status": self.status, "attributes": dict(self.attributes)}


class MemoryTracer:
    kind = "memory"

    def __init__(self, path: str | Path | None = None, capture_content: bool = False, keep: int = 5000):
        self.path = Path(path) if path else None
        self.capture_content = capture_content
        self.spans = deque(maxlen=keep)
        self._lock = threading.Lock()

    def _scrub(self, attributes):
        return scrub(attributes, self.capture_content)

    @contextmanager
    def span(self, name: str, attributes: dict | None = None):
        parent = _span.get()
        sp = Span(name, parent.trace_id if parent else uuid.uuid4().hex, parent.span_id if parent else None, self._scrub)
        sp.update(attributes or {})
        token = _span.set(sp)
        try:
            yield sp
        except BaseException as e:
            sp.status = "error"
            sp.set("error.type", type(e).__name__)
            raise
        finally:
            sp.end = time.time()
            _span.reset(token)
            self._finish(sp)

    def _finish(self, sp):
        record = sp.to_dict()
        with self._lock:
            self.spans.append(record)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def current_trace_id(self):
        sp = _span.get()
        return sp.trace_id if sp else None

    def current_span(self):
        return _span.get()

    def trace(self, trace_id):
        """Every finished span of one trace, oldest first."""
        with self._lock:
            return [s for s in self.spans if s["trace_id"] == trace_id]


# ---------------------------------------------------------------- OpenTelemetry

class _OtelSpan:
    def __init__(self, span, scrubber):
        self._span, self._scrub = span, scrubber

    def set(self, key, value):
        for k, v in self._scrub({key: value}).items():
            self._span.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else json.dumps(v))

    def update(self, attributes):
        for k, v in attributes.items():
            self.set(k, v)

    @property
    def trace_id(self):
        return format(self._span.get_span_context().trace_id, "032x")


class OtelTracer:
    """Spans through the OpenTelemetry API. The exporter (where spans go) is configured once per
    process by configure_otel(), from the standard OTEL_* environment variables."""
    kind = "otel"

    def __init__(self, provider=None, capture_content: bool = False):
        from opentelemetry import trace
        self._trace = trace
        self._tracer = (provider or trace.get_tracer_provider()).get_tracer("atlas")
        self.capture_content = capture_content

    def _scrub(self, attributes):
        return scrub(attributes, self.capture_content)

    @contextmanager
    def span(self, name: str, attributes: dict | None = None):
        with self._tracer.start_as_current_span(name) as s:
            sp = _OtelSpan(s, self._scrub)
            sp.update(attributes or {})
            token = _span.set(sp)
            try:
                yield sp
            except BaseException as e:
                s.set_attribute("error.type", type(e).__name__)
                raise
            finally:
                _span.reset(token)

    def current_trace_id(self):
        ctx = self._trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None

    def current_span(self):
        return _span.get()


_otel_provider = None


def configure_otel(service_name: str = "atlas"):
    """Install an OTLP/HTTP exporter once per process. Needs requirements-otel.txt.
    The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT (or ..._TRACES_ENDPOINT) and OTEL_EXPORTER_OTLP_HEADERS itself."""
    global _otel_provider
    if _otel_provider is None:
        import os
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _otel_provider = provider
    return _otel_provider


def from_settings(s):
    """auto: OpenTelemetry when an OTLP endpoint is set and the SDK is installed, else the built-in tracer."""
    mode = s.tracer
    if mode == "auto":
        mode = "otel" if s.otlp_endpoint else "memory"
    if mode == "otel":
        try:
            return OtelTracer(configure_otel(), capture_content=s.trace_content)
        except ImportError:
            import logging
            logging.getLogger("atlas").warning("OpenTelemetry is not installed (pip install -r requirements-otel.txt); "
                                               "using the built-in tracer")
    elif mode != "memory":
        raise ValueError("ATLAS_TRACER must be auto, memory or otel, got " + repr(s.tracer))
    return MemoryTracer(s.trace_file or None, capture_content=s.trace_content)


# ---------------------------------------------------------------- what instrumented code calls

class _NoSpan:
    trace_id = None

    def set(self, key, value):
        pass

    def update(self, attributes):
        pass


@contextmanager
def span(name: str, attributes: dict | None = None):
    """A child of the current span, on the tracer of the current request. A no-op outside a request
    (CLI, evals, tests that don't install a tracer), so instrumented code never needs to check."""
    tracer = _tracer.get()
    if tracer is None:
        yield _NoSpan()
        return
    with tracer.span(name, attributes) as sp:
        yield sp


def use(tracer):
    """Make tracer the current one (returns a token for reset)."""
    return _tracer.set(tracer)


def reset(token):
    _tracer.reset(token)


def annotate(attributes: dict):
    """Add attributes to the current span (for an endpoint: the request span)."""
    tracer = _tracer.get()
    sp = tracer.current_span() if tracer else None
    if sp is not None:
        sp.update(attributes)


def current_trace_id():
    tracer = _tracer.get()
    return tracer.current_trace_id() if tracer else None


def outcome(**fields):
    """Tell the metrics what this request achieved: kind ("ask" | "chat"), answered, cost_usd."""
    o = _outcome.get()
    if o is not None:
        o.update(fields)


def begin_outcome():
    return _outcome.set({})


def end_outcome(token) -> dict:
    o = _outcome.get() or {}
    _outcome.reset(token)
    return o


class TracedAdapter:
    """Wraps the provider adapter: one model.call span per call, with tokens, cost and latency.
    Same interface as adapter.Adapter, so nothing else changes."""

    def __init__(self, inner, settings):
        self.inner, self.s = inner, settings

    @property
    def mode(self):
        return self.inner.mode

    def configured(self):
        return self.inner.configured()

    def complete(self, req: dict) -> dict:
        last = next((m.get("content") for m in reversed(req.get("messages", [])) if m.get("role") == "user"), None)
        with span("model.call", {"gen_ai.operation.name": "chat", "gen_ai.request.model": req.get("model"),
                                 "gen_ai.request.max_tokens": req.get("max_tokens"), "atlas.llm_mode": self.s.llm_mode,
                                 "atlas.tools_offered": len(req.get("tools") or []), "gen_ai.prompt": last}) as sp:
            t0 = time.perf_counter()
            res = self.inner.complete(req)
            usage = res.get("usage", {})
            sp.update({"gen_ai.usage.input_tokens": usage.get("input_tokens", 0),
                       "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
                       "atlas.cost_usd": cost_usd(usage, self.s.price_in_per_m, self.s.price_out_per_m),
                       # replay answers come from the recording, like a cache: nothing reached a provider
                       "atlas.cache_hit": self.s.llm_mode == "replay",
                       "atlas.latency_ms": round((time.perf_counter() - t0) * 1000, 3),
                       "atlas.tool_calls": len(res.get("tool_calls") or []),
                       "gen_ai.completion": res.get("text")})
            return res


# ---------------------------------------------------------------- metrics

def percentile(values, p: float):
    """Nearest-rank percentile: sort the values, return the one at 1-based rank ceil(p / 100 * n).
    p is 0 < p <= 100. Empty values -> None.  percentile([5, 1, 3, 2, 4], 50) -> 3; (..., 95) -> 5."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1]


class Metrics:
    """In-process counters for one replica. For many replicas, scrape each one (Prometheus) or
    compute the same numbers from traces in your tracing backend."""

    def __init__(self, window: int = 1000):
        self._lock = threading.Lock()
        self.requests = 0
        self.errors = 0
        self.latencies_ms = deque(maxlen=window)   # recent requests only, so percentiles follow the present
        self.by_route = Counter()
        self.asks = 0
        self.refusals = 0
        self.resolved = 0
        self.cost_usd = 0.0

    def observe(self, route: str, status: int, latency_ms: float, outcome: dict | None = None):
        o = outcome or {}
        with self._lock:
            self.requests += 1
            self.by_route[route] += 1
            self.latencies_ms.append(latency_ms)
            if status >= 500:
                self.errors += 1
            self.cost_usd += float(o.get("cost_usd") or 0.0)
            if o.get("kind") == "ask":
                self.asks += 1
                if not o.get("answered"):
                    self.refusals += 1
            if o.get("answered"):
                self.resolved += 1

    def snapshot(self) -> dict:
        """{"requests", "errors", "latency_ms": {"p50", "p95"}, "asks", "refusals", "refusal_rate",
            "resolved", "cost_usd_total", "cost_per_resolved_usd", "by_route"}

        refusal_rate = refusals / asks (None when there were no asks).
        cost_per_resolved_usd = all model spend / resolved requests (None when nothing was resolved):
        refusals and failures cost money too, and that cost lands on the requests that succeeded.
        Round the latencies to 3 decimals, the rate to 4 and the costs to 8.
        """
        with self._lock:
            lat = list(self.latencies_ms)
            p50, p95 = percentile(lat, 50), percentile(lat, 95)
            return {"requests": self.requests, "errors": self.errors,
                    "latency_ms": {"p50": None if p50 is None else round(p50, 3),
                                   "p95": None if p95 is None else round(p95, 3)},
                    "asks": self.asks, "refusals": self.refusals,
                    "refusal_rate": round(self.refusals / self.asks, 4) if self.asks else None,
                    "resolved": self.resolved, "cost_usd_total": round(self.cost_usd, 8),
                    "cost_per_resolved_usd": round(self.cost_usd / self.resolved, 8) if self.resolved else None,
                    "by_route": dict(sorted(self.by_route.items()))}

    def prometheus(self) -> str:
        """The snapshot in the Prometheus text format, for a scraper."""
        s = self.snapshot()
        lines = []

        def metric(name, kind, help_, samples):
            lines.extend(["# HELP %s %s" % (name, help_), "# TYPE %s %s" % (name, kind)])
            lines.extend("%s%s %s" % (name, labels, value) for labels, value in samples)

        metric("atlas_requests_total", "counter", "Requests served, by route.",
               [('{route="%s"}' % r, n) for r, n in s["by_route"].items()] or [("", 0)])
        metric("atlas_errors_total", "counter", "Responses with status 500 or above.", [("", s["errors"])])
        metric("atlas_request_latency_ms", "gauge", "Latency percentiles over recent requests.",
               [('{quantile="%s"}' % q, "NaN" if v is None else v)
                for q, v in (("0.5", s["latency_ms"]["p50"]), ("0.95", s["latency_ms"]["p95"]))])
        metric("atlas_asks_total", "counter", "Questions asked.", [("", s["asks"])])
        metric("atlas_refusals_total", "counter", "Questions Atlas refused.", [("", s["refusals"])])
        metric("atlas_resolved_total", "counter", "Requests that ended with an answer.", [("", s["resolved"])])
        metric("atlas_cost_usd_total", "counter", "Model spend in USD.", [("", s["cost_usd_total"])])
        return "\n".join(lines) + "\n"
