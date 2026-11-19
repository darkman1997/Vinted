# Vinted Bot (Telegram)

Bot Telegram che avvisa quando escono nuovi annunci su Vinted che corrispondono
alle ricerche salvate (con prezzo minimo/massimo, paese e parole da escludere).

Scritto in Python puro (solo libreria standard, Python 3.8+ — nessun `pip install`).

## File principali

| File | Descrizione |
|---|---|
| `vinted.py` | Il bot: gestisce i comandi Telegram e controlla i nuovi annunci |
| `vinted-stato.json` | Stato salvato: ricerche attive e annunci già visti |
| `.github/workflows/vinted.yml` | GitHub Action: controlla i nuovi annunci ogni 5 minuti |

## Configurazione

Variabili d'ambiente:

- `TELEGRAM_TOKEN` (obbligatoria) — token del bot da [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` (facoltativa) — chat di destinazione
- `STATO` (facoltativa) — percorso del file di stato (default `vinted-stato.json`)
- `DOMINIO` (facoltativa) — default `www.vinted.it`

> ⚠️ Su GitHub, salvare `TELEGRAM_TOKEN` nei **Secret** del repository
> (Settings → Secrets and variables → Actions), mai nel codice.

## Uso da terminale

```bash
python vinted.py --once                                # un giro di controllo
python vinted.py --dry-run                             # giro di prova, senza inviare nulla
python vinted.py --test                                # messaggio di prova su Telegram
python vinted.py --aggiungi "harry potter blu-ray" 15  # aggiunge una ricerca (max 15 €)
python vinted.py --lista                               # elenca le ricerche salvate
python vinted.py --togli 2                             # rimuove la ricerca n. 2
```

## Funzionamento su GitHub Actions

Il workflow `Vinted` viene eseguito automaticamente ogni 5 minuti:
controlla le ricerche salvate, invia i nuovi annunci su Telegram e salva
lo stato aggiornato nel repository. Può anche essere lanciato a mano
(scheda **Actions** → **Vinted** → **Run workflow**), con l'opzione
"manda un messaggio di prova" al posto del controllo completo.
