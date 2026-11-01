"""Renders docs/USER_MANUAL.md to a formatted PDF using headless Google Chrome.

Usage: python scripts/build_manual.py <output.pdf>
Build-time requirement: the `markdown` package (requirements-dev.txt).
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from colab_idle_guard import __version__  # noqa: E402

CSS = """
@page { size: A4; margin: 20mm 18mm 22mm 18mm;
  @bottom-center { content: counter(page); font-family: "Segoe UI", Arial, sans-serif; font-size: 9pt; color: #57606a; } }
@page :first { @bottom-center { content: none; } }
:root { --accent: #0b57d0; --ink: #1f2328; --muted: #57606a; --line: #d0d7de; --soft: #f6f8fa; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; line-height: 1.5; color: var(--ink); margin: 0; }
.cover { height: 247mm; display: flex; flex-direction: column; justify-content: center; page-break-after: always; }
.cover .mark { width: 22mm; height: 22mm; border-radius: 5mm; background: #1a7f37; margin-bottom: 10mm; position: relative; }
.cover .mark::after { content: ""; position: absolute; inset: 5mm; border: 2.6mm solid #fff; border-right-color: transparent; border-radius: 50%; }
.cover h1 { display: block; font-size: 34pt; margin: 0; letter-spacing: -0.5pt; border: 0; }
.cover .sub { font-size: 15pt; color: var(--muted); margin-top: 3mm; }
.cover .meta { margin-top: 18mm; font-size: 10pt; color: var(--muted); border-top: 1px solid var(--line); padding-top: 4mm; }
.toc { page-break-after: always; }
.toc h2 { border: 0; }
.toc ul { list-style: none; padding-left: 0; }
.toc ul ul { padding-left: 6mm; }
.toc li { margin: 1.2mm 0; }
.toc a { color: var(--ink); text-decoration: none; }
h1 { display: none; }
h2 { font-size: 16pt; color: var(--accent); margin: 9mm 0 3mm; padding-bottom: 1.5mm; border-bottom: 1.5px solid var(--line); page-break-after: avoid; }
h2:not(:first-of-type) { page-break-before: always; }
h3 { font-size: 12pt; margin: 6mm 0 2mm; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: var(--accent); }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; background: var(--soft); padding: 0.3mm 1mm; border-radius: 1mm; }
pre { background: var(--soft); border: 1px solid var(--line); border-radius: 2mm; padding: 3mm 4mm; white-space: pre-wrap; word-break: break-word; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { width: 100%; border-collapse: collapse; margin: 3mm 0 4mm; font-size: 9.5pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: var(--soft); text-align: left; font-weight: 600; }
th, td { border: 1px solid var(--line); padding: 1.6mm 2.4mm; vertical-align: top; }
blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #bf8700; background: #fff8e5; }
strong { font-weight: 600; }
"""


def render_html(md_text: str) -> str:
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"],
                           extension_configs={"toc": {"toc_depth": "2-3"}})
    body = md.convert(md_text)
    title_match = re.search(r"^# (.+)$", md_text, re.M)
    title = title_match.group(1).split("—")[0].strip() if title_match else "User Manual"
    cover = f"""
<section class="cover">
  <div class="mark"></div>
  <h1>{html.escape(title)}</h1>
  <div class="sub">User Manual</div>
  <div class="meta">Version {__version__} · Windows 11 · {date.today():%B %Y}<br>
  Releases idle Google Colab runtimes in Chrome and VS Code, without interrupting running cells.</div>
</section>
<section class="toc"><h2>Contents</h2>{md.toc}</section>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)} - User Manual</title><style>{CSS}</style></head>
<body>{cover}{body}</body></html>"""


def find_chrome() -> str:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        shutil.which("chrome") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise SystemExit("Google Chrome not found; it is needed to render the PDF.")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "Colab-Idle-Guard-User-Manual.pdf").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    source = (ROOT / "docs" / "USER_MANUAL.md").read_text(encoding="utf-8")
    if out.exists():
        out.unlink()
    with tempfile.TemporaryDirectory(prefix="cig-manual-", ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "manual.html"
        page.write_text(render_html(source), encoding="utf-8")
        # A throw-away profile keeps this separate from the user's running Chrome.
        cmd = [find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={Path(tmp) / 'profile'}", "--no-pdf-header-footer",
               f"--print-to-pdf={out}", page.as_uri()]
        subprocess.run(cmd, check=True, timeout=120, capture_output=True)
        # On Windows chrome.exe can return before its child process has written the file.
        last_size, stable = -1, 0
        for _ in range(120):
            size = out.stat().st_size if out.exists() else -1
            stable = stable + 1 if size > 0 and size == last_size else 0
            if stable >= 2:
                break
            last_size = size
            time.sleep(0.5)
    if not out.exists() or out.stat().st_size < 10_000:
        raise SystemExit("PDF was not produced. Chrome exited without writing it; this happens in shells that "
                         "restrict child processes (e.g. some sandboxed or job-object terminals). "
                         "Run the build from a normal PowerShell window.")
    print(f"{out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
