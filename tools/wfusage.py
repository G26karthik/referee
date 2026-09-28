"""Token usage of one workflow run, per task role: `python tools/wfusage.py <workflow transcript dir>`.

Transcripts live under ~/.claude/projects/<project>/<session>/subagents/workflows/wf_*. A role is
read from the agent's first prompt (the task id in its seal command, or the controller's
`run.py tasks`). Output tokens as logged by transcripts can under-count thinking.
"""
import collections
import json
import re
import sys
from pathlib import Path

KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def agent_usage(path: Path) -> tuple[str, str, int, collections.Counter]:
    role, model, turns, usage, seen = "?", "", 0, collections.Counter(), set()
    for line in path.open(encoding="utf-8"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") or {}
        if e.get("type") == "user" and role == "?":
            c = msg.get("content")
            text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
            if m := re.search(r'run\.py seal \S+ "([^"]+)"', text):
                role = m.group(1).split(":")[0]
            elif "run.py tasks" in text:
                role = "controller"
        if e.get("type") == "assistant" and msg.get("id") not in seen:
            seen.add(msg.get("id"))
            model, turns = msg.get("model", model), turns + 1
            usage.update({k: (msg.get("usage") or {}).get(k) or 0 for k in KEYS})
    return role, model.removeprefix("claude-"), turns, usage


def main(root: str) -> None:
    agg: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for f in Path(root).rglob("*.jsonl"):
        role, model, turns, usage = agent_usage(f)
        if turns:
            agg[f"{role} [{model}]"].update(usage, agents=1, turns=turns)
    total = sum(agg.values(), collections.Counter())
    print(f"{'role':34s} {'agents':>6s} {'turns':>6s} {'c_write':>9s} {'c_read':>10s} {'out':>8s}")
    for k, a in sorted(agg.items(), key=lambda kv: -kv[1]["cache_read_input_tokens"]) + [("TOTAL", total)]:
        print(f"{k:34s} {a['agents']:6d} {a['turns']:6d} {a['cache_creation_input_tokens']:9d} "
              f"{a['cache_read_input_tokens']:10d} {a['output_tokens']:8d}")


if __name__ == "__main__":
    main(sys.argv[1])
