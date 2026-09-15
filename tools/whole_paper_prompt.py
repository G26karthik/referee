"""Build paper-only whole-paper assessment prompts for the blinded v2 arm."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import state  # noqa: E402
from harness.artifacts import PaperDoc  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.prompts.audit import SECURITY  # noqa: E402
from harness.stages.audit import context  # noqa: E402
from harness import pdf as pdf_mod  # noqa: E402

INSTRUCTION = """\
You are assessing ONE paper as a whole in a fresh independent context. Judge only the
paper text below. You have not been shown, and must not infer, any REFEREE finding, grade,
target, route outcome, baseline response, other paper, or manuscript claim.

Answer seven questions separately: (1) real contribution, (2) strongest supporting
evidence, (3) strongest threat, (4) whether weaknesses are local or systemic, (5) claims
well supported as stated, (6) claims needing qualification, and (7) whether the core
contribution stands. Do not manufacture a verdict when the rendered text is insufficient.

Print ONLY this JSON:
{"verdict":"STRONG|SOUND_WITH_MINOR_CONCERNS|SUBSTANTIAL_CONCERNS|CENTRAL_CLAIM_NOT_ESTABLISHED|INCONCLUSIVE",
 "reason":"2-4 sentences",
 "real_contribution":"answer 1", "strongest_support":"answer 2",
 "strongest_threat":"answer 3", "weaknesses_are":"LOCAL|SYSTEMIC|MIXED",
 "claims_well_supported":["answer 5"],
 "claims_needing_qualification":["answer 6"],
 "core_contribution_stands":"YES|YES_QUALIFIED|NO|UNDETERMINED",
 "strongest_contribution":"same as real_contribution",
 "weakest_link":"same as strongest_threat"}"""


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
"""
    out = root / "reports" / "whole_paper_prompt.md"
    out.write_text(pdf_mod.sanitise_controls(body), encoding="utf-8")
    return out


def main(argv: list[str]) -> int:
    cfg = Config.load()
    for pid in argv:
        out = build_one(cfg, pid)
        print(f"{pid}: {len(out.read_text(encoding='utf-8'))} chars {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
