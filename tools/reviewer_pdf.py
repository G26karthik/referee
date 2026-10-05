"""Render reviewer.md pages to one PDF (a delivery convenience; it decides nothing).

  python tools/reviewer_pdf.py <out.pdf> <projects dir>/<paper-id> [...]

Markdown -> HTML (python-markdown, tables) -> PDF with a headless Chromium (Edge or Chrome) print. Each paper starts on
a new page. The text is the reviewer.md the harness wrote, unchanged.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
body { font-family: Georgia, 'Times New Roman', serif; font-size: 10pt; line-height: 1.38; color: #111; }
h1 { font-size: 15pt; margin: 0 0 4pt 0; } h2 { font-size: 12pt; margin: 12pt 0 4pt 0; border-bottom: 1px solid #bbb; }
h3 { font-size: 10.5pt; margin: 10pt 0 3pt 0; } p, li { margin: 2pt 0; }
table { border-collapse: collapse; margin: 4pt 0 6pt 0; font-size: 8.8pt; width: 100%; }
th, td { border-bottom: 1px solid #ddd; padding: 2pt 4pt; text-align: left; vertical-align: top; }
th { background: #f0f0f0; } code { font-family: Consolas, monospace; font-size: 8.5pt; }
.paper { page-break-after: always; } .paper:last-child { page-break-after: auto; }
"""


def browser() -> str:
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe", shutil.which("chromium") or "", shutil.which("google-chrome") or ""):
        if c and Path(c).exists():
            return c
    raise SystemExit("no headless Chromium (Edge or Chrome) found")


def main(argv: list[str]) -> None:
    out, roots = Path(argv[0]).resolve(), [Path(a) for a in argv[1:]]
    body = "".join(f'<div class="paper">{markdown.markdown((r / "reviewer.md").read_text(encoding="utf-8"), extensions=["tables"])}</div>'
                   for r in roots)
    with tempfile.TemporaryDirectory() as t:
        html = Path(t) / "reviewer.html"
        html.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>",
                        encoding="utf-8")
        subprocess.run([browser(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                        html.as_uri()], check=True, timeout=180, capture_output=True)
    print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
