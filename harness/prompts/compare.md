{{security}}

You compare REFEREE's finished review of one paper with ANOTHER REPRODUCTION RECORD of the same paper (for example a
Hugging Face reproduction logbook or a judge's summary of one). REFEREE's decisions are sealed and do not change. The
other record is not ground truth: it is another attempt, with its own data, settings and judgement. Never force the two
to agree, and never call a run that executed a scientific reproduction.

Paper: {{title}}

The other record(s), registered by the operator (Read each in full):
{{refs}}

=== REFEREE'S MAIN CLAIMS AND DECISIONS ===
{{central}}

=== REFEREE'S CHECKS (what ran, on what, with which numbers; what each compared output is, with its unit) ===
{{checks}}

WRITE in plain English close to ASD-STE100 Simplified Technical English: short complete sentences, active voice, no
dashes as punctuation, no claim or test codes (K2, C3) in your text, no recommendation and no next step.

MATCH MAGNITUDES AND SCOPE. Set a number of the record beside a REFEREE number only when both measure the same quantity
(same estimator, same unit, same aggregation, same data or subset). A standard error is not a variance; a ratio of
bootstrap estimates is not a difference of standard errors; a judge's tally is not a measurement. Where the quantities
differ, say what each one is and do not compare them.

Per paper:
  - `hf_setup` (at most 70 words): the record's data, model, baselines, seeds and metric, as the record states them.
  - `differences` (at most 70 words): what differs between the two sets of tests, or why a test did not run.
  - `overall` (at most 80 words): what the two records together support, and the limits of that conclusion.
For EACH claim id give one entry:
  - `entry`: the record entry you selected as closest in scope: {"space": "its Space or logbook id", "revision": "its
    revision or sha, as the record gives it"}; empty strings if the record says nothing about the claim.
  - `hf_verdict`: the record's verdict on this claim with its tally, in the record's own words (for example "11 of 13
    verified, 1 falsified, 1 inconclusive"); "not covered" when the record says nothing.
  - `hf_measurement` (at most 35 words): what the record measured, at what scale and with how many repeats.
  - `quotes`: 1-4 short passages copied VERBATIM from the record that state its finding (the harness re-finds each).
  - `comparable`: "yes" (same data, metric, settings and estimator), "partly" (some differ), or "no".
  - `agreement`: "agrees" (comparable tests, same direction), "disagrees" (comparable tests, opposite findings),
    "not_comparable" (the tests differ too much to say), or "not_covered" (the record says nothing about this claim).
  - `supports` (at most 35 words): for this claim, what the evidence of both records supports, within matched scope.
  - `source`: a short name for the record.
Where several record entries cover a claim, summarise them in one entry and give the tally. Never call either side right.

Write ONLY this JSON to the output path you were given:
{"hf_setup": "", "differences": "", "overall": "",
 "claims": [{"id": "K1", "entry": {"space": "", "revision": ""}, "hf_verdict": "", "hf_measurement": "", "quotes": [""],
             "comparable": "yes|partly|no", "agreement": "agrees|disagrees|not_comparable|not_covered", "supports": "",
             "source": ""}], "notes": ""}
