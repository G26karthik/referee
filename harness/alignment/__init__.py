"""Step 7 — aligning a claim with the released artifact BEYOND "does it emit the metric".

`harness.experiment_id` answers three narrow questions: which command, which quantity,
which configuration. It refuses correctly when a repository advertises several commands
that all emit the cited cell's quantity — `ambiguous`, over the shipped corpus 84 of
84 candidate scripts on one paper, none chosen — because a refusal is the honest answer
to "which one?" when nothing distinguishes them.

**This package is what distinguishes them, when the paper and the repository both say
enough to.** Every module here is a NEW source of structural evidence consulted
ADDITIONALLY, never a replacement for the existing refusal ladder:

  `argparse_surface`  what CLI flags a script's own entrypoint declares — name, default,
                      choices — read from its source, never invented
  `configs`           configuration files a script loads, and the flat field values they
                      declare
  `configuration`     the fix itself: which of several fitting candidates DECLARES the
                      configuration the cited row asks for, from its own hardcoded flags,
                      its own config file, or (last and weakest) its own filename
  `evaluator`         which candidate's code actually COMPUTES the cited quantity, from a
                      real metric call or a `compute_metrics`-shaped function, rather than
                      a bare quoted key that could be an argparse help string
  `trial`             an OPTIONAL, GATED `--help` invocation that CONFIRMS a statically
                      read argparse surface against the real program, under the same
                      isolation boundary repository execution requires

**What this package does not touch.** `backends.authorize()` and
`experiment_id.identities_established` are unchanged — every module here feeds evidence
INTO the existing `ambiguous` / `no_candidate` / `established` decision that
`experiment_id.resolve_experiment` already makes; none of them adds a fourth state, a
confidence score, or a second place that decision is made. A repository whose candidates
still cannot be told apart after configuration matching stays `ambiguous`, honestly.
"""
