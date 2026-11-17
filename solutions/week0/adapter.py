"""The provider adapter: the only module that talks to a model provider.

Modes (LLM_MODE):
  live    call the provider
  record  call the provider and save the response to the recordings file
  replay  return the saved response; never touch the network. A miss is an error.
  stub    a deterministic offline stand-in (stub_model.py) for evals and demos. Not a model.

Responses are normalised to:
  {"text": str, "tool_calls": [{"id", "name", "arguments"}], "usage": {"input_tokens", "output_tokens"}}
Same design as the browser lab "Record, replay and the provider adapter" (Unit 0, lesson 3).
"""
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path


class ProviderError(Exception):
    """The provider failed (network error, 429, 5xx). Callers decide whether to retry or fall back."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ReplayMiss(Exception):
    """Replay mode found no recording for this request. Re-record on purpose; never go live silently."""


def request_key(req: dict) -> str:
    """A stable key for what matters in a request: model, messages, tools and token cap."""
    material = {k: req.get(k) for k in ("model", "messages", "tools", "max_tokens")}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_tape(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}


def save_tape(path: Path, tape: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(tape, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class Adapter:
    def __init__(self, mode: str, live=None, tape: dict | None = None, tape_path: Path | None = None):
        if mode not in ("live", "record", "replay"):
            raise ValueError("LLM_MODE must be live, record or replay, got " + repr(mode))
        self.mode = mode
        self.live = live
        self.tape = tape if tape is not None else (load_tape(tape_path) if tape_path else {})
        self.tape_path = tape_path

    def complete(self, req: dict) -> dict:
        """Return a normalised response for req according to the mode.

        replay: return self.tape[key]["response"]; if the key is missing raise ReplayMiss(key).
                Never call self.live in replay mode.
        live:   return self.live(req).
        record: call self.live(req), store {"note": <last user message>, "response": res}
                under the key, save the tape if there is a tape_path, and return res.
        """
        key = request_key(req)
        if self.mode == "replay":
            if key not in self.tape:
                raise ReplayMiss(key)
            return self.tape[key]["response"]
        res = self.live(req)
        if self.mode == "record":
            user = [m.get("content", "") for m in req.get("messages", []) if m.get("role") == "user"]
            self.tape[key] = {"note": user[-1] if user else "", "response": res}
            if self.tape_path:
                save_tape(self.tape_path, self.tape)
        return res

    def configured(self) -> bool:
        return bool(self.tape) if self.mode == "replay" else self.live is not None


def openai_compatible(base_url: str, api_key: str = "", timeout: float = 60.0):
    """A live() function for any OpenAI-compatible /chat/completions endpoint (Ollama, vLLM, hosted APIs).

    Plain HTTP, no vendor SDK, so swapping providers is a configuration change.
    """
    def live(req: dict) -> dict:
        body = json.dumps({**req, "stream": False}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        http = urllib.request.Request(base_url + "/chat/completions", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(http, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ProviderError("provider returned HTTP %d" % e.code, e.code) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError("provider unreachable: %s" % e) from e
        msg = data["choices"][0]["message"]
        calls = [{"id": c.get("id") or "call_%d" % i, "name": c["function"]["name"],
                  "arguments": c["function"].get("arguments") if isinstance(c["function"].get("arguments"), str)
                  else json.dumps(c["function"].get("arguments") or {})}
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        u = data.get("usage") or {}
        return {"text": msg.get("content") or "", "tool_calls": calls,
                "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}
    return live


def from_settings(s) -> Adapter:
    if s.llm_mode == "stub":
        from . import stub_model
        return Adapter("live", live=stub_model.complete)
    live = openai_compatible(s.base_url, s.api_key) if s.base_url else None
    if s.llm_mode != "replay" and live is None:
        raise ValueError("LLM_BASE_URL is required when LLM_MODE is " + s.llm_mode)
    return Adapter(s.llm_mode, live=live, tape_path=s.recordings)
