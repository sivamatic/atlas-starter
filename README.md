# atlas-starter: the real Atlas, in Python

The browser labs teach each idea in small steps. This repo is where you build the same ideas as a real service: FastAPI, real HTTP to a real model, real tests. It grows one milestone per unit.

| Milestone | Unit | Status |
|---|---|---|
| **week0: Hello Atlas.** Health and readiness, provider adapter with record/replay, budget guardrail and kill switch, a bounded agent loop with one read-only tool | Unit 0 | Ready |
| **week3: Answers with sources.** Hybrid search (keyword + vector, fused with RRF) on Postgres + pgvector, permissions inside the search, answers that cite their sources or refuse | Unit 3 | Ready |
| **week4: Evals and a merge gate.** A golden set, recall, citation precision, refusal accuracy and a permission-leak count, a reranker that measurably helps, and a CI gate that blocks a regression | Unit 4 | Ready |
| **week5: Approved tickets.** Atlas drafts a ticket, the requester approves it, exactly one ticket is created, even with double clicks, races and a flaky ticket system. Full audit trail | Units 4–5 | Ready |
| **week6: Tracing and operations.** One trace per request (request, retrieval, rerank, model call, tool call) with tokens, cost, latency and refusal reason, OpenTelemetry export to Langfuse or Phoenix, `/metrics`, no question text in traces by default | Unit 6 | Ready |
| **Deploy.** Dockerfile (non-root, healthcheck), Azure Container Apps with Postgres + pgvector, optional Kubernetes manifests | Unit 6 | Ready |
| **Industry examples.** The same Atlas on three fictional datasets (bank, clinic, on-call), each with a golden set, design doc and recorded demo | Showcase | Ready |

## What is real and what is simulated

| Real | Simulated |
|---|---|
| The FastAPI service, the agent loop, the budget rules, the HTTP calls to an OpenAI-compatible model API | The `check_status` tool reads `data/status.json`, not a real status page |
| `recordings/tape.json` was recorded from a real model (`llama3.2:3b` via Ollama) | Replay proves your code handles that conversation. It does **not** prove answer quality, injection resistance or that another model behaves the same. That's what evals (Unit 4) are for. |
| Postgres full-text search, pgvector HNSW with iterative scans, SQL that makes approval atomic. `tests/test_pg.py` runs all of it on a real database | The knowledge base is 7 short Markdown articles in `data/kb/`. A real one has thousands, from Confluence or SharePoint, synced by a job |
| `EMBED_MODE=live` calls a real embedding model (`bge-m3` via Ollama, or any OpenAI-compatible `/embeddings`) | The default `EMBED_MODE=hash` embedder is deterministic, not semantic. It keeps tests offline and replay stable. Don't judge retrieval quality with it |
| | Identity: the `X-Atlas-User` header and `data/users.json` stand in for a gateway that validates an SSO token and looks up groups. Never trust a client-supplied header in production |
| | `FakeTicketSystem` stands in for ServiceNow, Jira Service Management or Zendesk. It's idempotent by key, and can be told to fail, so you can test retries |
| The eval metrics, the gate and the CI workflow. A drop below a threshold really fails the build | `LLM_MODE=stub` (`atlas/stub_model.py`) is a deterministic stand-in model for evals and demos. It answers from the best-matching sources or says NOT_FOUND. It tests retrieval, permissions, rerank and refusals, not a real model's writing. Use `--live` for that |
| Traces are real OpenTelemetry spans when the SDK is installed, and reach Langfuse or Phoenix over OTLP | The industry examples (`examples/`) are fictional companies, people and policies |

## Set up (once, needs the internet)

You need Python 3.11 or later.

```
git clone https://github.com/sivamatic/atlas-starter.git
cd atlas-starter
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

After that, everything below runs offline.

## Week 0: Hello Atlas

1. Run the tests. Most fail, because the code has `TODO(week0)` gaps:
   ```
   pytest -m week0
   ```
2. Fill in the TODOs, in this order. Each one matches a browser lab you've already done.

   | File | What to write | Browser lab |
   |---|---|---|
   | `atlas/app.py` | `readiness()` | Unit 0 · Health, readiness and your first service |
   | `atlas/budget.py` | `Budget.authorize()`, `Budget.record()` | Unit 0 · Fund it safely |
   | `atlas/adapter.py` | `Adapter.complete()` | Unit 0 · Record, replay and the provider adapter |
   | `atlas/agent.py` | the loop in `run()` | Start here · Tool calls: how a model takes action. (Unit 2 · Build a bounded agent loop goes deeper; the TODO comments walk you through it now.) |

3. When `pytest -m week0` is green, run the service and ask it something:
   ```
   uvicorn atlas.app:app --port 8000
   ```
   Open http://127.0.0.1:8000/docs, try `POST /v1/chat` with `{"message": "My VPN keeps dropping every few minutes. What should I do?"}`. That exact question is recorded, so it works with no model and no key. Any other question returns a loud `replay_miss` error. That's on purpose.

4. Optional: talk to a real model. Any OpenAI-compatible endpoint works. The free local option is [Ollama](https://ollama.com):
   ```
   ollama pull llama3.2:3b
   set LLM_MODE=live                       # PowerShell: $env:LLM_MODE="live"
   set LLM_BASE_URL=http://127.0.0.1:11434/v1
   python -m atlas.cli "Is email down?"
   ```
   Use `LLM_MODE=record` to save new recordings for your tests. For a hosted provider, set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` and the prices from its pricing page (see `.env.example`), and set a budget alert first.

Stuck? Worked solutions are in `solutions/week0/`. Try the hints in the TODO comments first.

## Week 3: Answers with sources

Atlas now answers from the IT knowledge base and cites where each answer came from. Two users asking the same question can get different answers, because each sees only the articles their groups allow.

1. Run `pytest -m week3`. The new TODOs:

   | File | What to write | Browser lab |
   |---|---|---|
   | `atlas/retrieval.py` | `rrf()` | Unit 3 · Hybrid search with reciprocal rank fusion |
   | `atlas/retrieval.py` | `Retriever.search()` | Unit 3 · Permissions inside retrieval |
   | `atlas/ask.py` | `parse_citations()`, `ask()` | Unit 3 · Project: Atlas retrieval |

   Provided and worth reading: `kb.py` (splits articles into sections), `embed.py`, `identity.py`, and the two stores in `retrieval.py`. `MemoryStore` is BM25 + cosine in memory; `PgStore` is the same interface on Postgres.

2. Try it with the recorded question (works offline):
   ```
   uvicorn atlas.app:app --port 8000
   ```
   `POST /v1/ask` with header `X-Atlas-User: priya` and `{"question": "My VPN keeps dropping every few minutes. What should I do?"}`. Then try `X-Atlas-User: sam` (no groups): Atlas refuses without calling the model.

3. Run it on Postgres, the way production would. Needs [Docker](https://docs.docker.com/get-docker/):
   ```
   docker compose up -d
   $env:ATLAS_DB_URL="postgresql://atlas:atlas@127.0.0.1:5433/atlas"
   $env:ATLAS_TEST_PG_URL=$env:ATLAS_DB_URL
   pytest -m pg
   ```
   Add `$env:EMBED_MODE="live"` and `$env:LLM_BASE_URL="http://127.0.0.1:11434/v1"` (after `ollama pull bge-m3`) for real embeddings. One table per embedding model, because vectors from different models can't be compared.

**Look closely at the recording.** It is a real answer from `llama3.2:3b`, and it's not a good one. With the hash embedder, the keyword side matched common words ("every", "minutes", "keeps") in the email and laptop articles, those chunks made the top 4, and the model cited them. Every citation is valid, so the grounding check passes, but two are irrelevant. With `EMBED_MODE=live` (bge-m3) on Postgres, all four chunks were VPN sections. And when we asked the live 3B model the same question repeatedly, it sometimes cited and sometimes didn't, and Atlas refused. Three lessons: citations prove *where* an answer came from, not that it's right; retrieval quality needs its own eval (Unit 3 · Recall); small models are inconsistent, which is why the evals in Unit 4 run many times.

## Week 4: Evals and a merge gate

A demo that works once proves little. Week 4 turns "it looks right" into numbers, and blocks a change that makes them worse.

1. Run `pytest -m week4`. The new TODOs:

   | File | What to write | Browser lab |
   |---|---|---|
   | `atlas/evals.py` | `score_case()`, `summarize()`, `gate()` | Unit 4 · Evals and the golden set |
   | `atlas/rerank.py` | `overlap_score()`, `OverlapReranker.rerank()`, then set `DEFAULT = "overlap"` | Unit 4 · Rerank after fusion |

   Provided: `run_case()` and `evaluate()` in `evals.py`, the 22 cases in `data/golden.json`, `evals.thresholds.json` and `tools/eval_gate.py`.

2. A golden case names a user, a question, and either the documents that must be cited (`expect_docs`) or `expect_refusal: true`. Refusal cases cover out-of-scope questions, documents the user may not read, and a prompt injection. `forbidden_docs` and `must_not_contain` turn a quiet leak into a failed case.

   | Metric | Meaning |
   |---|---|
   | `recall_at_k` | share of expected documents in the top k retrieved chunks |
   | `mrr` | how high the first right chunk ranks |
   | `citation_precision` | cited chunks that come from an expected document |
   | `refusal_accuracy` | refused exactly when it should |
   | `permission_leaks` | cases that retrieved, cited or said something the user may not see. Must be 0 |
   | `overall` | mean of recall, precision and refusal accuracy; 0 if anything leaked |

3. Run the gate:
   ```
   python tools/eval_gate.py --compare
   ```
   It prints a table, writes `reports/eval-report.json` (gitignored) and exits 0 on pass, 1 below a threshold, 2 on a setup error. Offline it uses `LLM_MODE=stub` and hash embeddings, so the numbers are the same on every machine. `--live --repeat 3` runs the same cases against your real model (`LLM_MODE`, `LLM_BASE_URL`) several times, because small models are inconsistent.

   Measured on the solved build (22 cases):

   | metric | rerank off | rerank overlap |
   |---|---|---|
   | recall_at_k | 1.0 | 1.0 |
   | mrr | 0.9688 | 1.0 |
   | citation_precision | 0.9286 | 0.9655 |
   | refusal_accuracy | 1.0 | 1.0 |
   | permission_leaks | 0 | 0 |
   | overall | 0.9762 | 0.9885 |

   The reranker reads a bigger pool from fusion (`ATLAS_RERANK_POOL`, 20) and scores each chunk by how many question terms it covers, whether they appear in the heading, and how close together they are, blended with the fused score. It runs after the permission filter, so it can never surface a chunk the user may not read. `ATLAS_RERANK=off` turns it off. `ATLAS_RERANK=cross-encoder` uses a small cross-encoder model instead (optional: `pip install sentence-transformers`; never needed for the tests).

4. The gate in CI. `.github/workflows/ci.yml` runs on every push and pull request: it runs `tools/check_starter.py`, builds the solved copy and runs the eval gate on the default dataset and on every industry example. A drop below `evals.thresholds.json`, or a single leak, fails the check.

   **In your own copy:** once your `atlas/` passes, replace the check_starter and solved-copy steps with one step: `python tools/eval_gate.py`. Then, in GitHub, open Settings, Branches (or Rules, Rulesets), add a rule for `main`, and mark the `gate` check as required. From then on a pull request that lowers recall or leaks a document cannot merge. See GitHub's docs on [required status checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging). If you keep the `paths` filter and make the check required, a pull request that does not touch those paths never runs it and waits on a pending check; that is why the fork version drops the filter ([skipped workflows and required checks](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)).
## Week 5: Approved tickets, exactly once

Atlas may draft a ticket on its own. Creating one is a side effect in another company's system, so a person approves it, and a double click, a retry or a timeout must never produce two tickets.

1. Run `pytest -m week5`. The TODO is `TicketService.approve()` in `atlas/tickets.py`. Its docstring lists the six rules, in order. Browser labs: Unit 4 · Guardrails and approvals, Unit 5 · Idempotent side effects.

2. The API:

   | Call | Result |
   |---|---|
   | `POST /v1/tickets/proposals` `{"summary", "details"}` | 201, a draft. Nothing is created yet |
   | `POST /v1/tickets/proposals/{id}/approve` with `Idempotency-Key` | 201 and a ticket id. Same key again: 200, same ticket |
   | same, with a different key | 409: this draft was already approved |
   | same, as another user | 403 |
   | same, while the ticket system is down | 503 after 3 tries. The approval is kept; retry with the same key |
   | `GET /v1/tickets/proposals/{id}` | the draft and its audit trail |

   Drafts expire after `ATLAS_TICKET_TTL_S` (900 s). With `ATLAS_DB_URL` set, drafts and the audit trail live in Postgres; otherwise in SQLite in memory.

The idea that makes this work: claim the draft with one atomic `UPDATE ... WHERE status = 'pending'`, and pass the same idempotency key to the ticket system on every retry, so the ticket system can deduplicate too.

## Week 6: Tracing and operations

When a user says "Atlas gave me a wrong answer at 10:14", you need to find that request and see what it retrieved, what the model was sent, how long each step took and what it cost.

1. Run `pytest -m week6`. The new TODOs are in `atlas/telemetry.py`: `scrub()` (keep question and answer text out of spans), `percentile()` and `Metrics.snapshot()`. The tracer, the middleware and the spans are provided; read them.

2. Every request gets one trace:

   ```text
   atlas.request   http.route, http.response.status_code, atlas.tenant, atlas.user.role, atlas.latency_ms,
                   atlas.stop_reason, atlas.refusal_reason, atlas.cost_usd, atlas.citations
     retrieval     atlas.retrieval.k, .pool, .store, .hits
     rerank        atlas.rerank.name, .candidates, .kept, .doc_ids
     model.call    gen_ai.request.model, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
                   atlas.cost_usd, atlas.cache_hit, atlas.latency_ms
     tool.call     gen_ai.tool.name, atlas.tool.ok, atlas.tool.error   (agent loop, POST /v1/chat)
   ```
   `atlas.refusal_reason` is `no_sources` or `ungrounded` when Atlas refuses. Every response has an `X-Trace-Id` header with the trace id; error bodies include it too. Health, readiness and `/metrics` are not traced, so probes do not drown real traffic.

3. Privacy: question, prompt and answer text are not stored in spans. `ATLAS_TRACE_CONTENT=1` opts in, and even then emails and long numbers are masked. Users paste personal data into questions; your tracing backend should not collect it by default.

4. `GET /metrics` returns JSON: request count, error count, p50 and p95 latency, refusal rate, cost per resolved request, and requests by route. `GET /metrics?format=prometheus` returns the same in Prometheus text format.

5. Where spans go:

   | Setting | Result |
   |---|---|
   | nothing set | in-memory tracer (tests, `/metrics`) |
   | `ATLAS_TRACE_FILE=var/traces.jsonl` | also append each span as one JSON line |
   | `pip install -r requirements-otel.txt` and `OTEL_EXPORTER_OTLP_ENDPOINT` set | OpenTelemetry SDK with the OTLP HTTP exporter (`ATLAS_TRACER=auto`, the default) |

   The OpenTelemetry packages are optional. Without them, Atlas uses the built-in tracer. The OTLP exporter reads the standard variables. With `OTEL_EXPORTER_OTLP_ENDPOINT` it appends `/v1/traces`; `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is used exactly as given ([OTLP exporter spec](https://opentelemetry.io/docs/specs/otel/protocol/exporter/), [Python exporter](https://opentelemetry-python.readthedocs.io/en/latest/exporter/otlp/otlp.html)). Set `OTEL_SERVICE_NAME=atlas` to name the service ([SDK configuration](https://opentelemetry.io/docs/languages/sdk-configuration/general/)). Attribute names follow the [GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) where one exists.

   **Arize Phoenix, local** ([Docker deployment](https://arize.com/docs/phoenix/self-hosting/deployment-options/docker), [configuration](https://arize.com/docs/phoenix/self-hosting/configuration)). Port 6006 serves the UI and OTLP over HTTP at `/v1/traces`; 4317 is OTLP gRPC.
   ```
   docker compose --profile phoenix up -d phoenix
   pip install -r requirements-otel.txt
   $env:OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:6006"
   $env:OTEL_SERVICE_NAME="atlas"
   uvicorn atlas.app:app --port 8000
   ```
   Send a few `/v1/ask` requests, then open http://localhost:6006. Stop it with `docker compose --profile phoenix down`.

   **Langfuse** ([OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry)). Langfuse takes OTLP over HTTP only (no gRPC) at `/api/public/otel`, with Basic auth made from your public and secret key:
   ```
   $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("pk-lf-...:sk-lf-..."))
   $env:OTEL_EXPORTER_OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel"      # US: https://us.cloud.langfuse.com/api/public/otel; self-hosted: http://localhost:3000/api/public/otel
   $env:OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20$auth,x-langfuse-ingestion-version=4"
   ```
   The space after `Basic` must be written `%20`: the Python exporter URL-decodes header values. Keep the keys out of the repository.

## Deploy

One real cloud path, and Kubernetes if you want it. The image runs the solved build: `python tools/apply_solutions.py var/deploy-build`, then build from there.

1. **Container.** `Dockerfile`: `python:3.12-slim`, dependencies installed in a separate layer, a non-root user (uid 10001), and a `HEALTHCHECK` on `/v1/health` ([Dockerfile reference](https://docs.docker.com/reference/dockerfile/)). `.dockerignore` keeps `.git`, virtual environments, `.env`, `var/`, `reports/` and `deploy/` out of the image.
   ```
   python tools/apply_solutions.py var/deploy-build
   docker build -t atlas:dev var/deploy-build
   docker run --rm -p 8000:8000 -e LLM_MODE=stub atlas:dev
   ```
   Then `curl http://127.0.0.1:8000/v1/ready`.

2. **Azure Container Apps** (`deploy/azure-container-apps/`). `deploy.ps1` creates a resource group, an Azure Database for PostgreSQL flexible server with `vector` on the extension allow-list, and the container app with `az containerapp up`, with the database URL stored as a secret. Run it with `-Plan` first to print every command without running it. The folder README explains the cost (a Burstable B1ms server runs all month, even when idle) and the teardown: `az group delete --name <group> --yes --no-wait`. Every command and flag is linked to Microsoft Learn there.

3. **Kubernetes, optional** (`deploy/k8s/`). A deployment with liveness on `/v1/health` and readiness on `/v1/ready`, resource requests and limits, a non-root, read-only, no-capabilities security context; a service, an HPA (2 to 6 replicas at 70% CPU), a configmap and an example secret. The folder README walks through kind or minikube.

`pytest -m deploy` checks the Dockerfile and manifests: they parse, the probes point at real routes, the pod runs as non-root, and the example secret holds no values. The manifest tests need `pip install pyyaml` and are skipped without it.

## Industry examples

The same Atlas code on three fictional companies. Only the data changes: `ATLAS_DATA_DIR=examples/<name>`. Each one is also published on its own, finished and ready to run: [atlas-bank](https://github.com/sivamatic/atlas-bank), [atlas-healthcare](https://github.com/sivamatic/atlas-healthcare), [atlas-devops](https://github.com/sivamatic/atlas-devops).

| Example | Who uses it | What it shows |
|---|---|---|
| `examples/bank` | advisors at a fictional retail and private bank | research entitled by region, fee schedules by role, no personal advice |
| `examples/healthcare` | staff at a fictional community clinic | role-based policies, refusal of any patient health information |
| `examples/devops` | engineers on call at a fictional software company | runbooks for the rotation only, exact commands, incident tickets through the approval flow |

Each folder has `kb/*.md`, `users.json`, `golden.json`, `system_prompt.txt`, `refusal.txt`, `docs/design.md` (problem, users, architecture, trade-offs, eval results, risks) and `demo.md`, a transcript recorded by `python tools/make_demo.py --all` from real CLI and HTTP runs.

```
$env:ATLAS_DATA_DIR="examples/devops"; $env:LLM_MODE="stub"
python -m atlas.cli ask --user ana "How do I roll back the last checkout-api deploy?"
python tools/eval_gate.py --data-dir examples/devops --compare
```

Run them on the solved build (`python tools/apply_solutions.py var/solved`, then `cd var/solved`). `test_each_industry_example_passes_its_gate` runs every example's gate in CI. To make your own, copy a folder, write the documents with an `acl:` line each, write the users and at least ten golden cases, and run the gate.
## How this maps to the project rubric

| Rubric line | Proven by |
|---|---|
| Health and readiness behave as specified | `test_readiness_*`, `test_health_*`, `test_ready_endpoint` |
| Chat goes through the adapter; no vendor SDK outside it | `test_no_vendor_sdk_outside_adapter` |
| Replay passes with zero keys and zero network | every test runs with the network blocked |
| Budget guardrail: cap, ceiling, kill switch | `test_budget_*`, `test_kill_switch_stops_everything` |
| The agent loop is bounded and survives bad tool calls and outages | `test_step_limit_*`, `test_malformed_*`, `test_provider_outage_*` |
| Hybrid search beats either half alone on exact codes | `test_rrf_*`, `test_keyword_side_finds_exact_identifiers` |
| Users never see sources they can't read, and still get k results | `test_priya_never_sees_*`, `test_filtering_inside_the_search_still_fills_k`, `test_search_drops_a_chunk_*` |
| Answers cite real sources or refuse | `test_ungrounded_answers_are_refused_not_shown`, `test_no_sources_means_no_model_call` |
| Exactly one ticket per approval, with an audit trail | `test_double_click_*`, `test_racing_approvals_*`, `test_outage_keeps_the_approval_*`, `test_api_end_to_end` |
| The golden set passes the gate with zero leaks, and rerank measurably helps | `test_golden_set_passes_the_gate_with_no_leaks`, `test_rerank_measurably_improves_the_golden_set`, `test_score_case_counts_every_kind_of_leak` |
| A regression fails the build | `test_eval_gate_cli_writes_a_report_and_fails_below_threshold`, `test_gate_reports_every_broken_or_missing_metric`, `.github/workflows/ci.yml` |
| One trace per request, with the right parent and child spans and attributes | `test_ask_produces_a_span_tree_and_returns_its_trace_id`, `test_spans_carry_the_attributes_on_call_needs`, `test_otel_tracer_builds_the_same_tree` |
| No question text in traces unless you opt in | `test_question_and_answer_text_never_reach_a_span_by_default`, `test_content_capture_is_an_explicit_opt_in` |
| Metrics are correct | `test_percentile_is_nearest_rank`, `test_metrics_snapshot_math`, `test_metrics_endpoint_counts_real_requests` |
| The container and manifests are production-shaped | `pytest -m deploy` |
| The same code serves other industries | `test_each_industry_example_passes_its_gate` |
| The same behaviour on real Postgres | `pytest -m pg` |

## For maintainers

`python tools/check_starter.py` checks that the starter fails each milestone's tests and that starter plus solutions passes them. Solutions are applied cumulatively, in the order week0, week3, week4, week5, week6. `python tools/apply_solutions.py <dir>` builds a solved copy anywhere (`--upto week4` stops early). A new TODO must not sit on an earlier week's code path, or that week's tests would fail too: that is why `rerank.DEFAULT` is `"off"` in the starter.

Recordings are keyed by a hash of the request. Reranking changes the prompt, so the week 3 recorded test pins `ATLAS_RERANK=off`, and week 4 has its own recorded entry. To add one, run `LLM_MODE=record LLM_BASE_URL=http://127.0.0.1:11434/v1 python -m atlas.cli ask --user priya "<question>"` in a solved copy and copy `recordings/tape.json` back.

After changing an example, run `python tools/make_demo.py --all` to re-record the demos, and update the eval table in its `docs/design.md` from the new transcript.

---

Part of [AI for Engineers](https://ai4e.phenome.app). The course explains every piece of this code, week by week.
