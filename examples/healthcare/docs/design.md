# Design: staff assistant for a community clinic

All names, policies and patient details here are fictional ("Larchmere Community Clinic"). This is a teaching build, not clinical or legal guidance.

## Problem

Front desk, billing and clinical staff ask the same policy questions every day: slot lengths, no-shows, identity checks, claims deadlines, when lab results are released. The answers are in policy documents. Some policies are for one team only. The assistant must never handle patient health information (PHI): it has no patient records, and it must not repeat patient details that a user types into a question.

## Users

| user | role | groups |
|---|---|---|
| maria | Front desk coordinator | staff |
| devon | Billing specialist | staff, billing |
| okoye | Physician | staff, clinical |
| lin | Practice nurse | staff, clinical |
| temp | Agency temp, not yet credentialed | none |

| document | acl |
|---|---|
| scheduling, no-shows, privacy, records-requests | staff |
| billing-claims | billing |
| lab-results | clinical |
## Architecture

Same Atlas code as the main course build. Only the data directory changes (`ATLAS_DATA_DIR=examples/healthcare`).

```text
user (X-Atlas-User; a signed token in production)
  -> POST /v1/ask
     -> hybrid retrieval: BM25 + cosine (or Postgres tsvector + pgvector), fused with RRF
        -> permission filter: only chunks whose acl shares a group with the user
     -> rerank (term overlap + proximity, on by default)
     -> model with the dataset's system_prompt.txt, numbered sources only
     -> grounded answer with citations, or the dataset's refusal.txt
  -> POST /v1/tickets/proposals, then /approve with an Idempotency-Key (one ticket, audit trail)
every request: one trace (request -> retrieval -> rerank -> model -> tool), /metrics, X-Trace-Id
```

Files in this folder:

- `kb/*.md`: the documents. Front matter `acl:` names the groups that may read each one.
- `users.json`: fictional staff and their groups.
- `system_prompt.txt` and `refusal.txt`: the dataset's own instructions and refusal message.
- `golden.json`: the eval cases. `demo.md`: a recorded session from `tools/make_demo.py`.
## Trade-offs

- PHI is kept out by design, not by filtering answers. The knowledge base has policies only. The system prompt tells the model it has no patient records and must not repeat patient details. The golden set checks that answers never contain the name, date of birth or record number from the question (`must_not_contain`).
- Traces do not store question text by default (`ATLAS_TRACE_CONTENT=0`). Staff will paste patient details into questions, so the question must not land in a tracing backend.
- Role-based access is by team. A nurse cannot read billing rules, and the front desk cannot read the lab results policy. That is stricter than many clinics need. It is easy to relax by changing `acl:` lines; it is hard to tighten after a leak.
- Medical questions ("What dose of ibuprofen?") are refused. The documents do not cover them, and a staff assistant must not give clinical advice.
## Eval results

Real run: `python tools/eval_gate.py --data-dir examples/healthcare --compare`, solved build, offline stub model, hash embeddings. Thresholds from `evals.thresholds.json`.

| metric | rerank off | rerank on (overlap) | threshold |
|---|---|---|---|
| cases | 18 | 18 | - |
| recall_at_k | 1.0 | 1.0 | >= 0.9 |
| mrr | 1.0 | 1.0 | >= 0.8 |
| citation_precision | 0.9048 | 0.9048 | >= 0.9 |
| refusal_accuracy | 1.0 | 1.0 | >= 0.9 |
| answer_rate | 1.0 | 1.0 | - |
| permission_leaks | 0 | 0 | == 0 |
| overall | 0.9683 | 0.9683 | >= 0.9 |

18 cases: 11 answerable, 7 refusals: lab results policy for the front desk, billing rules for a nurse, two PHI questions (a named patient's lab value, a record number's diagnosis), a prompt injection asking for patient records, a medication dose question and an uncredentialed temp. Citation precision is 0.9048: in two answers the model also cites a neighbouring staff policy (the identity check answer cites records-requests too, and the records timeline answer cites privacy). Both extra citations are readable by the user and on topic, so this is not a leak; the gate still passes. Rerank does not change it, because both documents talk about identity checks.

## Risks

- A user can still type PHI into a question. The assistant does not store it in traces, but the HTTP access log and any model provider will see it. Use a provider with a business associate agreement (or a self-hosted model) before real staff use it.
- Policy documents change. A stale answer about consent or records timelines is a compliance risk. Review dates belong in the front matter and the eval set.
- The stub model shows the pipeline refuses PHI questions because nothing in the knowledge base matches. A live model might still echo a name from the question. Run `--live --repeat 3` and keep the `must_not_contain` cases.