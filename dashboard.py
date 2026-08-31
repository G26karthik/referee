#!/usr/bin/env python3
"""Local live-monitoring dashboard for the single-harness research pipeline.

A single self-contained, stdlib-only web server that observes (read-only) the
harness state under ``projects/<pid>/`` and renders a shadcn-style dark
dashboard:

  - LEFT panel  : all sessions grouped by pipeline stage (Literature, Ideation,
                  Grounding Tournament, Experiments, Paper, Review, ...), each a
                  collapsible group with a count badge.
  - RIGHT panel : the selected session's live logs + final output when done.

A "session" is one unit of harness work, derived from the on-disk state:
  * every completed ``research_log.jsonl`` entry  -> a done session,
  * the currently-active ``project.phase`` (while ``status == active``) -> a
    running session with a live spinner,
  * for the grounding tournament, one session PER candidate (read from
    ``candidates/<slot>/``) so the K parallel chains each show their own row,
  * optionally, extra raw log files passed via ``--tail`` / ``$SH_DASH_TAIL``
    are surfaced as verbatim-tail sessions.

Endpoints (polled by the page every ~1.5s):
  GET /                       -> the embedded single-page HTML/CSS/JS dashboard
  GET /api/sessions           -> the grouped session list (JSON)
  GET /api/log?id=<sid>       -> the current log/output text for one session

Run:
  python dashboard.py [--port 8787] [--projects DIR] [--tail FILE ...]

This is a pure read-only observer. It never writes to or mutates harness state.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# Stage taxonomy: map harness phase / log-type -> a human left-panel group.
# Order here is the display order of the groups.
# --------------------------------------------------------------------------- #
GROUP_ORDER = [
    "Direction",
    "Literature",
    "Ideation",
    "Grounding Tournament",
    "Grounding",
    "Experiments",
    "Analysis",
    "Paper",
    "Review",
    "Checkpoints",
    "Raw logs",
    "Other",
]

# base "phase" token (before any ":") -> group label
_PHASE_GROUP = {
    "created": "Direction",
    "direction": "Direction",
    "literature": "Literature",
    "ideation": "Ideation",
    "tournament": "Grounding Tournament",
    "grounding": "Grounding",
    "hypothesis": "Grounding",
    "design": "Grounding",
    "experiment": "Experiments",
    "ingest": "Ingestion",
    "audit": "Audit",
    "report": "Report",
    "results_critic": "Analysis",
    "analysis": "Analysis",
    "paper": "Paper",
    "review": "Review",
    "checkpoint": "Checkpoints",
    "finalize": "Direction",
    "done": "Direction",
}


def _group_for_phase(phase: str) -> str:
    base = (phase or "").split(":", 1)[0]
    return _PHASE_GROUP.get(base, "Other")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# A project counts as genuinely "running" only if a live node process for it
# exists, OR its project.json was touched within this many seconds. Many old
# projects are still status==active because they died/finished without
# finalizing — recency + a live-process probe keep them from showing as
# perpetually running.
LIVE_RECENCY_S = 120


def _parse_ts(ts: str | None) -> float | None:
    """Parse the harness UTC timestamp (YYYY-MM-DDTHH:MM:SSZ) to an epoch float."""
    if not ts:
        return None
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


def _is_live(pid: str, updated_at: str | None) -> bool:
    """Authoritative running check for a project (read-only, kills nothing):
      (a) a live OS node process for this pid (pgrep -f matches the project id
          the node runs as `... run.py node <name> --project <pid>`); else
      (b) project.json.updated_at within LIVE_RECENCY_S; else NOT running.
    Falls back to recency alone if pgrep is unavailable."""
    if pid and shutil.which("pgrep"):
        try:
            # -f matches the full command line; the pid is a unique token in it.
            r = subprocess.run(
                ["pgrep", "-f", pid],
                capture_output=True, text=True, timeout=2,
            )
            # exclude our own dashboard process from a stray match on the pid token
            hits = [ln for ln in r.stdout.split() if ln.strip()
                    and int(ln) != os.getpid()]
            if hits:
                return True
        except Exception:
            pass  # fall through to recency
    ts = _parse_ts(updated_at)
    if ts is None:
        return False
    return (time.time() - ts) <= LIVE_RECENCY_S


def _short(s: Any, n: int = 90) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# State reading (read-only). No dependency on the harness package so the
# dashboard runs even if imports/env are unavailable.
# --------------------------------------------------------------------------- #
def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_log(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return out
    return out


def _tail_lines(p: Path, n: int = 400) -> str:
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"(could not read {p}: {e})"
    lines = txt.splitlines()
    if len(lines) > n:
        lines = ["… (%d earlier lines omitted) …" % (len(lines) - n)] + lines[-n:]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Log-entry -> headline string (the "final output" a done session shows).
# --------------------------------------------------------------------------- #
def _headline(entry: dict) -> str:
    t = entry.get("type", "")
    h = entry.get("headers", {}) or {}
    try:
        if t == "direction_doc":
            return f"vision: {_short(h.get('vision'), 100)}  ({h.get('n_sub', '?')} sub-capabilities)"
        if t == "literature_round":
            return (f"round {h.get('round', '?')} · {h.get('papers', '?')} papers, "
                    f"{h.get('candidate_gaps', h.get('gaps', '?'))} gaps "
                    f"(focus: {_short(h.get('focus'), 50)})")
        if t == "paper_summary":
            return f"{_short(h.get('title'), 80)}  ({h.get('gaps_so_far', '?')} gaps so far)"
        if t == "literature_candidates":
            return f"{h.get('count', '?')} candidate gaps shortlisted"
        if t == "gap_list":
            return f"{h.get('count', '?')} novel gaps"
        if t == "idea_chosen":
            return f"“{_short(h.get('title'), 90)}”  ({h.get('upside', '')})"
        if t == "hypothesis_chain":
            return f"hypothesis chain · {h.get('n_steps', '?')} steps  [{h.get('idea_id', '')}]"
        if t == "study_spec":
            return f"{_short(h.get('goal'), 100)}  [{h.get('idea_id', '')}]"
        if t == "tournament_summary" or t == "tournament":
            w = h.get('winner_verdict', '?')
            return (f"winner {h.get('winner_id', '?')} → {w} "
                    f"({h.get('candidates', '?')} candidates"
                    + (", ALL FAILED" if h.get('all_failed') else "") + ")")
        if t == "run_report":
            return (f"{h.get('experiment_id', '')} · {h.get('scale', '')} → "
                    f"{h.get('status', '?')}" + ("  [STUB]" if h.get('stub') else ""))
        if t == "results_analysis":
            return (f"{h.get('experiment_id', '')} → {h.get('verdict', '?')} "
                    f"(matches prediction: {h.get('matches', '?')})")
        if t == "paper_ingested":
            return (f"{h.get('title', '?')[:60]} · {h.get('sections', 0)} sections · "
                    f"{h.get('tables', 0)} tables · {h.get('claims', 0)} claims")
        if t == "audit_lens":
            return f"{h.get('lens', '?')} → {h.get('findings', 0)} findings"
        if t == "eval_report":
            return f"{h.get('verdict', '?')} · {h.get('findings', 0)} findings"
        if t == "paper_draft":
            return f"draft {'(revised, rev %s)' % h.get('revision') if h.get('revision') else '(written)'}"
        if t == "review":
            return f"{h.get('persona', 'reviewer')} → {h.get('rec', '?')} (avg {h.get('avg', '?')})"
        if t == "checkpoint":
            return f"{h.get('checkpoint', '')} → {h.get('decision', '?')}"
    except Exception:
        pass
    # generic fallback: compact the headers dict
    if h:
        return _short(", ".join(f"{k}={v}" for k, v in h.items()), 120)
    return t or "(artifact)"


# --------------------------------------------------------------------------- #
# Session assembly.
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, projects_dir: Path, tail_files: list[Path]):
        self.projects_dir = projects_dir
        self.tail_files = tail_files

    # -- discovery -------------------------------------------------------- #
    def _project_dirs(self) -> list[Path]:
        if not self.projects_dir.exists():
            return []
        return sorted(
            (d for d in self.projects_dir.iterdir()
             if d.is_dir() and (d / "project.json").exists()),
            key=lambda d: d.name,
            reverse=True,
        )

    def build_sessions(self) -> list[dict]:
        """Return the full flat session list. Grouping is done client-side."""
        sessions: list[dict] = []
        for pdir in self._project_dirs():
            meta = _read_json(pdir / "project.json") or {}
            sessions.extend(self._project_sessions(pdir, meta))
        for tf in self.tail_files:
            sessions.append(self._tail_session(tf))
        return sessions

    def build_runs(self) -> list[dict]:
        """One entry per project for the run selector, most-recently-updated
        first. `live` reflects the authoritative running check."""
        runs: list[dict] = []
        for pdir in self._project_dirs():
            meta = _read_json(pdir / "project.json") or {}
            pid = meta.get("id", pdir.name)
            status = meta.get("status", "?")
            live = (status == "active") and _is_live(pid, meta.get("updated_at"))
            runs.append({
                "pid": pid,
                "direction": _short(meta.get("direction") or pid, 44),
                "status": status,
                "live": live,
                "updated_at": meta.get("updated_at"),
            })
        runs.sort(key=lambda r: _parse_ts(r["updated_at"]) or 0.0, reverse=True)
        return runs

    def _pid_label(self, meta: dict) -> str:
        d = meta.get("direction") or meta.get("id") or "project"
        return _short(d, 44)

    def _project_sessions(self, pdir: Path, meta: dict) -> list[dict]:
        pid = meta.get("id", pdir.name)
        plabel = self._pid_label(meta)
        # "active" in project.json is necessary but NOT sufficient — a run that
        # died/finished without finalizing stays active forever. Only a project
        # that is BOTH active and genuinely live gets a running row.
        active = (meta.get("status") == "active")
        live = active and _is_live(pid, meta.get("updated_at"))
        entries = _read_log(pdir / "research_log.jsonl")
        out: list[dict] = []

        # 1) one done session per completed research_log entry
        for i, e in enumerate(entries):
            phase = e.get("phase", "")
            group = _group_for_phase(phase)
            out.append({
                "id": f"{pid}::log::{i}",
                "group": group,
                "title": self._entry_title(e),
                "sub": plabel,
                "state": "done",
                "started": None,
                "finished": e.get("ts"),
                "final_output": _headline(e),
                "cost": e.get("cost_usd"),
                "pid": pid,
                "_kind": "log",
                "_pid": pid,
                "_idx": i,
            })

        # 2) grounding TOURNAMENT -> one session per candidate (the key view).
        #    Candidate rows are only "running" when the run is genuinely live.
        out.extend(self._tournament_sessions(pdir, pid, plabel, live))

        # 3) the currently-active phase -> a running session (only if LIVE)
        if live:
            phase = meta.get("phase", "")
            if phase and phase not in ("done",):
                # skip if the tournament running-rows already cover the live phase
                base = phase.split(":", 1)[0]
                out.append({
                    "id": f"{pid}::live",
                    "group": _group_for_phase(phase),
                    "title": f"▶ {self._phase_title(phase)}",
                    "sub": plabel,
                    "state": "running",
                    "started": meta.get("updated_at"),
                    "finished": None,
                    "final_output": None,
                    "cost": meta.get("cost_usd"),
                    "pid": pid,
                    "_kind": "live",
                    "_pid": pid,
                    "_phase": phase,
                })
        return out

    def _entry_title(self, e: dict) -> str:
        t = e.get("type", "artifact")
        phase = e.get("phase", "")
        # experiment run reports carry the exp id in the phase (experiment:<id>)
        if t == "run_report" and ":" in phase:
            return f"experiment · {phase.split(':', 1)[1]}"
        if t == "checkpoint" and ":" in phase:
            return f"checkpoint · {phase.split(':', 1)[1]}"
        if t == "review":
            h = e.get("headers", {}) or {}
            return f"review · {h.get('persona', '?')}"
        pretty = t.replace("_", " ")
        return pretty

    def _phase_title(self, phase: str) -> str:
        base, _, rest = phase.partition(":")
        pretty = {
            "literature": "Surveying literature",
            "ideation": "Proposing ideas",
            "tournament": "Grounding tournament (parallel candidates)",
            "grounding": "Grounding hypothesis",
            "hypothesis": "Planning hypothesis",
            "design": "Designing study",
            "experiment": "Running experiment",
            "results_critic": "Analyzing results",
            "analysis": "Analyzing results",
            "paper": "Drafting paper",
            "review": "Reviewing paper",
            "checkpoint": "Checkpoint review",
            "direction": "Forming direction",
            "created": "Initializing",
            "ingest": "Ingesting paper",
            "audit": "Auditing paper",
            "report": "Synthesizing report",
        }.get(base, base or "working")
        return f"{pretty}: {rest}" if rest else pretty

    def _tournament_sessions(self, pdir: Path, pid: str, plabel: str, active: bool) -> list[dict]:
        cand_dir = pdir / "candidates"
        summary = _read_json(pdir / "tournament" / "summary.json")
        if not cand_dir.exists() and not summary:
            return []
        winner_id = (summary or {}).get("winner_id")
        # winner_verdict per candidate from the ranking, if present
        rank_by_id: dict[str, dict] = {}
        for r in (summary or {}).get("ranking", []) or []:
            rank_by_id[r.get("idea_id")] = r

        out: list[dict] = []
        slots = sorted(cand_dir.iterdir()) if cand_dir.exists() else []
        for slot in slots:
            if not slot.is_dir():
                continue
            gr = _read_json(slot / "grounding" / "report.json")
            spec = _read_json(slot / "studies" / "spec.json")
            idea = _read_json(slot / "ideas" / "chosen.json") or {}
            idea_id = idea.get("idea_id") or slot.name
            rank = rank_by_id.get(idea_id, {})
            title = rank.get("title") or (idea.get("title") if idea else None) or slot.name
            # a candidate is "done" once its grounding report exists; else still running
            done = gr is not None or bool(rank)
            verdict = (gr or {}).get("go_no_go") or rank.get("go_no_go")
            if done:
                state = "done"
                if verdict in ("undetectable", "baseline_mismatch"):
                    state = "failed"
            else:
                state = "running" if active else "done"
            crown = " ★" if idea_id == winner_id else ""
            fo = None
            if done:
                fo = f"{verdict or '?'}"
                pv = (gr or {}).get("power_verdict") or rank.get("power_verdict")
                if pv:
                    fo += f" · power: {pv}"
                if idea_id == winner_id:
                    fo = "WINNER ★ · " + fo
            out.append({
                "id": f"{pid}::cand::{slot.name}",
                "group": "Grounding Tournament",
                "title": f"candidate: {_short(title, 60)}{crown}",
                "sub": plabel,
                "state": state,
                "started": None,
                "finished": None,
                "final_output": fo,
                "cost": None,
                "pid": pid,
                "_kind": "candidate",
                "_pid": pid,
                "_slot": slot.name,
            })
        return out

    def _tail_session(self, tf: Path) -> dict:
        exists = tf.exists()
        return {
            "id": f"tail::{tf}",
            "group": "Raw logs",
            "title": f"tail · {tf.name}",
            "sub": str(tf.parent),
            "state": "running" if exists else "failed",
            "started": None,
            "finished": None,
            "final_output": None if exists else "(file not found)",
            "cost": None,
            "pid": "__raw__",
            "_kind": "tail",
            "_path": str(tf),
        }

    # -- per-session log text --------------------------------------------- #
    def log_for(self, sid: str) -> dict:
        try:
            if sid.startswith("tail::"):
                return self._log_tail(sid[len("tail::"):])
            pid, _, rest = sid.partition("::")
            kind, _, arg = rest.partition("::")
            if kind == "log":
                return self._log_entry(pid, int(arg))
            if kind == "live":
                return self._log_live(pid)
            if kind == "candidate":
                return self._log_candidate(pid, arg)
        except Exception as e:  # noqa: BLE001
            return {"state": "failed", "text": f"(error reading session: {e})", "final_output": None}
        return {"state": "failed", "text": "(unknown session id)", "final_output": None}

    def _log_entry(self, pid: str, idx: int) -> dict:
        pdir = self.projects_dir / pid
        entries = _read_log(pdir / "research_log.jsonl")
        if idx >= len(entries):
            return {"state": "done", "text": "(entry no longer present)", "final_output": None}
        e = entries[idx]
        headline = _headline(e)
        parts = [
            f"# {self._entry_title(e)}",
            f"type      : {e.get('type')}",
            f"phase     : {e.get('phase')}",
            f"timestamp : {e.get('ts')}",
            f"cost_usd  : {e.get('cost_usd')}",
            "",
            "## headline",
            headline,
            "",
            "## headers",
            json.dumps(e.get("headers", {}), indent=2),
        ]
        # inline the linked artifact file if present + readable
        p = e.get("path")
        if p:
            ap = Path(p)
            if not ap.is_absolute():
                ap = pdir / p
            parts += ["", f"## artifact  ({p})"]
            if ap.exists():
                obj = _read_json(ap)
                if obj is not None:
                    parts.append(json.dumps(obj, indent=2)[:20000])
                else:
                    parts.append(_tail_lines(ap, 400))
            else:
                parts.append("(artifact file not found)")
        return {"state": "done", "text": "\n".join(parts), "final_output": headline}

    def _log_live(self, pid: str) -> dict:
        pdir = self.projects_dir / pid
        meta = _read_json(pdir / "project.json") or {}
        state = "running" if meta.get("status") == "active" else "done"
        entries = _read_log(pdir / "research_log.jsonl")
        tail = entries[-25:]
        parts = [
            f"# ▶ live phase: {meta.get('phase')}",
            f"project   : {meta.get('id')}",
            f"status    : {meta.get('status')}",
            f"updated   : {meta.get('updated_at')}",
            f"llm cost  : ${meta.get('cost_usd')}   gpu cost: ${meta.get('gpu_cost_usd')}",
            "",
            f"## recent research_log.jsonl (last {len(tail)} of {len(entries)})",
        ]
        for e in tail:
            parts.append(f"[{e.get('ts')}] {e.get('type'):<20} {_headline(e)}")
        if not tail:
            parts.append("(no artifacts logged yet — stage in progress)")
        fo = None
        if state == "done":
            fo = "finalized"
        return {"state": state, "text": "\n".join(parts), "final_output": fo}

    def _log_candidate(self, pid: str, slot: str) -> dict:
        pdir = self.projects_dir / pid
        sdir = pdir / "candidates" / slot
        summary = _read_json(pdir / "tournament" / "summary.json") or {}
        idea = _read_json(sdir / "ideas" / "chosen.json") or {}
        idea_id = idea.get("idea_id") or slot
        winner = summary.get("winner_id")
        gr = _read_json(sdir / "grounding" / "report.json")
        spec = _read_json(sdir / "studies" / "spec.json")
        chain = _read_json(sdir / "hypotheses" / "chain.json")
        done = gr is not None
        state = "running"
        verdict = (gr or {}).get("go_no_go")
        if done:
            state = "failed" if verdict in ("undetectable", "baseline_mismatch") else "done"
        parts = [
            f"# tournament candidate: {slot}",
            f"idea_id   : {idea_id}" + ("   ★ WINNER" if idea_id == winner else ""),
            f"title     : {idea.get('title', '')}",
            "",
            "## chain of custody (candidates/%s/)" % slot,
            f"  deep_survey  : {'✓' if (sdir / 'quant_findings.json').exists() or (sdir / 'literature').exists() else '…'}",
            f"  hypothesis   : {'✓' if chain else '…'}",
            f"  study spec   : {'✓' if spec else '…'}",
            f"  grounding    : {'✓' if gr else '… (probe running)'}",
        ]
        if spec:
            parts += ["", "## study spec", json.dumps(spec, indent=2)[:8000]]
        if gr:
            parts += ["", "## grounding report", json.dumps(gr, indent=2)[:12000]]
        else:
            parts += ["", "(grounding probe still running — L4 baseline reconciliation + power check)"]
        fo = None
        if done:
            fo = ("WINNER ★ · " if idea_id == winner else "") + str(verdict)
        return {"state": state, "text": "\n".join(parts), "final_output": fo}

    def _log_tail(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"state": "failed", "text": f"(file not found: {path})", "final_output": None}
        return {"state": "running", "text": _tail_lines(p, 600), "final_output": None}


# --------------------------------------------------------------------------- #
# HTTP server.
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    store: Store = None  # type: ignore[assignment]

    def log_message(self, *a):  # silence default request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/api/sessions":
            try:
                sessions = self.store.build_sessions()
                runs = self.store.build_runs()
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e), "sessions": [], "runs": [],
                            "generated_at": _now()}, 200)
                return
            self._json({"generated_at": _now(), "sessions": sessions, "runs": runs})
            return
        if u.path == "/api/runs":
            try:
                self._json({"generated_at": _now(), "runs": self.store.build_runs()})
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e), "runs": []}, 200)
            return
        if u.path == "/api/log":
            qs = parse_qs(u.query)
            sid = (qs.get("id") or [""])[0]
            if not sid:
                self._json({"error": "missing id"}, 400)
                return
            self._json(self.store.log_for(sid))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


# --------------------------------------------------------------------------- #
# Embedded single-page app. No CDN, no external fonts/scripts (CSP-safe).
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>single-harness · live monitor</title>
<style>
  :root{
    --bg:#09090b; --panel:#0c0c0f; --panel2:#111114; --border:#1e1e22;
    --border2:#27272a; --fg:#fafafa; --muted:#a1a1aa; --muted2:#71717a;
    --accent:#fafafa; --run:#e4e4e7; --fail:#dc7f7f; --done:#52525b;
    --radius:10px; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);
    font-size:13px;line-height:1.45;-webkit-font-smoothing:antialiased}
  /* explicit rows: topbar auto, content row = remaining viewport bounded to 0
     (minmax(0,1fr)) so aside + main can host their own scroll regions. */
  .app{display:grid;grid-template-columns:340px 1fr;grid-template-rows:auto minmax(0,1fr);
    height:100vh;overflow:hidden}
  header.topbar{grid-column:1/3;display:flex;align-items:center;gap:10px;
    padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
  .topbar .brand{font-weight:600;letter-spacing:-.01em}
  .topbar .dot{width:7px;height:7px;border-radius:50%;background:var(--run);
    box-shadow:0 0 0 0 rgba(228,228,231,.55);animation:pulse 1.8s infinite}
  .topbar .meta{color:var(--muted2);font-size:12px;margin-left:auto;font-family:var(--mono)}

  /* left panel */
  aside{border-right:1px solid var(--border);background:var(--panel);
    display:flex;flex-direction:column;min-height:0;overflow:hidden}
  .runsel{flex:none;padding:10px 12px;border-bottom:1px solid var(--border);background:var(--panel)}
  .runsel label{display:block;font-size:10px;font-weight:600;text-transform:uppercase;
    letter-spacing:.06em;color:var(--muted2);margin:0 2px 5px}
  .runsel select{width:100%;appearance:none;-webkit-appearance:none;
    background:var(--panel2);color:var(--fg);border:1px solid var(--border2);
    border-radius:8px;padding:7px 28px 7px 10px;font:inherit;font-size:12.5px;cursor:pointer;
    background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 15px) 55%,calc(100% - 10px) 55%;
    background-size:5px 5px,5px 5px;background-repeat:no-repeat}
  .runsel select:hover{border-color:var(--muted2)}
  .runsel select:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  .grouplist{flex:1;min-height:0;overflow-y:auto}
  .group{border-bottom:1px solid var(--border)}
  .group>.ghead{display:flex;align-items:center;gap:8px;width:100%;
    padding:9px 14px;background:none;border:0;color:var(--muted);cursor:pointer;
    font:inherit;font-weight:600;text-align:left;letter-spacing:.02em;text-transform:uppercase;
    font-size:11px}
  .group>.ghead:hover{color:var(--fg)}
  .group>.ghead .chev{transition:transform .15s;color:var(--muted2);font-size:10px}
  .group.collapsed>.ghead .chev{transform:rotate(-90deg)}
  .group .badge{margin-left:auto;background:var(--panel2);border:1px solid var(--border2);
    color:var(--muted);border-radius:999px;padding:1px 8px;font-size:11px;font-weight:600;
    font-family:var(--mono);text-transform:none}
  .rows{display:block}
  .group.collapsed .rows{display:none}
  .row{display:flex;align-items:flex-start;gap:9px;padding:8px 14px 8px 22px;cursor:pointer;
    border-left:2px solid transparent}
  .row:hover{background:var(--panel2)}
  .row.sel{background:var(--panel2);border-left-color:var(--accent)}
  .row .sdot{flex:none;width:8px;height:8px;border-radius:50%;margin-top:4px;background:var(--done)}
  .row.running .sdot{background:var(--run);box-shadow:0 0 0 0 rgba(228,228,231,.5);animation:pulse 1.6s infinite}
  .row.failed .sdot{background:var(--fail)}
  .row .rmain{min-width:0;flex:1}
  .row .rtitle{color:var(--fg);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row .rsub{color:var(--muted2);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row .rfo{color:var(--muted);font-size:11px;margin-top:2px;font-family:var(--mono);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .empty{padding:40px 20px;color:var(--muted2);text-align:center;font-size:12.5px}

  /* right panel */
  /* min-height:0 lets this column flex child be shorter than its content so the
     log body's overflow:auto engages (the flexbox min-content-size gotcha). */
  main{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
  .rhead{flex:none;padding:14px 20px;border-bottom:1px solid var(--border);background:var(--panel)}
  .rhead .htop{display:flex;align-items:center;gap:10px}
  .rhead h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .rhead .hsub{color:var(--muted2);font-size:12px;margin-top:3px;font-family:var(--mono)}
  .statebadge{flex:none;font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;
    border:1px solid var(--border2);color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.03em}
  .statebadge.running{color:#000;background:var(--run);border-color:var(--run)}
  .statebadge.done{color:var(--fg);background:var(--panel2)}
  .statebadge.failed{color:var(--fail);border-color:var(--fail)}
  .fo{margin-top:10px;padding:9px 12px;border:1px solid var(--border2);border-radius:var(--radius);
    background:var(--panel2);color:var(--fg);font-family:var(--mono);font-size:12px;
    display:flex;gap:8px;align-items:baseline}
  .fo .tick{color:var(--run);font-weight:700}
  .logwrap{flex:1;min-height:0;overflow-y:auto;padding:16px 20px}
  pre.log{margin:0;font-family:var(--mono);font-size:12px;color:var(--muted);
    white-space:pre-wrap;word-break:break-word;line-height:1.55}
  pre.log .hl{color:var(--fg)}
  .placeholder{height:100%;display:flex;align-items:center;justify-content:center;
    color:var(--muted2);font-size:13px}
  /* [hidden] must win over the display:flex/display:block above (an explicit
     display rule otherwise defeats the UA [hidden]{display:none}). */
  [hidden]{display:none !important}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(228,228,231,.5)}70%{box-shadow:0 0 0 6px rgba(228,228,231,0)}100%{box-shadow:0 0 0 0 rgba(228,228,231,0)}}
  .grouplist::-webkit-scrollbar,.logwrap::-webkit-scrollbar{width:10px}
  .grouplist::-webkit-scrollbar-thumb,.logwrap::-webkit-scrollbar-thumb{background:var(--border2);border-radius:6px}
  @media (max-width:820px){.app{grid-template-columns:1fr;grid-template-rows:auto 40vh minmax(0,1fr)}
    aside{grid-row:2}main{grid-row:3}}
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <span class="dot" aria-hidden="true"></span>
    <span class="brand">single-harness</span>
    <span style="color:var(--muted2);font-size:12px">live monitor</span>
    <span class="meta" id="meta">connecting…</span>
  </header>
  <aside aria-label="Sessions by stage">
    <div class="runsel">
      <label for="runselect">Run</label>
      <select id="runselect" aria-label="Filter sessions by run"></select>
    </div>
    <div class="grouplist" id="left"></div>
  </aside>
  <main>
    <div class="rhead" id="rhead" hidden>
      <div class="htop">
        <h1 id="rtitle">—</h1>
        <span class="statebadge" id="rbadge">—</span>
      </div>
      <div class="hsub" id="rsub"></div>
      <div class="fo" id="rfo" hidden></div>
    </div>
    <div class="logwrap" id="logwrap">
      <div class="placeholder" id="ph">Select a session to view its live logs.</div>
      <pre class="log" id="log" hidden></pre>
    </div>
  </main>
</div>
<script>
"use strict";
var GROUP_ORDER = %%GROUP_ORDER%%;
// runFilter: a pid to show, or '*' for all runs. Null = "not yet chosen a
// default" — the first /api/sessions response picks the current run.
var state = { sessions: [], runs: [], selected: null, collapsed: {},
              logState: null, stick: true, runFilter: null, runTouched: false };

function el(tag, cls, txt){ var e=document.createElement(tag); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e; }

function fmtCount(n){ return String(n); }

function visibleSessions(){
  var f = state.runFilter;
  if(!f || f==='*') return state.sessions;
  return state.sessions.filter(function(s){ return s.pid===f || s.pid==='__raw__'; });
}

function renderRuns(){
  var sel = document.getElementById('runselect');
  // rebuild options, preserving the current selection value across polls
  var want = (state.runFilter==null? '' : state.runFilter);
  var opts = state.runs.map(function(r){
    var tag = r.live? 'live' : r.status;
    return {v:r.pid, label:r.direction+' · '+tag};
  });
  opts.push({v:'*', label:'All runs'});
  // only rebuild if the option set changed (avoid clobbering an open dropdown)
  var sig = opts.map(function(o){return o.v+':'+o.label;}).join('|');
  if(sel.__sig !== sig){
    sel.innerHTML='';
    opts.forEach(function(o){ var op=el('option',null,o.label); op.value=o.v; sel.appendChild(op); });
    sel.__sig = sig;
  }
  if(sel.value !== want && want!=='') sel.value = want;
}

function renderLeft(){
  var left = document.getElementById('left');
  var sessions = visibleSessions();
  var byGroup = {};
  sessions.forEach(function(s){ (byGroup[s.group]=byGroup[s.group]||[]).push(s); });
  var groups = GROUP_ORDER.filter(function(g){return byGroup[g];})
    .concat(Object.keys(byGroup).filter(function(g){return GROUP_ORDER.indexOf(g)<0;}));

  if(sessions.length===0){
    left.innerHTML='';
    var msg = state.sessions.length===0
      ? 'No projects or sessions yet.\nStart a run with run.py new — sessions appear here live.'
      : 'No sessions for this run.';
    left.appendChild(el('div','empty',msg));
    return;
  }
  var frag = document.createDocumentFragment();
  groups.forEach(function(g){
    var rows = byGroup[g];
    var running = rows.filter(function(r){return r.state==='running';}).length;
    var wrap = el('div','group'+(state.collapsed[g]?' collapsed':''));
    var head = el('button','ghead'); head.type='button';
    head.appendChild(el('span','chev','▾'));
    head.appendChild(el('span',null,g));
    var badge = el('span','badge', running? (running+' · '+rows.length) : fmtCount(rows.length));
    head.appendChild(badge);
    head.onclick=function(){ state.collapsed[g]=!state.collapsed[g]; renderLeft(); };
    wrap.appendChild(head);
    var rc = el('div','rows');
    rows.forEach(function(s){
      var row = el('div','row '+s.state+(state.selected===s.id?' sel':''));
      row.appendChild(el('span','sdot'));
      var m = el('div','rmain');
      m.appendChild(el('div','rtitle', s.title));
      m.appendChild(el('div','rsub', s.sub||''));
      if(s.final_output) m.appendChild(el('div','rfo', s.final_output));
      row.appendChild(m);
      row.onclick=function(){ select(s.id); };
      rc.appendChild(row);
    });
    wrap.appendChild(rc);
    frag.appendChild(wrap);
  });
  left.innerHTML=''; left.appendChild(frag);
}

function currentSession(){
  for(var i=0;i<state.sessions.length;i++) if(state.sessions[i].id===state.selected) return state.sessions[i];
  return null;
}

function renderRightHead(){
  var s = currentSession();
  var rh = document.getElementById('rhead');
  var ph = document.getElementById('ph');
  var log = document.getElementById('log');
  // Placeholder ONLY when nothing is selected; otherwise header + log, no
  // placeholder anywhere. Driven here so it survives every ~1.5s re-render.
  if(!s){ rh.hidden=true; ph.hidden=false; log.hidden=true; return; }
  rh.hidden=false; ph.hidden=true; log.hidden=false;
  document.getElementById('rtitle').textContent = s.title;
  document.getElementById('rsub').textContent = s.sub||'';
  var b = document.getElementById('rbadge');
  var st = (state.logState && state.logState.state) || s.state;
  b.textContent = st; b.className = 'statebadge '+st;
  var fo = document.getElementById('rfo');
  var text = (state.logState && state.logState.final_output) || (st!=='running'? s.final_output : null);
  if(text){ fo.hidden=false; fo.innerHTML=''; if(st==='done'){var t=el('span','tick','✓');fo.appendChild(t);} fo.appendChild(el('span',null,text)); }
  else fo.hidden=true;
}

function select(id){
  state.selected = id; state.logState=null; state.stick=true;
  document.getElementById('log').textContent='loading…';
  renderLeft(); renderRightHead(); fetchLog();  // renderRightHead toggles ph/log
}

function fetchLog(){
  if(!state.selected) return;
  var id = state.selected;
  fetch('/api/log?id='+encodeURIComponent(id)).then(function(r){return r.json();}).then(function(d){
    if(state.selected!==id) return; // selection changed mid-flight
    state.logState = d;
    var log = document.getElementById('log');
    var wrap = document.getElementById('logwrap');
    var atBottom = (wrap.scrollTop+wrap.clientHeight) >= (wrap.scrollHeight-40);
    log.textContent = d.text||'';
    renderRightHead();
    if(state.stick && atBottom) wrap.scrollTop = wrap.scrollHeight;
  }).catch(function(){});
}

function pickDefaultRun(){
  // most-recently-updated LIVE run; else most-recent active; else most-recent
  // project; else all. runs[] arrives already sorted newest-first.
  var runs = state.runs;
  if(!runs.length){ state.runFilter='*'; return; }
  var live = runs.filter(function(r){return r.live;});
  var active = runs.filter(function(r){return r.status==='active';});
  state.runFilter = (live[0]||active[0]||runs[0]).pid;
}

function fetchSessions(){
  fetch('/api/sessions').then(function(r){return r.json();}).then(function(d){
    state.sessions = d.sessions||[];
    state.runs = d.runs||[];
    if(state.runFilter===null && !state.runTouched) pickDefaultRun();
    renderRuns();
    var vis = visibleSessions();
    var running = vis.filter(function(s){return s.state==='running';}).length;
    document.getElementById('meta').textContent =
      vis.length+' sessions · '+running+' running · '+ (d.generated_at||'');
    if(state.selected && !currentSession()) { /* keep id; may reappear */ }
    renderLeft(); renderRightHead();
  }).catch(function(){
    document.getElementById('meta').textContent='server unreachable… retrying';
  });
}

document.getElementById('runselect').addEventListener('change', function(e){
  state.runFilter = e.target.value; state.runTouched = true;
  renderLeft(); renderRightHead();
});

// poll loops
fetchSessions(); setInterval(fetchSessions, 1500);
setInterval(function(){
  var s = currentSession();
  var st = (state.logState && state.logState.state) || (s&&s.state);
  // always refresh a running session's log; refresh a done one occasionally too
  if(state.selected){ fetchLog(); }
}, 1500);
</script>
</body>
</html>
"""


def _fill_page() -> None:
    """Inject the group-order list into the client template (all UI glyphs are
    already literal UTF-8 in the source, so no further decoding is needed)."""
    global PAGE
    PAGE = PAGE.replace("%%GROUP_ORDER%%", json.dumps(GROUP_ORDER))


def main() -> int:
    ap = argparse.ArgumentParser(description="single-harness live monitoring dashboard")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SH_DASH_PORT", "8787")))
    ap.add_argument("--host", default=os.environ.get("SH_DASH_HOST", "127.0.0.1"))
    ap.add_argument("--projects", default=os.environ.get("SH_DASH_PROJECTS", ""),
                    help="projects dir (default: <this file>/projects)")
    ap.add_argument("--tail", action="append", default=[],
                    help="extra raw log file to tail verbatim as a session (repeatable)")
    args = ap.parse_args()

    projects_dir = Path(args.projects) if args.projects else (Path(__file__).resolve().parent / "projects")
    tails = list(args.tail)
    env_tail = os.environ.get("SH_DASH_TAIL", "")
    if env_tail:
        tails += [t for t in re.split(r"[:,]", env_tail) if t.strip()]
    tail_files = [Path(t).expanduser() for t in tails]

    _fill_page()
    Handler.store = Store(projects_dir, tail_files)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("single-harness live dashboard")
    print(f"  projects : {projects_dir}")
    if tail_files:
        print(f"  tailing  : {', '.join(str(t) for t in tail_files)}")
    print(f"  open     : {url}")
    print("  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
