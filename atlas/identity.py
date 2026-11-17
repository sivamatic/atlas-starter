"""Who is asking (provided, not a TODO). A stand-in for the API gateway.

SIMULATED: the caller names themselves in the X-Atlas-User header and we look them up in
data/users.json. A real gateway validates a signed token (OIDC/JWT from your identity
provider) and reads groups from it. Never trust a user-supplied header in production.
What IS real here: every request that reads data or acts carries a user, and their
groups decide what retrieval may return and who may approve what.
"""
import json
from dataclasses import dataclass

from .config import ROOT


@dataclass(frozen=True)
class User:
    id: str
    name: str
    groups: tuple
    role: str = ""


def load_directory(path=ROOT / "data" / "users.json") -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {uid: User(uid, u["name"], tuple(u["groups"]), u.get("role", "")) for uid, u in raw.items()}


def lookup(directory: dict, header_value: str | None) -> User | None:
    return directory.get((header_value or "").strip().lower())
