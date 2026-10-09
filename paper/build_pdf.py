"""Render paper/loadguard_report.md to a two-column A4 PDF (Edge/Chrome headless; no MathJax: the report has no equations and uses $ amounts).

    python paper/build_pdf.py            # needs `pip install markdown` and Edge (Windows) or Chrome

Layout: title, authors and a centred abstract span the page; the body flows in two columns.
Wide figures use <div class="wide"> and span both columns.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent
SRC, DST = HERE / "loadguard_report.md", HERE / "loadguard_report.pdf"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("google-chrome") or "", shutil.which("chromium") or ""]

CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
body { font-family: 'Cambria', 'Georgia', serif; font-size: 9.6pt; line-height: 1.38; color: #111; margin: 0; }
h1 { font-size: 16.5pt; text-align: center; margin: 0 6% 8px; line-height: 1.22; }
.authors { text-align: center; font-size: 10pt; margin-bottom: 2px; }
.affil { text-align: center; font-size: 8.8pt; color: #444; margin-bottom: 12px; }
.abstract { width: 78%; margin: 0 auto 14px; font-size: 9.2pt; text-align: justify; }
.abstract h2 { text-align: center; font-size: 10.5pt; margin: 0 0 4px; letter-spacing: 0.04em; }
.abstract p { margin: 3px 0; }
.keywords { width: 78%; margin: 0 auto 14px; font-size: 8.6pt; text-align: center; color: #333; }
hr.rule { border: 0; border-top: 0.6px solid #999; margin: 0 0 10px; }
.cols { column-count: 2; column-gap: 7mm; column-fill: balance; }
.cols h2 { font-size: 11pt; margin: 11px 0 4px; break-after: avoid; }
.cols h3 { font-size: 9.9pt; margin: 8px 0 3px; break-after: avoid; font-style: italic; }
p { text-align: justify; margin: 3px 0 6px; hyphens: auto; }
table { border-collapse: collapse; margin: 6px auto 8px; font-size: 7.9pt; break-inside: avoid; }
th, td { border-top: 0.6px solid #999; border-bottom: 0.6px solid #999; padding: 2px 4px; text-align: left; }
th { background: #f2f2f2; }
code { font-family: Consolas, monospace; font-size: 8.2pt; }
img { display: block; max-width: 100%; margin: 4px auto 2px; }
.fig { break-inside: avoid; margin: 6px 0 8px; }
.wide { column-span: all; break-inside: avoid; margin: 6px 0 10px; }
.wide img { max-width: 94%; }
.caption { text-align: justify; font-size: 8.3pt; color: #222; margin: 2px 0 6px; }
.box { border: 0.6px solid #1a73e8; background: #f3f8fe; padding: 5px 8px; margin: 6px 0 8px; break-inside: avoid; font-size: 9pt; }
ul, ol { margin: 3px 0 6px 16px; padding: 0; }
li { margin: 1px 0; }
ul { list-style-type: "•  "; }
.refs p { font-size: 8.2pt; text-align: left; margin: 2px 0; padding-left: 14px; text-indent: -14px; }
"""
MATHJAX = """
<script>window.MathJax={tex:{inlineMath:[['$','$']],displayMath:[['$$','$$']]},svg:{fontCache:'global'}};</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg.js"></script>
"""


def protect_math(text):
    store = []

    def keep(m):
        store.append(m.group(0))
        return f"@@MATH{len(store) - 1}@@"

    text = re.sub(r"\$\$.+?\$\$", keep, text, flags=re.S)
    text = re.sub(r"(?<!\\)\$[^$\n]+?\$", keep, text)
    return text, store


def main():
    text, store = protect_math(SRC.read_text(encoding="utf-8"))
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "md_in_html"])
    for i, m in enumerate(store):
        html = html.replace(f"@@MATH{i}@@", m.replace("<", "&lt;").replace(">", "&gt;"))
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><base href='{HERE.as_uri()}/'>"
           f"<style>{CSS}</style></head><body>{html}</body></html>")
    browser = next(b for b in BROWSERS if b and Path(b).exists())
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8", dir=HERE) as f:
        f.write(doc)
        tmp = Path(f.name)
    try:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        "--virtual-time-budget=25000", f"--print-to-pdf={DST}", tmp.as_uri()],
                       check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        tmp.unlink(missing_ok=True)
    print("wrote", DST, DST.stat().st_size, "bytes")


if __name__ == "__main__":
    sys.exit(main())
