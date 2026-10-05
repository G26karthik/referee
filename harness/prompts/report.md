{{security}}

You write the PLAIN-LANGUAGE PARTS of a reviewer-facing report on one paper. A human referee decides; they must
understand the paper's claims, what REFEREE tested and what it found, without first reading the paper. Everything
that decides is already computed by the harness and printed in the draft below: each claim's decision and reason,
what ran, the numbers, the counts, what was not tested. You may not change a decision, add a finding or add a number.

Paper: {{title}}   (full text under READ, page-tagged)

=== THE DRAFT REPORT (the harness prints all of this; your parts go where it says "no overview" and under each claim) ===
{{draft}}

=== THE MAIN CLAIMS (ids, decisions, the checks behind them) ===
{{central}}

=== THE CHECKS (statuses, reasons, numbers, deviations) ===
{{checks}}

WRITE, in plain English close to ASD-STE100 Simplified Technical English:
  - Short sentences (at most 20 words). One idea per sentence. Active voice. Common words.
  - Define every technical term the first time you use it, in a few words, or list it under `terms`.
  - Put the finding first, then its limits.
  - Use only numbers that appear in the draft or the checks (copy them with the same digits). Do not compute new
    numbers (no new percentages, ratios or differences). Say "most", "all", "some" instead.
  - Never write a check code (C3) or a claim code (K2) as a heading; you may cite one in parentheses as evidence.
  - Do not use the words verified, reproduced, confirmed, validated, replicated, refuted, disproved, counterexample or
    contradicted: the decision line already says what the harness decided. Say "the test supports", "the test did
    not support", "no case violated", "the test could not run".
  - Missing evidence is not evidence against a claim. A blocked or undecided test shows nothing either way. A test on
    finite cases never proves a theorem. A failed proof step does not show the theorem is false.

1. `overview` (at most 130 words): what problem the paper addresses, what it proposes, and how it supports its claims
   (experiments, proofs). No judgement.
2. `claims`: for EACH claim id, an `explanation` (at most 120 words) of why the decision came out as it did: what was
   tested (data, method, metric, scope) in plain words, what the result means, and the specific limit or reason
   (which settings were not run, what changed from the paper, which reading the result depends on, what blocked it).
3. `terms`: up to 10 technical terms a reviewer needs, each with a one-sentence definition.
4. `open_questions`: up to 4 concrete questions for the authors that would settle what remains open.

Write ONLY this JSON to the output path you were given:
{"overview": "", "claims": [{"id": "K1", "explanation": ""}], "terms": [{"term": "", "definition": ""}],
 "open_questions": [""]}
