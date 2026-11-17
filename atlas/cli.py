"""Ask Atlas from the command line. Respects LLM_MODE.

  python -m atlas.cli "My VPN keeps dropping"                    the week 0 agent loop
  python -m atlas.cli ask --user priya "My VPN keeps dropping"    week 3: answer with citations, as priya

With LLM_MODE=record this is how you create recordings for CI. LLM_MODE=stub answers offline
without a model (week 4: evals and demos). ATLAS_DATA_DIR=examples/bank uses another dataset.
"""
import json
import sys

from . import adapter, agent, config, embed, identity, rerank, retrieval
from . import ask as ask_mod
from .budget import Budget


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    s = config.load()
    llm, budget = adapter.from_settings(s), Budget(s.max_cost_usd, s.max_tokens_per_call, s.kill_switch)
    if argv[0] == "ask":
        if len(argv) < 4 or argv[1] != "--user":
            print(__doc__)
            return 2
        user = identity.lookup(identity.load_directory(s.data_dir / "users.json"), argv[2])
        if user is None:
            print("unknown user " + argv[2] + "; see " + str(s.data_dir / "users.json"))
            return 2
        e = embed.from_settings(s)
        retriever = rerank.wrap(retrieval.Retriever(retrieval.build(s, e), e), s)
        out = ask_mod.ask(" ".join(argv[3:]), user, retriever, llm, budget, s)
    else:
        out = agent.run(" ".join(argv), llm, budget, s)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
