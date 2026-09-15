# Step 7 — `harness/alignment/`: telling fitting candidates apart by configuration

Not a checkpoint. Checkpoint C is after Step 8 (reimplementation) lands, per the
approved order. Verified by property tests and two real-corpus reruns, both producing
no new run identity to `projects/`.

## What this closes

`experiment_id.resolve_experiment` refuses `ambiguous` when several advertised commands
emit the cited cell's quantity — measured on the real corpus, 84 of 84 candidate scripts
on `apt-icml`, none chosen. What none of that checking asked is whether the SCRIPTS
THEMSELVES declare which configuration they run under, against what the cited row asks
for. A repository can say "this script is `--sparsity 0.5`" as plainly as it says "this
script emits accuracy," and until now nothing read the first half of that sentence.

## The five modules

| module | answers | never |
|---|---|---|
| `argparse_surface` | what CLI flags an entrypoint's own `add_argument` calls declare | executes anything — text only |
| `configs` | what a referenced config file declares, flat fields only | needs a YAML dependency, or guesses a nested field |
| `candidates` | what ONE candidate's own text/config/argparse-default declares | infers from a FILENAME |
| `configuration` | which candidates the cited row's own configuration excludes | narrows on silence — only on contradiction |
| `trial` | (optional, gated) confirms a static surface by running `--help` | runs without `SH_ALLOW_ALIGNMENT_TRIAL` AND sufficient isolation |

Three tiers of evidence, most specific first, in `candidates.declared_configuration`: a
script's own hardcoded flag beats a referenced config file's value beats an argparse
default. A candidate's filename is never a source — `scripts/sparsity50.sh` declaring
nothing is asserted by a dedicated test, because a name is not evidence and every claim
this harness makes needs a quotable `source_ref`.

`configuration.narrow` eliminates by CONTRADICTION only. A candidate declaring
`sparsity=0.2` when the cited row says `0.5` is dropped; a candidate declaring nothing is
never dropped for silence — dropping it would be inventing evidence the repository never
gave. Narrowing to one requires that the survivor GENUINELY AGREES on at least one field,
not merely that it wasn't contradicted.

The row's own cells are read, not only the caption — the caption is usually shared across
every row of a table, so "50% sparsity" in a caption cannot tell a 50%-row from an
80%-row in the same table; the row can, and now does.

## What this does not touch

`backends.authorize()` and `experiment_id.identities_established` are unchanged and
pytest-asserted so (`test_backends_authorize_is_untouched_by_this_package`,
`test_identities_established_signature_and_behaviour_are_unchanged`). Every module here
feeds evidence INTO the existing `ambiguous` / `no_candidate` / `established` decision;
none of them adds a fourth state or a confidence score. `trial` is asserted never called
by identity resolution (`test_trial_is_optional_identity_resolution_never_requires_it`)
— static evidence alone must be able to resolve `ambiguous`, and did, in the synthetic
fixture below, with the gate never mentioned.

## The synthetic fixture, end to end

Two scripts, both emitting accuracy, both implementing the cited method — the exact
shape that reached `ambiguous` before this package existed:

```
scripts/r20.sh: python eval_apt.py --sparsity 0.2 --base_model llama-7b
scripts/r50.sh: python eval_apt.py --sparsity 0.5 --base_model llama-7b
```

Table row 0 states 20% sparsity, row 1 states 50%. Each now resolves to its own script:

```
row0 (20%): established -> ['bash', 'scripts/r20.sh']
row1 (50%): established -> ['bash', 'scripts/r50.sh']
```

with the excluded candidate's contradiction named in the record (`declares
sparsity='0.5', the cited cell's row/caption says sparsity='20'`).

## Two defects found and fixed against the REAL corpus, not the fixture

Running `resolve_experiment` against `apt-icml`'s actual, cloned repository (162
candidate commands considered) surfaced two things the synthetic fixture could not:

1. **A shell variable reference was read as a literal.** 76 of 162 real candidates pass
   `--model_name_or_path "${model_name}"` — the value is set elsewhere (a line above, a
   parent script, an outer loop), not something this module resolves. Read as the literal
   string `'${model_name}'`, every one of those 76 candidates appeared to CONTRADICT the
   cited row's real model name, which would have made a genuine narrowing indistinguishable
   from a wall of false contradictions. Fixed: a value containing `$` is now treated as
   unresolved, exactly like a candidate that declares nothing for that field — never
   guessed at, never read as a literal that happens to disagree. Pinned by
   `test_candidates_never_treats_an_unresolved_shell_variable_as_a_literal`, both in the
   module's own self-check and in `tests/test_alignment.py`.
2. **A shell line-continuation backslash was read as part of a number.** `--sparsity
   0.06\` at the end of a wrapped line captured `0.06\` rather than `0.06`. Cosmetic here
   — the comparison still correctly read it as different from the cited row's 5% either
   way — but stripped now so the declared value matches what the script actually passes.

After both fixes: narrowing on the real repository goes from 84 candidates to 9, using
`model=RoBERTa, sparsity=5` matched against the cited row, with every remaining
contradiction reason naming a REAL declared value. It does not reach one candidate for
this cell, and is honestly reported as still `ambiguous` — narrowing is real progress
without inventing a false certainty. Verified: `projects/` digest
(`tools/corpus_digest.py`) unchanged across both reruns:
`d5ea07b8c761bd77...`; all eight papers' dispositions unchanged from Step 6.

## Suite

1841 passed, 0 failed, 0 skipped — `tests/test_alignment.py` (26 tests) plus one new
self-check assertion in `harness/alignment/candidates.py`'s own `__main__` block, and six
new self-check modules auto-discovered by `tests/test_self_checks.py`
(`argparse_surface`, `configs`, `candidates`, `configuration`, `evaluator`, `trial`).

## Next

Step 8 — `harness/reimplement_driver.py` (fourth driver) + `reimpl_exec` provenance +
`ReimplementationConformance` with the strict semantics decision 1 specifies. This is
what will let Step 6's route fallback actually produce admissible evidence via
`INDEPENDENT_RECONSTRUCTION`, rather than being refused with "no driver for that route
exists yet." Then Checkpoint C, full rerun. No manuscript edits until then.
