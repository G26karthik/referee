"""Build the SINGLE-MODEL CRITIQUE baseline prompt for each paper.

    python tools/baseline_prompt.py                  # every ingested paper
    python tools/baseline_prompt.py acl iclr         # named papers

Writes `<project>/baseline/prompt.md` per paper and prints where.

**What this arm is.** One model, one context, one pass over the paper, asked for the same
human-facing unit REFEREE produces: located concerns with verbatim evidence, a severity,
and a short overall assessment. It is NOT "ChatGPT" and must never be called that unless a
real OpenAI model produced it; the arm is named by what it is, and whatever model actually
answers is recorded beside the output.

**Why it is built from `stages.audit.context` rather than from the PDF.** A comparison is
only about architecture if everything else is held constant, and the easiest way to rig
this one is to give the arms different text. REFEREE's lenses see
`pdf.render_sections(doc, SECTION_BUDGET_CHARS)` — a TRUNCATED paper, 33 to 84 percent of
the extracted prose on this corpus. Handing the baseline the full PDF would make the
comparison a measurement of how much paper each arm was shown. So this reuses the identical
`context(doc)` call and the identical return schema, and the ONLY difference from a lens
prompt is the instruction in the middle: no lens specialisation, no panel, no harness.

**What is deliberately withheld.** No REFEREE finding, question, target, ledger, disposition
or prior review. The baseline must not be able to agree with an answer it was shown.

**What is deliberately KEPT.** The security preamble, because the paper text is untrusted
data for any reader; and the evidence-quoting requirement, because the same post-hoc
verification (`stages.audit.verify_evidence`) is applied to BOTH arms afterwards. An arm
that was never asked to quote would score zero on support through a formatting difference
rather than through anything about its reasoning.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import state                                            # noqa: E402
from harness.artifacts import PaperDoc                               # noqa: E402
from harness.config import Config                                    # noqa: E402
from harness.prompts.audit import SECURITY, _RETURN                  # noqa: E402
from harness.stages.audit import context                             # noqa: E402
from harness import pdf as pdf_mod                                   # noqa: E402

INSTRUCTION = """\
You are reviewing ONE paper, as a first-round referee. Judge only this paper.

Read it and report the concerns you would raise. There is no panel, no second reader and
no follow-up round: this is one pass, and what you write is the whole review.

For each concern, give:
  * what is wrong or missing, in one or two sentences of plain language;
  * the VERBATIM text, table cell, caption or equation from the paper that shows it;
  * where it is, as specifically as you can;
  * how much it matters, as FATAL / MAJOR / MINOR / NOTE;
  * what would settle it.

SEVERITY, defined so that it means the same thing here as anywhere else:
  FATAL  if true, a central claim of the paper does not stand.
  MAJOR  a central claim is materially weakened; the paper needs new evidence, not new prose.
  MINOR  a supporting claim or presentation defect; fixable in revision without new experiments.
  NOTE   a clarification request; no claim is at stake.

QUOTE EXACTLY. Every quotation you give is re-checked against the parsed paper afterwards,
and a concern whose quote does not occur there is dropped. A concern nobody can locate is
not a review comment.

SEPARATE EVIDENCE FROM INFERENCE. What the paper printed and what you conclude from it are
different things and go in different fields.

A CLEAN PAPER IS A REAL RESULT. Zero FATAL and zero MAJOR concerns, a few MINOR ones and
some open questions is a complete, correct review of good work. Do not manufacture severity
to look thorough, and do not soften a real problem to look agreeable.

YOU HAVE NO SEARCH TOOL. Do not assert an external number, a prior result or a baseline
value from memory as though you had checked it. If the paper does not say how something was
obtained, that omission is itself the finding."""


def build_one(cfg: Config, pid: str) -> Path:
    root = state.project_dir(cfg, pid)
    doc = PaperDoc(**state.read_json(root / "paper" / "doc.json"))
    ctx = context(doc)
    body = f"""{SECURITY}

{INSTRUCTION}

Paper: {doc.title or "(title not detected)"}

=== CLAIMS THE PAPER MAKES ABOUT ITSELF ===
{ctx.get("claims_text") or "(none extracted)"}

=== NUMBERS THE PAPER REPORTS ===
{ctx.get("numbers_text") or "(none extracted)"}

=== TABLES (each cell addressed T<table>:r<row>:c<col>) ===
{ctx.get("tables_text") or "(no tables extracted)"}

=== FIGURE CAPTIONS (addressed F<n>) ===
{ctx.get("figures_text") or "(none extracted)"}

=== EQUATIONS (addressed E<n>) ===
{ctx.get("equations_text") or "(none extracted)"}

=== SECTIONS ===
{ctx.get("sections_text") or "(no section text extracted)"}

{_RETURN.replace("<lens>", "baseline")}"""
    out = root / "baseline" / "prompt.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pdf_mod.sanitise_controls(body), encoding="utf-8")
    return out


def main(argv: list[str]) -> int:
    cfg = Config.load()
    pids = argv or sorted(p.name for p in cfg.projects_dir.iterdir()
                          if (p / "paper" / "doc.json").exists())
    for pid in pids:
        path = build_one(cfg, pid)
        print(f"{pid:24s} {len(path.read_text(encoding='utf-8')):7d} chars  {path}")
    print(f"\n{len(pids)} baseline prompt(s). Same rendering and same schema as the lens "
          f"prompts; the only difference is the instruction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
