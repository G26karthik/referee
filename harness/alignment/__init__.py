"""Step 7 — aligning a claim with the released artifact BEYOND "does it emit the metric".

`harness.experiment_id` answers three narrow questions: which command, which quantity,
which configuration. It refuses correctly when a repository advertises several commands
that all emit the cited cell's quantity — `ambiguous`, over the shipped corpus 84 of
84 candidate scripts on one paper, none chosen — because a refusal is the honest answer
to "which one?" when nothing distinguishes them.

**This package is what distinguishes them, when the paper and the repository both say
enough to.** Two modules remain, each feeding evidence INTO the existing `ambiguous` /
`no_candidate` / `established` decision `experiment_id.resolve_experiment` already makes,
never adding a fourth state or a second place that decision is made:

  `candidates`     which fitting candidate DECLARES the configuration the cited row asks
                   for, from its own hardcoded `--flag value` text — the only source
                   live-measurement ever found narrowing a real candidate set (255 fields
                   recovered on APT's 84-candidate ambiguity)
  `configuration`  the comparison itself: field extraction from the cited cell's row and
                   caption, normalisation, and `narrow()` — contradiction eliminates,
                   silence does not

**`argparse_surface` (CLI flag defaults) and `configs` (config-file values) were built,
measured, and deleted 2026-09-21.** Both were real, independently-reasoned fallback tiers
behind the script's own hardcoded text. Re-run live against every repository-paper
checkout this harness has cached: 0 fields from config files on any paper, 2 fields from
argparse defaults — both on a paper whose two candidates never land in the same
`fitting` set, so neither field could ever reach a comparison. Zero effect on any outer
identity decision, on every real repository this harness has processed — the same
standard `literature.py`/`validation.py`/`claimlink.py` were removed under (see
`CLAUDE.md`'s Known limitations). `evaluator` (code-structure metric detection) was
measured the same way and found equally inert; it stays as a documented no-op rather than
being deleted outright because `experiment_id.py` calls it unconditionally.

**What this package does not touch.** `backends.authorize()` and
`experiment_id.identities_established` are unchanged by any of this.
"""
