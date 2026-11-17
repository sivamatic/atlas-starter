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
    # TODO(week0): the loop. Each step:
    #   1. budget.authorize(...), then adapter.complete({"model", "messages", "tools": SPECS, "max_tokens"})
    #   2. steps += 1; cost = cost_usd(res["usage"], prices from settings); budget.record(cost)
    #   3. no tool_calls in the response -> return the answer (stop_reason "answered")
    #   4. otherwise append the assistant message (OpenAI format: tool_calls with id, type "function",
    #      function {name, arguments}), run each tool with run_tool(), record {"name", "ok"} in calls,
    #      and append {"role": "tool", "tool_call_id": id, "content": json.dumps(result)}
    # After max_steps model calls, return stop_reason "max_steps" with answer None.
    raise NotImplementedError("TODO(week0): agent loop")
