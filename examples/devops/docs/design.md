# Design: on-call assistant for a software company

All names, services and commands here are fictional ("Quillstone"). `deployctl` and `dbctl` are made-up internal tools.

## Problem

An on-call engineer paged at night needs the right runbook step fast, quoted exactly. Runbooks, postmortems, the service catalog and the severity policy are spread across wikis. Runbooks with production commands are for the on-call rotation only. The security incident procedure is for the security team only. When the docs do not cover a problem, the engineer should open an incident ticket in one step.

## Users

| user | role | groups |
|---|---|---|
| ana | Site reliability engineer, on call | engineering, oncall |
| ben | Backend engineer | engineering |
| chloe | Security engineer | engineering, security |
| pat | Product manager | product (no documents) |

| document | acl |
|---|---|
| incident-severity, service-catalog, postmortem-search-outage | engineering |
| runbook-checkout-5xx, runbook-replica-lag | oncall |
| security-credential-leak | security |
## Architecture

Same Atlas code as the main course build. Only the data directory changes (`ATLAS_DATA_DIR=examples/devops`).

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
Tickets: the ticket proposal and approval endpoints stand in for the incident tool. Atlas proposes an incident ticket; the engineer approves it with an Idempotency-Key, so a retry during a flaky night never files two incidents. `FakeTicketSystem` in `atlas/tickets.py` is where a real PagerDuty, Opsgenie or Jira adapter would go.

## Trade-offs

- Commands are quoted from the source, not generated. The system prompt says so, and the golden set checks that people outside the rotation never see `deployctl rollback` or `dbctl pool remove` (`must_not_contain`).
- Restricting runbooks to the on-call group is a real choice. It keeps destructive commands away from people without production access, but a backend engineer helping in an incident gets a refusal. In practice, add incident responders to `oncall` for the length of the incident.
- Speed over breadth: top 4 chunks and short answers. An engineer at 3 a.m. wants the step, then the link.
## Eval results

Real run: `python tools/eval_gate.py --data-dir examples/devops --compare`, solved build, offline stub model, hash embeddings. Thresholds from `evals.thresholds.json`.

| metric | rerank off | rerank on (overlap) | threshold |
|---|---|---|---|
| cases | 14 | 14 | - |
| recall_at_k | 1.0 | 1.0 | >= 0.9 |
| mrr | 1.0 | 1.0 | >= 0.8 |
| citation_precision | 1.0 | 1.0 | >= 0.9 |
| refusal_accuracy | 1.0 | 1.0 | >= 0.9 |
| answer_rate | 1.0 | 1.0 | - |
| permission_leaks | 0 | 0 | == 0 |
| overall | 1.0 | 1.0 | >= 0.9 |

14 cases: 9 answerable, 5 refusals: a runbook command for a non-on-call engineer, the security procedure for an SRE, a prompt injection asking for every command, an out-of-scope Kafka question and a product manager with no engineering access. Two questions were made more specific while building this set: the first versions also matched the service catalog, and the model cited it alongside the runbook (citation precision 0.875, gate fail). That is the gate doing its job: it found ambiguous questions before users did.

## Risks

- A wrong runbook step is worse than no answer. Keep runbooks short, owned and dated, and add a golden case for each command.
- Runbooks drift from the real system. Link the eval gate to the runbook repository, so a runbook change runs the gate.
- The stub model proves retrieval, access and refusals. A live model may paraphrase a command. Run `--live --repeat 3` and check the answers contain the exact command for on-call users.