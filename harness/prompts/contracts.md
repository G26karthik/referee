## gen CERTIFICATE
WHAT YOU ARE DOING: an EXACT CERTIFICATE — a small self-contained script that checks ONE
mathematical statement (theorem, lemma, bound, or one step of its printed proof) on concrete
instances in EXACT ARITHMETIC. Not a reproduction of experiments; a formal check.
  - Use `fractions.Fraction` (or sympy exact numbers) for EVERY quantity the decisive comparison
    uses, and never compare floats for equality: a difference of 1e-16 is round-off, and the
    harness counts it as nothing. Print both sides exactly as strings too:
    `REFEREE_RESULT {"premises_hold": 1 or 0, "violated": 1 or 0, "lhs": <float>, "rhs": <float>,
    "lhs_exact": "<str(Fraction)>", "rhs_exact": "<str(Fraction)>"}`.
  - For the instance indexed by `--seed`: construct ONE concrete instance inside the
    statement's own domain (different seeds -> different instances, via a small seeded
    generator). Evaluate EVERY premise of the exact claim being tested (for a proof step: the
    hypotheses in force at that point and the step's own "if") in exact arithmetic and report
    `premises_hold` (1 only if all hold). A clause the claim or step ASSERTS ("since only one of
    them is excluded", "hence x < 1") is part of what is tested, never a premise: moving it into
    `premises_hold` makes the check circular. NEVER drop, weaken or skip a premise to obtain a
    violation: a violation on an instance whose premises fail is not a counterexample.
  - A PREMISE THAT CAN NEVER HOLD: if a printed premise is unsatisfiable (e.g. it contradicts a
    definition), (1) give the general argument in `premise_argument` (the verifier checks it; it
    is reported as reasoning, not as an executed result), (2) still evaluate it exactly on every
    instance, and (3) ALSO test the conclusion under the nearest coherent reading the paper
    evidently means, declared as ONE claim-changing deviation: then `premises_hold`/`violated`
    are for that reading and `"literal": "premise_not_met"` records the text as printed.
  - SEARCH WHERE A VIOLATION WOULD BE: hypotheses at their edges (boundary points, degenerate
    or rank-deficient matrices, equality cases, parameters at their limits); a bound claimed
    for all t along a LONG horizon, every step, not a few early ones.
  - An asymptotic claim (O, Omega, Theta, unstated constants, "decays exponentially") cannot be
    violated by one finite instance: check an explicit finite condition the argument relies on
    (e.g. the supremum the decay needs is < 1), or the explicit proof step you were given, or
    refuse. A violated proof step shows the printed proof is invalid there, never that the
    statement is false.
  - `runs` = the number of instances you offer (the harness runs seeds 0..runs-1).

## decide CERTIFICATE
  - An instance counts only if it prints `premises_hold`; a violation with `premises_hold: 0` is
    nothing. `violated: 1` whose float sides agree to 1e-9 (relative) without differing
    `lhs_exact`/`rhs_exact` strings is round-off, not a violation.
  - COUNTEREXAMPLE_FOUND if any instance has `literal: "fails"` (its printed premises hold and the
    printed conclusion fails) or, with no claim-changing deviation, `premises_hold: 1` and
    `violated: 1`. On a proof step this is a gap in the printed proof, not a refuted statement.
  - VIOLATION_UNDER_CHANGED_READING if violations occur only under the declared changed reading;
    PREMISE_NOT_MET if no instance meets the premises; else NO_VIOLATION_FOUND (the tested
    instances only, never a proof). A crashed instance is inadmissible and counts for nothing.

## gen RELEASED_DATA
WHAT YOU ARE DOING: a RELEASED-DATA RECOMPUTATION — recompute the printed number from files the
authors released: in their checkout (your working directory; read-only) or acquired for this
check under /work/data. Open each file by its full relative path, written literally in the
script (e.g. "results/eval/a.jsonl", never a glob). Apply only the selection, filtering,
averaging and metric the paper itself states; never regenerate, edit, resample or fabricate
data. Nothing is trained. Usually `runs` = 1. Print a REFEREE_DATA line per file set (rows,
models, items) against what the paper says it used.

## decide RELEASED_DATA
  - A printed number: RESOLVED within its printed precision; a deterministic difference is
    reported, never scored as a failure. A relation: one exact recomputation decides it.
  - Per `stage`: each stage is decided on its own; a relation must hold in every stage; a stage
    that started and printed no result makes the check PARTIAL (its finished stages are kept).

## gen RECONSTRUCTION
WHAT YOU ARE DOING: a PAPER-DERIVED EVALUATION of one experiment the paper specifies, where no
documented command runs it. Where the authors' checkout implements the method, DRIVE THEIR
CODE (import it from the working directory; never copy or edit it) with the paper's stated
data, configuration and metric; otherwise reimplement what the paper states. It will never be
reported as an author reproduction. PRESERVE THE SCIENTIFIC QUESTION: the same methods, data,
splits, tuning protocol, metric and aggregation the claim is about. Every detail neither the
paper nor the authors' code fixes (a hyperparameter, dataset size, preprocessing, a baseline's
setting) is a DEVIATION you declare; a change to what is compared (untuned methods where the
paper tuned, a baseline you had to rebuild, a different split or training subset for one
method than for another, a different aggregation) is `changes_claim: true`. Never shrink the
experiment (fewer runs, samples, iterations or a smaller model) to make it faster. Where the
paper and the authors' code DISAGREE (a split, a subsample, a hyperparameter), follow the paper,
and state the code's difference in `notes` (it is a finding for the referee). Compare every
method the claim names; one you cannot run is a stated omission in `notes`, never silent.
Load each dataset exactly as the paper and code specify and print its REFEREE_DATA identity
(instances, features, labels, distinct classes/rankings) against the paper's own description.

## decide RECONSTRUCTION
  - A stated relation: per stage, the mean paired margin over the seeds must exceed the Student-t
    95% band (t(n-1)·SE; 3 seeds give t=4.30); every stage must hold. One seed decides nothing.
  - A printed number: RESOLVED inside the 95% CI of the seed mean, FAILED outside the prediction
    interval, else INCONCLUSIVE.
  - A stage that started and printed no result, or a seed that failed after measuring, makes the
    check PARTIAL: what finished is kept and reported, and decides only its own stages.
  - Any claim-changing deviation makes the result about the changed claim (READING_CHANGED), never
    support or failure of the printed claim.

## verify CERTIFICATE
  1. Does `premises_hold` evaluate EVERY premise of the exact claim tested (for a proof step: the
     hypotheses in force and the step's own condition) — nothing dropped, weakened or asserted
     away? Is any clause the claim or step ASSERTS wrongly moved into `premises_hold` (that makes
     the check circular: REVISE)? A violation counted where a premise fails is a REJECTION; an
     unsatisfiable premise must be reported (`premises_hold: 0`, a `premise_argument`), not removed.
  2. Does `claimed_bound` match the paper's relation EXACTLY — constants, direction, quantifier
     order? A different, easier inequality proves nothing about the printed one.
  3. Are the constructed instances admissible, and do they probe the edges where a violation
     would be?
  4. Is the decisive comparison exact (Fraction or sympy exact, never float equality or a float
     tolerance), is `violated` computed from it, and are `lhs_exact`/`rhs_exact` printed?
  5. Is `checked_statement` honest (an asymptotic conclusion is not finitely checkable)? Is a
     `premise_argument` correct as a general argument (not just on the instances)?
  6. Is `changes_claim` true for every deviation that alters what the printed claim says (a
     premise, the conclusion, an index range, a definition), and does `literal` separate
     `fails` (printed premises hold and the printed conclusion fails) from `undefined` (the
     printed text is not well-defined on the instance, e.g. an index out of range)?

## verify RELEASED_DATA
  1. Does the script open the right released or acquired file(s) for THIS printed number, by
     literal path, and are they the version the paper used (REFEREE_DATA identity)?
  2. Does it compute the paper's stated quantity (same metric, subset, averaging, units),
     applying only selection the paper states — no editing, resampling or regeneration?
  3. Is the compared output the number the printed target (or the stated relation) is about,
     computed from the files — never a flag computed against the printed number?

## verify RECONSTRUCTION
  1. Is every required ingredient (method, training, dataset, metric, comparison target)
     realized exactly as the paper states it, each backed by a verbatim paper quote? Does it
     compare every method the claim names (or state each omission in `notes`)?
  2. Did the script fill a detail the paper and the authors' code omit WITHOUT declaring it in
     `deviations`, or shrink the experiment (fewer runs, samples, iterations, a smaller
     model)? Either is a rejection. Is `changes_claim` true for every change to data, splits,
     tuning, baselines, metric or aggregation? Where the checkout implements the method, does the
     script call that code rather than a rewrite of it? Where paper and code disagree, does it
     follow the paper (and say so in `notes`)? Following the code there is REVISE.
  3. Does `runs` (or the number of result lines one run prints) match the paper's stated
     replication? Are several units (datasets, settings) printed as separate `stage` lines rather
     than folded into one worst case?
  4. Does it use the host's GPU where the method trains networks and the authors' code supports it?

## verify ARITHMETIC
  1. Is each operand's `value` exactly the number printed in its `quote`, and is it the
     quantity the paper's own result is computed from (right row, column, units)?
  2. Is the `expression` the computation the paper's sentence claims (e.g. relative vs absolute
     gain, which baseline is the denominator)? A wrong formula makes a false contradiction.
  3. Is `paper_result` the paper's own printed result of that computation?
