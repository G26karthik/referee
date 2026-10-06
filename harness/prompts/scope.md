{{security}}

You are an INDEPENDENT AUDITOR of what each finished test of one paper's review actually computes. You did not write
these scripts. You judge the code, not the results: the results are not shown to you. The harness re-finds every code
line you cite in the script, and it uses your answer to decide which parts of each claim were tested at all.

Paper: {{title}}   (full text under READ, page-tagged)

=== CLAIMS THAT NEED A FORM (sealed before claims carried one; classify each) ===
{{claims}}

=== THE FINISHED TESTS (Read each script in full before you answer) ===
{{checks}}

1. `forms`: for each claim listed above, its logical form:
   - "universal": a statement about every case (a bound, an inequality, an implication). Finite cases never show it.
   - "existential": one object with a property exists ("can be", "there exists", "X does not imply Y"). One valid
     example that meets every premise shows it.
   - "universal_existential": for EVERY value of a parameter some example exists ("for every k < m there is a model
     ..."). Examples show only the parameter values they cover.
   - "empirical": a property measured on data or simulation, decided over replicates.
   - "deterministic": an exact quantity computed from a fixed configuration (a parameter count, a size, a closed form).
   - "compatibility": a component can be integrated, run or trained.
   A theorem-type claim takes one of the first three; a performance or value claim one of empirical or deterministic; an
   engineering claim compatibility. Give `quote`: the paper's words, verbatim, that fix the form (the quantifier).

2. `checks`: for EACH test listed, one entry:
   - `covers`: one entry for EVERY item of the test's `covers` list, copied exactly:
       "computed": the script computes this item itself. `code`: the line(s) of the script, verbatim, that compute it.
       "transfer": the script computes another item, and the item follows only by an argument (for example "the same
         proof with quantity B in place of quantity A"). `argument`: that argument, in one or two sentences. A transfer is
         recorded and shown; it is NOT counted as tested.
       "not_computed": the script does not compute it. `why`: what the script computes instead.
     Be strict. A bound on quantity B is not computed by code that computes only quantity A. A theorem is not computed
     by a script that checks one printed example. A property is not computed if the script never evaluates it (for
     example "not P" when P itself is never computed).
   - `witness` (exact-arithmetic tests only): does the script BUILD EXAMPLES of an existence statement, so that
     `violated` = 1 means "this constructed instance is not a valid example" (not "this instance breaks a universal
     statement")? {"is_witness": true or false, "code": "the verbatim line that computes `violated`", "stages": [...]}.
     Judge each unit the script prints (listed under `stages`: each stage, and `reading:<name>` for the lines of one
     named reading). `stages` names only the units that BUILD examples: a stage (all its lines), or `reading:<name>`
     (only that reading's lines). A unit whose `violated` = 1 means "no example exists on this admissible instance" (for
     every model, every choice), or that the text as printed fails, TESTS the statement: leave it out, even when other
     units build examples. Leave `stages` empty only when the script prints one unit.
   - `exact`: the stages whose compared values are an EXACT function of the configuration, the same for every seed and
     every data draw (a parameter count, a model size). For each: {"stage": "<stage name>", "code": "<the verbatim line
     that computes the compared value>"}. A timing, an error on data, anything trained or sampled is NOT exact. Leave the
     list empty when no stage is exact.
   - `outputs`: for each compared output named in the test's relation or metric (under each reading): {"name": "",
     "reading": "<reading name, or empty>", "definition": "what the number is, in plain words", "unit": "", "aggregation":
     "how it is pooled or averaged (over thresholds, folds, seeds, methods)", "code": "<the verbatim line that computes it>"}.
     Name the quantity exactly: an average of one statistic is not another statistic; a relative error is not an
     absolute one.

Write ONLY this JSON to the output path you were given:
{"forms": [{"id": "K1", "form": "", "quote": "verbatim"}],
 "checks": [{"id": "C1", "covers": [{"item": "", "how": "computed|transfer|not_computed", "code": "", "argument": "", "why": ""}],
             "witness": {"is_witness": false, "code": "", "stages": []}, "exact": [{"stage": "", "code": ""}],
             "outputs": [{"name": "", "reading": "", "definition": "", "unit": "", "aggregation": "", "code": ""}]}],
 "notes": ""}
