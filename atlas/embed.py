"""Embeddings (provided, not a TODO).

  hash  Offline and deterministic: words are hashed into a fixed-size vector. It behaves like
        keyword matching, NOT like a real embedding model. It exists so tests and replay run
        with no network and give the same results every time. Never ship it.
  live  A real embedding model through any OpenAI-compatible /embeddings endpoint,
        e.g. Ollama with bge-m3. Use this to see what semantic search adds.
"""
import hashlib
import json
import math
import re
import urllib.error
import urllib.request

from .adapter import ProviderError

WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokens(text: str) -> list[str]:
    return WORD.findall(text.lower())


class HashEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = "hash-%d" % dim

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in tokens(t):
                h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
                v[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class LiveEmbedder:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 60.0):
        self.base_url, self.model, self.api_key, self.timeout = base_url, model, api_key, timeout
        self.name = model
        self.dim = len(self(["dimension probe"])[0])

    def __call__(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(self.base_url + "/embeddings", method="POST", headers=headers,
                                     data=json.dumps({"model": self.model, "input": texts}).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ProviderError("embeddings returned HTTP %d" % e.code, e.code) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError("embeddings unreachable: %s" % e) from e
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


def from_settings(s):
    if s.embed_mode == "hash":
        return HashEmbedder()
    if s.embed_mode == "live":
        if not s.base_url:
            raise ValueError("EMBED_MODE=live needs LLM_BASE_URL")
        return LiveEmbedder(s.base_url, s.embed_model, s.api_key)
    raise ValueError("EMBED_MODE must be hash or live, got " + repr(s.embed_mode))
