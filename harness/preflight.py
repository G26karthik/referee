"""Before a corpus run: is every paper distinct, and will each be reviewed as itself?

`python -m harness.preflight` runs the self-check; `python run.py preflight` runs it over
`papers/`.

**Why this exists as a REFUSAL rather than a hope.** `paper_id` is a slug of the filename,
which is readable and is not an identity: `APT _ ICML.pdf`, `APT-ICML.pdf` and
`apt icml.pdf` all slugify to `apt-icml`. `ingest.allocate_paper_id` already handles the
collision by comparing `content_sha`, and it already handles a legacy project with no
recorded sha by matching titles. Two things follow that a run should not discover halfway
through:

  1. TWO DIFFERENT PAPERS THAT SLUGIFY THE SAME are fine, and the operator should be told
     which id each got, because "we reviewed eight papers" is only checkable if the eight
     ids are eight papers.
  2. THE SAME PAPER SUBMITTED TWICE under two filenames is a duplicate and inflates every
     corpus count. It is reported here rather than silently reviewed twice.

**And the case that motivated it.** An eight-paper run was specified as "the existing seven
plus a newly selected CVPR paper at `papers/CVPR.pdf`". On this disk `papers/CVPR.pdf` is
byte-identical to the paper already reviewed as `projects/cvpr`: same `content_sha`, same
recorded `source_path`. The run would have produced seven reviews and reported eight
papers, and nothing in the pipeline would have said so — `allocate_paper_id` correctly
recognises the document as the same one and resumes its project, which is exactly right
per paper and invisible in aggregate.

So this module answers one question, before anything is spent: **for each requested file,
which paper id will it get, is that id already carrying a DIFFERENT document, and is any
other requested file the same document as this one?** Every answer is derived from the
PDF's bytes and from what is on disk. Nothing here reviews anything.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .stages import ingest
from . import state

# What a preflight can conclude about one requested file. Closed, and the two that stop a
# run are named so a caller can key on them rather than on a message.
PREFLIGHT_STATES = (
    "NEW",                  # a document this projects tree has not seen
    "RESUMES",              # the same document as an existing project; it will resume
    "DISAMBIGUATED",        # its slug is taken by a different document; it gets a suffix
    "DUPLICATE_REQUEST",    # another requested file is the SAME document
    "UNREADABLE",           # the bytes could not be hashed
)
BLOCKING_STATES = ("DUPLICATE_REQUEST", "UNREADABLE")
assert set(BLOCKING_STATES) <= set(PREFLIGHT_STATES), BLOCKING_STATES


def _recorded_sha(cfg: Config, pid: str) -> str | None:
    """The `content_sha` an existing project recorded, or None if there is no project.

    Empty string is a real answer and is not None: a project ingested before that field
    existed HAS a doc and has no sha, which is the case `ingest._same_paper_by_content`
    falls back to title matching for.
    """
    doc = cfg.projects_dir / pid / "paper" / "doc.json"
    if not doc.exists():
        return None
    try:
        return str(json.loads(doc.read_text(encoding="utf-8")).get("content_sha") or "")
    except (OSError, ValueError):
        return ""


def inspect_paper(cfg: Config, src: Path, seen: dict[str, Path]) -> dict:
    """What will happen to this one file, without ingesting it.

    `seen` maps content_sha -> the first requested file carrying it, and is MUTATED, so
    the second appearance of one document is reported as a duplicate rather than both
    being reported as new. The caller owns the dict, which is what makes the order of the
    report the order of the request.
    """
    try:
        sha = ingest.content_sha(src)
    except (OSError, ValueError) as exc:
        return {"path": str(src), "paper_id": "", "content_sha": "",
                "state": "UNREADABLE", "detail": f"could not hash the file: {exc}"}

    slug = ingest.paper_id_for(src)
    if sha in seen:
        return {"path": str(src), "paper_id": "", "content_sha": sha,
                "state": "DUPLICATE_REQUEST",
                "detail": (f"byte-identical to {seen[sha].name}, already in this request. "
                           f"Reviewing it twice would report two papers and produce one "
                           f"review.")}
    seen[sha] = src

    pid, same = ingest.allocate_paper_id(cfg, src, sha)
    recorded = _recorded_sha(cfg, pid)
    # `pid != slug` FIRST. The suffixed id is by definition one no project holds yet, so
    # checking "does a project exist for pid" ahead of it reported the interesting case —
    # the readable slug is taken by a different document — as an unremarkable NEW paper,
    # and the operator would not have learned that two of their papers collide by name.
    if pid != slug:
        state = "DISAMBIGUATED"
        detail = (f"the readable slug '{slug}' is held by a DIFFERENT document, so this "
                  f"paper is '{pid}'. Two papers, two ids, nothing shared.")
    elif recorded is None:
        state, detail = "NEW", f"no project exists for '{pid}'; it will be created"
    elif same:
        state = "RESUMES"
        detail = (f"the same document as the existing project '{pid}' "
                  f"(content_sha {sha}); that review will resume rather than restart. "
                  f"It is ONE paper in the corpus, not two.")
    else:
        state, detail = "NEW", f"'{pid}' exists and holds this document's first ingest"
    return {"path": str(src), "paper_id": pid, "content_sha": sha,
            "state": state, "detail": detail}


def check(cfg: Config, sources: list[str | Path]) -> dict:
    """The whole preflight for one requested batch.

    `distinct_documents` is the number a corpus claim may use. It is counted from
    content_sha and not from the length of the request, because those are the two numbers
    an eight-paper claim can differ on.
    """
    seen: dict[str, Path] = {}
    entries = [inspect_paper(cfg, Path(s), seen) for s in sources]
    blocking = [e for e in entries if e["state"] in BLOCKING_STATES]
    ids = [e["paper_id"] for e in entries if e["paper_id"]]
    return {
        "requested": len(entries),
        "distinct_documents": len({e["content_sha"] for e in entries if e["content_sha"]}),
        "distinct_paper_ids": len(set(ids)),
        "entries": entries,
        "blocking": [f"{Path(e['path']).name}: {e['detail']}" for e in blocking],
        "ok": not blocking,
        "resumes": [e["paper_id"] for e in entries if e["state"] == "RESUMES"],
        "new": [e["paper_id"] for e in entries if e["state"] == "NEW"],
        "disambiguated": [e["paper_id"] for e in entries if e["state"] == "DISAMBIGUATED"],
    }


def render(result: dict) -> str:
    """One table, and a sentence a corpus claim can be checked against."""
    L = ["# Corpus preflight", "",
         f"{result['requested']} file(s) requested · "
         f"{result['distinct_documents']} distinct document(s) · "
         f"{result['distinct_paper_ids']} distinct paper id(s)", "",
         "| file | paper id | content sha | state |", "|---|---|---|---|"]
    for e in result["entries"]:
        L.append(f"| `{Path(e['path']).name}` | `{e['paper_id'] or '—'}` | "
                 f"`{e['content_sha'] or '—'}` | {e['state']} |")
    L += ["", "## What each state means", ""]
    for e in result["entries"]:
        L.append(f"- `{Path(e['path']).name}` — **{e['state']}**: {e['detail']}")
    if result["blocking"]:
        L += ["", "## This batch is refused", ""]
        L += [f"- {b}" for b in result["blocking"]]
        L += ["", "A batch whose file count and document count disagree cannot support a "
                  "claim about how many papers were reviewed."]
    else:
        L += ["", f"Every requested file is a distinct document. A claim about "
                  f"{result['distinct_documents']} paper(s) is checkable from the "
                  f"`content_sha` column above."]
    L.append("")
    return "\n".join(L)


def run(cfg: Config, sources: list[str | Path], out: Path | None = None) -> dict:
    result = check(cfg, sources)
    out = out or (cfg.projects_dir.parent / "reports")
    out.mkdir(parents=True, exist_ok=True)
    state.write_json(out / "preflight.json", result)
    (out / "preflight.md").write_text(render(result), encoding="utf-8")
    result["paths"] = {"json": str(out / "preflight.json"), "md": str(out / "preflight.md")}
    return result


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = Config(projects_dir=root / "projects")
        cfg.projects_dir.mkdir(parents=True)

        # Two files with the SAME bytes under different names, and one different file.
        a = root / "Alpha Paper.pdf"
        a.write_bytes(b"%PDF-1.4 alpha")
        b = root / "alpha-paper-copy.pdf"
        b.write_bytes(b"%PDF-1.4 alpha")
        c = root / "beta.pdf"
        c.write_bytes(b"%PDF-1.4 beta")

        r = check(cfg, [a, b, c])
        assert r["requested"] == 3
        assert r["distinct_documents"] == 2, "two of the three are one document"
        assert not r["ok"], "a duplicate request must refuse the batch"
        states = [e["state"] for e in r["entries"]]
        assert states == ["NEW", "DUPLICATE_REQUEST", "NEW"], states
        assert "byte-identical" in r["entries"][1]["detail"]

        # The motivating case: a requested file that IS an existing project's document.
        pid = ingest.paper_id_for(c)
        paper_dir = cfg.projects_dir / pid / "paper"
        paper_dir.mkdir(parents=True)
        state.write_json(paper_dir / "doc.json", {
            "paper_id": pid, "title": "Beta", "content_sha": ingest.content_sha(c)})
        r2 = check(cfg, [c])
        assert r2["entries"][0]["state"] == "RESUMES", r2["entries"][0]
        assert r2["resumes"] == [pid]
        assert "ONE paper in the corpus, not two" in r2["entries"][0]["detail"]
        assert r2["ok"], "resuming an existing review is not an error"
        assert r2["distinct_documents"] == 1

        # A DIFFERENT document whose filename slugifies onto that taken id.
        d = root / "beta.pdf.d" / "beta.pdf"
        d.parent.mkdir()
        d.write_bytes(b"%PDF-1.4 a different beta")
        r3 = check(cfg, [d])
        assert r3["entries"][0]["state"] == "DISAMBIGUATED", r3["entries"][0]
        assert r3["entries"][0]["paper_id"] != pid
        assert "Two papers, two ids" in r3["entries"][0]["detail"]

        # an unreadable path refuses rather than being counted
        r4 = check(cfg, [root / "absent.pdf"])
        assert r4["entries"][0]["state"] == "UNREADABLE" and not r4["ok"]
        assert r4["distinct_documents"] == 0, "an unhashable file counts as no document"

        text = render(r)
        assert "This batch is refused" in text and "byte-identical" in text
        assert "distinct document(s)" in text
        assert "checkable from the" in render(r2)
    print("harness.preflight self-check ok")


if __name__ == "__main__":
    _self_check()
