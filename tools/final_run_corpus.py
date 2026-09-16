"""The batch corpus-accounting artifact `make_corpus_table.py` reads.

`python tools/final_run_corpus.py [run_dir]` writes `<run_dir>/reports/corpus.json`.

`harness.corpus.account` is pure and already computed inside `controller.review_papers`
for every batch call; nothing WRITES its result to `reports/corpus.json` on its own,
because a single-paper `run.py review --paper X` invocation (what `tools/final_run.sh`
and `tools/final_run_subagent.sh` both do, one paper per process) only ever accounts for
that one paper. This calls the SAME batch entrypoint with all eight already-`done`
papers at once — which resumes every one of them instantly, spending nothing — purely to
get the eight-paper `CorpusReport` a real multi-paper request would have produced.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import controller  # noqa: E402
from harness.config import Config  # noqa: E402

PAPERS = [
    "papers/5993d35ff0996b52.pdf", "papers/ACl.pdf", "papers/APT _ ICML.pdf",
    "papers/CVPR.pdf", "papers/ICLR.pdf", "papers/0c06a98d7c818f6f.pdf",
    "papers/2024_icml_sapg.pdf", "papers/sanchez24a_ICML.pdf",
]


def main(argv: list[str]) -> int:
    run_dir = Path(argv[1] if len(argv) > 1 else "runs_final_2026-09-16")
    cfg = Config(projects_dir=run_dir / "projects")
    res = controller.review_papers(cfg, PAPERS, skip_probe=True)
    corpus = res.get("corpus")
    if not corpus:
        raise SystemExit(f"review_papers returned no corpus accounting: {res!r}")
    out = run_dir / "reports" / "corpus.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    import json
    out.write_text(json.dumps(corpus, indent=1), encoding="utf-8")
    print(f"wrote {out}: {len(corpus.get('entries') or [])} entries, "
          f"requested={corpus.get('requested')} completed={corpus.get('completed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
