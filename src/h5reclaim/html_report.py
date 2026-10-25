"""Self-contained recovery summaries rendered only from saved evidence JSON."""

import html
import json
import os
from pathlib import Path
import tempfile
from math import prod


def _escape(value):
    return html.escape(str(value), quote=True)


def _summary(report):
    accepted, unknown = report.get("accepted_elements"), report.get("unknown_elements")
    if isinstance(accepted, int) and isinstance(unknown, int):
        return accepted, unknown, "elements"
    descriptor, mappings = report.get("dataset") or {}, report.get("mappings")
    shape, chunks = descriptor.get("shape"), descriptor.get("chunks")
    if isinstance(shape, list) and chunks and isinstance(mappings, list):
        coordinates = {tuple(row["coordinate"]) for row in mappings}
        accepted = sum(prod(min(width, size - start) for start, width, size in zip(at, chunks, shape))
                       for at in coordinates)
        return accepted, prod(shape) - accepted, "elements"
    counts = report.get("counts") or {}
    return counts.get("recovered", 0), sum(v for k, v in counts.items() if k != "recovered" and isinstance(v, int)), "chunks"


def render_report_html(report):
    if (not isinstance(report, dict) or report.get("tool") != "h5reclaim"
            or report.get("schema_version") != 1 or report.get("outcome") not in ("complete", "partial")):
        raise ValueError("expected a completed H5Reclaim version-1 evidence report")
    records = report.get("datasets") if isinstance(report.get("datasets"), list) else [
        {"path": report.get("dataset", {}).get("path", "unknown"), "report": report}]
    if len(records) > 100000:
        raise ValueError("HTML dataset list exceeds its rendering budget")
    rows, accepted_total, unknown_total, omitted_total = [], 0, 0, 0
    for record in records:
        selected = record.get("report") or {}
        dataset = selected.get("dataset") or {}
        accepted, unknown, unit = _summary(selected)
        if unit == "elements":
            accepted_total += accepted
            unknown_total += unknown
        shape = dataset.get("shape")
        shape_text = "null" if shape is None else "scalar" if not shape else " × ".join(map(str, shape))
        omitted = list(dataset.get("attributes_omitted") or []) + list(dataset.get("whole_file_attributes_omitted") or [])
        omitted_total += len(omitted)
        method = selected.get("automatic_routing", {}).get("selected") or selected.get("operation") or selected.get("mode") or selected.get("index", {}).get("type") or "structural"
        maps = selected.get("validity") or {}
        map_path = maps.get("element_status") or maps.get("dataset") or selected.get("element_status") or selected.get("validity_map") or "See route-specific report"
        total = accepted + unknown
        percent = 100 * accepted / total if total else 0
        outcome = selected.get("outcome", "unknown")
        details = {"method": method, "validity_map": map_path, "type": dataset.get("dtype"),
                   "chunks": dataset.get("chunks"), "omitted_attributes": omitted,
                   "historical_integrity": selected.get("historical_integrity"),
                   "scientific_context": selected.get("scientific_context"),
                   "limits": selected.get("limits") or selected.get("integrity_note")}
        rows.append(f'<tr data-outcome="{_escape(outcome)}"><td><strong>{_escape(record.get("path"))}</strong><small>{_escape(shape_text)}</small></td>'
            f'<td><span class="badge {_escape(outcome)}">{_escape(outcome)}</span></td>'
            f'<td><b>{accepted:,}</b> accepted / {unknown:,} unknown {_escape(unit)}'
            f'<div class="bar" aria-label="{percent:.1f}% accepted"><i style="width:{percent:.3f}%"></i></div></td>'
            f'<td><details><summary>{_escape(method)}</summary><pre>{_escape(json.dumps(details, indent=2, ensure_ascii=False))}</pre></details></td></tr>')
    failures = report.get("failures") or []
    failures_html = "".join(f'<li><b>{_escape(f.get("path", "unknown"))}</b>: {_escape(f.get("reason", "unresolved"))}</li>' for f in failures[:1000])
    source = report.get("source") or {}
    source_name = source.get("path", "Unknown input")
    payload = json.dumps(report, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>H5Reclaim recovery report</title><style>
:root{font:15px/1.55 system-ui,sans-serif;color:#1e293b;background:#f3f5f8}*{box-sizing:border-box}body{margin:0}main{max-width:1240px;margin:44px auto;padding:0 28px}header{border-bottom:1px solid #d7dde6;padding-bottom:24px}h1{font-size:32px;letter-spacing:-1px;margin:8px 0}h2{font-size:19px}.eyebrow{font-size:12px;letter-spacing:2px;font-weight:750;color:#395bc0}p{margin:8px 0}small{display:block;color:#64748b;overflow-wrap:anywhere}.source{overflow-wrap:anywhere}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:28px 0}.card,section{background:white;border:1px solid #dce2e9;border-radius:10px}.card{padding:18px}.card b{display:block;font-size:27px}.card span{color:#64748b}section{padding:22px;margin:20px 0}.note{border-left:3px solid #4768cd;padding-left:16px}input,select,button{font:inherit;background:white;border:1px solid #cbd5e1;border-radius:6px;padding:8px 12px}input{min-width:260px}.controls{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}table{width:100%;border-collapse:collapse;text-align:left}th{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:#64748b}th,td{padding:14px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top}td:first-child{max-width:340px;overflow-wrap:anywhere}.badge{font-size:12px;border-radius:5px;padding:3px 8px;background:#edf0f5}.complete{color:#17624b;background:#e6f4ee}.partial{color:#8c5d12;background:#fff2da}.bar{height:5px;background:#e6eaf0;border-radius:3px;overflow:hidden;margin-top:8px;max-width:260px}.bar i{display:block;background:#3c62be;height:100%}summary{cursor:pointer;font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-width:500px;max-height:360px;overflow:auto}footer{font-size:12px;color:#64748b;padding:20px 0}@media(max-width:750px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:0 14px;margin:20px auto}.table-wrap{overflow:auto}table{min-width:700px}}@media print{.controls{display:none}main{margin:0}section,.card{break-inside:avoid}}
</style><main><header><div class="eyebrow">H5RECLAIM / RECOVERY EVIDENCE</div><h1>''' + _escape(report["outcome"].capitalize()) + ''' recovery</h1><p class="source">''' + _escape(source_name) + '''</p><small>Saved report version ''' + _escape(report.get("tool_version", "unknown")) + '''</small></header><div class="cards">''' + "".join(
        f'<div class="card"><b>{value:,}</b><span>{label}</span></div>' for value, label in (
            (len(records), "datasets exported"), (accepted_total, "accepted elements with reported totals"),
            (unknown_total, "unknown elements with reported totals"), (len(failures), "unresolved datasets"))) + '''</div>
<section class="note"><b>Use validity maps before analysis.</b><p>Nonaccepted positions may display a fill value. An accepted value is supported by the route's recorded evidence. Historical equality requires a suitable earlier capture.</p><small>This page is generated from the saved JSON report. It does not reopen the source or verify the current recovered file. Element totals include only routes that report element counts.</small></section>
<section><h2>Datasets</h2><div class="controls"><input id="search" aria-label="Filter dataset paths" placeholder="Filter dataset paths"><select id="outcome" aria-label="Filter outcomes"><option value="">All outcomes</option><option>complete</option><option>partial</option></select><button id="download">Download evidence JSON</button><button onclick="window.print()">Print report</button></div><div class="table-wrap"><table><thead><tr><th>Dataset / shape</th><th>Outcome</th><th>Coverage</th><th>Evidence details</th></tr></thead><tbody>''' + "".join(rows) + '''</tbody></table></div></section>''' + (
        '<section><h2>Unresolved datasets</h2><ul>' + failures_html + '</ul></section>' if failures else "") + '''<footer>H5Reclaim. Self-contained report; no external scripts, fonts or network requests.</footer></main><script type="application/json" id="evidence">''' + payload + '''</script><script>
const search=document.querySelector('#search'),outcome=document.querySelector('#outcome');function filter(){for(const row of document.querySelectorAll('tbody tr'))row.hidden=!(row.firstElementChild.textContent.toLowerCase().includes(search.value.toLowerCase())&&(!outcome.value||row.dataset.outcome===outcome.value));}search.addEventListener('input',filter);outcome.addEventListener('change',filter);document.querySelector('#download').addEventListener('click',()=>{const blob=new Blob([document.querySelector('#evidence').textContent],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='recovery-evidence.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
</script></html>'''


def write_html_report(report_path, destination):
    report_path, destination = Path(report_path), Path(destination)
    if report_path.stat().st_size > 32 << 20:
        raise ValueError("HTML report input exceeds 32 MiB")
    document = render_report_html(json.loads(report_path.read_text(encoding="utf-8")))
    with tempfile.TemporaryDirectory(prefix=".h5reclaim-html-", dir=destination.parent) as directory:
        staged = Path(directory) / "report.html"
        staged.write_text(document, encoding="utf-8")
        os.link(staged, destination)
    return destination
