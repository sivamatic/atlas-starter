"""Atlas tools. Week 0 has one read-only tool: check_status.

Tools validate their own arguments and return errors as results, so a confused model
gets a chance to correct itself instead of crashing the request.
"""
import json
from pathlib import Path

from . import telemetry
from .config import ROOT

STATUS = json.loads((ROOT / "data" / "status.json").read_text(encoding="utf-8"))

SPECS = [{
    "type": "function",
    "function": {
        "name": "check_status",
        "description": "Get the current status of an internal IT service at Meridian Systems, including known incidents and workarounds.",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string", "enum": sorted(STATUS)}},
            "required": ["service"],
        },
    },
}]


def check_status(service: str) -> dict:
    return {"service": service, **STATUS[service]}


def run_tool(name: str, raw_args: str) -> dict:
    """Run a tool call from the model. Always returns {"ok": True, "result": ...} or {"ok": False, "error": ...}."""
    with telemetry.span("tool.call", {"gen_ai.tool.name": name}) as sp:
        out = _run_tool(name, raw_args)
        sp.update({"atlas.tool.ok": out["ok"], "atlas.tool.error": out.get("error")})
        return out


def _run_tool(name: str, raw_args: str) -> dict:
    if name != "check_status":
        return {"ok": False, "error": "unknown tool %r; available: check_status" % name}
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "arguments are not valid JSON"}
    service = args.get("service") if isinstance(args, dict) else None
    if service not in STATUS:
        return {"ok": False, "error": "service must be one of " + ", ".join(sorted(STATUS))}
    return {"ok": True, "result": check_status(service)}
