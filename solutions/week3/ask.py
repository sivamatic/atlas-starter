"""Answer a question from the knowledge base, with citations (Unit 3 project).

The rule: Atlas answers only from sources the user is allowed to read, and every answer
points at them. If retrieval finds nothing, or the model's answer cites nothing (or cites
a source that doesn't exist), Atlas says it couldn't find it instead of guessing.

What this check does NOT prove: that each sentence is actually supported by the source it
cites. That needs an eval or a grounding guardrail (Unit 4).
"""
import re

from .budget import cost_usd

SYSTEM_ASK = ("You are Atlas, the IT help assistant for Meridian Systems. Answer only from the numbered sources. "
              "After each sentence that uses a source, cite its number in square brackets, like [1]. "
              "If the sources do not answer the question, reply with exactly NOT_FOUND.")
REFUSAL = ("I couldn't find that in the IT knowledge base you have access to. "
           "Ask me to open a ticket and someone from IT will help.")
CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def build_messages(question: str, hits: list[dict], system: str | None = None) -> list[dict]:
    """system: a dataset's own prompt (settings.system_prompt); empty or None means SYSTEM_ASK."""
    blocks = ["[%d] %s - %s\n%s" % (i, h["title"], h["section"], h["text"]) for i, h in enumerate(hits, 1)]
    return [{"role": "system", "content": system or SYSTEM_ASK},
            {"role": "user", "content": "Sources:\n\n" + "\n\n".join(blocks) + "\n\nQuestion: " + question}]


def parse_citations(text: str) -> list[int]:
    """Every source number cited as [n] or [n, m] in text: unique, ascending.
    "Use US-East [1]. Restart first [2][1]." -> [1, 2];  "see [1, 3]" -> [1, 3];  "no cites" -> []
    """
    nums = set()
    for m in CITE.finditer(text):
        nums.update(int(x) for x in m.group(1).split(","))
    return sorted(nums)


def ask(question: str, user, retriever, adapter, budget, settings) -> dict:
    """Return {"answer", "grounded", "stop_reason", "citations", "cost_usd"}.

    1. hits = retriever.search(question, user.groups, k=settings.top_k)
    2. No hits: return the refusal (settings.refusal or REFUSAL) with stop_reason "no_sources", without calling the model.
    3. budget.authorize(settings.max_tokens_per_call), then one model call:
       adapter.complete({"model": settings.model, "messages": build_messages(question, hits, settings.system_prompt),
                         "tools": [], "max_tokens": settings.max_tokens_per_call})
       and budget.record() the cost (see cost_usd and the prices in settings).
    4. Grounding check: if the text contains NOT_FOUND, cites nothing, or cites a number outside
       1..len(hits), return the refusal (settings.refusal or REFUSAL) with stop_reason "ungrounded". Never show that text.
    5. Otherwise return the text with stop_reason "answered", grounded True, and one citation per
       cited number: {"n", "id", "title", "section", "url"} taken from hits[n - 1].
    """
    hits = retriever.search(question, user.groups, k=settings.top_k)
    if not hits:
        return {"answer": settings.refusal or REFUSAL, "grounded": False, "stop_reason": "no_sources", "citations": [], "cost_usd": 0.0}
    budget.authorize(settings.max_tokens_per_call)
    res = adapter.complete({"model": settings.model, "messages": build_messages(question, hits, settings.system_prompt),
                            "tools": [], "max_tokens": settings.max_tokens_per_call})
    cost = cost_usd(res.get("usage", {}), settings.price_in_per_m, settings.price_out_per_m)
    budget.record(cost)
    text = (res.get("text") or "").strip()
    cited = parse_citations(text)
    if "NOT_FOUND" in text or not cited or any(n < 1 or n > len(hits) for n in cited):
        return {"answer": settings.refusal or REFUSAL, "grounded": False, "stop_reason": "ungrounded", "citations": [], "cost_usd": cost}
    return {"answer": text, "grounded": True, "stop_reason": "answered", "cost_usd": cost,
            "citations": [{"n": n, "id": hits[n - 1]["id"], "title": hits[n - 1]["title"],
                           "section": hits[n - 1]["section"], "url": hits[n - 1]["url"]} for n in cited]}
