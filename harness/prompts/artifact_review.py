"""Ask a reader the question the kickoff brief asked for: did the authors implement it right?

`python -m harness.prompts.artifact_review` runs the self-check.

**This is the one reader that is shown CODE.** Every other model in this system reads the
paper; this one reads the paper's method section alongside the authors' pinned checkout and
answers whether the second does what the first says. That question is not answerable from
the paper alone and was never reliably answerable by AST pattern matching either —
`harness/code_audit.py` carried ten such rules, measured over the four repository papers
those rules produced six hits of which five were false, every one because an identifier is
not a semantic category, and nine of the ten are now deleted rather than kept unused.

**It proposes; it establishes nothing.** Its access is the paper, a read-only view of the
pinned checkout, and no execution. Every code citation it writes is relocated by
`artifact_evidence.relocate` — the file must be in the checkout, the quoted text must be in
the file, and it must occur exactly once — and a proposal whose citation does not relocate
is DROPPED, exactly as a lens's finding is dropped when its quotation is not in the paper.
A model statement about code is not artifact evidence by itself.

**And it may not conclude that a result is wrong.** A code or configuration inconsistency
may create a verified concern, establish a reproducibility defect, trigger execution or
focused validation, and become material where a central claim provably depends on it. None
of those is reachable by writing a stronger sentence here: `artifact_evidence` has no
spelling for "the reported result is false", and the strongest authority any proposal from
this reader can be awarded is PAPER_ARTIFACT_MISMATCH, which additionally requires an
addressed paper statement and a bound experiment identity.
"""
from __future__ import annotations

from .audit import SECURITY

# `sonnet` rather than `opus`. The task is located reading — find the line, quote it
# exactly, say what it configures — and the expensive model buys nothing the relocation
# gate does not already guarantee. The two tools are the minimum for reading a checkout
# nobody has mapped: `Grep` to find the file, `Read` to quote it.
ROLE_SPEC = {"role": "authors'-code auditor", "model": "sonnet", "tools": ("Read", "Grep")}

# What a proposal may claim. Closed, because an open field would be a place for "this is
# wrong" to arrive as a category, and the whole point is that reading code does not
# establish that.
CONCERN_KINDS = (
    "PAPER_CODE_MISMATCH",        # the method section says X; this file sets Y
    "MISSING_EXPERIMENT_PATH",    # the paper reports an experiment nothing here runs
    "METRIC_MISMATCH",            # the metric implemented is not the metric named
    "SPLIT_LEAKAGE_CONCERN",      # evaluation data reaches fitting or selection
    "BASELINE_IMPLEMENTATION",    # the comparison arm is not built the way its source builds it
    "DEAD_CLAIMED_MECHANISM",     # a mechanism the paper claims is defined and never called
    "HARD_CODED_RESULT",          # a reported number is a literal rather than a computation
    "SUSPICIOUS_IMPLEMENTATION",  # something else a referee would want to look at
)

_RULES = f"""\
=== WHAT YOU ARE DOING ===
You are reading ONE paper and the authors' OWN released code at ONE pinned commit, and
answering ONE question:

    DOES THIS CODE DO WHAT THE PAPER SAYS IT DOES?

Not "is this good code". Not "is the paper right". Not "would I have done it this way".
The question is whether the released artifact implements the method the paper describes
and produces the numbers the paper reports the way the paper says they were produced.

=== WHAT YOU MAY AND MAY NOT CONCLUDE ===
You may propose that the paper and the code disagree, and you must show both halves.
You may NOT conclude that a reported result is false. Reading code cannot establish that:
a configuration in the repository may not be the configuration the reported run used, a
file may be a leftover, a default may be overridden by a launcher you were not shown. If
you believe a result is wrong, the honest form of that belief is a concern with both
locations quoted, and something else decides what follows from it.

=== EVERY CODE CITATION IS RELOCATED BEFORE IT COUNTS ===
For each concern you give a repository-relative FILE and a VERBATIM QUOTE from it. The
harness then opens that file at the pinned commit and searches for your quote. If the file
is not there, or the quote is not in it, or the quote occurs more than once,
THE CONCERN IS DROPPED. It is not softened and not reported — it is gone. So:

  * quote a line long enough to be unique in the file, and short enough to be one thing;
  * copy it character for character, including the exact spelling of identifiers;
  * never quote from memory, and never reconstruct a line you did not open.

=== AND EVERY PAPER CITATION IS RE-VERIFIED TOO ===
When your concern is that the paper says one thing and the code another, quote the PAPER
sentence verbatim as well. It is searched for the same way.
A paraphrase of the method section is not a citation of it, and a concern whose paper
half is a paraphrase cannot establish a mismatch — it becomes, at most, an observation about the code.

=== SAY WHICH EXPERIMENT, AND SHOW WHERE THAT IS WRITTEN DOWN ===
A repository sets a batch size in a dozen places. "Some config says 32" contradicts
nothing. If your concern is a mismatch, name WHICH experiment or table of the paper the
file you cite configures — and then name the place that SAYS SO, because your reading of
which config goes with which experiment is not itself evidence.

`identity_basis` must be one of:

    paper_names_the_command     the paper itself prints the command or the path
    readme_maps_the_experiment  the checkout's README maps the experiment to the file
    script_passes_the_config    a committed script names both the experiment and the file
    authors_experiment_table    the repository documents its experiments in a table
    auditor_assertion           none of the above; this is your reading

For the middle three, give `identity_file` and `identity_quote`: the harness opens that
file at the pinned commit and searches for the quote exactly as it does for your code
citation. If it does not relocate, the identity is recorded as PARTIAL.

**`auditor_assertion` is a legitimate answer and you should use it when it is true.** A
concern whose identity is your reading is still worth a referee's time — it is recorded as
a concern with both locations verified, not as a demonstrated inconsistency. What is not
acceptable is claiming one of the other four when you have not opened the file that says
so. Nothing is gained by it: the harness checks.

=== WHAT YOU ARE NOT SHOWN, AND WHY ===
You are not shown any finding from any other reader, any severity, any grade, or any
verdict. A reader that knows what somebody else suspects goes looking for it. Read the
code and the method section and say what you find.

=== WHEN NOT TO ANSWER ===
Omit a concern rather than guess. A repository this large will always contain something
that looks odd out of context; a concern you cannot locate precisely is noise that costs a
referee time, and there is no credit here for volume. Zero concerns in a faithful
repository is the correct answer and is reported as such.

=== CONCERN KINDS ===
{chr(10).join('    ' + k for k in CONCERN_KINDS)}"""

_RETURN = """\
Print ONLY this JSON to standard output. Do not write any file, and do not run anything.

{"files_read": <how many files you opened>,
 "concerns": [
   {"kind": "one of the CONCERN KINDS above",
    "title": "one compressed line",
    "statement": "what the code does, and what the paper says, in plain terms",
    "file": "repository-relative path, e.g. configs/cifar100.yaml",
    "code_quote": "VERBATIM line or lines from that file, unique within it",
    "paper_quote": "VERBATIM sentence from the paper, or \\"\\" if this is about the code alone",
    "paper_value": "what the paper states, e.g. 128, or \\"\\"",
    "artifact_value": "what the code sets, e.g. 32, or \\"\\"",
    "experiment_id": "which experiment/table this file configures and how you know, or \\"\\"",
    "identity_basis": "paper_names_the_command | readme_maps_the_experiment | "
                      "script_passes_the_config | authors_experiment_table | "
                      "auditor_assertion",
    "identity_file": "the file that states the experiment-to-config link, or \\"\\"",
    "identity_quote": "VERBATIM line from that file stating the link, or \\"\\"",
    "config_key": "the configuration key artifact_value is the value of, or \\"\\"",
    "counter_explanations": ["the innocent reading, which you must supply"]}
 ],
 "notes": "what you could not check, and why"}"""


def build(title: str, method_text: str, tree_text: str, commit: str,
          repo_url: str = "", repo_path: str = "", reported_text: str = "") -> str:
    """The authors'-code prompt for one paper at one commit.

    Shown the paper's METHOD and EXPERIMENT text rather than the whole paper, plus the
    file tree and the pinned SHA. Deliberately not shown the Abstract's claims: a reader
    given the headline result reads the code looking for the reason it might be wrong,
    and this reader's usefulness depends on it reading the code for what it says.
    """
    return f"""{SECURITY}

You are auditing the authors' OWN released code against the paper that describes it.

Paper: {title or "(title not detected)"}
Repository: {repo_url or "(url not recorded)"}
Pinned commit: {commit or "(unknown — say so and stop)"}
Checkout (read-only, this exact commit): {repo_path or "(not available)"}

{_RULES}

=== WHAT THE PAPER SAYS IT DID ===
{method_text or "(no method text was recovered)"}

=== WHAT THE PAPER REPORTS ===
{reported_text or "(no reported quantities were extracted)"}

=== THE CHECKOUT ===
{tree_text or "(no file listing available)"}

{_RETURN}"""


def _self_check() -> None:
    body = build("A Paper", "We train for 100 epochs.", "train.py\nconfigs/a.yaml",
                 "deadbeef" * 5, repo_url="https://example.invalid/r",
                 repo_path="/tmp/repo")
    assert "DOES THIS CODE DO WHAT THE PAPER SAYS IT DOES?" in body
    assert '"concerns"' in body and "train.py" in body
    # The five rules that bound this reader, each stated to it.
    for rule in ("You may NOT conclude that a reported result is false",
                 "THE CONCERN IS DROPPED",
                 "A paraphrase of the method section is not a citation of it",
                 "Omit a concern rather than guess",
                 "is a legitimate answer and you should use it when it is true"):
        assert rule in body, rule
    # Every identity basis is offered by name, so the auditor cannot invent a sixth.
    for basis in ("paper_names_the_command", "readme_maps_the_experiment",
                  "script_passes_the_config", "authors_experiment_table",
                  "auditor_assertion"):
        assert basis in body, basis
    # It is shown no finding, no severity, no grade and no verdict, and the SIGNATURE is
    # what makes that so — there is no parameter that could carry one.
    import inspect
    params = set(inspect.signature(build).parameters)
    assert params == {"title", "method_text", "tree_text", "commit", "repo_url",
                      "repo_path", "reported_text"}, params
    for banned in ("severity", "finding", "grade", "verdict", "lens"):
        assert banned not in " ".join(params), banned
    # Read-only tools, and no execution among them.
    assert set(ROLE_SPEC["tools"]) == {"Read", "Grep"}, ROLE_SPEC
    for forbidden in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
        assert forbidden not in ROLE_SPEC["tools"], forbidden
    print("harness.prompts.artifact_review self-check ok")


if __name__ == "__main__":
    _self_check()
