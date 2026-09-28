## gen CERTIFICATE
WHAT YOU ARE DOING: an EXACT CERTIFICATE — a small self-contained script that checks ONE
mathematical statement (theorem, lemma, bound, or one step of its printed proof) on concrete
instances in EXACT ARITHMETIC. Not a reproduction of experiments; a formal check.
  - Use `fractions.Fraction` (never float) for the comparison that decides the result.
  - For the instance indexed by `--seed`: construct ONE concrete instance inside the
    statement's own domain (different seeds -> different instances, via a small seeded
    generator); `assert` EVERY stated hypothesis on it in exact arithmetic and let a failed
    assertion raise (the harness reads that as an inadmissible instance, never as a
    counterexample); evaluate the checked relation exactly; print
    `REFEREE_RESULT {"violated": 1 or 0, "lhs": <float>, "rhs": <float>}`.
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
WHAT YOU ARE DOING: an INDEPENDENT RECONSTRUCTION of one small experiment the paper specifies
completely. It is not the authors' code and will never be reported as theirs. THE RULE THAT
MATTERS MOST: invent nothing — no default hyperparameter, dataset size, or preprocessing the
paper does not state; a number from gaps you filled is a statement about your choices. Never
shrink the experiment (fewer runs, samples, iterations or a smaller model) to make it faster.

## verify CERTIFICATE
  1. Does `hypotheses` carry EVERY hypothesis the statement (or, for a proof step, the proof at
     that point) states — nothing missing, nothing weakened?
  2. Does `claimed_bound` match the paper's relation EXACTLY — constants, direction, quantifier
     order? A different, easier inequality proves nothing about the printed one.
  3. Are the constructed instances admissible, and do they probe the edges where a violation
     would be?
  4. Is the decisive comparison exact (`Fraction`), and is `violated` computed from it?
  5. Is `checked_statement` honest (an asymptotic conclusion is not finitely checkable)?

## verify RELEASED_DATA
  1. Does the script open the right released file(s) for THIS printed number, by literal path?
  2. Does it compute the paper's stated quantity (same metric, subset, averaging, units),
     applying only selection the paper states — no editing, resampling or regeneration?
  3. Is the output it prints the number to compare against the printed target?

## verify RECONSTRUCTION
  1. Is every required ingredient (method, training, dataset, metric, comparison target)
     realized exactly as the paper states it, each backed by a verbatim paper quote?
  2. Did the script invent any detail the paper omits, or shrink the experiment (fewer runs,
     samples, iterations, a smaller model)? Either is a rejection.
  3. Does `runs` match the paper's stated replication?

## verify ARITHMETIC
  1. Is each operand's `value` exactly the number printed in its `quote`, and is it the
     quantity the paper's own result is computed from (right row, column, units)?
  2. Is the `expression` the computation the paper's sentence claims (e.g. relative vs absolute
     gain, which baseline is the denominator)? A wrong formula makes a false contradiction.
  3. Is `paper_result` the paper's own printed result of that computation?
