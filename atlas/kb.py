"""Load the knowledge base and split it into chunks (provided, not a TODO).

Each article in data/kb/*.md has front matter (title, acl, owner, updated) and "## " sections.
We chunk by section, the structure-aware chunking from Unit 3 lesson 1: a section is one idea,
and its heading travels with it so a chunk still makes sense on its own.

The acl travels with every chunk. Retrieval filters on it, so a user only ever gets
chunks from articles they're allowed to read (Unit 3 lesson 4).
"""
import hashlib
import re
from pathlib import Path

from .config import ROOT

KB_DIR = ROOT / "data" / "kb"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse(doc_id: str, raw: str) -> dict:
    raw = raw.replace("\r\n", "\n")
    m = re.match(r"---\n(.*?)\n---\n", raw, re.S)
    if not m:
        raise ValueError(doc_id + ": missing front matter")
    meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    acl = [g.strip() for g in meta.get("acl", "").split(",") if g.strip()]
    if not acl:
        raise ValueError(doc_id + ": every article needs an acl; there is no public-by-default")
    return {"doc_id": doc_id, "title": meta["title"], "acl": acl, "updated": meta.get("updated", ""),
            "body": raw[m.end():]}


def chunk(doc: dict) -> list[dict]:
    """One chunk per "## " section. Text before the first heading becomes an "Overview" chunk."""
    parts = re.split(r"^## +(.+)$", doc["body"], flags=re.M)
    sections = [("Overview", parts[0])] + list(zip(parts[1::2], parts[2::2]))
    out = []
    for section, text in sections:
        text = " ".join(text.split())
        if not text:
            continue
        cid = doc["doc_id"] + "#" + slug(section)
        out.append({"id": cid, "doc_id": doc["doc_id"], "title": doc["title"], "section": section.strip(),
                    "text": text, "acl": doc["acl"], "url": "kb/" + doc["doc_id"] + ".md#" + slug(section),
                    "hash": hashlib.sha256((doc["title"] + "|" + section + "|" + text + "|" + ",".join(doc["acl"])).encode("utf-8")).hexdigest()[:16]})
    return out


def load(kb_dir: Path = KB_DIR) -> list[dict]:
    chunks = []
    for p in sorted(Path(kb_dir).glob("*.md")):
        chunks += chunk(parse(p.stem, p.read_text(encoding="utf-8")))
    return chunks
