#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tron_graph.py — Analisi investigativa di flussi su rete TRON.

Costruisce un grafo dei trasferimenti (TRX nativo e/o TRC20 come USDT) a partire
da uno o piu' indirizzi seed, espandendo per livelli di connessione (BFS), e lo
esporta in GEXF (per Gephi) e CSV (nodi + archi).

Caratteristiche principali:
  - Sorgente dati: TronGrid (nodo ufficiale), paginazione a cursore (fingerprint)
    + finestre temporali (min/max_block_timestamp) per non sfondare i limiti.
  - Analisi iniziale: classifica ogni seed come prevalentemente TRX o TRC20.
  - Direzione configurabile: solo entrata (in), solo uscita (out), o entrambe.
  - Profondita' (livelli) di espansione configurabile.
  - Rilevamento punti di uscita cross-chain: marca bridge/CEX noti e NON li espande.
    (Il following cross-chain vero richiede un provider dedicato: vedi note.)
  - Input indirizzi da file .csv / .txt / qualsiasi testo (estrazione via regex).

Uso tipico:
  python3 tron_graph.py -i indirizzi.csv --direction both --depth 2 \
      --asset auto --output indagine --api-key $TRONGRID_KEY
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict, deque

import requests

try:
    import networkx as nx
except ImportError:
    sys.exit("Manca networkx. Installa con: pip install networkx")

# --------------------------------------------------------------------------- #
# Costanti
# --------------------------------------------------------------------------- #
TRONGRID_BASE = "https://api.trongrid.io"
ADDR_RE = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")  # indirizzo base58 TRON
SUN = 1_000_000  # 1 TRX = 1e6 SUN
PAGE_LIMIT = 200  # massimo consentito da TronGrid

# Contratto USDT-TRC20 (verificato). Usato come default per il filtro TRC20.
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Etichette note di base. ESTENDI tramite --labels (CSV: address,type,name).
# NB: tieni questa lista minima e verificata; i bridge/CEX vanno forniti da te
# perche' gli indirizzi cambiano e non vanno asseriti alla cieca.
DEFAULT_LABELS = {
    USDT_CONTRACT: {"type": "token_contract", "name": "USDT (TRC20)"},
}


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
def load_addresses(path):
    """Estrae indirizzi TRON validi da .csv, .txt o qualsiasi file testuale.

    Per i CSV scandisce tutte le celle; per gli altri formati applica la regex
    sull'intero contenuto. Restituisce una lista deduplicata mantenendo l'ordine.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File non trovato: {path}")

    text_chunks = []
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
            for row in csv.reader(fh):
                text_chunks.extend(row)
    else:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text_chunks.append(fh.read())

    seen, out = set(), []
    for chunk in text_chunks:
        for match in ADDR_RE.findall(chunk):
            if match not in seen:
                seen.add(match)
                out.append(match)
    if not out:
        raise ValueError("Nessun indirizzo TRON valido trovato nel file di input.")
    return out


def load_labels(path):
    """Carica un CSV di etichette (address,type,name) e lo fonde con i default."""
    labels = dict(DEFAULT_LABELS)
    if not path:
        return labels
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            addr = (row.get("address") or "").strip()
            if ADDR_RE.fullmatch(addr):
                labels[addr] = {
                    "type": (row.get("type") or "labeled").strip(),
                    "name": (row.get("name") or "").strip(),
                }
    return labels


def load_keys(path):
    """Carica le API key da un CSV. Accetta due layout:

    A) righe "servizio,chiave":
         trongrid,LA_TUA_KEY_TRONGRID
         tronscan,LA_TUA_KEY_TRONSCAN
    B) riga di intestazione con colonne dedicate:
         trongrid_key,tronscan_key
         LA_TUA_KEY_TRONGRID,LA_TUA_KEY_TRONSCAN

    Ritorna {"trongrid": ..., "tronscan": ...} (chiavi assenti = non presenti).
    """
    keys = {}
    if not path:
        return keys
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        rows = list(csv.reader(fh))
    # scarta righe vuote e commenti (#)
    rows = [r for r in rows
            if any(c.strip() for c in r) and not r[0].strip().startswith("#")]
    if not rows:
        return keys

    header = [c.strip().lower() for c in rows[0]]

    # Layout B: intestazione con colonne che contengono 'key' + riga dati
    if any("key" in h for h in header) and len(rows) >= 2:
        for h, v in zip(header, rows[1]):
            if "trongrid" in h:
                keys["trongrid"] = v.strip()
            elif "tronscan" in h:
                keys["tronscan"] = v.strip()
        return keys

    # Layout A: righe "servizio,chiave"
    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) >= 2:
            name, val = cells[0].lower(), cells[1]
            if "trongrid" in name:
                keys["trongrid"] = val
            elif "tronscan" in name:
                keys["tronscan"] = val
    return keys


# --------------------------------------------------------------------------- #
# Client TronGrid
# --------------------------------------------------------------------------- #
class TronGrid:
    """Client minimale con retry/backoff e rate limiting."""

    def __init__(self, api_key=None, sleep=0.25, timeout=30, max_retries=4):
        self.session = requests.Session()
        if api_key:
            self.session.headers["TRON-PRO-API-KEY"] = api_key
        self.sleep = sleep
        self.timeout = timeout
        self.max_retries = max_retries

    def _get(self, url, params):
        backoff = 1.0
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                time.sleep(self.sleep)
                return resp.json()
            except requests.RequestException:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2
        return {"data": [], "meta": {}}

    def _paginate(self, endpoint, base_params):
        """Itera tutte le pagine via fingerprint per una singola finestra."""
        params = dict(base_params)
        params["limit"] = PAGE_LIMIT
        while True:
            payload = self._get(f"{TRONGRID_BASE}{endpoint}", params)
            data = payload.get("data", []) or []
            for item in data:
                yield item
            meta = payload.get("meta", {}) or {}
            fingerprint = meta.get("fingerprint")
            if not fingerprint or not data:
                break
            params["fingerprint"] = fingerprint

    @staticmethod
    def _windows(start_ms, end_ms, window_days):
        """Spezza [start, end] in finestre temporali (ms)."""
        if window_days <= 0:
            yield start_ms, end_ms
            return
        step = window_days * 86_400_000
        cur = start_ms
        while cur < end_ms:
            yield cur, min(cur + step, end_ms)
            cur += step

    def fetch_trc20(self, address, direction, start_ms, end_ms,
                    window_days, contract=None):
        endpoint = f"/v1/accounts/{address}/transactions/trc20"
        for w_start, w_end in self._windows(start_ms, end_ms, window_days):
            params = {
                "only_confirmed": "true",
                "min_block_timestamp": w_start,
                "max_block_timestamp": w_end,
                "order_by": "block_timestamp,asc",
            }
            if contract:
                params["contract_address"] = contract
            if direction == "in":
                params["only_to"] = "true"
            elif direction == "out":
                params["only_from"] = "true"
            yield from self._paginate(endpoint, params)

    def fetch_native(self, address, direction, start_ms, end_ms, window_days):
        endpoint = f"/v1/accounts/{address}/transactions"
        for w_start, w_end in self._windows(start_ms, end_ms, window_days):
            params = {
                "only_confirmed": "true",
                "min_block_timestamp": w_start,
                "max_block_timestamp": w_end,
                "order_by": "block_timestamp,asc",
            }
            if direction == "in":
                params["only_to"] = "true"
            elif direction == "out":
                params["only_from"] = "true"
            yield from self._paginate(endpoint, params)


# --------------------------------------------------------------------------- #
# Client TronScan (etichette / tag / flag di rischio)
# --------------------------------------------------------------------------- #
class TronScan:
    """Recupera publicTag/greyTag/redTag e i flag di sicurezza di un indirizzo.

    Usa la stessa intestazione di autenticazione di TronGrid (TRON-PRO-API-KEY)
    ma con la propria API key. I risultati sono in cache per non ripetere le
    chiamate sullo stesso indirizzo durante la BFS.
    """

    BASE = "https://apilist.tronscanapi.com/api"

    # parole chiave per inferire il tipo dal tag pubblico
    CEX_KW = ("binance", "okx", "okex", "bybit", "huobi", "htx", "kucoin",
              "gate", "bitfinex", "kraken", "mexc", "bitget", "coinbase",
              "poloniex", "exchange", "hot wallet", "hotwallet", "deposit")
    BRIDGE_KW = ("bridge", "swap", "router", "stargate", "allbridge",
                 "wormhole", "celer", "cbridge", "multichain")

    def __init__(self, api_key=None, sleep=0.25, timeout=30, max_retries=4):
        self.session = requests.Session()
        if api_key:
            self.session.headers["TRON-PRO-API-KEY"] = api_key
        self.sleep = sleep
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache = {}

    def _get(self, path, params):
        backoff = 1.0
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(f"{self.BASE}{path}", params=params,
                                        timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                time.sleep(self.sleep)
                return resp.json()
            except requests.RequestException:
                if attempt == self.max_retries - 1:
                    return {}
                time.sleep(backoff)
                backoff *= 2
        return {}

    def enrich(self, address):
        """Ritorna {type,name,public_tag,grey_tag,red_tag,blacklist,fraud}."""
        if address in self._cache:
            return self._cache[address]
        acc = self._get("/account", {"address": address}) or {}
        public = (acc.get("publicTag") or "").strip()
        grey = (acc.get("greyTag") or "").strip()
        red = (acc.get("redTag") or "").strip()
        sec = self._get("/security/account/data", {"address": address}) or {}
        info = {
            "public_tag": public,
            "grey_tag": grey,
            "red_tag": red,
            "blacklist": bool(sec.get("is_black_list")),
            "fraud": bool(sec.get("has_fraud_transaction")),
            "type": self._infer_type(public, grey, red),
            "name": public or grey or red,
        }
        self._cache[address] = info
        return info

    @classmethod
    def _infer_type(cls, public, grey, red):
        tag = f"{public} {grey}".lower()
        if red:
            return "risk"
        if any(k in tag for k in cls.CEX_KW):
            return "cex"
        if any(k in tag for k in cls.BRIDGE_KW):
            return "bridge"
        if public or grey:
            return "service"
        return "address"


# --------------------------------------------------------------------------- #
# Parsing dei trasferimenti -> archi normalizzati
# --------------------------------------------------------------------------- #
def parse_trc20(record):
    """Estrae (src, dst, amount, symbol, ts, txid) da un record TRC20."""
    if record.get("type") != "Transfer":
        return None
    info = record.get("token_info", {}) or {}
    decimals = int(info.get("decimals", 6) or 6)
    symbol = info.get("symbol") or "TRC20"
    try:
        amount = int(record.get("value", "0")) / (10 ** decimals)
    except (TypeError, ValueError):
        return None
    src = record.get("from")
    dst = record.get("to")
    if not (src and dst):
        return None
    return src, dst, amount, symbol, record.get("block_timestamp"), \
        record.get("transaction_id")


def parse_native(tx):
    """Estrae (src, dst, amount_TRX, 'TRX', ts, txid) da una transazione nativa."""
    try:
        contract = tx["raw_data"]["contract"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if contract.get("type") != "TransferContract":
        return None  # ignora TRC10, smart contract, ecc.
    val = contract.get("parameter", {}).get("value", {})
    src = hex_to_base58(val.get("owner_address"))
    dst = hex_to_base58(val.get("to_address"))
    amount = (val.get("amount", 0) or 0) / SUN
    if not (src and dst):
        return None
    return src, dst, amount, "TRX", tx.get("block_timestamp"), tx.get("txID")


def hex_to_base58(hex_addr):
    """Converte un indirizzo hex (41...) in base58 se serve; richiede base58."""
    if not hex_addr:
        return None
    if hex_addr.startswith("T"):
        return hex_addr
    try:
        import base58
        import hashlib
        raw = bytes.fromhex(hex_addr)
        checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
        return base58.b58encode(raw + checksum).decode()
    except Exception:
        return hex_addr  # fallback: lascia il formato hex


# --------------------------------------------------------------------------- #
# Analisi iniziale: classificazione asset del seed
# --------------------------------------------------------------------------- #
def classify_asset(client, address, sample_window):
    """Stima se l'indirizzo opera prevalentemente in TRX o TRC20.

    Campiona un numero limitato di trasferimenti recenti da entrambi gli endpoint
    e confronta i conteggi. Ritorna 'trx', 'trc20' o 'mixed'.
    """
    start_ms, end_ms = sample_window
    trc20 = sum(1 for _ in zip(
        range(PAGE_LIMIT),
        client.fetch_trc20(address, "both", start_ms, end_ms, 0)))
    native = 0
    for _, tx in zip(range(PAGE_LIMIT),
                     client.fetch_native(address, "both", start_ms, end_ms, 0)):
        if parse_native(tx):
            native += 1
    if trc20 == 0 and native == 0:
        return "vuoto"
    if trc20 >= 3 * max(native, 1):
        return "trc20"
    if native >= 3 * max(trc20, 1):
        return "trx"
    return "mixed"


# --------------------------------------------------------------------------- #
# Costruzione del grafo (BFS per livelli)
# --------------------------------------------------------------------------- #
def build_graph(client, seeds, args, labels, tronscan=None):
    graph = nx.MultiDiGraph()
    start_ms = args.start_ms
    end_ms = args.end_ms

    for s in seeds:
        graph.add_node(s, seed=True)

    visited = set()
    # frontiera: (address, livello)
    frontier = deque((s, 0) for s in seeds)

    while frontier:
        address, level = frontier.popleft()
        if address in visited or level > args.depth:
            continue
        visited.add(address)

        # Etichetta il nodo corrente: label manuale > TronScan.
        if address in labels:
            _apply_meta(graph, address, labels[address])
        elif tronscan and args.enrich:
            _apply_meta(graph, address, tronscan.enrich(address))
        node_type = graph.nodes[address].get("type", "address") \
            if address in graph else "address"

        # Punto di uscita cross-chain/CEX: non espandere oltre (salvo i seed).
        if level > 0 and args.stop_on_exchange and \
                node_type in ("cex", "bridge", "exchange"):
            continue

        records = []
        if args.asset in ("trc20", "both", "auto"):
            for r in client.fetch_trc20(address, args.direction, start_ms,
                                        end_ms, args.window_days,
                                        contract=args.contract):
                parsed = parse_trc20(r)
                if parsed:
                    records.append(parsed)
        if args.asset in ("trx", "both", "auto"):
            for tx in client.fetch_native(address, args.direction, start_ms,
                                          end_ms, args.window_days):
                parsed = parse_native(tx)
                if parsed:
                    records.append(parsed)

        counterparties = set()
        for src, dst, amount, symbol, ts, txid in records:
            if amount < args.min_amount:
                continue
            # Coerenza con la direzione richiesta (difesa lato client)
            if args.direction == "in" and dst != address:
                continue
            if args.direction == "out" and src != address:
                continue
            _add_edge(graph, src, dst, amount, symbol)
            other = dst if src == address else src
            counterparties.add(other)

        # Prepara il livello successivo
        if level < args.depth:
            for cp in counterparties:
                if cp not in visited:
                    frontier.append((cp, level + 1))

    # Etichettatura finale (anche le foglie non visitate):
    # i label manuali hanno precedenza, poi i tag TronScan.
    for node in list(graph.nodes):
        if node in labels:
            _apply_meta(graph, node, labels[node])
        elif tronscan and args.enrich and "type" not in graph.nodes[node]:
            _apply_meta(graph, node, tronscan.enrich(node))

    _finalize_node_metrics(graph)
    return graph


def _apply_meta(graph, address, meta):
    """Applica gli attributi di etichetta a un nodo (creandolo se assente)."""
    if address not in graph:
        graph.add_node(address)
    node = graph.nodes[address]
    for key in ("type", "name", "public_tag", "grey_tag", "red_tag",
                "blacklist", "fraud"):
        if key in meta and meta[key] not in (None, ""):
            node[key] = meta[key]


def _add_edge(graph, src, dst, amount, symbol):
    """Aggrega gli archi per (src, dst, asset): somma importo e conta le tx."""
    key = symbol
    if graph.has_edge(src, dst, key=key):
        graph[src][dst][key]["amount"] += amount
        graph[src][dst][key]["count"] += 1
        graph[src][dst][key]["weight"] += amount
    else:
        graph.add_edge(src, dst, key=key, asset=symbol, amount=amount,
                       count=1, weight=amount)


def _finalize_node_metrics(graph):
    """Calcola metriche utili per Gephi e marca i nodi non etichettati."""
    for node in graph.nodes:
        in_amt = sum(d["amount"] for _, _, d in graph.in_edges(node, data=True))
        out_amt = sum(d["amount"] for _, _, d in graph.out_edges(node, data=True))
        graph.nodes[node]["total_in"] = float(round(in_amt, 6))
        graph.nodes[node]["total_out"] = float(round(out_amt, 6))
        graph.nodes[node].setdefault("seed", False)
        graph.nodes[node].setdefault("type", "address")
        graph.nodes[node].setdefault("name", "")
        graph.nodes[node].setdefault("public_tag", "")
        graph.nodes[node].setdefault("red_tag", "")
        graph.nodes[node].setdefault("blacklist", False)
        graph.nodes[node].setdefault("fraud", False)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def export(graph, basename):
    gexf_path = f"{basename}.gexf"
    nodes_path = f"{basename}_nodes.csv"
    edges_path = f"{basename}_edges.csv"

    nx.write_gexf(graph, gexf_path)

    with open(nodes_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Id", "Label", "seed", "type", "name", "public_tag",
                    "red_tag", "blacklist", "fraud", "total_in", "total_out"])
        for n, d in graph.nodes(data=True):
            w.writerow([n, n, d.get("seed"), d.get("type"), d.get("name"),
                        d.get("public_tag", ""), d.get("red_tag", ""),
                        d.get("blacklist", False), d.get("fraud", False),
                        d.get("total_in"), d.get("total_out")])

    with open(edges_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Source", "Target", "asset", "amount", "count",
                    "weight", "Type"])
        for s, t, d in graph.edges(data=True):
            w.writerow([s, t, d.get("asset"), round(d.get("amount", 0), 6),
                        d.get("count"), round(d.get("weight", 0), 6), "Directed"])

    return gexf_path, nodes_path, edges_path


# --------------------------------------------------------------------------- #
# Helpers tempo
# --------------------------------------------------------------------------- #
def to_ms(value, default):
    """Converte una data 'YYYY-MM-DD' o un timestamp(ms) in ms; None -> default."""
    if value is None:
        return default
    if value.isdigit():
        return int(value)
    import datetime as dt
    d = dt.datetime.strptime(value, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Analisi del grafo dei flussi su rete TRON (TRX/TRC20).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("-i", "--input", required=True,
                   help="File con gli indirizzi seed (.csv, .txt o testo).")
    p.add_argument("--direction", choices=["in", "out", "both"], default="both",
                   help="Connessioni: solo entrata, solo uscita o entrambe.")
    p.add_argument("--depth", type=int, default=1,
                   help="Livelli di connessione (hop) da espandere.")
    p.add_argument("--asset", choices=["auto", "trx", "trc20", "both"],
                   default="auto",
                   help="Asset da seguire. 'auto' classifica e poi segue entrambi.")
    p.add_argument("--contract", default=USDT_CONTRACT,
                   help="Contratto TRC20 da filtrare (default USDT). "
                        "Usa 'all' per non filtrare.")
    p.add_argument("--start", default=None,
                   help="Data inizio 'YYYY-MM-DD' o timestamp ms.")
    p.add_argument("--end", default=None,
                   help="Data fine 'YYYY-MM-DD' o timestamp ms.")
    p.add_argument("--window-days", type=int, default=0,
                   help="0 = finestra temporale unica con paginazione a cursore "
                        "(consigliato). Imposta un valore positivo SOLO per "
                        "indirizzi con volumi enormi, per limitare la memoria.")
    p.add_argument("--min-amount", type=float, default=0.0,
                   help="Soglia minima di importo per includere un arco.")
    p.add_argument("--labels", default=None,
                   help="CSV di etichette (address,type,name) per bridge/CEX.")
    p.add_argument("-o", "--output", default="tron_graph",
                   help="Prefisso dei file di output (.gexf e _nodes/_edges.csv).")
    p.add_argument("--keys", default=None,
                   help="CSV con le API key (righe 'servizio,chiave' oppure "
                        "intestazione 'trongrid_key,tronscan_key').")
    p.add_argument("--api-key", default=None,
                   help="API key TronGrid. Precedenza: questo arg > --keys > "
                        "variabile d'ambiente TRONGRID_KEY.")
    p.add_argument("--tronscan-key", default=None,
                   help="API key TronScan per l'arricchimento dei nodi. "
                        "Precedenza: questo arg > --keys > TRONSCAN_KEY.")
    p.add_argument("--no-enrich", dest="enrich", action="store_false",
                   help="Disattiva l'arricchimento dei nodi via TronScan.")
    p.add_argument("--no-stop-on-exchange", dest="stop_on_exchange",
                   action="store_false",
                   help="Espandi la BFS anche oltre i nodi CEX/bridge.")
    p.set_defaults(enrich=True, stop_on_exchange=True)
    p.add_argument("--sleep", type=float, default=0.25,
                   help="Pausa tra le chiamate (rate limiting).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # Normalizza finestra temporale
    now_ms = int(time.time() * 1000)
    args.start_ms = to_ms(args.start, 0)
    args.end_ms = to_ms(args.end, now_ms)
    if args.contract.lower() == "all":
        args.contract = None

    # Guardia: troppe finestre = molte chiamate a vuoto su intervalli ampi.
    if args.window_days > 0:
        n_win = (args.end_ms - args.start_ms) / (args.window_days * 86_400_000)
        if n_win > 500:
            print(f"[!] Attenzione: {int(n_win)} finestre temporali da "
                  f"{args.window_days}g. Rischi molte chiamate a vuoto. "
                  f"Usa --window-days 0 o restringi con --start/--end.")

    seeds = load_addresses(args.input)
    labels = load_labels(args.labels)

    # Risoluzione API key: arg esplicito > file CSV > variabile d'ambiente.
    file_keys = load_keys(args.keys)
    trongrid_key = (args.api_key or file_keys.get("trongrid")
                    or os.environ.get("TRONGRID_KEY"))
    tronscan_key = (args.tronscan_key or file_keys.get("tronscan")
                    or os.environ.get("TRONSCAN_KEY"))

    client = TronGrid(api_key=trongrid_key, sleep=args.sleep)
    tronscan = None
    if args.enrich and tronscan_key:
        tronscan = TronScan(api_key=tronscan_key, sleep=args.sleep)
    elif args.enrich and not tronscan_key:
        print("[!] Enrichment richiesto ma manca la key TronScan: salto i tag.")

    print(f"[*] Indirizzi seed: {len(seeds)}")
    if not trongrid_key:
        print("[!] Nessuna API key TronGrid: possibili limiti di rate stringenti.")

    # --- Analisi iniziale: classificazione asset ---
    print("[*] Analisi iniziale (classificazione asset)...")
    sample_win = (max(args.start_ms, now_ms - 30 * 86_400_000), args.end_ms)
    for s in seeds:
        kind = classify_asset(client, s, sample_win)
        print(f"    {s} -> {kind}")

    # --- Costruzione grafo ---
    print(f"[*] Costruzione grafo: direction={args.direction}, "
          f"depth={args.depth}, asset={args.asset}, "
          f"enrich={'on' if tronscan else 'off'}")
    graph = build_graph(client, seeds, args, labels, tronscan=tronscan)
    print(f"[*] Nodi: {graph.number_of_nodes()} | Archi: {graph.number_of_edges()}")

    exits = [n for n, d in graph.nodes(data=True)
             if d.get("type") in ("bridge", "cex", "exchange")]
    if exits:
        print(f"[!] Punti di uscita cross-chain/CEX rilevati: {len(exits)}")
        for b in exits:
            print(f"    {b} ({graph.nodes[b].get('type')}) "
                  f"{graph.nodes[b].get('name')}")

    risky = [n for n, d in graph.nodes(data=True)
             if d.get("blacklist") or d.get("fraud") or d.get("type") == "risk"]
    if risky:
        print(f"[!] Nodi segnalati (blacklist/frode/red tag): {len(risky)}")
        for r in risky:
            d = graph.nodes[r]
            print(f"    {r} blacklist={d.get('blacklist')} "
                  f"fraud={d.get('fraud')} tag={d.get('red_tag') or d.get('name')}")

    # --- Export ---
    gexf, nodes_csv, edges_csv = export(graph, args.output)
    print(f"[+] GEXF : {gexf}")
    print(f"[+] Nodi : {nodes_csv}")
    print(f"[+] Archi: {edges_csv}")


if __name__ == "__main__":
    main()
