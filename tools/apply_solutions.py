"""Build a solved copy of the repo: the starter plus every worked solution, applied in week order.

  python tools/apply_solutions.py OUT_DIR             all weeks
  python tools/apply_solutions.py OUT_DIR --upto week4

Used by check_starter.py, by CI (the eval gate runs on the solved build) and by the Dockerfile
(build arg APPLY_SOLUTIONS=1). Learners don't need it: their own atlas/ is the solved build.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(".venv", ".venv-x64", "__pycache__", ".pytest_cache", "var", ".env", "reports")


def weeks() -> list[str]:
    return sorted((p.name for p in (ROOT / "solutions").iterdir() if p.is_dir()), key=lambda n: int(n[4:]))


def apply(target: Path, upto: str | None = None) -> list[str]:
    """Copy solutions/weekN/*.py over target/atlas for every week up to and including upto."""
    all_weeks = weeks()
    chosen = all_weeks[:all_weeks.index(upto) + 1] if upto else all_weeks
    for week in chosen:
        for f in (ROOT / "solutions" / week).glob("*.py"):
            shutil.copy(f, Path(target) / "atlas" / f.name)
    return chosen


def build(out: Path, upto: str | None = None) -> list[str]:
    """Copy the repo to out (which must not exist) and apply solutions."""
    shutil.copytree(ROOT, out, ignore=IGNORE)
    return apply(out, upto)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", help="directory to create, or '.' to apply in place (only inside a throwaway copy)")
    p.add_argument("--upto", help="last week to apply, e.g. week4")
    a = p.parse_args(argv)
    if a.out == ".":
        applied = apply(Path.cwd(), a.upto)
    else:
        applied = build(Path(a.out), a.upto)
    print("applied " + ", ".join(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
