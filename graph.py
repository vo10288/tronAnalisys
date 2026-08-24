"""Costruzione del grafo dei flussi: BFS per livelli, dedup, budget, metriche."""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal

import networkx as nx

from .addresses import USDT_CONTRACT
from .http import ApiError
from .parsing import Transfer, parse_native, parse_trc20

log = logging.getLogger(__name__)

BOUNDARY_TYPES = ("cex", "bridge", "exchange", "token_contract")
MAX_TXIDS_PER_EDGE = 25  # campione di riferimento, non l'elenco completo


@dataclass
class ScanConfig:
    direction: str = "both"
    depth: int = 1
    assets: tuple[str, ...] = ("trx", "trc20")
    contract: str | None = USDT_CONTRACT
    start_ms: int = 0
    end_ms: int = 0
    window_days: int = 0
    min_amounts: dict[str, Decimal] = field(default_factory=dict)
    max_nodes: int = 5_000
    max_degree: int = 500
    max_transfers_per_address: int | None = 20_000
    stop_on_exchange: bool = True
    enrich: bool = True
    enrich_max_nodes: int = 500
    enrich_workers: int = 4
    keep_transfers: bool = False

    def min_amount_for(self, asset: str) -> Decimal:
        """Soglia per asset: 5 TRX e 5 USDT non sono la stessa cosa."""
        if asset in self.min_amounts:
            return self.min_amounts[asset]
        return self.min_amounts.get("*", Decimal(0))


@dataclass
class EdgeAggregate:
    src: str
    dst: str
    asset: str
    amount: Decimal = Decimal(0)
    count: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    txids: list[str] = field(default_factory=list)

    def add(self, transfer: Transfer) -> None:
        self.amount += transfer.amount
        self.count += 1
        ts = transfer.timestamp
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        if len(self.txids) < MAX_TXIDS_PER_EDGE:
            self.txids.append(transfer.txid)


def _iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat()


class GraphBuilder:
    """Espansione BFS a partire dai seed, con dedup dei trasferimenti."""

    def __init__(self, client, config: ScanConfig, labels: dict[str, dict],
                 tronscan=None) -> None:
        self.client = client
        self.config = config
        self.labels = labels
        self.tronscan = tronscan

        self.edges: dict[tuple[str, str, str], EdgeAggregate] = {}
        self.meta: dict[str, dict] = {}
        self.transfers: list[Transfer] = []
        self._seen_transfers: set[tuple] = set()
        self.stats = {
            "transfers_seen": 0,
            "transfers_duplicated": 0,
            "transfers_below_threshold": 0,
            "addresses_visited": 0,
            "addresses_failed": 0,
            "hubs_not_expanded": 0,
            "boundaries_not_expanded": 0,
        }
        self.truncated = False
        self.interrupted = False
        self.errors: list[dict] = []

    # -------------------------------------------------------------- meta --

    def _meta(self, address: str) -> dict:
        node = self.meta.get(address)
        if node is None:
            node = {"seed": False, "level": None, "type": "", "name": ""}
            self.meta[address] = node
        return node

    def _resolve_meta(self, address: str) -> dict:
        """Etichetta manuale (prioritaria) o tag TronScan, con cache."""
        node = self._meta(address)
        if node.get("type"):
            return node
        if address in self.labels:
            node.update(self.labels[address])
        elif self.tronscan is not None:
            try:
                node.update(self.tronscan.enrich(address))
            except ApiError as exc:  # pragma: no cover - difensivo
                log.warning("Enrichment fallito per %s: %s", address, exc)
        return node

    def _is_boundary(self, address: str) -> bool:
        return self._meta(address).get("type") in BOUNDARY_TYPES

    # ------------------------------------------------------------ fetch --

    def _fetch_transfers(self, address: str) -> list[Transfer]:
        cfg = self.config
        cap = cfg.max_transfers_per_address
        out: list[Transfer] = []

        if "trc20" in cfg.assets:
            for record in self.client.fetch_trc20(
                address, cfg.direction, cfg.start_ms, cfg.end_ms,
                cfg.window_days, contract=cfg.contract, max_records=cap,
            ):
                parsed = parse_trc20(record)
                if parsed:
                    out.append(parsed)

        if "trx" in cfg.assets:
            for record in self.client.fetch_native(
                address, cfg.direction, cfg.start_ms, cfg.end_ms,
                cfg.window_days, max_records=cap,
            ):
                parsed = parse_native(record)
                if parsed:
                    out.append(parsed)

        if cap is not None and len(out) >= cap:
            log.warning("%s: raggiunto il cap di %d trasferimenti", address, cap)
            self._meta(address)["truncated"] = True
            self.truncated = True
        return out

    def _accept(self, transfer: Transfer, address: str) -> bool:
        cfg = self.config
        if cfg.direction == "in" and transfer.dst != address:
            return False
        if cfg.direction == "out" and transfer.src != address:
            return False
        if transfer.amount < cfg.min_amount_for(transfer.asset):
            self.stats["transfers_below_threshold"] += 1
            return False
        return True

    def _record(self, transfer: Transfer) -> None:
        key = (transfer.src, transfer.dst, transfer.asset)
        edge = self.edges.get(key)
        if edge is None:
            edge = EdgeAggregate(*key)
            self.edges[key] = edge
        edge.add(transfer)
        if self.config.keep_transfers:
            self.transfers.append(transfer)

    # -------------------------------------------------------------- BFS --

    def build(self, seeds: list[str]) -> nx.MultiDiGraph:
        cfg = self.config
        for seed in seeds:
            node = self._meta(seed)
            node["seed"] = True
            node["level"] = 0

        frontier: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
        enqueued = set(seeds)
        visited: set[str] = set()

        try:
            while frontier:
                address, level = frontier.popleft()
                if address in visited:
                    continue
                visited.add(address)
                node = self._resolve_meta(address)
                if node.get("level") is None:
                    node["level"] = level

                if level > 0 and cfg.stop_on_exchange and self._is_boundary(address):
                    node["expanded"] = False
                    node["boundary"] = True
                    self.stats["boundaries_not_expanded"] += 1
                    continue

                log.info("[L%d] %s", level, address)
                try:
                    transfers = self._fetch_transfers(address)
                except ApiError as exc:
                    # Un indirizzo che fallisce non deve far perdere l'intera
                    # scansione: viene marcato e la BFS prosegue.
                    log.error("Fetch fallito per %s: %s", address, exc)
                    node["fetch_error"] = True
                    self.stats["addresses_failed"] += 1
                    self.errors.append({"address": address, "error": str(exc)})
                    self.truncated = True
                    continue

                self.stats["addresses_visited"] += 1
                node["expanded"] = True
                counterparties: set[str] = set()

                for transfer in transfers:
                    self.stats["transfers_seen"] += 1
                    if not self._accept(transfer, address):
                        continue
                    key = transfer.dedup_key
                    if key in self._seen_transfers:
                        # Stessa tx vista dal lato opposto: senza questo
                        # controllo gli importi vengono contati due volte.
                        self.stats["transfers_duplicated"] += 1
                        continue
                    self._seen_transfers.add(key)
                    self._record(transfer)
                    other = transfer.counterparty(address)
                    if other and other != address:
                        counterparties.add(other)

                node["degree_observed"] = len(counterparties)

                if len(counterparties) > cfg.max_degree:
                    # Tipico di hot wallet e sweeper: espanderli fa esplodere
                    # il grafo senza aggiungere informazione investigativa.
                    log.warning(
                        "%s ha %d controparti (>%d): non lo espando",
                        address, len(counterparties), cfg.max_degree,
                    )
                    node["hub"] = True
                    self.stats["hubs_not_expanded"] += 1
                    self.truncated = True
                    continue

                if level >= cfg.depth:
                    continue

                for cp in sorted(counterparties):
                    if cp in enqueued:
                        continue
                    if len(enqueued) >= cfg.max_nodes:
                        log.warning("Budget di %d nodi raggiunto", cfg.max_nodes)
                        self.truncated = True
                        break
                    enqueued.add(cp)
                    frontier.append((cp, level + 1))
        except KeyboardInterrupt:  # pragma: no cover - interattivo
            log.warning("Interrotto: esporto il grafo parziale")
            self.interrupted = True
            self.truncated = True

        self._enrich_remaining()
        return self.to_networkx()

    # --------------------------------------------------------- enrichment --

    def _enrich_remaining(self) -> None:
        """Arricchisce i nodi non ancora etichettati, in parallelo e con cap.

        La versione precedente chiamava TronScan una volta per nodo in
        sequenza: su qualche migliaio di nodi erano ore. Qui si arricchiscono
        prima i nodi piu' rilevanti (per volume) e solo fino a enrich_max_nodes.
        """
        if not self.config.enrich or self.tronscan is None:
            return

        volumes: dict[str, Decimal] = {}
        for edge in self.edges.values():
            volumes[edge.src] = volumes.get(edge.src, Decimal(0)) + edge.amount
            volumes[edge.dst] = volumes.get(edge.dst, Decimal(0)) + edge.amount

        pending = [a for a in self._all_addresses() if not self._meta(a).get("type")]
        pending.sort(key=lambda a: volumes.get(a, Decimal(0)), reverse=True)
        pending = pending[: self.config.enrich_max_nodes]
        if not pending:
            return

        log.info("Arricchimento di %d nodi via TronScan...", len(pending))
        workers = max(1, self.config.enrich_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for address, info in zip(pending, pool.map(self.tronscan.enrich, pending)):
                if info:
                    self._meta(address).update(info)

    def _all_addresses(self) -> set[str]:
        addresses = set(self.meta)
        for edge in self.edges.values():
            addresses.add(edge.src)
            addresses.add(edge.dst)
        return addresses

    # ------------------------------------------------------------ output --

    def to_networkx(self) -> nx.MultiDiGraph:
        graph = nx.MultiDiGraph()
        graph.graph.update(
            {
                "direction": self.config.direction,
                "depth": self.config.depth,
                "assets": ",".join(self.config.assets),
                "truncated": self.truncated,
                "interrupted": self.interrupted,
            }
        )

        for address in sorted(self._all_addresses()):
            meta = self._meta(address)
            graph.add_node(
                address,
                label=address,
                seed=bool(meta.get("seed", False)),
                level=int(meta.get("level") if meta.get("level") is not None else -1),
                type=meta.get("type") or "address",
                name=meta.get("name", "") or "",
                public_tag=meta.get("public_tag", "") or "",
                grey_tag=meta.get("grey_tag", "") or "",
                red_tag=meta.get("red_tag", "") or "",
                blacklist=bool(meta.get("blacklist", False)),
                fraud=bool(meta.get("fraud", False)),
                hub=bool(meta.get("hub", False)),
                boundary=bool(meta.get("boundary", False)),
                expanded=bool(meta.get("expanded", False)),
                truncated=bool(meta.get("truncated", False)),
                fetch_error=bool(meta.get("fetch_error", False)),
            )

        for (src, dst, asset), edge in self.edges.items():
            graph.add_edge(
                src, dst, key=asset,
                asset=asset,
                amount=float(edge.amount),
                amount_exact=str(edge.amount),
                weight=float(edge.amount),
                count=edge.count,
                first_ts=edge.first_ts or 0,
                last_ts=edge.last_ts or 0,
                first_seen=_iso(edge.first_ts),
                last_seen=_iso(edge.last_ts),
                sample_txids=";".join(edge.txids),
            )

        _finalize_node_metrics(graph)
        return graph


def _finalize_node_metrics(graph: nx.MultiDiGraph) -> None:
    for node in graph.nodes:
        in_amt = sum(d["amount"] for _, _, d in graph.in_edges(node, data=True))
        out_amt = sum(d["amount"] for _, _, d in graph.out_edges(node, data=True))
        in_cnt = sum(d["count"] for _, _, d in graph.in_edges(node, data=True))
        out_cnt = sum(d["count"] for _, _, d in graph.out_edges(node, data=True))
        attrs = graph.nodes[node]
        attrs["total_in"] = round(in_amt, 6)
        attrs["total_out"] = round(out_amt, 6)
        attrs["net_flow"] = round(in_amt - out_amt, 6)
        attrs["tx_in"] = in_cnt
        attrs["tx_out"] = out_cnt
        attrs["in_degree"] = graph.in_degree(node)
        attrs["out_degree"] = graph.out_degree(node)


def compute_network_metrics(graph: nx.MultiDiGraph, *,
                            betweenness_limit: int = 3_000) -> dict:
    """Aggiunge community (Louvain) e centralita' come attributi dei nodi.

    Nota metodologica: una community e' un'ipotesi strutturale, non un
    cluster di indirizzi attribuibile allo stesso soggetto.
    """
    info: dict = {"communities": 0, "betweenness": False}
    if graph.number_of_nodes() == 0:
        return info

    simple = nx.Graph()
    simple.add_nodes_from(graph.nodes)
    for src, dst, data in graph.edges(data=True):
        if src == dst:
            continue
        weight = simple.get_edge_data(src, dst, default={}).get("weight", 0.0)
        simple.add_edge(src, dst, weight=weight + float(data.get("amount", 0.0)))

    try:
        communities = nx.community.louvain_communities(simple, weight="weight", seed=42)
        for index, members in enumerate(communities):
            for node in members:
                graph.nodes[node]["community"] = index
        info["communities"] = len(communities)
    except Exception as exc:  # pragma: no cover - dipende dalla versione di nx
        log.warning("Louvain non disponibile: %s", exc)
        for node in graph.nodes:
            graph.nodes[node]["community"] = -1

    degree = nx.degree_centrality(simple)
    for node, value in degree.items():
        graph.nodes[node]["degree_centrality"] = round(value, 6)

    if graph.number_of_nodes() <= betweenness_limit:
        between = nx.betweenness_centrality(simple, weight=None)
        for node, value in between.items():
            graph.nodes[node]["betweenness"] = round(value, 6)
        info["betweenness"] = True
    else:
        log.info(
            "Betweenness saltata: %d nodi oltre il limite di %d",
            graph.number_of_nodes(), betweenness_limit,
        )
        for node in graph.nodes:
            graph.nodes[node]["betweenness"] = 0.0
    return info


def classify_asset(client, address: str, start_ms: int, end_ms: int,
                   sample: int = 200) -> str:
    """Stima se un indirizzo opera in TRX, TRC20 o entrambi.

    A differenza della versione precedente il confronto e' simmetrico: da
    entrambi i lati si contano solo i trasferimenti effettivamente parsati.
    """
    trc20 = 0
    for record in client.fetch_trc20(address, "both", start_ms, end_ms,
                                     0, contract=None, max_records=sample):
        if parse_trc20(record):
            trc20 += 1

    native = 0
    for record in client.fetch_native(address, "both", start_ms, end_ms,
                                      0, max_records=sample):
        if parse_native(record):
            native += 1

    if trc20 == 0 and native == 0:
        return "vuoto"
    if trc20 >= 3 * max(native, 1):
        return "trc20"
    if native >= 3 * max(trc20, 1):
        return "trx"
    return "mixed"
