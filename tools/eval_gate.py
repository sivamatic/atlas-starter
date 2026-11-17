"""The merge gate (Unit 4): run the golden set, print the metrics, write a JSON report and exit 1
when any metric breaks evals.thresholds.json. CI runs this on every pull request.

  python tools/eval_gate.py                              default dataset (data/), offline stub model
  python tools/eval_gate.py --data-dir examples/bank     another dataset
  python tools/eval_gate.py --compare                    rerank off vs on, side by side
  python tools/eval_gate.py --live --repeat 3            a real model (LLM_* env vars), three runs

Offline it needs no network and no model, so the numbers are the same on every machine.
Exit codes: 0 pass, 1 below thresholds, 2 usage or setup error.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas import adapter, config, evals  # noqa: E402

METRICS = ["cases", "recall_at_k", "mrr", "citation_precision", "refusal_accuracy", "answer_rate",
           "permission_leaks", "overall"]


def settings_for(args, rerank_mode):
    env = dict(os.environ)
    if not args.live:
        env["LLM_MODE"] = "stub"
        env["EMBED_MODE"] = "hash"
        env.pop("ATLAS_DB_URL", None)
    if args.data_dir:
        env["ATLAS_DATA_DIR"] = args.data_dir
    if rerank_mode is not None:
        env["ATLAS_RERANK"] = rerank_mode
    return config.load(env)


def table(rows: dict) -> str:
    names = list(rows)
    width = max(len(m) for m in METRICS) + 2
    lines = ["metric".ljust(width) + "".join(n.rjust(16) for n in names)]
    for m in METRICS:
        lines.append(m.ljust(width) + "".join(str(rows[n].get(m, "")).rjust(16) for n in names))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", help="dataset directory (default: ATLAS_DATA_DIR or data)")
    ap.add_argument("--golden", help="golden file (default: <data-dir>/golden.json)")
    ap.add_argument("--thresholds", default=str(ROOT / "evals.thresholds.json"))
    ap.add_argument("--report", default=str(ROOT / "reports" / "eval-report.json"))
    ap.add_argument("--rerank", help="off | overlap | cross-encoder (default: ATLAS_RERANK or the rerank.py default)")
    ap.add_argument("--compare", action="store_true", help="also run with rerank off and show both")
    ap.add_argument("--live", action="store_true", help="use LLM_MODE and EMBED_MODE from the environment")
    ap.add_argument("--repeat", type=int, default=1, help="run every case N times (useful with --live)")
    args = ap.parse_args(argv)

    try:
        thresholds = json.loads(Path(args.thresholds).read_text(encoding="utf-8"))
        s = settings_for(args, args.rerank)
        runs = {}
        if args.compare:
            off = settings_for(args, "off")
            runs["rerank-off"] = (off,) + evals.evaluate(off, args.golden, repeat=args.repeat)
        from atlas import rerank
        name = "rerank-" + (s.rerank or rerank.DEFAULT)
        t0 = time.perf_counter()
        runs[name] = (s,) + evals.evaluate(s, args.golden, repeat=args.repeat)
        seconds = round(time.perf_counter() - t0, 3)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print("eval gate setup error: %s" % exc, file=sys.stderr)
        return 2
    except adapter.ReplayMiss as exc:
        print("eval gate setup error: no recording for request %s. With --live, set LLM_MODE=live and "
              "LLM_BASE_URL (or LLM_MODE=stub)" % exc, file=sys.stderr)
        return 2
    except adapter.ProviderError as exc:
        print("eval gate setup error: the model is unavailable: %s" % exc, file=sys.stderr)
        return 2

    gated = runs[name][2]
    passed, reasons = evals.gate(gated, thresholds)
    print("dataset: %s   model: %s   golden cases: %d" % (s.data_dir.name, s.llm_mode, gated["cases"]))
    print(table({n: r[2] for n, r in runs.items()}))
    failed = [r["id"] for r in runs[name][1] if evals.score_case(r)["leak"] or r["refused"] != r["expect_refusal"]]
    if failed:
        print("cases with a wrong refusal or a leak: " + ", ".join(failed))
    print("GATE: " + ("PASS" if passed else "FAIL - " + "; ".join(reasons)))

    report = {"dataset": str(s.data_dir.relative_to(ROOT)) if s.data_dir.is_relative_to(ROOT) else str(s.data_dir),
              "llm_mode": s.llm_mode, "gated_run": name, "seconds": seconds, "thresholds": thresholds,
              "passed": passed, "reasons": reasons,
              "runs": {n: {"rerank": cfg.rerank or "default", "metrics": m, "results": res} for n, (cfg, res, m) in runs.items()}}
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("report: " + str(out))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
