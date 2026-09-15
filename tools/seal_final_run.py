"""Seal the v2 evaluation with paper/prompt hashes and per-file checksums."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs_final_codex_v2_2026-09-15"
PAPERS = (
    "0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
    "apt-icml", "cvpr", "iclr", "sanchez24a-icml",
)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    records = []
    for paper_id in PAPERS:
        project = RUN / "projects" / paper_id
        project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
        paths = {
            "paper_pdf": Path(project_meta["paper_path"]),
            "rendered_paper": project / "paper" / "doc.json",
            "baseline_prompt": project / "baseline" / "prompt.md",
            "baseline_response": project / "baseline" / "response.json",
            "whole_paper_prompt": project / "reports" / "whole_paper_prompt.md",
            "whole_paper_response": project / "reports" / "whole_paper_response.json",
        }
        records.append({
            "paper_id": paper_id,
            "sha256": {name: sha(path) for name, path in paths.items()},
        })
    manifest = {
        "run_id": RUN.name,
        "papers": records,
        "model_calls": {"baseline": 8, "whole_paper": 8, "total": 16},
        "controller": "codex exec v0.154.0-alpha.6.2",
        "provider_reported": "openai",
        "model_reported": "gpt-5.6-luna",
        "filesystem_isolation": "unrecorded / not provable",
        "authorization": "RUN_AUTHORIZATION.json",
    }
    manifest_path = RUN / "RUN_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    files = []
    for directory, dirnames, filenames in os.walk(RUN):
        # Virtual environments are transient dependency caches and contain Windows-
        # inaccessible POSIX reparse points. They are not scientific run artifacts.
        if Path(directory).name == "runs":
            pass
        if Path(directory).name == "env":
            dirnames[:] = []
            continue
        files.extend(
            Path(directory) / name for name in filenames if name != "SHA256SUMS"
        )
    files.sort()
    lines = [f"{sha(path)}  {path.relative_to(RUN).as_posix()}" for path in files]
    (RUN / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sealed {len(files)} artifacts across {len(PAPERS)} papers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
