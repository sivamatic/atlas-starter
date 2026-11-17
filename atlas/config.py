"""Settings come from environment variables, never from code.

Model names and prices change often, so they live in configuration (see .env.example)
and you record which model you tested, and when, in your provider-choice note.
"""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    llm_mode: str              # live | record | replay
    model: str
    base_url: str              # any OpenAI-compatible endpoint, e.g. http://127.0.0.1:11434/v1
    api_key: str
    recordings: Path
    max_steps: int             # hard limit on model calls per request
    max_tokens_per_call: int
    max_cost_usd: float        # spend ceiling for this process
    price_in_per_m: float      # USD per million input tokens
    price_out_per_m: float     # USD per million output tokens
    kill_switch: bool
    db_url: str = ""            # "" = in-memory (dev and tests); postgresql://... for Postgres + pgvector
    embed_mode: str = "hash"    # hash (offline, deterministic, NOT semantic) | live (OpenAI-compatible /embeddings)
    embed_model: str = "bge-m3"
    top_k: int = 4              # sources passed to the model per answer
    ticket_ttl_s: int = 900     # a ticket proposal expires if not approved in time
    data_dir: Path = ROOT / "data"  # kb/, users.json, golden.json; examples/<x> for the industry builds
    tenant: str = "meridian"    # a label on traces and reports, so you can tell deployments apart
    system_prompt: str = ""     # "" = the Meridian IT prompt in ask.py; a dataset can ship system_prompt.txt
    refusal: str = ""           # "" = ask.REFUSAL; a dataset can ship refusal.txt
    rerank: str = ""            # off | overlap | cross-encoder; "" = the default in rerank.py
    rerank_pool: int = 20       # fused candidates the reranker looks at before keeping top_k
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    tracer: str = "auto"        # auto | memory | otel. auto = otel when an OTLP endpoint is set and the SDK is installed
    otlp_endpoint: str = ""     # OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    trace_file: str = ""        # memory tracer: append finished spans as JSON lines here ("" = keep in memory only)
    trace_content: bool = False  # put question and answer text in spans? Off by default: traces are not a PII store


def _data_dir(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load(env=None) -> Settings:
    e = os.environ if env is None else env
    data_dir = _data_dir(e.get("ATLAS_DATA_DIR", "data"))
    prompt_file = data_dir / "system_prompt.txt"
    return Settings(
        llm_mode=e.get("LLM_MODE", "replay"),
        model=e.get("LLM_MODEL", "llama3.2:3b"),
        base_url=e.get("LLM_BASE_URL", "").rstrip("/"),
        api_key=e.get("LLM_API_KEY", ""),
        recordings=Path(e.get("LLM_RECORDINGS", ROOT / "recordings" / "tape.json")),
        max_steps=int(e.get("ATLAS_MAX_STEPS", "4")),
        max_tokens_per_call=int(e.get("ATLAS_MAX_TOKENS_PER_CALL", "400")),
        max_cost_usd=float(e.get("ATLAS_MAX_COST_USD", "0.50")),
        price_in_per_m=float(e.get("LLM_PRICE_IN_PER_M", "0")),
        price_out_per_m=float(e.get("LLM_PRICE_OUT_PER_M", "0")),
        kill_switch=e.get("ATLAS_KILL_SWITCH", "0") == "1",
        db_url=e.get("ATLAS_DB_URL", ""),
        embed_mode=e.get("EMBED_MODE", "hash"),
        embed_model=e.get("EMBED_MODEL", "bge-m3"),
        top_k=int(e.get("ATLAS_TOP_K", "4")),
        ticket_ttl_s=int(e.get("ATLAS_TICKET_TTL_S", "900")),
        data_dir=data_dir,
        tenant=e.get("ATLAS_TENANT", "meridian" if data_dir == ROOT / "data" else data_dir.name),
        system_prompt=prompt_file.read_text(encoding="utf-8").strip() if prompt_file.exists() else "",
        refusal=(data_dir / "refusal.txt").read_text(encoding="utf-8").strip() if (data_dir / "refusal.txt").exists() else "",
        rerank=e.get("ATLAS_RERANK", ""),
        rerank_pool=int(e.get("ATLAS_RERANK_POOL", "20")),
        rerank_model=e.get("ATLAS_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
        tracer=e.get("ATLAS_TRACER", "auto"),
        otlp_endpoint=e.get("OTEL_EXPORTER_OTLP_ENDPOINT", "") or e.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
        trace_file=e.get("ATLAS_TRACE_FILE", ""),
        trace_content=e.get("ATLAS_TRACE_CONTENT", "0") == "1",
    )
