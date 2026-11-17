# Design: advisor assistant for a retail and private bank

All names, products, rates and research views here are fictional ("Corvane Bank"). This is a teaching build, not advice.

## Problem

Advisors answer client questions about products, suitability rules, complaints and research. The answers live in policy documents and research notes that differ by region and role. Advisors lose time searching, and a wrong answer can be a regulatory breach. The research notes are entitled: an EU advisor must not see the APAC desk's notes, and retail advisors must not see the private banking fee schedule or the insider lists.

## Users

| user | role | groups |
|---|---|---|
| elena | Retail advisor, EU | advisors-all, advisors-eu |
| kenji | Retail advisor, APAC | advisors-all, advisors-apac |
| ruth | Private banker, EU | advisors-all, advisors-eu, private-banking |
| omar | Compliance officer | advisors-all, compliance |
| intern | Summer intern, not yet entitled | none |

| document | acl |
|---|---|
| fixed-saver, suitability, complaints | advisors-all |
| research-eu-outlook | advisors-eu |
| research-apac-rates | advisors-apac |
| private-banking-fees | private-banking |
| insider-lists | compliance |
## Architecture

Same Atlas code as the main course build. Only the data directory changes (`ATLAS_DATA_DIR=examples/bank`).

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

- Entitlements are enforced in retrieval, not in the prompt. A chunk the user cannot read never reaches the model, so a prompt injection cannot talk the model into quoting it. The cost: the assistant cannot even say "that exists but you are not entitled". It refuses the same way it refuses an unknown question.
- Refusal over helpfulness. A personal investment question ("Should I buy shares for my own pension?") is refused, because the documents do not cover it. A general-purpose model would answer, and that answer would be advice.
- Groups come from users.json. In a real bank they come from the entitlement system through a signed token, and they change daily. Cache them for minutes, not days.
## Eval results

Real run: `python tools/eval_gate.py --data-dir examples/bank --compare`, solved build, offline stub model, hash embeddings. Thresholds from `evals.thresholds.json`.

| metric | rerank off | rerank on (overlap) | threshold |
|---|---|---|---|
| cases | 17 | 17 | - |
| recall_at_k | 1.0 | 1.0 | >= 0.9 |
| mrr | 1.0 | 1.0 | >= 0.8 |
| citation_precision | 1.0 | 1.0 | >= 0.9 |
| refusal_accuracy | 1.0 | 1.0 | >= 0.9 |
| answer_rate | 1.0 | 1.0 | - |
| permission_leaks | 0 | 0 | == 0 |
| overall | 1.0 | 1.0 | >= 0.9 |

17 cases: 10 answerable (one per document, several per user), 7 refusals: cross-region research (both directions), the fee schedule for a retail advisor, insider lists for a private banker, a prompt injection, a personal investment question and a user with no entitlements. Rerank changes nothing here: the documents use distinct vocabulary, so fusion already ranks the right one first.

## Risks

- Research notes go stale fast. Each note has an `updated` date; a production build should refuse or warn on notes past their review date.
- A suitability answer is not a suitability assessment. The assistant tells the advisor the rule; the advisor still runs the assessment.
- The stub model proves retrieval, entitlements and refusals. It does not prove the live model's wording. Run `--live --repeat 3` against the real model before release, and record the result.
- Audit: regulators may ask what the assistant told an advisor. Keep traces with the trace id, but keep question text out of span attributes (the default) and store transcripts under the bank's retention policy.