## gen CERTIFICATE
WHAT YOU ARE DOING: an EXACT CERTIFICATE — a small self-contained script that checks ONE
mathematical statement (theorem, lemma, bound, or one step of its printed proof) on concrete
instances in EXACT ARITHMETIC. Not a reproduction of experiments; a formal check.
  - Use `fractions.Fraction` (never float) for the comparison that decides the result.
  - For the instance indexed by `--seed`: construct ONE concrete instance inside the
    statement's own domain (different seeds -> different instances, via a small seeded
    generator). Evaluate EVERY premise of the exact claim being tested (for a proof step: the
    step's own "if" and every hypothesis in force at that point) in exact arithmetic and report
    `premises_hold` (1 only if all hold). NEVER drop, weaken or skip a premise to obtain a
    violation: a violation on an instance whose premises fail is not a counterexample, and
    when no instance you can construct meets the premises, report `premises_hold: 0` — the
    harness records PREMISE_NOT_MET, which is itself a finding for the referee. Evaluate the
    conclusion exactly and print
    `REFEREE_RESULT {"premises_hold": 1 or 0, "violated": 1 or 0, "lhs": <float>, "rhs": <float>}`.
  - SEARCH WHERE A VIOLATION WOULD BE: hypotheses at their edges (boundary points, degenerate
    or rank-deficient matrices, equality cases, parameters at their limits); a bound claimed
    for all t along a LONG horizon, every step, not a few early ones.
  - An asymptotic claim (O, Omega, Theta, unstated constants) cannot be violated by a finite
    instance: check the explicit proof step you were given, or refuse. A violated proof step
    shows the printed proof is invalid at that step, never that the statement is false.
  - `runs` = the number of instances you offer (the harness runs seeds 0..runs-1).

## gen RELEASED_DATA
WHAT YOU ARE DOING: a RELEASED-DATA RECOMPUTATION — recompute the printed number from files the
authors released in their checkout (your working directory; read-only). Open each file by its
full relative path, written literally in the script (e.g. "results/eval/a.jsonl", never a glob).
Apply only the selection, filtering, averaging and metric the paper itself states; never
regenerate, edit, resample or fabricate data. Nothing is trained. Usually `runs` = 1.

## gen RECONSTRUCTION
WHAT YOU ARE DOING: a PAPER-DERIVED EVALUATION of one experiment the paper specifies, where no
documented command runs it. Where the authors' checkout implements the method, DRIVE THEIR
CODE (import it from the working directory; never copy or edit it) with the paper's stated
data, configuration and metric; otherwise reimplement what the paper states. It will never be
reported as an author reproduction. THE RULE THAT MATTERS MOST: every detail neither the paper
nor the authors' code fixes (a hyperparameter, dataset size, preprocessing, a baseline's
setting) is a DEVIATION you declare; a number from undeclared choices is rejected. Never shrink
the experiment (fewer runs, samples, iterations or a smaller model) to make it faster.
Where the paper and the authors' code DISAGREE (a split, a subsample, a hyperparameter), the paper
is what is being checked: follow the paper, and state the code's difference in `notes` (it is a
finding for the referee).

## verify CERTIFICATE
  1. Does `premises_hold` evaluate EVERY premise of the exact claim tested (for a proof step: the
     step's own condition and every hypothesis in force) — nothing dropped, weakened or asserted
     away? A violation counted on an instance where a premise fails is a REJECTION; an
     unsatisfiable premise must be reported (`premises_hold: 0`), not removed.
  2. Does `claimed_bound` match the paper's relation EXACTLY — constants, direction, quantifier
     order? A different, easier inequality proves nothing about the printed one.
  3. Are the constructed instances admissible, and do they probe the edges where a violation
     would be?
  4. Is the decisive comparison exact (`Fraction`), and is `violated` computed from it?
  5. Is `checked_statement` honest (an asymptotic conclusion is not finitely checkable)?
  6. Is `changes_claim` true for every deviation that alters what the printed claim says (a
     premise, the conclusion, an index range, a definition), and does `literal` separate
     `fails` (premises hold and the conclusion fails as printed) from `undefined` (the printed
     text is not well-defined on the instance, e.g. an index out of range)?

## verify RELEASED_DATA
  1. Does the script open the right released file(s) for THIS printed number, by literal path?
  2. Does it compute the paper's stated quantity (same metric, subset, averaging, units),
     applying only selection the paper states — no editing, resampling or regeneration?
  3. Is the compared output the number the printed target (or the stated relation) is about,
     computed from the files — never a flag computed against the printed number?

## verify RECONSTRUCTION
  1. Is every required ingredient (method, training, dataset, metric, comparison target)
     realized exactly as the paper states it, each backed by a verbatim paper quote?
  2. Did the script fill a detail the paper and the authors' code omit WITHOUT declaring it in
     `deviations`, or shrink the experiment (fewer runs, samples, iterations, a smaller
     model)? Either is a rejection. Where the checkout implements the method, does the script
     call that code rather than a rewrite of it? Where paper and code disagree, does it follow the paper (and say so
     in `notes`)? Following the code there is REVISE; following the paper there is correct.
  3. Does `runs` (or the number of result lines one run prints) match the paper's stated
     replication?

## verify ARITHMETIC
  1. Is each operand's `value` exactly the number printed in its `quote`, and is it the
     quantity the paper's own result is computed from (right row, column, units)?
  2. Is the `expression` the computation the paper's sentence claims (e.g. relative vs absolute
     gain, which baseline is the denominator)? A wrong formula makes a false contradiction.
  3. Is `paper_result` the paper's own printed result of that computation?
