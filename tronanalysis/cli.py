from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__
from .analytics import add_graph_analytics, detect_patterns, shortest_paths
from .exporters import export_all
from .http import JsonDiskCache, ResilientJsonClient
from .investigator import InvestigationOptions, build_graph
from .labels import load_labels
from .report import write_report
from .tron import TRONGRID_BASE, TRONSCAN_BASE, USDT_CONTRACT, TronGridProvider, TronScanEnricher
from .utils import load_addresses, load_dotenv, load_keys_csv, parse_time_ms


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tron-analysis",
        description="Blockchain investigation / graph analysis su TRON (TRX e TRC20).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", required=True, help="File contenente uno o più indirizzi seed.")
    p.add_argument("-o", "--output", default="output/tron_investigation", help="Prefisso output.")
    p.add_argument("--direction", choices=["in", "out", "both"], default="both")
    p.add_argument("--depth", type=int, default=2, help="Numero di hop BFS da espandere.")
    p.add_argument("--asset", choices=["auto", "trx", "trc20", "both"], default="auto")
    p.add_argument("--contract", default=USDT_CONTRACT, help="Contratto TRC20; usare 'all' per tutti i token.")
    p.add_argument("--start", help="YYYY-MM-DD oppure timestamp ms.")
    p.add_argument("--end", help="YYYY-MM-DD oppure timestamp ms.")
    p.add_argument("--window-days", type=int, default=0, help="Spezza l'intervallo in finestre temporali.")
    p.add_argument("--min-amount", type=float, default=0.0)
    p.add_argument("--max-nodes", type=int, default=5000)
    p.add_argument("--max-edges", type=int, default=20000)
    p.add_argument("--max-counterparties-per-node", type=int, default=500)
    p.add_argument("--labels", help="CSV manuale: address,type,name,risk,is_contract,source,confidence,evidence")
    p.add_argument("--keys", help="CSV API key compatibile con la versione precedente.")
    p.add_argument("--api-key", help="TronGrid API key; meglio usare .env/TRONGRID_KEY.")
    p.add_argument("--tronscan-key", help="TronScan API key; meglio usare .env/TRONSCAN_KEY.")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--no-enrich", dest="enrich", action="store_false")
    p.add_argument("--no-stop-on-exchange", dest="stop_on_exchange", action="store_false")
    p.add_argument("--sleep", type=float, default=0.20, help="Pacing minimo fra chiamate API non in cache.")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--cache-dir", default=".cache/tronanalysis")
    p.add_argument("--cache-ttl", type=int, default=86400)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--path-from", help="Calcola fino a 10 shortest paths da questo nodo.")
    p.add_argument("--path-to", help="Nodo destinazione per --path-from.")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.set_defaults(enrich=True, stop_on_exchange=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.depth < 0 or args.max_nodes < 1 or args.max_edges < 1 or args.max_counterparties_per_node < 1:
        raise SystemExit("depth/limiti non validi")
    load_dotenv(args.env_file)
    file_keys = load_keys_csv(args.keys)
    trongrid_key = args.api_key or file_keys.get("trongrid") or os.environ.get("TRONGRID_KEY")
    tronscan_key = args.tronscan_key or file_keys.get("tronscan") or os.environ.get("TRONSCAN_KEY")
    now_ms = int(time.time() * 1000)
    start_ms = parse_time_ms(args.start, 0)
    end_ms = parse_time_ms(args.end, now_ms)
    if start_ms > end_ms:
        raise SystemExit("--start deve precedere --end")
    contract = None if str(args.contract).lower() == "all" else args.contract
    seeds = load_addresses(args.input)
    labels = load_labels(args.labels)

    cache = None if args.no_cache else JsonDiskCache(args.cache_dir, args.cache_ttl)
    tg_http = ResilientJsonClient(trongrid_key, args.sleep, args.timeout, args.retries, cache)
    provider = TronGridProvider(tg_http)
    enricher = None
    ts_http = None
    if args.enrich and tronscan_key:
        ts_http = ResilientJsonClient(tronscan_key, args.sleep, args.timeout, args.retries, cache)
        enricher = TronScanEnricher(ts_http)
    elif args.enrich:
        logging.warning("TronScan key assente: enrichment disattivato. Impostare TRONSCAN_KEY in .env.")

    options = InvestigationOptions(
        direction=args.direction,
        depth=args.depth,
        asset=args.asset,
        contract=contract,
        start_ms=start_ms,
        end_ms=end_ms,
        window_days=args.window_days,
        min_amount=args.min_amount,
        max_nodes=args.max_nodes,
        max_edges=args.max_edges,
        max_counterparties_per_node=args.max_counterparties_per_node,
        stop_on_exchange=args.stop_on_exchange,
        enrich=args.enrich,
    )
    logging.info("Seed=%s | depth=%s | direction=%s | asset=%s", len(seeds), args.depth, args.direction, args.asset)
    result = build_graph(provider, seeds, options, labels=labels, enricher=enricher)
    analytics = add_graph_analytics(result.graph)
    patterns = detect_patterns(result.graph)
    paths = export_all(result.graph, result.transfers, args.output)
    provenance = tg_http.provenance + (ts_http.provenance if ts_http else [])
    report_path = Path(f"{args.output}_report.html")
    write_report(
        result.graph,
        analytics,
        patterns,
        paths,
        report_path,
        provenance,
        seeds,
        result.truncated,
        result.truncation_reasons,
    )
    logging.info("Nodi=%s | archi=%s | trasferimenti unici=%s", result.graph.number_of_nodes(), result.graph.number_of_edges(), len(result.transfers))
    logging.info("Report: %s", report_path)

    if args.path_from or args.path_to:
        if not (args.path_from and args.path_to):
            logging.warning("Per la path analysis servono sia --path-from sia --path-to")
        else:
            found = shortest_paths(result.graph, args.path_from, args.path_to)
            if not found:
                print("Nessun percorso trovato nel grafo acquisito.")
            else:
                for idx, path in enumerate(found, 1):
                    print(f"PATH {idx}: {' -> '.join(path)}")
    if result.truncated:
        logging.warning("Analisi troncata: %s", ", ".join(result.truncation_reasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
