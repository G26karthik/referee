"""Why a delegated reviewer did not produce a result — and whether asking again could
possibly help. Pure: text in, classification out. No I/O, no Config, no model.

The defect this module generalises was real and specific. A hard account-level rate limit
("You've hit your session limit · resets 3:20pm") was one more `AuditDriverError`, so the
controller spent its whole retry budget in nine seconds against a wall that would not move
for hours, and left the paper permanently `waiting` with no recorded reason a reader could
act on. `audit_driver.RateLimited` fixed that ONE case by hand-carving one regex and one
exception class.

One case is not the shape of the problem. A missing CLI, a revoked credential, an
unrecognised flag, a service outage and a truncated JSON object all arrive as the same
non-zero exit with the same stderr channel, and they have three genuinely different right
responses:

  RETRY NOW        the same prompt may well succeed — a truncated response, a socket
                   reset, a timeout. This is the only class that should consume an attempt.
  RETRY LATER      the world has to change first. An account limit, a 429, a 5xx, an
                   overloaded upstream. Retrying inside this process is spending budget to
                   be told the same thing.
  DO NOT RETRY     the same input will fail identically forever — no CLI on PATH, an
                   unknown flag, an authentication refusal, a PDF that will not parse.
                   Retrying is not merely wasteful, it hides the operator's actual fix.

`kind` names WHICH of those it is at a granularity an operator can act on, `retry` says
which of the three, and both are persisted on the case so the reason survives the process.

`python -m harness.failures` runs the self-check.
"""
from __future__ import annotations

import re

# What went wrong, at the granularity an operator's next action differs at.
FAILURE_KINDS = (
    "rate_limited",        # the operator's own account is over its usage limit
    "service_unavailable", # upstream is down, overloaded, or returning 5xx/429
    "transport",           # the connection dropped mid-request
    "timeout",             # the reviewer did not finish inside its budget
    "not_installed",       # no reviewer command exists on this machine
    "unauthenticated",     # a reviewer exists but will not act for this operator
    "bad_invocation",      # the command we issued is not one the reviewer accepts
    "malformed_output",    # the reviewer answered, and the answer was not usable
    "extraction_failed",   # the PDF could not be parsed into a document
    "gate_closed",         # an operator-controlled gate is shut — not a failure at all
    "unknown",             # unclassified; treated as retry-now, the conservative default
)

# The three responses. Anything not `now` must not consume a retry attempt: the whole
# point of separating these is that a bounded budget is spent only where spending it
# could change the answer.
RETRY_POLICIES = ("now", "later", "never")

# One ordered table, most specific pattern first. Order matters and is the only reason
# this is a list rather than a dict: "invalid api key" is an authentication problem even
# though it also contains "invalid", and an "unknown option --allowedTools" is a bad
# invocation even though the word "error" appears beside it.
#
# Deliberately conservative. A pattern that is not confidently one of these is `unknown`
# and retries NOW, because the failure mode of over-classifying is worse than the failure
# mode of one wasted attempt: a genuinely transient failure marked `never` silently
# abandons a paper that would have succeeded on the next try.
_RULES: tuple[tuple[str, str, str], ...] = (
    (r"(session|usage|rate)\s*limit", "rate_limited", "later"),
    (r"\b429\b|too many requests|quota\s*exceeded", "rate_limited", "later"),
    (r"\b5\d\d\b\s*(error|status)?|service unavailable|internal server error"
     r"|overloaded|temporarily unavailable", "service_unavailable", "later"),
    (r"connection (reset|refused|aborted|closed)|econnreset|broken pipe"
     r"|network is unreachable|ssl.*(error|verify)", "transport", "now"),
    (r"timed out|timeout after|deadline exceeded", "timeout", "now"),
    (r"command not found|is not recognized as|no such file or directory"
     r"|executable not found|not installed", "not_installed", "never"),
    (r"unauthori[sz]ed|authentication|not logged in|invalid api key|permission denied"
     r"|forbidden|\b40[13]\b|please (run )?login", "unauthenticated", "never"),
    (r"unknown (option|argument|flag)|unrecognized (option|argument)|invalid choice"
     r"|no such option|usage: ", "bad_invocation", "never"),
    (r"cannot open|not a pdf|damaged|cannot parse.*pdf|no /root object"
     r"|failed to open (the )?(document|pdf)", "extraction_failed", "never"),
    (r"gate (is )?(closed|shut)|not set|set SH_", "gate_closed", "never"),
    (r"no json|not valid json|expecting value|unterminated|truncated"
     r"|did not print|empty (response|output)", "malformed_output", "now"),
)
_COMPILED = tuple((re.compile(p, re.I), kind, policy) for p, kind, policy in _RULES)

_RESET_RE = re.compile(r"resets?\s+([^\n\"'.]{0,40})", re.I)


def reset_hint(text: str) -> str:
    """The reviewer's own words about when it will work again, verbatim and unparsed.

    Free text on purpose. A wall-clock time in an unnamed timezone ("3:20pm
    (Asia/Kolkata)") is a hint for a human, and parsing it into a datetime would invite
    the controller to sleep or poll on a value the upstream never promised to keep.
    """
    m = _RESET_RE.search(text or "")
    return m.group(1).strip() if m else ""


def classify(text: str) -> tuple[str, str, str]:
    """(kind, retry_policy, reset_hint) for one failure's combined stderr/stdout tail.

    Total and deterministic: every input gets a kind, and an unrecognised one is
    `unknown`/`now` rather than an exception or a guess at something more specific.
    """
    t = str(text or "")
    for rx, kind, policy in _COMPILED:
        if rx.search(t):
            return kind, policy, (reset_hint(t) if policy == "later" else "")
    return "unknown", "now", ""


def consumes_attempt(policy: str) -> bool:
    """Only a retry-NOW failure spends part of a bounded budget. See the module docstring."""
    return policy == "now"


if __name__ == "__main__":  # self-check: python -m harness.failures
    cases = [
        ("You've hit your session limit · resets 3:20pm (Asia/Kolkata)",
         "rate_limited", "later", "3:20pm (Asia/Kolkata)"),
        ("HTTP 429 Too Many Requests", "rate_limited", "later", ""),
        ("Error: 503 Service Unavailable", "service_unavailable", "later", ""),
        ("upstream overloaded, try again", "service_unavailable", "later", ""),
        ("ConnectionResetError: [Errno 104] Connection reset by peer", "transport", "now", ""),
        ("the request timed out", "timeout", "now", ""),
        ("'claude' is not recognized as an internal or external command",
         "not_installed", "never", ""),
        ("Error: Unauthorized. Please run `claude login`.", "unauthenticated", "never", ""),
        ("error: unknown option '--allowedTools'", "bad_invocation", "never", ""),
        ("cannot open broken.pdf: damaged or missing", "extraction_failed", "never", ""),
        ("the reviewer printed no JSON at all", "malformed_output", "now", ""),
        ("some entirely novel catastrophe", "unknown", "now", ""),
        ("", "unknown", "now", ""),
    ]
    for text, want_kind, want_policy, want_hint in cases:
        kind, policy, hint = classify(text)
        assert (kind, policy, hint) == (want_kind, want_policy, want_hint), \
            (text, kind, policy, hint)
        assert kind in FAILURE_KINDS and policy in RETRY_POLICIES

    # The property the controller depends on: a bounded retry budget is spent ONLY where
    # spending it could change the answer.
    assert consumes_attempt("now")
    assert not consumes_attempt("later") and not consumes_attempt("never")
    for text, _, policy, _ in cases:
        if policy != "now":
            assert not consumes_attempt(policy), text

    # Ordering, asserted rather than assumed: each of these matches an EARLIER rule too.
    assert classify("invalid api key")[0] == "unauthenticated"
    assert classify("429: usage limit reached")[0] == "rate_limited"
    print("failures self-check OK")
