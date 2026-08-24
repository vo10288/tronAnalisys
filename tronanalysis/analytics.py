from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import networkx as nx


@dataclass(slots=True)
class PatternFinding:
    pattern: str
    node: str
    score: float
    rationale: str


def collapsed_digraph(graph: nx.MultiDiGraph) -> nx.DiGraph:
    out = nx.DiGraph()
    for node, attrs in graph.nodes(data=True):
        out.add_node(node, **attrs)
    for src, dst, data in graph.edges(data=True):
        amount_weight = float(data.get("weight", data.get("amount", 0.0)) or 0.0)
        count = int(data.get("count", 1) or 1)
        # Cross-asset amounts are not comparable; centrality/community use transaction
        # frequency as an asset-neutral weight. Nominal amount is retained separately.
        if out.has_edge(src, dst):
            out[src][dst]["weight"] += count
            out[src][dst]["count"] += count
            out[src][dst]["amount_weight"] += amount_weight
        else:
            out.add_edge(src, dst, weight=count, count=count, amount_weight=amount_weight)
    return out


def add_graph_analytics(graph: nx.MultiDiGraph, betweenness_limit: int = 2000, seed: int = 42) -> dict[str, Any]:
    dg = collapsed_digraph(graph)
    ug = dg.to_undirected()
    summary: dict[str, Any] = {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "components": nx.number_connected_components(ug) if ug.number_of_nodes() else 0,
    }
    if not dg.number_of_nodes():
        return summary

    pagerank = nx.pagerank(dg, weight="weight") if dg.number_of_edges() else {n: 0.0 for n in dg}
    if dg.number_of_nodes() <= betweenness_limit:
        betweenness = nx.betweenness_centrality(dg, weight=None, normalized=True)
        summary["betweenness_mode"] = "exact"
    else:
        k = min(500, max(50, int(dg.number_of_nodes() ** 0.5 * 10)))
        betweenness = nx.betweenness_centrality(dg, k=k, weight=None, normalized=True, seed=seed)
        summary["betweenness_mode"] = f"sampled:{k}"

    communities: list[set[str]] = []
    if ug.number_of_edges():
        try:
            communities = list(nx.community.louvain_communities(ug, weight="weight", seed=seed))
            summary["community_algorithm"] = "louvain"
        except (AttributeError, nx.NetworkXError):
            communities = list(nx.community.greedy_modularity_communities(ug, weight="weight"))
            summary["community_algorithm"] = "greedy_modularity"
    else:
        communities = [{n} for n in ug]
        summary["community_algorithm"] = "isolated"
    community_id = {node: idx for idx, community in enumerate(communities, start=1) for node in community}
    summary["communities"] = len(communities)

    for node in dg.nodes:
        attrs = graph.nodes[node]
        attrs["in_degree"] = int(dg.in_degree(node))
        attrs["out_degree"] = int(dg.out_degree(node))
        attrs["degree"] = int(dg.degree(node))
        attrs["pagerank"] = float(pagerank.get(node, 0.0))
        attrs["betweenness"] = float(betweenness.get(node, 0.0))
        attrs["community"] = int(community_id.get(node, 0))
    summary["top_pagerank"] = sorted(pagerank.items(), key=lambda x: x[1], reverse=True)[:10]
    summary["top_betweenness"] = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:10]
    return summary


def shortest_paths(graph: nx.MultiDiGraph, source: str, target: str, max_paths: int = 10) -> list[list[str]]:
    dg = collapsed_digraph(graph)
    if source not in dg or target not in dg:
        return []
    try:
        generator = nx.shortest_simple_paths(dg, source, target)
        paths: list[list[str]] = []
        for path in generator:
            paths.append(path)
            if len(paths) >= max_paths:
                break
        return paths
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def detect_patterns(graph: nx.MultiDiGraph, min_degree: int = 5) -> list[PatternFinding]:
    dg = collapsed_digraph(graph)
    findings: list[PatternFinding] = []
    for node in dg.nodes:
        indeg = dg.in_degree(node)
        outdeg = dg.out_degree(node)
        if indeg >= min_degree and indeg >= max(2 * outdeg, min_degree):
            findings.append(PatternFinding("fan_in/collector", node, float(indeg), f"{indeg} sorgenti vs {outdeg} destinazioni"))
        if outdeg >= min_degree and outdeg >= max(2 * indeg, min_degree):
            findings.append(PatternFinding("fan_out/distributor", node, float(outdeg), f"{outdeg} destinazioni vs {indeg} sorgenti"))
        if indeg == 1 and outdeg == 1:
            incoming_by_asset: dict[str, float] = defaultdict(float)
            outgoing_by_asset: dict[str, float] = defaultdict(float)
            for _, _, data in graph.in_edges(node, data=True):
                incoming_by_asset[str(data.get("asset") or "UNKNOWN")] += float(data.get("amount", 0) or 0)
            for _, _, data in graph.out_edges(node, data=True):
                outgoing_by_asset[str(data.get("asset") or "UNKNOWN")] += float(data.get("amount", 0) or 0)
            best: tuple[float, str] | None = None
            for asset in set(incoming_by_asset) & set(outgoing_by_asset):
                amount_in, amount_out = incoming_by_asset[asset], outgoing_by_asset[asset]
                if amount_in <= 0 or amount_out <= 0:
                    continue
                ratio = min(amount_in, amount_out) / max(amount_in, amount_out)
                if best is None or ratio > best[0]:
                    best = (ratio, asset)
            if best and best[0] >= 0.80:
                ratio, asset = best
                findings.append(PatternFinding("peel_like", node, ratio, f"in/out {asset} simili ({ratio:.1%}); euristica, non attribution"))
    findings.sort(key=lambda f: f.score, reverse=True)
    return findings
