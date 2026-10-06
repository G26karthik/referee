"""Render reviewer.md pages to one PDF (a delivery convenience; it decides nothing), and measure each paper's length.

  python tools/reviewer_pdf.py <out.pdf> <projects dir>/<paper-id> [...] [--max-pages 2]

Markdown -> HTML (python-markdown, tables) -> PDF with a headless Chromium (Edge or Chrome) print. Each paper starts on
a new page. The text is the reviewer.md the harness wrote, unchanged. Each paper is also printed alone and its pages
counted; a paper longer than --max-pages is reported and the tool exits 3 (the combined PDF is still written).
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

CSS = """
@page { size: A4; margin: 13mm 13mm 13mm 13mm; }
body { font-family: Georgia, 'Times New Roman', serif; font-size: 9.4pt; line-height: 1.3; color: #111; }
h1 { font-size: 13.5pt; margin: 0 0 3pt 0; } h2 { font-size: 11pt; margin: 8pt 0 3pt 0; border-bottom: 1px solid #bbb; }
h3 { font-size: 10pt; margin: 8pt 0 2pt 0; } p, li { margin: 2pt 0; } ul { margin: 2pt 0 2pt 0; padding-left: 14pt; }
table { border-collapse: collapse; margin: 3pt 0 5pt 0; font-size: 8.2pt; width: 100%; }
th, td { border-bottom: 1px solid #ddd; padding: 2pt 3pt; text-align: left; vertical-align: top; }
th { background: #f0f0f0; } code { font-family: Consolas, monospace; font-size: 8pt; }
.paper { page-break-after: always; } .paper:last-child { page-break-after: auto; }
"""


def browser() -> str:
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe", shutil.which("chromium") or "", shutil.which("google-chrome") or ""):
        if c and Path(c).exists():
            return c
    raise SystemExit("no headless Chromium (Edge or Chrome) found")


def _print(roots: list[Path], out: Path, work: Path) -> None:
    body = "".join(f'<div class="paper">{markdown.markdown((r / "reviewer.md").read_text(encoding="utf-8"), extensions=["tables"])}</div>'
                   for r in roots)
    html = work / f"{out.stem}.html"
    html.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>",
                    encoding="utf-8")
    out.unlink(missing_ok=True)
    # its own profile: with the browser already open, a headless call on the user's profile hands off and prints nothing
    subprocess.run([browser(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={work / 'profile'}",
                    f"--print-to-pdf={out}", html.as_uri()], check=True, timeout=180, capture_output=True)
    if not out.exists() or out.stat().st_size == 0:
        raise SystemExit(f"no PDF was written to {out}")


def pages(pdf: Path) -> int:
    """The number of page objects in a PDF (Chromium writes one /Type /Page per page)."""
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf.read_bytes()))


def main(argv: list[str]) -> int:
    limit = 0
    if "--max-pages" in argv:
        i = argv.index("--max-pages")
        limit, argv = int(argv[i + 1]), argv[:i] + argv[i + 2:]
    out, roots = Path(argv[0]).resolve(), [Path(a) for a in argv[1:]]
    long = []
    with tempfile.TemporaryDirectory() as t:
        _print(roots, out, Path(t))
        for r in roots:
            one = Path(t) / f"{r.name}.pdf"
            _print([r], one, Path(t))
            n = pages(one)
            print(f"{r.name}: {n} page(s)")
            if limit and n > limit:
                long.append(f"{r.name} ({n} pages)")
    print(out)
    if long:
        print(f"longer than {limit} pages: {', '.join(long)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
