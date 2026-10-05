{{security}}

You compare REFEREE's finished review of one paper with ANOTHER REPRODUCTION RECORD of the same paper (for example a
Hugging Face reproduction logbook or a judge's summary of one). REFEREE's decisions are sealed and do not change. The
other record is not ground truth: it is another attempt, with its own data, settings and judgement.

Paper: {{title}}

The other record(s), registered by the operator (Read each in full):
{{refs}}

=== REFEREE'S MAIN CLAIMS AND DECISIONS ===
{{central}}

=== REFEREE'S CHECKS (what ran, on what, with which numbers) ===
{{checks}}

For EACH claim id give one entry:
  - `reference_finding`: what the other record found about this claim, in one or two plain sentences (its verdict label
    and its numbers, attributed to it: "The record reports ...").
  - `quotes`: 1-4 short passages copied VERBATIM from the record that state that finding (the harness re-finds each).
  - `comparable`: "yes" (same data, metric, settings and estimator), "partly" (some differ), or "no".
  - `why`: what differs or matches between the two tests, concretely: the dataset or subset, the metric or estimator,
    the settings, the baselines, the number of seeds, released results audited versus models re-run, the printed
    condition versus a repaired one. A different number from a different test is not a disagreement.
  - `agreement`: "agrees" (comparable tests, same direction), "disagrees" (comparable tests, opposite findings),
    "not_comparable" (the tests differ too much to say), or "not_covered" (the record says nothing about this claim).
  - `source`: a short name for the record (for example "HF logbook arvkevi").
Where several records cover a claim, summarise them in one entry and say how many agree. Never call either side right.

Write ONLY this JSON to the output path you were given:
{"claims": [{"id": "K1", "reference_finding": "", "quotes": [""], "comparable": "yes|partly|no", "why": "",
             "agreement": "agrees|disagrees|not_comparable|not_covered", "source": ""}], "notes": ""}
