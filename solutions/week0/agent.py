"""The Atlas agent loop: one bounded agent.

Ask the model what to do, run the tools it asks for, feed the results back, repeat,
until it answers or a hard limit stops it. Limits are not optional in production:
a confused model that keeps calling tools must stop, and cost must stay capped.
"""
import json

from .budget import cost_usd
from .tools import SPECS, run_tool

SYSTEM = ("You are Atlas, the IT help assistant for Meridian Systems. "
          "When someone reports a problem with a service, call check_status first. "
          "Then answer in two or three plain sentences and mention any incident number.")


def run(message: str, adapter, budget, settings) -> dict:
    """Run one request and return
    {"answer": str | None, "stop_reason": "answered" | "max_steps", "steps": int,
     "tool_calls": [{"name", "ok"}], "cost_usd": float}

    Rules:
      - At most settings.max_steps model calls. If the limit is hit, return
        stop_reason "max_steps" and answer None.
      - Call budget.authorize(settings.max_tokens_per_call) before every model call
        and budget.record(cost) after it.
      - A failing tool call is returned to the model as a result, never raised.
    """
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}]
    calls, total, steps = [], 0.0, 0
    while steps < settings.max_steps:
        budget.authorize(settings.max_tokens_per_call)
        res = adapter.complete({"model": settings.model, "messages": messages, "tools": SPECS,
                                "max_tokens": settings.max_tokens_per_call})
        steps += 1
        cost = cost_usd(res.get("usage", {}), settings.price_in_per_m, settings.price_out_per_m)
        budget.record(cost)
        total += cost
        if not res.get("tool_calls"):
            return {"answer": res.get("text", ""), "stop_reason": "answered", "steps": steps,
                    "tool_calls": calls, "cost_usd": total}
        messages.append({"role": "assistant", "content": res.get("text", ""),
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                                        for c in res["tool_calls"]]})
        for c in res["tool_calls"]:
            out = run_tool(c["name"], c["arguments"])
            calls.append({"name": c["name"], "ok": out["ok"]})
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out)})
    return {"answer": None, "stop_reason": "max_steps", "steps": steps, "tool_calls": calls, "cost_usd": total}
