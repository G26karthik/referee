"""Summarize sealed SINGLE-MODEL CODEX CRITIQUE artifacts without scoring quality."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: baseline_summary.py <run-root> <paper-id> [...]")
    run = Path(argv[0]).resolve()
    rows, totals, dropped = [], Counter(), 0
    for pid in argv[1:]:
        base = run / "projects" / pid / "baseline"
        report_path, side_path = base / "accepted.json", base / "accepted.driver.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        side = json.loads(side_path.read_text(encoding="utf-8"))
        if hashlib.sha256(report_path.read_bytes()).hexdigest() != side["content_sha256"]:
            raise ValueError(f"content seal mismatch: {pid}")
        prompt = base / "prompt.md"
        if hashlib.sha256(prompt.read_bytes()).hexdigest() != side["prompt_sha256"]:
            raise ValueError(f"prompt seal mismatch: {pid}")
        counts = Counter(str(f.get("severity") or "") for f in report.get("findings", []))
        totals.update(counts)
        dropped += int(side.get("dropped", 0))
        rows.append({"paper_id": pid, "kept": sum(counts.values()), "dropped": side["dropped"],
                     "severity": dict(counts), "raw_response": side["raw_response"],
                     "delegation_mode": side["delegation_mode"],
                     "reviewer": side["reviewer"], "tool_policy": side["tool_policy"],
                     "tool_policy_provable": side["tool_policy_provable"]})
    result = {
        "arm": "SINGLE-MODEL CODEX CRITIQUE", "papers": rows,
        "totals": {"kept": sum(totals.values()), "dropped": dropped,
                   "severity": dict(totals)},
        "interpretation": ("Concern counts are descriptive outputs, not quality scores. "
                           "No human or adjudicated ground truth is available."),
    }
    out_json = run / "reports" / "single_model_baseline.json"
    out_md = run / "reports" / "single_model_baseline.md"
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    lines = ["# SINGLE-MODEL CODEX CRITIQUE", "",
             "Each row is one fresh single-paper reasoning context. Responses used the same "
             "rendered paper context and evidence schema as a REFEREE lens, but no REFEREE "
             "artifact. Evidence quotations were checked by the same deterministic gate.", "",
             "| Paper | Kept | Dropped | FATAL | MAJOR | MINOR | NOTE |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        s = row["severity"]
        lines.append(f"| `{row['paper_id']}` | {row['kept']} | {row['dropped']} | "
                     f"{s.get('FATAL', 0)} | {s.get('MAJOR', 0)} | {s.get('MINOR', 0)} | {s.get('NOTE', 0)} |")
    lines += ["", f"Total: {sum(totals.values())} kept, {dropped} dropped. "
              f"Severity is model-assigned: {dict(totals)}.", "",
              "Concern counts do not measure factual validity, usefulness, recall, or superiority. "
              "No human or adjudicated ground truth exists for this run, so comparative quality "
              "is not computed."]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)} papers; kept={sum(totals.values())}; dropped={dropped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
