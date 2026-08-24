from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

import networkx as nx

from .models import NodeAttribution, Transfer
from .tron import TronGridProvider, TronScanEnricher

LOG = logging.getLogger(__name__)
STOP_TYPES = {"cex", "exchange", "bridge"}


@dataclass(slots=True)
class InvestigationOptions:
    direction: str = "both"
    depth: int = 2
    asset: str = "auto"
    contract: str | None = None
    start_ms: int = 0
    end_ms: int = 0
    window_days: int = 0
    min_amount: float = 0.0
    max_nodes: int = 5000
    max_edges: int = 20000
    max_counterparties_per_node: int = 500
    stop_on_exchange: bool = True
    enrich: bool = True


@dataclass(slots=True)
class InvestigationResult:
    graph: nx.MultiDiGraph
    transfers: list[Transfer]
    truncated: bool
    truncation_reasons: list[str]


def build_graph(
    provider: TronGridProvider,
    seeds: list[str],
    options: InvestigationOptions,
    labels: dict[str, NodeAttribution] | None = None,
    enricher: TronScanEnricher | None = None,
) -> InvestigationResult:
    labels = labels or {}
    graph = nx.MultiDiGraph(chain="TRON")
    for seed in seeds:
        graph.add_node(seed, seed=True, discovery_level=0)

    queue = deque((seed, 0) for seed in seeds)
    visited: set[str] = set()
    transfers_by_uid: dict[str, Transfer] = {}
    reasons: list[str] = []
    truncated = False

    while queue:
        address, level = queue.popleft()
        if address in visited or level > options.depth:
            continue
        visited.add(address)
        _apply_best_attribution(graph, address, labels, enricher, options.enrich)
        node_type = graph.nodes[address].get("type", "address")
        if level > 0 and options.stop_on_exchange and node_type in STOP_TYPES:
            graph.nodes[address]["expansion_stopped"] = "entity_boundary"
            continue

        local: dict[str, Transfer] = {}
        try:
            for transfer in provider.transfers(
                address=address,
                direction=options.direction,
                start_ms=options.start_ms,
                end_ms=options.end_ms,
                window_days=options.window_days,
                asset=options.asset,
                contract=options.contract,
            ):
                if transfer.amount < options.min_amount:
                    continue
                if options.direction == "in" and transfer.dst != address:
                    continue
                if options.direction == "out" and transfer.src != address:
                    continue
                local[transfer.uid] = transfer
        except Exception as exc:
            LOG.error("Acquisizione fallita per %s: %s", address, exc)
            graph.nodes[address]["acquisition_error"] = str(exc)[:500]
            continue

        # Expand toward the most relevant counterparties first, not set/random order.
        counterparty_relevance: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for transfer in local.values():
            other = transfer.dst if transfer.src == address else transfer.src
            counterparty_relevance[other][0] += 1.0
            counterparty_relevance[other][1] += abs(transfer.amount)
        # Transaction count is the primary ranking key: amounts from different assets
        # are not directly comparable. Nominal volume is only a tie-breaker.
        ranked = sorted(
            counterparty_relevance,
            key=lambda n: (counterparty_relevance[n][0], counterparty_relevance[n][1]),
            reverse=True,
        )
        if len(ranked) > options.max_counterparties_per_node:
            ranked = ranked[: options.max_counterparties_per_node]
            graph.nodes[address]["counterparties_truncated"] = True
            truncated = True
            reasons.append(f"max_counterparties_per_node:{address}")
        allowed_counterparties = set(ranked)

        for transfer in local.values():
            other = transfer.dst if transfer.src == address else transfer.src
            new_nodes = {n for n in (transfer.src, transfer.dst) if n not in graph}
            if graph.number_of_nodes() + len(new_nodes) > options.max_nodes:
                truncated = True
                reasons.append("max_nodes")
                continue
            edge_key = _edge_key(transfer)
            if not graph.has_edge(transfer.src, transfer.dst, key=edge_key) and graph.number_of_edges() >= options.max_edges:
                truncated = True
                reasons.append("max_edges")
                queue.clear()
                break
            if transfer.uid not in transfers_by_uid:
                transfers_by_uid[transfer.uid] = transfer
                _add_transfer(graph, transfer)
            if level < options.depth and other in allowed_counterparties and other not in visited and other in graph:
                old_level = graph.nodes[other].get("discovery_level")
                graph.nodes[other]["discovery_level"] = level + 1 if old_level is None else min(old_level, level + 1)
                queue.append((other, level + 1))

    # Enrich leaves too. Manual labels always win.
    for node in list(graph.nodes):
        _apply_best_attribution(graph, node, labels, enricher, options.enrich)
        graph.nodes[node].setdefault("seed", node in seeds)
        graph.nodes[node].setdefault("discovery_level", options.depth + 1)

    _finalize_amount_metrics(graph)
    # de-duplicate reasons while preserving order
    reasons = list(dict.fromkeys(reasons))
    return InvestigationResult(graph, list(transfers_by_uid.values()), truncated, reasons)


def _apply_best_attribution(
    graph: nx.MultiDiGraph,
    address: str,
    labels: dict[str, NodeAttribution],
    enricher: TronScanEnricher | None,
    enrich: bool,
) -> None:
    if address not in graph:
        graph.add_node(address)
    current_source = graph.nodes[address].get("attribution_source")
    if address in labels:
        graph.nodes[address].update(labels[address].as_graph_attrs())
        return
    if current_source:
        return
    if enrich and enricher:
        graph.nodes[address].update(enricher.enrich(address).as_graph_attrs())
    else:
        graph.nodes[address].update(NodeAttribution().as_graph_attrs())


def _edge_key(transfer: Transfer) -> str:
    return f"{transfer.asset}:{transfer.token_contract or 'native'}"


def _add_transfer(graph: nx.MultiDiGraph, transfer: Transfer) -> None:
    key = _edge_key(transfer)
    attrs = graph.get_edge_data(transfer.src, transfer.dst, key=key)
    ts = transfer.timestamp_ms or 0
    if attrs:
        attrs["amount"] = float(attrs.get("amount", 0.0)) + transfer.amount
        attrs["count"] = int(attrs.get("count", 0)) + 1
        attrs["weight"] = float(attrs.get("weight", 0.0)) + abs(transfer.amount)
        if ts:
            first = int(attrs.get("first_seen_ms") or ts)
            last = int(attrs.get("last_seen_ms") or ts)
            attrs["first_seen_ms"] = min(first, ts)
            attrs["last_seen_ms"] = max(last, ts)
        txids = attrs.get("txids", "")
        txid_set = [x for x in txids.split(";") if x]
        if transfer.txid not in txid_set and len(txid_set) < 100:
            txid_set.append(transfer.txid)
        attrs["txids"] = ";".join(txid_set)
    else:
        graph.add_edge(
            transfer.src,
            transfer.dst,
            key=key,
            asset=transfer.asset,
            token_contract=transfer.token_contract,
            amount=float(transfer.amount),
            count=1,
            weight=abs(float(transfer.amount)),
            first_seen_ms=ts,
            last_seen_ms=ts,
            txids=transfer.txid,
        )


def _finalize_amount_metrics(graph: nx.MultiDiGraph) -> None:
    for node in graph.nodes:
        incoming = list(graph.in_edges(node, data=True))
        outgoing = list(graph.out_edges(node, data=True))
        in_by_asset: dict[str, float] = defaultdict(float)
        out_by_asset: dict[str, float] = defaultdict(float)
        for _, _, d in incoming:
            in_by_asset[str(d.get("asset") or "UNKNOWN")] += float(d.get("amount", 0) or 0)
        for _, _, d in outgoing:
            out_by_asset[str(d.get("asset") or "UNKNOWN")] += float(d.get("amount", 0) or 0)
        # Backward-compatible nominal totals; use *_by_asset for financial interpretation.
        graph.nodes[node]["total_in"] = round(sum(in_by_asset.values()), 8)
        graph.nodes[node]["total_out"] = round(sum(out_by_asset.values()), 8)
        graph.nodes[node]["total_in_by_asset"] = json.dumps({k: round(v, 8) for k, v in sorted(in_by_asset.items())})
        graph.nodes[node]["total_out_by_asset"] = json.dumps({k: round(v, 8) for k, v in sorted(out_by_asset.items())})
        graph.nodes[node]["tx_in_count"] = sum(int(d.get("count", 0)) for _, _, d in incoming)
        graph.nodes[node]["tx_out_count"] = sum(int(d.get("count", 0)) for _, _, d in outgoing)
        graph.nodes[node].setdefault("type", "address")
        graph.nodes[node].setdefault("name", "")
        graph.nodes[node].setdefault("risk", False)
        graph.nodes[node].setdefault("is_contract", False)
        graph.nodes[node].setdefault("attribution_source", "")
        graph.nodes[node].setdefault("attribution_confidence", "unknown")
        graph.nodes[node].setdefault("attribution_evidence", "")
