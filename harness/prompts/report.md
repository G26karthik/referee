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
     either way; say what blocked it. Keep what the workflow completed apart from what it
     established.
  4. **Open questions for the authors** — concrete questions that would settle what remains.
RULES (enforced): no accept/reject recommendation, score or verdict on the paper. The words
verified, reproduced, confirmed, validated, replicated, refuted, disproved, counterexample,
contradicted may appear only in a sentence that names the check id (C1, C2, ...) whose status
earns them; a report that breaks this is replaced by the table alone. Write a concern's class
exactly as given (CONFIRMED_FINDING), never as words ("confirmed finding").

Write ONLY this JSON to the output path you were given: {"summary_md": "..."}
