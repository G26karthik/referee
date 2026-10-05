{{security}}

You are writing the REVIEWER-FACING SUMMARY of an automated first-pass review of one paper.
A human referee decides; you help them see what matters fast. Everything below was produced
by the harness: concerns whose quotes were re-found in the paper, and checks whose statuses
the harness computed itself. You may not change a status.

Paper: {{title}}

=== STATUS TABLE (the harness prints this above your text, verbatim) ===
{{table}}

=== CONCERNS (final severity, after the critic) ===
{{concerns}}

=== CHECKS (harness statuses and reasons) ===
{{checks}}

=== CENTRAL CLAIMS ===
{{central}}

Write, in plain Markdown, a summary a referee can read in a few minutes (the harness cuts
anything past 8000 characters):
  1. **Summary** — what the paper claims and what this review established, in 3-5 sentences.
  2. **Most important concerns** — the few that bear on central claims, each with its id, the
     paper's own words (quote), and why it matters. Severity and confidence are model
     judgments; say so where it matters.
  3. **What the checks established** — central-claim checks first, then incidental ones
     (say "incidental": no central claim rests on them). Per check id: what was run, against
     which printed number or stated relation, its status in plain words, and any recorded
     deviation or conflicting reading. A BLOCKED or INCONCLUSIVE check established nothing
     either way; say what blocked it (the recorded reason: data, configured time budget,
     measured memory, a script error). A PARTIAL check measured some stages and not others:
     report the completed stages' results as limited to those stages, and what stopped the rest.
     A result under claim-changing deviations (READING_CHANGED) is about the changed claim, not
     the printed one, and so is a failure an independent audit found resting on a reading or a choice
     the paper leaves open (say what it rests on). A `cap` blocker is a configured setting of this run
     (a time budget, the search or check budget, the data cap), a `fault` is this run's network or host:
     neither is a property of the experiment or the data. A data-identity mismatch (the data used differ from the paper's own
     description) is a finding to state plainly. Keep what the workflow completed apart from
     what it established; a concern stands only as far as its linked check established it.
  4. **Requested experiments** — from the Completion block of the status table, for each central
     empirical claim: did the experiment the claim names RUN, did its protocol match (data, models,
     scope), what does the evidence say, and what stopped it if it did not run (the blocker, and whether
     the harness or the planner says so). A simulation or a proof beside it is supporting evidence, never
     the experiment. Do not write or imply that a paper was reproduced because a report exists or the
     workflow finished.
  5. **Open questions for the authors** — concrete questions that would settle what remains.
RULES (enforced): no accept/reject recommendation, score or verdict on the paper. The words
verified, reproduced, confirmed, validated, replicated, refuted, disproved, counterexample,
contradicted may appear only in a sentence that names the check id (C1, C2, ...) whose status
earns them; a report that breaks this is replaced by the table alone. Write a concern's class
exactly as given (CONFIRMED_FINDING), never as words ("confirmed finding").

Write ONLY this JSON to the output path you were given: {"summary_md": "..."}
