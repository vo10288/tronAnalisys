"""Export: GEXF per Gephi, CSV nodi/archi/trasferimenti, manifest di run."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import logging
import os

import networkx as nx

log = logging.getLogger(__name__)

NODE_COLUMNS = [
    "Id", "Label", "seed", "level", "type", "name", "public_tag", "grey_tag",
    "red_tag", "blacklist", "fraud", "hub", "boundary", "expanded",
    "truncated", "fetch_error", "total_in", "total_out", "net_flow",
    "tx_in", "tx_out", "in_degree", "out_degree", "community",
    "degree_centrality", "betweenness",
]

EDGE_COLUMNS = [
    "Source", "Target", "Type", "asset", "amount", "amount_exact", "count",
    "weight", "first_seen", "last_seen", "sample_txids",
]

TRANSFER_COLUMNS = ["timestamp", "datetime_utc", "txid", "src", "dst", "asset",
                    "amount", "contract"]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def export_graph(graph: nx.MultiDiGraph, basename: str, *,
                 transfers=None, manifest: dict | None = None) -> list[str]:
    """Scrive gli output e ritorna i percorsi dei file creati."""
    directory = os.path.dirname(os.path.abspath(basename))
    os.makedirs(directory, exist_ok=True)

    paths: list[str] = []

    gexf_path = f"{basename}.gexf"
    nx.write_gexf(graph, gexf_path)
    paths.append(gexf_path)

    nodes_path = f"{basename}_nodes.csv"
    with open(nodes_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(NODE_COLUMNS)
        for node, data in graph.nodes(data=True):
            writer.writerow(
                [node, node] + [data.get(col, "") for col in NODE_COLUMNS[2:]]
            )
    paths.append(nodes_path)

    edges_path = f"{basename}_edges.csv"
    with open(edges_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EDGE_COLUMNS)
        for src, dst, data in graph.edges(data=True):
            writer.writerow(
                [src, dst, "Directed"]
                + [data.get(col, "") for col in EDGE_COLUMNS[3:]]
            )
    paths.append(edges_path)

    if transfers:
        transfers_path = f"{basename}_transfers.csv"
        with open(transfers_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(TRANSFER_COLUMNS)
            for t in sorted(transfers, key=lambda x: x.timestamp):
                writer.writerow([
                    t.timestamp,
                    dt.datetime.fromtimestamp(
                        t.timestamp / 1000, tz=dt.timezone.utc
                    ).isoformat(),
                    t.txid, t.src, t.dst, t.asset, str(t.amount),
                    t.contract or "",
                ])
        paths.append(transfers_path)

    if manifest is not None:
        manifest_path = f"{basename}_manifest.json"
        payload = dict(manifest)
        payload["outputs"] = {
            os.path.basename(p): {"sha256": _sha256(p), "bytes": os.path.getsize(p)}
            for p in paths
        }
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        paths.append(manifest_path)

    return paths
