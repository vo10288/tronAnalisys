"""Interfaccia a riga di comando di tronAnalisys."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation

from . import __version__
from .addresses import USDT_CONTRACT, load_addresses, load_keys, load_labels
from .export import export_graph
from .graph import GraphBuilder, ScanConfig, classify_asset, compute_network_metrics
from .http import RateLimiter, HttpStats
from .trongrid import TronGrid
from .tronscan import TronScan

log = logging.getLogger("tron_analysis")


def to_ms(value: str | None, default: int) -> int:
    """Converte 'YYYY-MM-DD' o un timestamp in millisecondi."""
    if value is None:
        return default
    value = value.strip()
    if value.isdigit():
        number = int(value)
        # Tolleranza: un timestamp in secondi viene promosso a millisecondi.
        return number * 1000 if number < 10_000_000_000 else number
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Data non valida: {value!r} (usa YYYY-MM-DD o un timestamp)"
        ) from exc
    return int(parsed.timestamp() * 1000)


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"Importo non valido: {value!r}") from exc


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tron-analysis",
        description="Analisi del grafo dei flussi su rete TRON (TRX/TRC20).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", required=True,
                   help="File con gli indirizzi seed (.csv, .txt o testo).")
    p.add_argument("-o", "--output", default="tron_graph",
                   help="Prefisso dei file di output.")

    scan = p.add_argument_group("scansione")
    scan.add_argument("--direction", choices=["in", "out", "both"], default="both")
    scan.add_argument("--depth", type=int, default=1,
                      help="Livelli di connessione (hop) da espandere.")
    scan.add_argument("--asset", choices=["auto", "trx", "trc20", "both"],
                      default="auto",
                      help="'auto' campiona i seed e sceglie gli endpoint da usare.")
    scan.add_argument("--contract", default=USDT_CONTRACT,
                      help="Contratto TRC20 da seguire; 'all' per non filtrare. "
                           "ATTENZIONE: il default segue solo USDT anche con "
                           "--asset both.")
    scan.add_argument("--start", default=None, help="Data 'YYYY-MM-DD' o timestamp.")
    scan.add_argument("--end", default=None, help="Data 'YYYY-MM-DD' o timestamp.")
    scan.add_argument("--window-days", type=int, default=0,
                      help="0 = finestra unica con paginazione a cursore.")
    scan.add_argument("--strict-addresses", action="store_true",
                      help="Scarta i seed con checksum base58 non valido.")

    limits = p.add_argument_group("soglie e budget")
    limits.add_argument("--min-amount", type=_decimal, default=Decimal(0),
                        help="Soglia minima per tutti gli asset senza soglia propria.")
    limits.add_argument("--min-amount-trx", type=_decimal, default=None,
                        help="Soglia minima in TRX.")
    limits.add_argument("--min-amount-trc20", type=_decimal, default=None,
                        help="Soglia minima per i token TRC20 (unita' del token).")
    limits.add_argument("--max-nodes", type=int, default=5000,
                        help="Numero massimo di indirizzi accodati per l'espansione.")
    limits.add_argument("--max-degree", type=int, default=500,
                        help="Oltre questo numero di controparti il nodo non "
                             "viene espanso (hot wallet / sweeper).")
    limits.add_argument("--max-transfers", type=int, default=20000,
                        help="Cap di trasferimenti scaricati per indirizzo (0 = nessuno).")
    limits.add_argument("--no-stop-on-exchange", dest="stop_on_exchange",
                        action="store_false",
                        help="Espandi la BFS anche oltre CEX/bridge.")

    enrich = p.add_argument_group("arricchimento")
    enrich.add_argument("--labels", default=None,
                        help="CSV di etichette (address,type,name).")
    enrich.add_argument("--no-enrich", dest="enrich", action="store_false",
                        help="Disattiva l'arricchimento via TronScan.")
    enrich.add_argument("--enrich-max-nodes", type=int, default=500,
                        help="Numero massimo di nodi arricchiti (per volume).")
    enrich.add_argument("--enrich-workers", type=int, default=4,
                        help="Thread paralleli per l'arricchimento.")

    output = p.add_argument_group("output")
    output.add_argument("--save-transfers", action="store_true",
                        help="Esporta anche il dettaglio dei singoli trasferimenti.")
    output.add_argument("--no-metrics", dest="metrics", action="store_false",
                        help="Salta community detection e centralita'.")
    output.add_argument("-v", "--verbose", action="count", default=0)

    keys = p.add_argument_group("credenziali")
    keys.add_argument("--keys", default=None, help="CSV con le API key.")
    keys.add_argument("--api-key", default=None,
                      help="API key TronGrid (> --keys > TRONGRID_KEY).")
    keys.add_argument("--tronscan-key", default=None,
                      help="API key TronScan (> --keys > TRONSCAN_KEY).")
    keys.add_argument("--sleep", type=float, default=0.25,
                      help="Intervallo minimo fra le chiamate.")

    p.set_defaults(enrich=True, stop_on_exchange=True, metrics=True)
    p.add_argument("--version", action="version", version=f"tronAnalisys {__version__}")
    return p.parse_args(argv)


def _resolve_assets(args, client, seeds, start_ms, end_ms) -> tuple[str, ...]:
    """A differenza della v1, il risultato della classificazione viene usato."""
    if args.asset == "both":
        return ("trx", "trc20")
    if args.asset in ("trx", "trc20"):
        return (args.asset,)

    log.info("Classificazione asset dei seed (campionamento)...")
    kinds = set()
    for seed in seeds:
        kind = classify_asset(client, seed, start_ms, end_ms)
        log.info("  %s -> %s", seed, kind)
        kinds.add(kind)

    if kinds <= {"trc20", "vuoto"} and "trc20" in kinds:
        return ("trc20",)
    if kinds <= {"trx", "vuoto"} and "trx" in kinds:
        return ("trx",)
    return ("trx", "trc20")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING - 10 * min(args.verbose, 2),
        format="%(levelname).1s %(message)s",
        stream=sys.stderr,
    )

    started = time.time()
    now_ms = int(started * 1000)
    start_ms = to_ms(args.start, 0)
    end_ms = to_ms(args.end, now_ms)
    if start_ms >= end_ms:
        print("[!] Intervallo temporale vuoto: --start deve precedere --end.")
        return 2

    contract = None if str(args.contract).lower() == "all" else args.contract

    if args.window_days > 0:
        windows = (end_ms - start_ms) / (args.window_days * 86_400_000)
        if windows > 500:
            print(f"[!] {int(windows)} finestre da {args.window_days}g: "
                  f"molte chiamate a vuoto. Valuta --window-days 0.")

    seeds = load_addresses(args.input, strict=args.strict_addresses)
    labels = load_labels(args.labels)

    file_keys = load_keys(args.keys)
    trongrid_key = (args.api_key or file_keys.get("trongrid")
                    or os.environ.get("TRONGRID_KEY"))
    tronscan_key = (args.tronscan_key or file_keys.get("tronscan")
                    or os.environ.get("TRONSCAN_KEY"))
    if not trongrid_key:
        print("[!] Nessuna API key TronGrid: rate limit stringenti.")

    limiter = RateLimiter(args.sleep)
    stats = HttpStats()
    client = TronGrid(trongrid_key, limiter=limiter, stats=stats)

    tronscan = None
    if args.enrich and tronscan_key:
        tronscan = TronScan(tronscan_key, limiter=RateLimiter(args.sleep), stats=stats)
    elif args.enrich:
        print("[!] Enrichment richiesto ma manca la key TronScan: salto i tag.")

    min_amounts: dict[str, Decimal] = {"*": args.min_amount}
    if args.min_amount_trx is not None:
        min_amounts["TRX"] = args.min_amount_trx
    if args.min_amount_trc20 is not None:
        for symbol in ("USDT", "USDC", "TRC20"):
            min_amounts.setdefault(symbol, args.min_amount_trc20)
        min_amounts["*"] = args.min_amount_trc20

    assets = _resolve_assets(args, client, seeds, max(start_ms, now_ms - 30 * 86_400_000), end_ms)

    config = ScanConfig(
        direction=args.direction,
        depth=args.depth,
        assets=assets,
        contract=contract,
        start_ms=start_ms,
        end_ms=end_ms,
        window_days=args.window_days,
        min_amounts=min_amounts,
        max_nodes=args.max_nodes,
        max_degree=args.max_degree,
        max_transfers_per_address=args.max_transfers or None,
        stop_on_exchange=args.stop_on_exchange,
        enrich=args.enrich,
        enrich_max_nodes=args.enrich_max_nodes,
        enrich_workers=args.enrich_workers,
        keep_transfers=args.save_transfers,
    )

    print(f"[*] Seed: {len(seeds)} | direction={config.direction} "
          f"depth={config.depth} assets={'+'.join(assets)} "
          f"enrich={'on' if tronscan else 'off'}")

    builder = GraphBuilder(client, config, labels, tronscan=tronscan)
    graph = builder.build(seeds)

    metrics_info = {}
    if args.metrics:
        metrics_info = compute_network_metrics(graph)

    print(f"[*] Nodi: {graph.number_of_nodes()} | Archi: {graph.number_of_edges()} | "
          f"Trasferimenti unici: {builder.stats['transfers_seen'] - builder.stats['transfers_duplicated']}")
    if builder.stats["transfers_duplicated"]:
        print(f"[*] Duplicati scartati (stessa tx da entrambi i lati): "
              f"{builder.stats['transfers_duplicated']}")

    _report_nodes(graph, "type", ("bridge", "cex", "exchange"),
                  "Punti di uscita cross-chain/CEX")
    risky = [n for n, d in graph.nodes(data=True)
             if d.get("blacklist") or d.get("fraud") or d.get("type") == "risk"]
    if risky:
        print(f"[!] Nodi segnalati (blacklist/frode/red tag): {len(risky)}")
        for node in risky[:20]:
            data = graph.nodes[node]
            print(f"    {node} blacklist={data.get('blacklist')} "
                  f"fraud={data.get('fraud')} tag={data.get('red_tag') or data.get('name')}")

    if builder.truncated:
        print("[!] Grafo INCOMPLETO: budget/cap raggiunti o errori di rete. "
              "Vedi il manifest per il dettaglio.")

    manifest = {
        "tool": "tronAnalisys",
        "version": __version__,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 1),
        "seeds": seeds,
        "parameters": {
            "direction": config.direction,
            "depth": config.depth,
            "assets": list(assets),
            "contract": contract,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "window_days": config.window_days,
            "min_amounts": {k: str(v) for k, v in min_amounts.items()},
            "max_nodes": config.max_nodes,
            "max_degree": config.max_degree,
            "max_transfers_per_address": config.max_transfers_per_address,
            "stop_on_exchange": config.stop_on_exchange,
        },
        "result": {
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "complete": not builder.truncated,
            "interrupted": builder.interrupted,
            **builder.stats,
            **metrics_info,
        },
        "http": stats.as_dict(),
        "errors": builder.errors,
    }

    paths = export_graph(
        graph, args.output,
        transfers=builder.transfers if args.save_transfers else None,
        manifest=manifest,
    )
    for path in paths:
        print(f"[+] {path}")
    return 0


def _report_nodes(graph, attribute, values, title) -> None:
    found = [n for n, d in graph.nodes(data=True) if d.get(attribute) in values]
    if not found:
        return
    print(f"[!] {title}: {len(found)}")
    for node in found[:20]:
        data = graph.nodes[node]
        print(f"    {node} ({data.get('type')}) {data.get('name')}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
