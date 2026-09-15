# Checkpoint B — after question-centric routing

Run identity: `runs_postfix/B_post_question_centric_routing/`
Pre-fix artifacts: `projects/` — **unchanged**, by a digest whose recipe is now recorded
(`tools/corpus_digest.py`), `d5ea07b8c761bd77...` before and after.

Everything below compares against Checkpoint A, which is the same corpus under the same
sealed model outputs. Both runs copy the parsed document, the 32 lens files, the 38 grades
and the 8 whole-paper assessments, and re-derive only the deterministic half, so the
reasoning is identical between them by construction and every difference is attributable
to the code that changed.

Gates for both: `repo_exec=True`, `network=True`, `install=False`, `synthesis=True`,
`diagnostic_mode=False`, `max_targets=3`.

## What Step 5 changed

Routing keyed on `has_value` — had the extractor parsed a number at this address? That
gated every route, so a target with a printed quantity could reach execution and a target
without one could reach nothing, whatever question it raised. It is the right requirement
for "is 61.4 the number their code produces" and it is the wrong requirement for the
questions a referee actually asks.

Three layers now stand where that one boolean stood:

| layer | decides | lives in |
|---|---|---|
| `ReviewQuestion.kind` — 8 values | what KIND of question this is | `harness/questions.py` |
| `ROUTES_FOR_QUESTION` | which routes that kind can reach | `harness/discovery.py` |
| `Comparison.kind` — 4 values | what a result from that route is held against | `harness/comparison.py` |

The kind is derived from the finding's own closed-vocabulary self-classification —
`discrepancy_type`, then `baseline_class`, then the lens — by the same most-specific-first
walk `taxonomy.classify` and the question templates use. No count, number, metric name or
paper identity reaches it.

## The funnel, A to B

| term | A | B | why |
|---|---:|---:|---|
| discovered | 878 | 878 | extraction unchanged |
| structurally checkable | 716 | **764** | +48: three kinds that could reach no route now can |
| warranting an experiment | 43 | **88** | +45, decomposed below |
| targets that launched | 0 | **0** | nothing new executes |
| processes started | 0 | **0** | — |
| admitted as evidence | 0 | 0 | — |
| **warranted targets naming a question** | not recorded | **88 / 88** | the Step 5 invariant |
| **comparison blocked** | n/a | **6** | the new refusal |

### The +45 is two different things, and only 10 of it is the widening

| paper | warranting A -> B | cause |
|---|---|---|
| `acl` | 3 -> 7 | routing: attribution and control-presence questions reached a route |
| `apt-icml` | 2 -> 7 | routing |
| `iclr` | 4 -> 5 | routing |
| `cvpr` | 1 -> 21 | a set-level demotion switched off |
| `2024-icml-sapg` | 1 -> 16 | a set-level demotion switched off |

**The 35 are a second-order effect and they are the documented rule working.**
`_demote_when_a_central_target_is_being_pursued` reports a SUPPORTING target as an open
question rather than running it *while at least one CENTRAL target is being pursued*, and
stands them back up when none is. In both papers the single central executable target
correctly lost a route it should never have had:

- `cvpr` `TGT-BAS-P14127-170-2` is a CONTROL_PRESENCE question — "does the reported
  advantage survive comparison against the missing baseline?". It was reaching
  AUTHOR_CODE_EXECUTION, and re-deriving the number the paper already printed does not
  answer it. It now takes FOCUSED_VALIDATION_EXPERIMENT and refuses INFEASIBLE_SPECIFICATION:
  building the missing baseline needs the missing baseline's specification.
- `2024-icml-sapg` `TGT-CLM-P130-73` is a SPECIFICATION question — "what exactly was
  measured, and over what?" — on a paper with no repository. It was reaching
  INDEPENDENT_RECONSTRUCTION. A reconstruction of a procedure the paper does not state
  measures our reconstruction, so it now refuses INFEASIBLE_ROUTE.

Both refusals are better answers than the routes they replace. The 35 released SUPPORTING
targets are the rule's own stated intent: "a paper whose central claims are unaddressable
must not silently become a paper nothing is checked on."

`max_targets` is still 3, so `warranting` is a judgement and not a spend.

### Checkable went DOWN on three papers

`2024-icml-sapg` 32 -> 31, `5993d35ff0996b52` 11 -> 9, `sanchez24a-icml` 353 -> 351. All
three are SPECIFICATION questions on papers with no repository: nothing to inspect, and a
reconstruction cannot settle what the paper did not specify. They report NO_ROUTE, which
is what they are.

## The new refusal, and what it replaced

Six targets are COMPARISON_BLOCKED — all CENTRAL, all ATTRIBUTION or CONTROL_PRESENCE, all
on the FOCUSED_VALIDATION_EXPERIMENT route, across `acl` (3), `apt-icml` (2) and `iclr` (1).
Each says:

> a between arms comparison would be needed to answer this target and this review could
> not carry one out: the arms are specified and this review has no arithmetic that
> compares them. Every reconciliation this system performs holds a measured quantity
> against one the paper printed; nothing here holds one arm against another, so a run
> would produce two numbers and no verdict.

In Checkpoint A those six reached no route at all and were reported as *"no verification
route this system has would settle the question"* — a statement about `local_exec.reconcile`
dressed as a statement about this review's method inventory. A route exists; the arithmetic
at the end of it does not, and that is what Step 7 and Step 8 are for.

`launched` stayed at 0 across the change. **Widening what can be ROUTED did not widen what
is EXECUTED**, which is the requirement decision 10 imposes while only one of the four
comparisons has arithmetic behind it. Two independent gates enforce it — `may_be_compared`
before a process starts, and the same check again inside `reconcile` as a backstop — and
the provenance ceiling is applied before both, identically for every comparison kind.

## A defect this exposed, and its resolution

`2024-icml-sapg` moved from BLOCKED_ARTIFACT to **PASS_TO_HUMAN_CLEAN**, and `acl` from
BLOCKED_ARTIFACT to PASS_TO_HUMAN_CONCERNS. The cause was `disposition.BLOCKER_FOR_DISPOSITION`,
which omitted ADDRESSING_BLOCKED and NO_ROUTE_AVAILABLE on the theory that a limit of this
harness must never be reported as a property of the paper — correct about the paper, and it
does not follow that the paper should read as though it were checked.

The consequence was that **a paper whose central claim this system has no method for read
PASS_TO_HUMAN_CLEAN** — the same disposition as a paper that was fully checked and came out
clean. That is the defect the disposition field was added to fix, reappearing one level up:
Checkpoint A's finding was six GREEN papers whose central claim was never checkable, and this
was one paper whose central claim was never checkable reading CLEAN as well as GREEN.
COMPARISON_BLOCKED landed the same way, on the same reasoning, the moment Step 5 added it.

> **Resolved.** A fourth blocker class, `BLOCKED_METHOD`, now covers every target
> disposition that means "this review had nothing to check the central claim against":
> `ADDRESSING_BLOCKED`, `REPORTING_BLOCKED`, `NO_ROUTE_AVAILABLE`, `COMPARISON_BLOCKED`.
> None of the four is a finding about the paper, the artifact, or this host — the reason
> text says so explicitly — and none of them may again fall through to
> `PASS_TO_HUMAN_CLEAN`. Placed last in `_BLOCKER_PRECEDENCE`, after RESOURCE: a missing
> GPU is someone else's to supply, a missing method is this review's own gap.
>
> Rerunning this checkpoint after the fix: `0c06a98d7c818f6f` and `2024-icml-sapg` now read
> `BLOCKED_METHOD` (were `PASS_TO_HUMAN_CLEAN`), `acl` reads `BLOCKED_METHOD` (was
> `PASS_TO_HUMAN_CONCERNS`). The other five papers are unchanged — each already carried a
> stronger SPECIFICATION or ARTIFACT blocker on a central target, and METHOD's place at the
> foot of the precedence order means it never displaces those.
>
> The recurring shape of the bug — a new blocked-target disposition added to
> `TARGET_DISPOSITIONS` and never registered in the disposition layer — is now closed by a
> totality self-check: every value in `artifacts.BLOCKED_DISPOSITIONS` must appear in
> `BLOCKER_FOR_DISPOSITION` or in a named, justified `DELIBERATELY_UNCLASSIFIED` (currently
> just `AUTHORIZATION_BLOCKED`, a closed gate being this run's configuration rather than an
> absence of method). A future omission now fails the suite instead of shipping into a
> corpus rerun. See `tests/test_paper_disposition.py`.

## Suite

1769 passed, 0 failed, 0 skipped — 44 new tests in `tests/test_question_centric_routing.py`
plus a net 6 in `tests/test_paper_disposition.py` from the BLOCKED_METHOD fix above.

`modal` 1.5.4 is present in this interpreter, so the 21 sandbox tests that Checkpoint A
recorded as skipped now run and pass.

## One defect found and fixed inside this checkpoint

The first Checkpoint B run produced six COMPARISON_BLOCKED refusals whose sentence had a
hole in it: `Comparison.derive` returned `state="established"` for a BETWEEN_ARMS
comparison with two arms, `admits_verdict` refused it anyway because the kind is not
reconcilable, and the refusal interpolated an empty `reason`. `established` now means "can
be performed" and nothing else; the two ways a between-arms comparison fails —
`arms_unspecified` (the second arm could not be built) and `unsupported` (it could, and
this review has no arithmetic to read the result) — are separate states with separate
sentences. Pinned by a sweep asserting no comparison is ever both established and refused.

## What this checkpoint does NOT show

- Nothing about whether the concerns are correct. No adjudicated ground truth exists.
- Nothing about the authors'-code route working. It still binds on no paper here.
- Nothing about the isolation boundary in production. No sandbox has been leased.
- Nothing about attribution questions being ANSWERED. They now reach a route and are
  refused at a named gate, which is a better record and not an answer.
