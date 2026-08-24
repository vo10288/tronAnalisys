# tronAnalisys 2.0 (migliorato/perfezionato con Claud.ai)

Analisi investigativa dei flussi TRX/TRC20 sulla rete TRON: dai seed a un grafo
delle relazioni esportabile in Gephi.

Refactor di `tron_graph-4.py` (711 righe in un file unico) in un package con
test offline.

## Installazione

```bash
pip install -e ".[dev]"
```

Dipendenze: solo `requests` e `networkx`. La conversione base58check e' interna
(prima serviva il pacchetto `base58`, importato dentro una funzione e con
fallback silenzioso).

## Uso

```bash
tron-analysis -i indirizzi.csv --direction both --depth 2 \
              --asset auto --contract all --min-amount-trc20 100 \
              --save-transfers -o indagine -v
```

Le API key si risolvono nell'ordine: argomento CLI > `--keys file.csv` >
variabili d'ambiente `TRONGRID_KEY` / `TRONSCAN_KEY`.

Output (`indagine*`):

| File | Contenuto |
| --- | --- |
| `.gexf` | grafo per Gephi, con community e centralita' gia' calcolate |
| `_nodes.csv` | nodi con tag, flag di rischio, volumi, livello BFS, metriche |
| `_edges.csv` | archi aggregati per (mittente, destinatario, asset) con `first_seen`/`last_seen` |
| `_transfers.csv` | singoli trasferimenti (con `--save-transfers`), per la timeline |
| `_manifest.json` | parametri, statistiche, errori e SHA-256 degli output |

## Cosa e' cambiato rispetto alla v1

### Correttezza dei dati

| Problema | Effetto | Fix |
| --- | --- | --- |
| Nessuna dedup dei trasferimenti | con `--direction both` la stessa tx veniva scaricata da entrambi i lati: importi e conteggi **raddoppiati** | `Transfer.dedup_key` + set globale |
| `int(info.get("decimals", 6) or 6)` | i token con 0 decimali venivano divisi per 1e6 | lettura esplicita del campo |
| `hex_to_base58` con `except: return hex_addr` | stesso indirizzo presente come due nodi distinti | `InvalidAddress` con validazione prefisso/lunghezza |
| Transazioni native fallite incluse | archi per flussi mai avvenuti | controllo di `ret[0].contractRet` |
| `--asset auto` | la classificazione veniva calcolata, stampata e ignorata (`auto` == `both`) | il risultato seleziona davvero gli endpoint |
| `--min-amount` unico | 5 TRX e 5 USDT trattati allo stesso modo | `--min-amount-trx` / `--min-amount-trc20` |
| Importi in `float` | perdita di precisione su valori grandi | `Decimal` internamente, `amount_exact` nell'export |

### Robustezza

- **Niente perdita dati silenziosa**: prima un fetch fallito restituiva una
  risposta vuota, indistinguibile da "nessuna transazione". Ora alza `ApiError`,
  l'indirizzo viene marcato `fetch_error` e la scansione prosegue.
- **Budget espliciti**: `--max-nodes`, `--max-degree` (gli hot wallet non
  vengono espansi), `--max-transfers`.
- **Retry sensati**: nessun tentativo ripetuto sui 4xx, `Retry-After`
  rispettato, backoff con jitter, rate limiter condiviso fra thread.
- **Paginazione protetta**: fingerprint ripetuti e `MAX_PAGES` non generano
  piu' loop.
- `Ctrl-C` esporta il grafo parziale invece di perdere tutto.

### Valore analitico

- Gli archi conservano `first_seen`, `last_seen`, `count` e un campione di
  txid: la dimensione temporale prima veniva scartata da `_add_edge`.
- Community detection (Louvain) e centralita' (degree, betweenness) calcolate
  prima dell'export, cosi' il GEXF arriva in Gephi gia' colorabile.
- Manifest di run con parametri, hash degli output e flag `complete`: senza
  quel flag un grafo troncato da un budget e' indistinguibile da un grafo
  completo.
- Arricchimento TronScan parallelo e limitato ai nodi piu' rilevanti per
  volume (prima: una chiamata sequenziale per ogni nodo del grafo).

## Struttura

```
tron_analysis/
├── addresses.py   base58check, validazione, input (seed, label, chiavi)
├── http.py        rate limiter thread-safe, retry, statistiche
├── trongrid.py    paginazione a cursore, finestre temporali
├── tronscan.py    tag pubblici e flag di rischio, con cache
├── parsing.py     record grezzi -> Transfer (Decimal)
├── graph.py       BFS, dedup, budget, aggregazione archi, metriche
├── export.py      GEXF, CSV, manifest
└── cli.py         argomenti e orchestrazione
```

```bash
pytest -q   # 18 test, nessuna chiamata di rete
```

## Nota metodologica

Una community individuata da Louvain e' un'ipotesi strutturale: non equivale a
un cluster di indirizzi riconducibili allo stesso soggetto. L'attribuzione
richiede evidenze ulteriori, euristiche specifiche, OSINT e dati off-chain.
CEX e bridge restano un confine investigativo: il flag `boundary` segnala dove
la ricostruzione on-chain si ferma.
