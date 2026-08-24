# tronAnalisys 2.0

**Open-source TRON blockchain investigation and graph analysis toolkit.**

`tronAnalisys` parte da uno o più indirizzi TRON e ricostruisce le relazioni on-chain in ingresso/uscita, espandendole per più hop. L'obiettivo è offrire un workflow trasparente e riproducibile per triage, studio, formazione e supporto investigativo quando non si dispone di una piattaforma enterprise.

> **Importante:** community detection, pattern recognition e tag pubblici sono elementi analitici. Non dimostrano da soli che più wallet appartengano alla stessa persona o organizzazione. L'attribution richiede corroborazione con ulteriori fonti.

## Cosa cambia nella v2

- codice modulare invece del singolo script monolitico;
- retry/backoff, `Retry-After`, pacing, cache JSON opzionale e protezione dai loop di pagination fingerprint;
- BFS multi-hop con limiti `max-nodes`, `max-edges` e controparti per nodo per evitare graph explosion;
- deduplicazione dei trasferimenti anche quando la stessa transazione viene incontrata da più wallet;
- preservazione di `txid`, timestamp, asset e contract address nel ledger CSV;
- enrichment TronScan via endpoint `accountv2`, con **fonte, confidence ed evidence** separate dai fatti on-chain;
- stop configurabile su CEX/bridge come boundary investigativo;
- PageRank, betweenness centrality, degree e community detection Louvain (fallback greedy modularity); le centralità pesate usano il numero di transazioni per evitare di sommare unità di asset diversi;
- pattern euristici `fan-in/collector`, `fan-out/distributor`, `peel_like`;
- shortest-path analysis fra due wallet presenti nel grafo;
- export GEXF, GraphML, CSV nodi/archi, CSV transazioni, JSON node-link e CSV Neo4j;
- report HTML con metriche, risk nodes, provenance API e SHA-256 degli output;
- `.env` per non inserire API key nel codice;
- test automatici con `pytest`.

## Requisiti

- Python 3.10+
- `requests`
- `networkx` 3.2+
- API key TronGrid consigliata
- API key TronScan necessaria soltanto per enrichment/tag

```bash
python3 -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\\Scripts\\activate      # Windows
pip install -r requirements.txt
```

Oppure installazione locale del package:

```bash
pip install -e .
```

## API key

Copia `.env.example` in `.env` e inserisci le tue chiavi:

```bash
cp .env.example .env
```

```text
TRONGRID_KEY=...
TRONSCAN_KEY=...
```

`.env` è escluso da Git tramite `.gitignore`. È mantenuta anche la compatibilità con `--keys keys.csv` e con `--api-key` / `--tronscan-key`, ma per evitare esposizioni accidentali è preferibile `.env`.

## Quick start

Inserisci gli indirizzi seed in `seeds.txt`, poi:

```bash
python tron_graph.py \
  -i seeds.txt \
  --direction both \
  --depth 2 \
  --asset trc20 \
  --contract TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t \
  --start 2026-01-01 \
  -o output/case001
```

Se hai installato il package:

```bash
tron-analysis -i seeds.txt --depth 2 -o output/case001
```

Per seguire tutti i TRC20:

```bash
tron-analysis -i seeds.txt --asset trc20 --contract all
```

Per includere TRX nativo e TRC20:

```bash
tron-analysis -i seeds.txt --asset both
```

## Controllo della graph explosion

Una BFS di secondo/terzo livello su wallet ad alta attività può produrre moltissimi nodi. La v2 applica limiti espliciti:

```bash
--max-nodes 5000
--max-edges 20000
--max-counterparties-per-node 500
```

Le controparti da espandere vengono ordinate prima per numero di transazioni e poi, a parità, per volume nominale osservato, evitando di confrontare direttamente importi di asset diversi. Se un limite viene raggiunto, il report segnala che il dataset è stato troncato: questa informazione è importante per la riproducibilità dell'analisi.

## Attribution: fatti vs intelligence

Il progetto mantiene separati:

1. **On-chain facts** — transazione, sorgente, destinazione, importo, asset, timestamp, txid.
2. **Graph analytics** — centralità, community, pattern, path.
3. **Attribution / intelligence** — tag pubblici TronScan o label manuali con `source`, `confidence`, `evidence`.

Le label manuali hanno precedenza su TronScan. Esempio CSV:

```csv
address,type,name,risk,is_contract,source,confidence,evidence
T...,cex,Example Exchange,false,false,case_note,high,confirmed by provider response
```

## Shortest paths

Dopo aver acquisito il grafo puoi chiedere fino a 10 percorsi semplici più brevi fra due indirizzi già presenti:

```bash
tron-analysis -i seeds.txt --depth 3 \
  --path-from T_SOURCE \
  --path-to T_TARGET
```

Il path è calcolato **solo sul dataset acquisito**, quindi l'assenza di un percorso non dimostra l'assenza di una relazione on-chain al di fuori del perimetro analizzato.

## Output

Con `-o output/case001` vengono prodotti:

- `case001.gexf` — Gephi;
- `case001.graphml` — graph tools compatibili;
- `case001_nodes.csv` — nodi con attribution + analytics;
- `case001_edges.csv` — archi aggregati;
- `case001_transfers.csv` — ledger delle transazioni uniche osservate;
- `case001.json` — node-link JSON;
- `case001_neo4j_nodes.csv` e `case001_neo4j_relationships.csv` — import Neo4j;
- `case001_report.html` — report investigativo sintetico con provenance e hash SHA-256.

I CSV generici possono inoltre essere mappati/importati in strumenti di link analysis come Gephi, Maltego o i2 Analyst's Notebook secondo il relativo schema di importazione.

## Pattern euristici

La v2 evidenzia alcuni indicatori strutturali:

- **fan-in / collector**: molte sorgenti verso poche destinazioni;
- **fan-out / distributor**: poche sorgenti verso molte destinazioni;
- **peel_like**: nodo 1-in/1-out con volumi simili.

Sono **euristiche di triage** e non classificazioni probatorie. Un comportamento simile può avere spiegazioni legittime.

## Cache e riproducibilità

Per impostazione predefinita le risposte GET vengono memorizzate in `.cache/tronanalysis` per 24 ore. La cache riduce rate-limit e chiamate duplicate.

```bash
--cache-ttl 86400
--no-cache
```

Il report non salva le API key. Nella provenance memorizza soltanto endpoint, nomi dei parametri e orario di accesso.

## Test

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Note investigative

- Un CEX o bridge può essere un **boundary**: la prosecuzione del tracing può richiedere dati off-chain o cross-chain intelligence. Per default questi nodi non vengono espansi; usare `--no-stop-on-exchange` per modificare il comportamento.
- Il software non sostituisce piattaforme enterprise dotate di dataset proprietari di attribution, risk intelligence e cross-chain tracing.
- Conserva sempre gli artefatti originali e documenta perimetro, filtri e timestamp dell'acquisizione.
- L'utilizzatore è responsabile del rispetto delle norme applicabili, delle autorizzazioni investigative e dei termini dei provider API.

## Compatibilità con il repository precedente

`tron_graph-4.py` resta presente come wrapper e richiama la nuova CLI, così i riferimenti al vecchio file non si rompono immediatamente. Per nuovi utilizzi è preferibile `tron_graph.py` oppure `tron-analysis`.

## Roadmap

L'architettura separa acquisizione TRON, analytics ed export. Questo rende possibile aggiungere in futuro altri provider/chain senza riscrivere la parte di graph analysis.
