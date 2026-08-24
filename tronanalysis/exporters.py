from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import networkx as nx

from .models import Transfer
from .utils import iso_utc


def export_all(graph: nx.MultiDiGraph, transfers: list[Transfer], basename: str | Path) -> dict[str, Path]:
    base = Path(basename)
    base.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        "gexf": base.with_suffix(".gexf"),
        "graphml": base.with_suffix(".graphml"),
        "nodes_csv": Path(f"{base}_nodes.csv"),
        "edges_csv": Path(f"{base}_edges.csv"),
        "transfers_csv": Path(f"{base}_transfers.csv"),
        "neo4j_nodes_csv": Path(f"{base}_neo4j_nodes.csv"),
        "neo4j_relationships_csv": Path(f"{base}_neo4j_relationships.csv"),
        "graph_json": Path(f"{base}.json"),
    }
    safe = _gexf_safe_copy(graph)
    nx.write_gexf(safe, paths["gexf"])
    nx.write_graphml(safe, paths["graphml"])
    _write_nodes(graph, paths["nodes_csv"])
    _write_edges(graph, paths["edges_csv"])
    _write_transfers(transfers, paths["transfers_csv"])
    _write_neo4j(graph, paths["neo4j_nodes_csv"], paths["neo4j_relationships_csv"])
    paths["graph_json"].write_text(json.dumps(nx.node_link_data(graph, edges="links"), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return paths


def _gexf_safe_copy(graph: nx.MultiDiGraph) -> nx.MultiDiGraph:
    out = nx.MultiDiGraph(**{k: _scalar(v) for k, v in graph.graph.items()})
    for node, attrs in graph.nodes(data=True):
        out.add_node(node, **{k: _scalar(v) for k, v in attrs.items()})
    for src, dst, key, attrs in graph.edges(keys=True, data=True):
        out.add_edge(src, dst, key=key, **{k: _scalar(v) for k, v in attrs.items()})
    return out


def _scalar(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _write_nodes(graph: nx.MultiDiGraph, path: Path) -> None:
    keys = sorted({k for _, data in graph.nodes(data=True) for k in data})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Id", "Label", *keys])
        w.writeheader()
        for node, data in graph.nodes(data=True):
            label = data.get("name") or node
            w.writerow({"Id": node, "Label": label, **data})


def _write_edges(graph: nx.MultiDiGraph, path: Path) -> None:
    keys = sorted({k for _, _, data in graph.edges(data=True) for k in data})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Source", "Target", "Type", *keys])
        w.writeheader()
        for src, dst, data in graph.edges(data=True):
            w.writerow({"Source": src, "Target": dst, "Type": "Directed", **data})


def _write_transfers(transfers: list[Transfer], path: Path) -> None:
    fields = ["txid", "src", "dst", "asset", "token_contract", "amount", "timestamp_ms", "timestamp_utc", "source", "uid"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in sorted(transfers, key=lambda x: (x.timestamp_ms or 0, x.txid)):
            w.writerow({**t.as_dict(), "timestamp_utc": iso_utc(t.timestamp_ms), "uid": t.uid})


def _write_neo4j(graph: nx.MultiDiGraph, nodes_path: Path, rels_path: Path) -> None:
    with nodes_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["address:ID", "name", "type", "community:int", "risk:boolean", ":LABEL"])
        for node, d in graph.nodes(data=True):
            node_type = str(d.get("type") or "Address").replace(" ", "_")
            w.writerow([node, d.get("name", ""), node_type, d.get("community", 0), str(bool(d.get("risk"))).lower(), f"Address;{node_type}"])
    with rels_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([":START_ID", ":END_ID", "asset", "amount:double", "count:int", "first_seen_ms:long", "last_seen_ms:long", ":TYPE"])
        for src, dst, d in graph.edges(data=True):
            w.writerow([src, dst, d.get("asset", ""), d.get("amount", 0), d.get("count", 0), d.get("first_seen_ms", 0), d.get("last_seen_ms", 0), "TRANSFERRED_TO"])
