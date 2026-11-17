"""The Atlas service.

  GET  /v1/health                         liveness: the process can answer. Checks nothing external.
  GET  /v1/ready                          readiness: adapter configured, budget available, knowledge base loaded
  POST /v1/chat                           {"message"} -> the agent's answer (week 0)
  POST /v1/ask                            {"question"} -> an answer with citations, from articles you may read (week 3)
  POST /v1/tickets/proposals              {"summary", "details"} -> a ticket draft; nothing is created yet (week 5)
  POST /v1/tickets/proposals/{id}/approve header Idempotency-Key -> exactly one ticket (week 5)
  GET  /v1/tickets/proposals/{id}         the draft and its audit trail
  GET  /metrics                           requests, p50/p95 latency, refusal rate, cost per resolved request,
                                          errors (week 6). ?format=prometheus for a scraper

Every response carries an X-Trace-Id header: the id of its trace (telemetry.py), also in the logs.

/v1/ask and /v1/tickets need the X-Atlas-User header (a stand-in for a real gateway; see identity.py).

Run:  uvicorn atlas.app:app --port 8000
"""
import json
import logging
import time
import uuid

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.routing import Match

from . import adapter as adapter_mod
from . import agent, config, identity, rerank, retrieval, telemetry
from . import ask as ask_mod
from . import embed as embed_mod
from . import tickets as tickets_mod
from .budget import Budget, BudgetExceeded

log = logging.getLogger("atlas")


def readiness(checks: dict) -> tuple[int, dict]:
    """checks is name -> bool. Return (status, body):
    status 200 if every check is true, otherwise 503;
    body {"ready": bool, "failing": [names of failing checks, sorted A-Z]}.
    """
    failing = sorted(k for k, ok in checks.items() if not ok)
    return (503 if failing else 200), {"ready": not failing, "failing": failing}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ProposalIn(BaseModel):
    summary: str = Field(min_length=1, max_length=200)
    details: str = Field(min_length=1, max_length=4000)


# Probes and scrapes run every few seconds. Tracing or counting them would drown the real traffic.
OPS_PATHS = {"/v1/health", "/v1/ready", "/metrics"}


def create_app(settings=None, llm=None, budget=None, retriever=None, tickets=None, users=None,
               tracer=None, metrics=None) -> FastAPI:
    s = settings or config.load()
    llm = telemetry.TracedAdapter(llm or adapter_mod.from_settings(s), s)
    budget = budget or Budget(s.max_cost_usd, s.max_tokens_per_call, s.kill_switch)
    users = users if users is not None else identity.load_directory(s.data_dir / "users.json")
    tracer = tracer or telemetry.from_settings(s)
    metrics = metrics or telemetry.Metrics()
    lazy = {"retriever": retriever, "tickets": tickets}
    app = FastAPI(title="Atlas", version="0.6.0")
    app.state.tracer, app.state.metrics = tracer, metrics

    def get_retriever():
        if lazy["retriever"] is None:
            e = embed_mod.from_settings(s)
            lazy["retriever"] = rerank.wrap(retrieval.Retriever(retrieval.build(s, e), e), s)
        return lazy["retriever"]

    def get_tickets():
        if lazy["tickets"] is None:
            lazy["tickets"] = tickets_mod.TicketService(tickets_mod.DB(s.db_url), tickets_mod.FakeTicketSystem(), s.ticket_ttl_s)
        return lazy["tickets"]

    def kb_loaded() -> bool:
        try:
            return get_retriever().store.count() > 0
        except Exception as e:  # a down database or embedding endpoint means not ready, not a crash
            log.warning(json.dumps({"check": "knowledge_base", "error": str(e)}))
            return False

    def new_trace_id():
        return telemetry.current_trace_id() or uuid.uuid4().hex[:12]

    def route_of(request):
        for r in app.routes:
            if r.matches(request.scope)[0] == Match.FULL:
                return r.path
        return "unmatched"

    @app.middleware("http")
    async def trace_and_measure(request: Request, call_next):
        # One trace per request: the root span is opened here, and everything the request does
        # (retrieval, rerank, model calls, tools) becomes its children.
        if request.url.path in OPS_PATHS:
            return await call_next(request)
        token, outcome_token = telemetry.use(tracer), telemetry.begin_outcome()
        t0, status = time.perf_counter(), 500
        try:
            with tracer.span("atlas.request", {"http.request.method": request.method, "http.route": route_of(request),
                                               "atlas.tenant": s.tenant}) as sp:
                response = await call_next(request)
                status = response.status_code
                ms = round((time.perf_counter() - t0) * 1000, 3)
                sp.update({"http.response.status_code": status, "atlas.latency_ms": ms})
                response.headers["X-Trace-Id"] = sp.trace_id
                return response
        finally:
            metrics.observe(route_of(request), status, (time.perf_counter() - t0) * 1000, telemetry.end_outcome(outcome_token))
            telemetry.reset(token)

    def fail(trace_id, status, code, detail):
        log.warning(json.dumps({"trace_id": trace_id, "error": code, "detail": detail}))
        telemetry.annotate({"atlas.error": code})
        return JSONResponse({"error": code, "detail": detail, "trace_id": trace_id}, status)

    def guarded(trace_id, fn):
        """Run fn and map the failures every model-backed route shares to clear HTTP errors."""
        try:
            return fn(), None
        except BudgetExceeded as e:
            return None, fail(trace_id, 503 if str(e) == "KILL_SWITCH" else 429, "budget", str(e))
        except adapter_mod.ReplayMiss as e:
            return None, fail(trace_id, 500, "replay_miss", "no recording for request " + str(e) + "; re-record with LLM_MODE=record")
        except adapter_mod.ProviderError as e:
            return None, fail(trace_id, 502, "provider_unavailable", str(e))

    def who(trace_id, header):
        user = identity.lookup(users, header)
        if user:  # role and groups, never the name: traces are not a staff directory
            telemetry.annotate({"atlas.user.role": user.role, "atlas.user.groups": ",".join(user.groups)})
        return user, (None if user else fail(trace_id, 401, "unauthenticated", "unknown or missing X-Atlas-User"))

    @app.get("/v1/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/ready")
    def ready():
        status, body = readiness({"llm_adapter": llm.configured(), "budget": budget.available(),
                                  "knowledge_base": kb_loaded()})
        return JSONResponse(body, status)

    @app.post("/v1/chat")
    def chat(body: ChatIn):
        trace_id, t0 = new_trace_id(), time.perf_counter()
        out, err = guarded(trace_id, lambda: agent.run(body.message, llm, budget, s))
        if err:
            return err
        out["trace_id"] = trace_id
        out["mode"] = s.llm_mode
        telemetry.outcome(kind="chat", answered=out["stop_reason"] == "answered", cost_usd=out["cost_usd"])
        telemetry.annotate({"atlas.stop_reason": out["stop_reason"], "atlas.steps": out["steps"], "atlas.cost_usd": out["cost_usd"]})
        log.info(json.dumps({"trace_id": trace_id, "steps": out["steps"], "stop": out["stop_reason"],
                             "cost_usd": out["cost_usd"], "ms": round((time.perf_counter() - t0) * 1000)}))
        return out

    @app.post("/v1/ask")
    def ask(body: AskIn, x_atlas_user: str | None = Header(None)):
        trace_id, t0 = new_trace_id(), time.perf_counter()
        user, err = who(trace_id, x_atlas_user)
        if err:
            return err
        out, err = guarded(trace_id, lambda: ask_mod.ask(body.question, user, get_retriever(), llm, budget, s))
        if err:
            return err
        out["trace_id"] = trace_id
        out["mode"] = s.llm_mode
        answered = out["stop_reason"] == "answered"
        telemetry.outcome(kind="ask", answered=answered, cost_usd=out["cost_usd"])
        telemetry.annotate({"atlas.stop_reason": out["stop_reason"], "atlas.refusal_reason": None if answered else out["stop_reason"],
                            "atlas.cost_usd": out["cost_usd"], "atlas.citations": len(out["citations"]),
                            "atlas.question.chars": len(body.question),
                            # Dropped by telemetry.scrub() unless ATLAS_TRACE_CONTENT=1
                            "atlas.question": body.question, "atlas.answer": out["answer"]})
        log.info(json.dumps({"trace_id": trace_id, "user": user.id, "stop": out["stop_reason"],
                             "sources": [c["id"] for c in out["citations"]], "cost_usd": out["cost_usd"],
                             "ms": round((time.perf_counter() - t0) * 1000)}))
        return out

    @app.post("/v1/tickets/proposals", status_code=201)
    def propose(body: ProposalIn, x_atlas_user: str | None = Header(None)):
        # In the full Atlas the agent calls this through a propose_ticket tool. Proposing is safe:
        # it writes a draft, never a ticket.
        trace_id = new_trace_id()
        user, err = who(trace_id, x_atlas_user)
        if err:
            return err
        return get_tickets().propose(user, body.summary, body.details)

    @app.get("/v1/tickets/proposals/{pid}")
    def proposal(pid: str, x_atlas_user: str | None = Header(None)):
        trace_id = new_trace_id()
        user, err = who(trace_id, x_atlas_user)
        if err:
            return err
        p = get_tickets().get(pid)
        if p is None or p["requester"] != user.id:
            return fail(trace_id, 404, "not_found", pid)
        return {**p, "audit": get_tickets().audit(pid)}

    @app.post("/v1/tickets/proposals/{pid}/approve")
    def approve(pid: str, x_atlas_user: str | None = Header(None), idempotency_key: str | None = Header(None)):
        trace_id = new_trace_id()
        user, err = who(trace_id, x_atlas_user)
        if err:
            return err
        if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
            return fail(trace_id, 400, "idempotency_key_required", "send an Idempotency-Key header (8-128 chars), "
                                                                   "the same one on every retry of this approval")
        try:
            out = get_tickets().approve(pid, user, idempotency_key)
        except tickets_mod.NotFound:
            return fail(trace_id, 404, "not_found", pid)
        except tickets_mod.Forbidden as e:
            return fail(trace_id, 403, "forbidden", str(e))
        except tickets_mod.Conflict as e:
            return fail(trace_id, 409, "conflict", str(e))
        except tickets_mod.TicketSystemDown as e:
            return fail(trace_id, 503, "ticket_system_unavailable", str(e) + "; retry with the same Idempotency-Key")
        log.info(json.dumps({"trace_id": trace_id, "user": user.id, "proposal": pid, "ticket": out["ticket_id"],
                             "replayed": out["replayed"]}))
        return JSONResponse({**out, "trace_id": trace_id}, 200 if out["replayed"] else 201)

    @app.get("/metrics")
    def metrics_endpoint(format: str = "json"):
        if format == "prometheus":
            return PlainTextResponse(metrics.prometheus(), media_type="text/plain; version=0.0.4")
        return metrics.snapshot()

    return app


def __getattr__(name):
    # Build the app lazily so importing this module (e.g. in tests) needs no configuration.
    if name == "app":
        return create_app()
    raise AttributeError(name)
