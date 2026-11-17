"""Maintainer gate for the starter repo (same rule as the browser labs' checker):
the starter must fail the week's tests, and starter + worked solutions must pass them all.
Solutions are applied cumulatively: checking week5 also applies week0 and week3.
Finally the whole suite runs once on the fully solved build.

  python tools/check_starter.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_solutions  # noqa: E402

ROOT = apply_solutions.ROOT
WEEKS = apply_solutions.weeks()


def pytest(cwd: Path, marker: str | None) -> int:
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + (["-m", marker] if marker else [])
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).returncode


def main() -> int:
    ok = True
    for week in WEEKS:
        starter = pytest(ROOT, week)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            apply_solutions.build(work, upto=week)
            solved = pytest(work, week)
        print("%s: starter %s, solution %s" % (week, "fails (good)" if starter == 1 else "UNEXPECTED exit %d" % starter,
                                               "passes" if solved == 0 else "FAILS"))
        ok = ok and starter == 1 and solved == 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        apply_solutions.build(work)
        full = pytest(work, None)
    print("all weeks: full suite on the solved build %s" % ("passes" if full == 0 else "FAILS"))
    return 0 if ok and full == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
