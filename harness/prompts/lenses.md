## overclaim
Audit, in this order:
  A. PREMISE. Is the stated reason the method should work actually sound? Distinguish a real
     mechanism from a plausible-sounding story. If the premise would equally "explain" the
     opposite result, it explains nothing — judge how much that threatens the central claim.
  B. GAIN vs NOISE. For each headline delta, compare it against the paper's OWN reported
     variance (std / CI / seed spread) where reported. A claimed 5% inside a ±3% seed band is
     worth recomputing and reporting with both numbers quoted. Missing variance alone is not
     automatically a finding.
  C. BASELINE MISREPRESENTATION. Is the baseline the standard one, tuned as carefully as the
     proposed method? Judge from what the PAPER supplies: the baseline's configuration, its
     cited source, whether tuning is reported. Never assert an external number from memory;
     if the paper does not say how the baseline was obtained, that omission is the finding.
  D. THEOREMS. Does a stated guarantee (bound, rate, consistency) actually cover the setting
     the experiments and claims rely on? Are its hypotheses satisfied where it is invoked?

## protocol
Audit for:
  A. LABEL LEAKAGE. Does any input encode the target? Any preprocessing, normalization,
     feature selection or imputation fitted on the FULL dataset before the split?
  B. TEST-SET CONTAMINATION. Is the test set used for model selection, early stopping,
     checkpoint choice or tuning? "We report the best epoch" against a test set IS tuning on
     test. Ordinary, disclosed selection on a VALIDATION set is not leakage.
  C. SPLIT INTEGRITY. Duplicates across splits; grouped data (same patient/user/document)
     split at the row level; time series split randomly; pretraining corpora that plausibly
     contain the benchmark.
  D. METRIC GAMING. Is the metric right for the claim and the class balance? A threshold tuned
     post hoc; a metric that differs from the one the cited baselines report.
  E. UNSTATED PROCEDURE. If the paper never says how the split was made, how many seeds were
     run, or how hyperparameters were chosen, grade the silence by how much it obscures
     whether the claim is supported.

## confound
Work in two passes.
PASS 1 — classify every variable the paper changed between baseline and proposed arm:
SCIENTIFIC (claimed responsible for the gain), NUISANCE (tuned per arm for fairness), FIXED.
Anything the paper changed but did not classify, you classify.
PASS 2 — attribution:
  A. CO-MOVEMENT. Did the proposed arm receive MORE of anything besides the mechanism —
     compute, epochs, parameters, data, tuning, schedule, optimizer? Judge how much of the
     claimed effect the confound could plausibly explain.
  B. MISSING SINGLE-VARIABLE ABLATION. Name the exact ablation that would isolate the
     mechanism and say whether the paper ran it.
  C. TUNING ASYMMETRY. A larger search space for the proposed method than the baseline can
     manufacture gains; disclosed equal-effort tuning is not a flaw.
  D. COMPONENT COUNT. If N components are bundled and only the bundle is evaluated, the
     paper measured the bundle — note whether its language credits one component alone.

## contradiction
Audit for:
  A. TEXT vs TABLE. For every numeric claim in the prose, find the printed number it
     summarizes and check it. Quote BOTH — the sentence and the table row — and RECOMPUTE the
     claimed delta from the printed numbers rather than eyeballing it.
  B. ABSTRACT vs CONCLUSION vs LIMITATIONS. Does a later section quietly concede something
     the abstract asserts flatly? Quote both.
  C. CHERRY-PICKED BASELINES. Does the comparison set change between tables? Is a metric
     reported where the method wins and omitted where it loses?
  D. ARITHMETIC. Do stated deltas match the numbers they are computed from? Do means match
     per-seed values? Do percentages sum? Use `calculation` for every one of these.
  E. PROOFS vs STATEMENTS. Does a proof actually establish the statement as printed —
     constants, quantifiers, the case split — or a weaker or different one?
