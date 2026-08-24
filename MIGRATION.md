# Migrazione dal repository originale alla v2

La v2 è stata progettata per poter sostituire il contenuto corrente del repository senza perdere il vecchio entry point.

## Procedura consigliata

1. Crea un branch/tag di backup della versione attuale.
2. Copia nel repository tutti i file e le cartelle della v2.
3. Non copiare `.env`, cache o output investigativi.
4. Conserva eventuali file personali/dataset del vecchio repository solo se ancora necessari.
5. Installa le dipendenze e lancia i test.
6. Esegui una piccola acquisizione nota e confronta i risultati con la versione precedente.
7. Pubblica come release/tag `v2.0.0` dopo la tua validazione.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
python tron_graph.py --help
```

## Compatibilità

- `tron_graph-4.py` resta disponibile come wrapper.
- `--keys`, `--api-key`, `--tronscan-key`, input CSV/TXT, `--direction`, `--depth`, `--asset`, `--contract`, `--start`, `--end`, `--window-days`, `--min-amount`, `--labels`, `--output`, `--sleep`, `--no-enrich` e `--no-stop-on-exchange` sono mantenuti o equivalenti.
- La nuova installazione package aggiunge il comando `tron-analysis`.

## Nota su file legacy

La v2 non include `tok.txt` perché non è necessario al nuovo motore. Se nel tuo workflow quel file ha un significato specifico, conservalo separatamente dopo aver verificato che non contenga credenziali o informazioni che non vuoi pubblicare.

## Prima del push

```bash
git status
git diff --stat
find . -name '.env' -o -name '*.log'
grep -RniE 'TRONGRID_KEY=.{10,}|TRONSCAN_KEY=.{10,}' . --exclude='.env.example' || true
pytest -q
```

Non è stata aggiunta automaticamente una licenza software: la scelta tra MIT, Apache-2.0, GPL o altra licenza è una decisione del maintainer.
