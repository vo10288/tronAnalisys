# Security

- Non inserire API key, token o credenziali nel repository.
- Usa `.env`, variabili d'ambiente o un secret manager locale.
- Se una chiave è stata pubblicata per errore, revocala e rigenerala presso il provider.
- I dataset investigativi possono contenere informazioni sensibili: evita di committare cartelle `output/`, cache o case data.
