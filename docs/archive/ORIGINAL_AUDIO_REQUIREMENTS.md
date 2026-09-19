# Requirements stated in the original audio

Derived from `docs/ORIGINAL_AUDIO_FEEDBACK_TRANSCRIPT.md`, the 2026-08-26 kickoff
recording, transcribed locally on 2026-09-16. **Every requirement below carries the
transcript passage it came from**, so that a reader can check the derivation and a human
can later verify the passage against the audio itself.

Evidence grade, used in the closure matrix:

- **AUDIO** — stated in the recording, quoted here.
- **AUDIO (ASR-uncertain)** — stated, but the wording depends on a term the transcript
  garbled. Meaning is clear; exact phrasing is not.

Nothing in this document is graded higher than the transcript supports. A machine
transcript is not a human-verified one, and the requirements whose force depends on exact
wording are marked.

---

## R1. The system is a first-round reviewer sitting between the venue and the referee

> "the bigger problem at hand is these venues are getting a lot of papers ... they don't
> have the bandwidth to review. what if you created a system that sits in between them and
> then does the first round of review"

Grade: **AUDIO.** This is the origin of the first-pass framing and it predates every later
discussion of it. It also settles a framing question directly: the product is a screen that
runs *before* a human referee, not a tool the referee operates.

**Status in the product: RESOLVED.** The pipeline is defined as first-pass throughout, and
the venue decision is outside the system.

---

## R2. Review is not reproduction. Question the paper itself

> "so like entirely we have to question the paper also, in a sense. Exactly. Not just
> reproducibility"

and, on what to look for:

> "there are kind of subtle leakages like the way they perform the experiments they
> actually leaked the label ... the protocol was not proper, they are evaluating the wrong
> thing, they are not asking or answering obvious questions"

Grade: **AUDIO.** The phrase "not just reproducibility" is explicit and is the supervisor
agreeing with the engineer's own summary.

**Status: RESOLVED.** Reproduction is one of eight declared routes; the four lenses look
for overclaiming, protocol defects, confounds and contradictions.

---

## R3. The two-changes confound is the named example of what to catch

> "suppose in one paper they changed two things, right? And then they say, okay, one of
> those things is our method. So gain in performance is because of our method. But did they
> actually verify that the gain is because of their method or the other thing that they
> changed?"

Grade: **AUDIO.**

**Status: RESOLVED as a capability, and it is the strongest case in the evaluation.** This
is precisely the WeatherGen case: augmentation policy and loss weighting moved together,
and the review asks whether the gain is attributable to the mechanism the paper credits.
The `confound` lens exists for this class.

---

## R4. Catch results that are inflated behind an intuitive theory

> "if I was getting 0.1% gain, I will say I am getting 5 percent gain ... that is doctoring
> the results and nobody is going to question it because the theory makes a lot of
> intuitive sense"

Grade: **AUDIO.**

**Status: PARTIAL.** The `overclaim` lens proposes such concerns and quotation verification
grounds them, but establishing that a stated gain is inflated requires either the paper's
own arithmetic to contradict itself or an admissible execution. On the evaluated corpus
neither occurred, so this capability is demonstrated in the proposing half and not in the
establishing half.

---

## R5. The report must be shorter than the paper. One to two pages

> "for these three papers the report should not be bigger than the paper itself okay that's
> very very important ... you can't say that you are improving or easing my work but then
> throw 10 pages of report ... it's better I read that paper itself"

and later:

> "create a concise report ... a paper should go in, a report should be an output, a
> one-pager two-pager report"

Grade: **AUDIO.** Stated twice, and marked "very very important" by the speaker.

**Status: RESOLVED and measured.** The reviewer report is bounded by construction (a
per-category cap, a global finding cap, a cap on every other section) and runs about
8.9 KB per paper on the evaluated corpus, two to three printed pages, against 13 KB of
machine report and 115 KB of ledger.

---

## R6. Red flags, yellow flags, greens we do not care about

> "these are the things, what are the red flags, what are the yellow flags, what are the
> greens we don't care about"

Grade: **AUDIO.** The three-colour triage vocabulary is the customer's own words, not an
invention of the implementation.

**Status: RESOLVED, with one deliberate departure that must be stated in the paper.** The
colours exist, but the implementation demoted them out of the review's headline: the
finding set and its resolution states are the result, and the colour is printed under scope
as a routing decision. That is a change from what the audio describes, made because a
colour cannot carry what four independent axes carry. The paper should own that as a design
decision rather than present the colours as the requested output.

---

## R7. Only run an experiment where the authors missed something obvious

> "we will not redesign all the experiments. we will only design experiment in cases where
> whatever experiments they have done and they have missed something very obvious ... only
> in that cases we will design our experiments, otherwise we will not do the experiments"

and:

> "our initial round of review will tell us whether we should trigger our own experiment
> planner or not"

Grade: **AUDIO.**

**Status: RESOLVED.** This is exactly the experiment-necessity decision: the review's own
findings decide whether an experiment is warranted, `NO_EXPERIMENT_NEEDED` is a successful
outcome, and the planner records why a run was justified as well as why it was refused.

---

## R8. Publish the architecture. Do not publish the prompts

> "we can draw a diagram and show, like, these are the different agents which are
> communicating with each other, and we will not give the exact prompts"

Grade: **AUDIO.** This is the origin of the disclosure policy, and it is now verified from
the recording rather than inferred.

**Status: RESOLVED.** The manuscript describes each model component by role, input
visibility, output schema and authority boundary, and reproduces no prompt text.

---

## R9. Target a Q1 venue

> "when it is that paper targeted, for q1 avenues it should go for q1 avenues"

Grade: **AUDIO (ASR-uncertain)** — "avenues" is certainly "venues".

**Status: OPEN, and it is a decision rather than work.** The manuscript is currently
venue-neutral. No Q1 journal template has been selected.

---

## R10. Several agents with different tool access, some cut off from the internet

> "what kind of agent should be there, why they should be there, and then each agent what
> kind of tool it should have access to ... some might should have access to literature
> search, some should be able to read papers, some should be totally cut off from the
> internet"

Grade: **AUDIO.**

**Status: RESOLVED, and it is one of the better-implemented requirements.** Each lens runs
as its own confined subprocess with an explicit allowed-tool set and a denied-tool policy;
the grader runs with zero tools, no filesystem access and no operator configuration; the
whole-paper reader likewise. "Totally cut off" is literally implemented for two of the six
roles.

---

## R11. The pipeline order the supervisor asked for

> "the first thing you would like to run is just checking the paper for obvious mistakes
> like there can be a pure contradiction in the paper itself ... they mentioned something
> in the abstract, they are mentioning something else in the conclusion ... or they are
> showing something in the table and they are making some other conclusion ... then it can
> be like novelty search ... then you finally get to the reproduction part ... and you can
> also check, like, if they have given the codebase, did they actually implement the code
> correctly, that can be one agent's job"

Grade: **AUDIO.** This is a four-stage order, stated explicitly:

| # | requested stage | status |
|---|---|---|
| 1 | obvious internal contradictions, abstract vs conclusion, table vs conclusion | **RESOLVED** — the `contradiction` lens plus the deterministic integrity layer |
| 2 | novelty search: did someone already do this | **IMPLEMENTED as a bounded prior-art investigation**, which is the honest form of the request: it may raise a concern about earlier work and can never report that a contribution is new |
| 3 | reproduction, and what the difference is | **PARTIAL** — implemented end to end, never completed on a real paper |
| 4 | did they implement the code correctly | **OPEN as a route.** Real static analysis exists and runs on every clone, but its output cannot become evidence |

**All four stages are now capabilities, and two of them are deliberately weaker than the
words the audio used.** Stage 4 became the artifact route: `ARTIFACT_INSPECTION` is
executable, `ARTIFACT_EVIDENCE` is reachable, and what it establishes is what the RELEASED
CODE does — never that a reported result is wrong. Stage 2 became a bounded prior-art
investigation: it searches public indexes for earlier work bearing on the paper's own
novelty claims, and it establishes no novelty, because the literature a bounded search
does not reach is not enumerable.

**That gap between the request and the capability is the honest part, not a shortfall.**
"Novelty search: did someone already do this" is answerable in one direction only. Finding
an earlier work is a concern a referee must adjudicate; finding none is a statement about
a search. The route is built so the stronger claim is inexpressible rather than merely
discouraged — see CLAUDE.md invariant 36 and `docs/LITERATURE_MEASUREMENT.md`.

---

## R12. Be extra careful that a non-matching reproduction is not our own bug

> "suppose you reproduced a method and the results are not matching whatever is mentioned
> in the paper, then in those cases we have to be extra sure that the implementation is
> correct or not ... review it multiple times and you can use some different models"

Grade: **AUDIO.**

**Status: RESOLVED, and implemented more strongly than asked.** The requirement asks for
careful review; the implementation makes it structural. A reconstruction is written by one
model and checked by a *separate* verifier in a distinct context, every required ingredient
must bind to both a paper locator and an implementation locator, and the conformance check
re-verifies the claimed binding against the actual generated text rather than trusting the
generator's self-report. Separately, infrastructure failures can never produce a failed
reproduction.

---

## R13. Avoid compute-heavy papers. Local first, then hosted compute

> "don't take a paper with LLM related thing because that is gonna require insane amount of
> compute ... pick something related to computer vision or federated learning ... initially
> you can build a pipeline on your local PC, once you're finalised and you think you need
> compute, let us know, we will give you access to GCP"

and

> "for the first paper choose a paper that you can run on your local machine, that's the
> fastest way you can iterate ... once that harness is ready we will run that harness
> either on Modal or some GPU compute or GCP"

Grade: **AUDIO (ASR-uncertain)** on the provider names.

**Status: PARTIAL.** The backend abstraction covers local, container and a remote leased
sandbox, and resource insufficiency is a first-class refusal rather than a failure. No
hosted backend has ever been leased, and the container backend has never run.

---

## R14. Three papers first, drawn at random from the screened 200

> "are you just picking three papers at random from these 200? yes. okay, it doesn't really
> matter, you can pick first three papers or random, doesn't matter"

and

> "we can start experimenting with three papers initially"

Grade: **AUDIO.**

**Status: OPEN, and it is a real divergence that the paper must state.** A three-paper
random draw from the 200 exists on disk and was executed with a recorded seed:
`sample_3` = `3f768a63fa8b8d9d`, `32dec1cdb1e7a0a0`, `a8c4c1ad09f5311e`
(`data/exports/sampling_manifest.json`, seed 42, drawn 2026-09-01). **None of those three
papers is in the evaluated eight.** The eight-paper corpus was assembled separately to
exercise heterogeneous review paths, and none of the eight appears in the eligible pool,
the 200, or the 3.

That is defensible as a systems evaluation and it is not what was asked for at kickoff. The
manuscript must show the two tracks as disjoint and say plainly that the prevalence track
is reserved and unexecuted.

---

## R15. Papers were pre-filtered for reproducibility on free-tier compute

> "the filter I kept for the papers actually included the compute requirement, so the
> papers which were chosen at the end are all like freely reproducible and Kaggle free
> tier"

Grade: **AUDIO.** Stated by the engineer about the screening pipeline, and confirmed by the
existence of exclusion rules in `data/exports/screening_statistics.csv`.

**Status: PARTIAL.** The eligibility rules are recorded and the counts are verifiable, but
the compute-requirement rule has not been traced to its rule id in this phase. The
manuscript should not describe the eligibility criteria until it has.

---

## Summary

| req | subject | status |
|---|---|---|
| R1 | first-round reviewer between venue and referee | RESOLVED |
| R2 | question the paper, not just reproduce it | RESOLVED |
| R3 | the two-changes confound | RESOLVED |
| R4 | inflated gains behind an intuitive theory | PARTIAL |
| R5 | report shorter than the paper, 1-2 pages | RESOLVED |
| R6 | red / yellow / green | RESOLVED, with a stated departure |
| R7 | experiment only where something obvious is missing | RESOLVED |
| R8 | publish the architecture, not the prompts | RESOLVED |
| R9 | target a Q1 venue | OPEN, a decision |
| R10 | per-agent tool access, some cut off entirely | RESOLVED |
| R11.1 | obvious contradictions | RESOLVED |
| R11.2 | **novelty search** | **OPEN, no code** |
| R11.3 | reproduction and the difference | PARTIAL |
| R11.4 | **did they implement the code correctly** | **OPEN as a route** |
| R12 | be sure a mismatch is not our bug | RESOLVED |
| R13 | avoid compute-heavy papers, local then hosted | PARTIAL |
| R14 | **three random papers from the 200** | **OPEN, diverged** |
| R15 | pre-filtered for free-tier reproducibility | PARTIAL |

Eight resolved, four partial, four open, one departure. The four open items are the two
missing pipeline stages the audio asked for (R11.2, R11.4), the corpus divergence (R14),
and a venue decision (R9).
