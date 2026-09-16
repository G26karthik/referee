#!/usr/bin/env bash
# The fresh eight-paper final run, under ONE consistent stack.
#
# THE SAME GATES, WITHOUT `--auto-audit` AND `--auto-grade`. Those two flags select the
# CLI_SUBPROCESS delegation mode, which spawns `claude` against a separate provider
# account; that account's session limit is what stopped the first attempt at four of
# eight papers. Dropping the flags does not turn delegation off — it selects the manual
# channel, where `review` writes every prompt, exits 2, and resumes once the readings
# exist. Those readings are supplied by isolated subagents of the controlling session
# and sealed through `tools/subagent_accept.py` (parts and syntheses) and `run.py accept`
# (grades and the whole-paper read), both of which record SESSION_SUBAGENT provenance:
# real context isolation, no provable filesystem sandbox, and `delegation.summarise`
# reports the corpus as non-homogeneous rather than describing either mode as the method.
#
# Every gate this system has is OPEN here, including execution, because a route left shut
# by our own configuration is a configuration outcome and not a scientific one. Whether
# anything actually runs is then decided by `backends.authorize` per target, as it should
# be. `SH_EXEC_BACKEND=container` because repository execution requires CONTAINER or
# REMOTE_SESSION isolation and Docker is present and tested on this host.
#
# Usage:  bash tools/final_run.sh [paper.pdf ...]
#         with no arguments it runs all eight, skipping any paper already complete.
set -u

cd "$(dirname "$0")/.."
PY=../.venv/Scripts/python.exe
RUN_DIR=runs_final_2026-09-16

export PYTHONUTF8=1
export SH_PROJECTS_DIR="$RUN_DIR/projects"
export SH_ALLOW_AUTO_AUDIT=1
export SH_ALLOW_GRADING=1
export SH_ALLOW_SUBSTANTIVE_VERDICT=1
export SH_ALLOW_CLAIM_LINKS=1
export SH_ALLOW_ARTIFACT_REVIEW=1
export SH_ALLOW_LITERATURE_SEARCH=1
export SH_ALLOW_LITERATURE_REVIEW=1
export SH_ALLOW_VALIDATION_DESIGN=1
export SH_ALLOW_NETWORK=1
export SH_ALLOW_INSTALL=1
export SH_ALLOW_REPO_EXEC=1
export SH_EXEC_BACKEND=container
# OpenAlex meters its API and this host's free allowance is spent; a 429 from it is an
# UNAUTHENTICATED outcome and not a completed query, so the DECLARED protocol is the two
# indexes that answer. Narrowing it here narrows what `protocol_completed` may claim.
export SH_LITERATURE_PROVIDERS=crossref,arxiv

PAPERS=(
  "papers/5993d35ff0996b52.pdf"
  "papers/ACl.pdf"
  "papers/APT _ ICML.pdf"
  "papers/CVPR.pdf"
  "papers/ICLR.pdf"
  "papers/0c06a98d7c818f6f.pdf"
  "papers/2024_icml_sapg.pdf"
  "papers/sanchez24a_ICML.pdf"
)
if [ "$#" -gt 0 ]; then PAPERS=("$@"); fi

mkdir -p "$RUN_DIR/projects" "$RUN_DIR/logs"
echo "=== final run $(date -u +%Y-%m-%dT%H:%M:%SZ) — ${#PAPERS[@]} paper(s) ==="

for pdf in "${PAPERS[@]}"; do
  slug="$(basename "$pdf" .pdf | tr -cd 'A-Za-z0-9_-')"
  log="$RUN_DIR/logs/$slug.log"
  echo "--- $pdf -> $log"
  # One paper per invocation. `review` resumes its own project, so a paper that already
  # reached a report costs an exit-0 no-op rather than a second set of delegated calls.
  "$PY" run.py review --paper "$pdf" --force-probe >"$log" 2>&1
  code=$?
  echo "    exit=$code  $(tail -1 "$log" | cut -c1-160)"
done

echo "=== done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
