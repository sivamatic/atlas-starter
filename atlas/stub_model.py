"""A deterministic, offline stand-in for a model (provided, not a TODO). LLM_MODE=stub.

It is NOT a language model. It reads the numbered sources in an /v1/ask prompt, quotes the
sentence from each source that shares the most words with the question, and cites it as [n].
If no source shares enough words with the question, it replies NOT_FOUND, like the prompt asks.

Why it exists: evals, demos and CI need to run the whole pipeline (retrieval, permissions,
rerank, the grounding check, refusals, tracing) with no network and no GPU, and get the same
result every time. It measures the plumbing, not answer quality. To measure a real model, run
the same golden set with LLM_MODE=live (tools/eval_gate.py --live) and repeat it a few times.

Token counts in "usage" are word counts, so cost and token numbers in stub mode are estimates.
"""
import re

from .retrieval import terms

# Words that say little about what the question is about, on top of the stop words in retrieval.py.
FILLER = set("about also any anything can't could did does doesn't get got has have help just keep keeps "
             "know let like make many much need needs please should still tell than them these they thing "
             "today want way would yes".split())
SOURCE = re.compile(r"\[(\d+)\] (.*?)\n(.*)", re.S)
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def content_terms(text: str) -> set[str]:
    return {stem(t) for t in terms(text) if t not in FILLER}


def answer(question: str, sources: list[tuple[int, str, str]], max_cites: int = 2) -> str:
    q = content_terms(question)
    if not q:
        return "NOT_FOUND"
    scored = []
    for n, heading, text in sources:
        matched = q & content_terms(heading + " " + text)
        # Enough overlap: two question words, or the only word of a one-word question.
        if len(matched) >= min(2, len(q)) and len(matched) / len(q) >= 0.34:
            scored.append((-len(matched), n, text))
    if not scored:
        return "NOT_FOUND"
    parts = []
    for _, n, text in sorted(scored)[:max_cites]:
        best = max(SENTENCE.split(text), key=lambda s: (len(q & content_terms(s)), -len(s)))
        parts.append(best.strip() + " [%d]" % n)
    return " ".join(parts)


def complete(req: dict) -> dict:
    """A live() function for adapter.Adapter: same request and response shape as a real provider."""
    user = next((m["content"] for m in reversed(req.get("messages", [])) if m.get("role") == "user"), "") or ""
    if "\n\nQuestion: " in user and user.startswith("Sources:"):
        body, question = user.rsplit("\n\nQuestion: ", 1)
        blocks = [SOURCE.match(b) for b in body[len("Sources:\n\n"):].split("\n\n")]
        sources = [(int(m.group(1)), m.group(2), m.group(3)) for m in blocks if m]
        text = answer(question, sources)
    else:
        text = "The offline stub model only answers /v1/ask questions from sources. Use LLM_MODE=replay or live for chat."
    words_in = sum(len(str(m.get("content") or "").split()) for m in req.get("messages", []))
    return {"text": text, "tool_calls": [], "usage": {"input_tokens": words_in, "output_tokens": len(text.split())}}
