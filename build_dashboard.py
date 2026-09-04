"""Bake audit/dashboard.json into web/dashboard.template.html -> dashboard.html.

    python -m recovery.dashboard   # regenerate the data first
    python build_dashboard.py      # then inline it into a standalone page
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
template = ROOT / "web" / "dashboard.template.html"
data_file = ROOT / "audit" / "dashboard.json"
out = ROOT / "dashboard.html"

if not data_file.exists():
    sys.exit("run `python -m recovery.dashboard` first (audit/dashboard.json missing)")

data = json.loads(data_file.read_text(encoding="utf-8"))
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

html = template.read_text(encoding="utf-8")
if "/*DATA*/" not in html:
    sys.exit("template has no /*DATA*/ marker")
html = html.replace("/*DATA*/", blob)
out.write_text(html, encoding="utf-8")
print(f"wrote {out.relative_to(ROOT)}  ({len(html):,} bytes, {len(data['sweep']['cells'])} sweep cells)")
