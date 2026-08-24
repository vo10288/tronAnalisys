from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import networkx as nx

from .analytics import PatternFinding
from .utils import sha256_file, utc_now_iso


def write_report(
    graph: nx.MultiDiGraph,
    analytics: dict[str, Any],
    patterns: list[PatternFinding],
    output_files: dict[str, Path],
    report_path: str | Path,
    provenance: list[dict[str, Any]],
    seeds: list[str],
    truncated: bool,
    truncation_reasons: list[str],
) -> Path:
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hashes = {name: sha256_file(file_path) for name, file_path in output_files.items() if file_path.is_file()}
    top_nodes = sorted(graph.nodes(data=True), key=lambda x: float(x[1].get("pagerank", 0)), reverse=True)[:20]
    risk_nodes = [(n, d) for n, d in graph.nodes(data=True) if d.get("risk") or d.get("red_tag")]
    unique_sources = []
    seen = set()
    for item in provenance:
        key = (item.get("url"), tuple(item.get("parameter_names", [])))
        if key not in seen:
            seen.add(key)
            unique_sources.append(item)

    def e(value: Any) -> str:
        return html.escape(str(value))

    rows_top = "".join(
        f"<tr><td><code>{e(n)}</code></td><td>{e(d.get('name',''))}</td><td>{e(d.get('type',''))}</td><td>{float(d.get('pagerank',0)):.6g}</td><td>{float(d.get('betweenness',0)):.6g}</td><td>{e(d.get('community',0))}</td></tr>"
        for n, d in top_nodes
    ) or "<tr><td colspan='6'>Nessun dato</td></tr>"
    rows_patterns = "".join(
        f"<tr><td>{e(p.pattern)}</td><td><code>{e(p.node)}</code></td><td>{p.score:.4g}</td><td>{e(p.rationale)}</td></tr>"
        for p in patterns[:100]
    ) or "<tr><td colspan='4'>Nessun pattern euristico rilevato</td></tr>"
    rows_risk = "".join(
        f"<tr><td><code>{e(n)}</code></td><td>{e(d.get('name',''))}</td><td>{e(d.get('red_tag',''))}</td><td>{e(d.get('attribution_source',''))}</td></tr>"
        for n, d in risk_nodes
    ) or "<tr><td colspan='4'>Nessun nodo marcato come risk dalle fonti usate</td></tr>"
    rows_hash = "".join(f"<tr><td>{e(k)}</td><td><code>{e(v)}</code></td></tr>" for k, v in hashes.items())
    rows_sources = "".join(
        f"<tr><td>{e(x.get('url',''))}</td><td>{e(', '.join(x.get('parameter_names',[])))}</td><td>{e(x.get('accessed_utc',''))}</td></tr>"
        for x in unique_sources
    )
    banner = ""
    if truncated:
        banner = f"<div class='warn'><b>Dataset troncato dai limiti di sicurezza:</b> {e(', '.join(truncation_reasons))}</div>"

    document = f"""<!doctype html>
<html lang='it'><head><meta charset='utf-8'><title>tronAnalisys Investigation Report</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:1200px;margin:30px auto;padding:0 20px;color:#202124}}h1,h2{{color:#111}}code{{font-size:.9em}}table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border:1px solid #ddd;padding:7px;text-align:left;vertical-align:top}}th{{background:#f3f3f3}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}}.card{{border:1px solid #ddd;border-radius:8px;padding:12px}}.warn{{background:#fff3cd;border:1px solid #ffe69c;padding:12px;border-radius:8px}}.note{{background:#eef5ff;padding:12px;border-radius:8px}}
</style></head><body>
<h1>tronAnalisys — Investigation Report</h1>
<p>Generato: {e(utc_now_iso())}</p>{banner}
<div class='grid'><div class='card'><b>Nodi</b><br>{graph.number_of_nodes()}</div><div class='card'><b>Archi aggregati</b><br>{graph.number_of_edges()}</div><div class='card'><b>Community</b><br>{e(analytics.get('communities',0))}</div><div class='card'><b>Componenti</b><br>{e(analytics.get('components',0))}</div><div class='card'><b>Nodi risk</b><br>{len(risk_nodes)}</div></div>
<h2>Perimetro</h2><p><b>Seed:</b> {e(', '.join(seeds))}</p>
<div class='note'><b>Nota metodologica:</b> una community o un pattern di grafo è un'indicazione analitica, non prova che gli indirizzi appartengano alla stessa persona o entità. Le attribution riportano separatamente fonte, confidenza ed evidenza.</div>
<h2>Nodi centrali</h2><table><tr><th>Address</th><th>Nome</th><th>Tipo</th><th>PageRank</th><th>Betweenness</th><th>Community</th></tr>{rows_top}</table>
<h2>Pattern euristici</h2><table><tr><th>Pattern</th><th>Nodo</th><th>Score</th><th>Motivazione</th></tr>{rows_patterns}</table>
<h2>Risk / red-tag</h2><table><tr><th>Address</th><th>Nome</th><th>Red tag</th><th>Fonte</th></tr>{rows_risk}</table>
<h2>Provenance API</h2><table><tr><th>Endpoint</th><th>Parametri (solo nomi)</th><th>Accesso UTC</th></tr>{rows_sources}</table>
<h2>Integrità output (SHA-256)</h2><table><tr><th>File</th><th>SHA-256</th></tr>{rows_hash}</table>
<h2>Analisi tecnica</h2><pre>{e(json.dumps(analytics, ensure_ascii=False, indent=2, default=str))}</pre>
</body></html>"""
    path.write_text(document, encoding="utf-8")
    return path
