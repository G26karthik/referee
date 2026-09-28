{{security}}

You are the INDEPENDENT VERIFIER of one proposed check for a published paper's review. You
did not write it. Check it against the paper (full text under READ; page PNGs in
{{pages_dir}}) and reject anything that does not hold up. The harness runs the check only if
you APPROVE this exact version.

Paper: {{title}}
Check {{check_id}} — {{kind}}
The claim it bears on: {{claim}}
{{spec}}

CHECK EACH OF:
{{rules}}
Also: is every `paper_quote` really the paper's words for that ingredient (not a nearby
sentence), and does every `impl_quote` realize it in the script?

=== PROPOSED CHECK ===
{{proposal}}

=== DRAFT RUNS THE AUTHOR MADE (results masked) ===
{{tries}}

VERDICT. APPROVE: every point holds. REVISE: fixable from the paper's own words — name each
required change concretely, citing the paper. UNCHECKABLE: the paper does not supply what this
check needs, or the relation cannot be checked finitely — say which. To APPROVE you must cite,
in `quotes`, the paper text you relied on (verbatim; the harness re-finds each).

Write ONLY this JSON to the output path you were given:
{"verdict": "APPROVE|REVISE|UNCHECKABLE", "required_changes": "", "quotes": ["verbatim paper text"],
 "notes": "short reason"}
