# CalcettoBot

Bot Telegram per organizzare partite di calcetto tra amici in una chat di gruppo:
iscrizioni, lista d'attesa, formazione automatica delle squadre bilanciate,
ranking qualitativo dei giocatori, statistiche vittorie e promemoria automatici.

Il numero di giocatori per partita è **configurabile per gruppo** (default 10 =
5 vs 5). Per passare a 7 vs 7 o 8 vs 8 basta `/configura`, senza toccare il codice.

- Python 3.11+
- [python-telegram-bot](https://python-telegram-bot.org/) v21 (API async, JobQueue)
- SQLite via SQLAlchemy ORM, file persistito su volume Docker
- Timezone fissa: `Europe/Rome`

---

## 1. Creare il bot su Telegram (BotFather)

1. Apri una chat con [@BotFather](https://t.me/BotFather).
2. `/newbot` → scegli nome e username → BotFather ti dà il **token** (`123456:AA...`).
3. `/setprivacy` → seleziona il bot → **Disable**.
   Con la privacy attiva il bot non vedrebbe i messaggi di testo del wizard di
   configurazione (ne vede solo comandi/reply/menzioni).
4. Consigliato: `/setjoingroups` → **Enable** (di default lo è).

### Permessi nel gruppo

Aggiungi il bot al gruppo e **promuovilo ad amministratore**. Serve per:

- ricevere in modo affidabile gli ingressi dei nuovi membri (`new_chat_members`);
- leggere l'elenco admin con `getChatAdministrators` (comandi `/voto` e `/configura`);
- (con privacy disattivata) vedere le risposte di testo durante `/configura`.

---

## 2. Variabili d'ambiente

Copia `.env.example` in `.env` e compila almeno `BOT_TOKEN`.

| Variabile | Default | Descrizione |
|---|---|---|
| `BOT_TOKEN` | — | **Obbligatorio.** Token di BotFather. |
| `DB_PATH` | `/data/calcetto.sqlite3` | Percorso del file SQLite (su volume in Docker). |
| `DATABASE_URL` | derivato da `DB_PATH` | URL SQLAlchemy completo; impostalo per usare PostgreSQL. |
| `DEFAULT_REQUIRED_PLAYERS` | `10` | Numero giocatori proposto in `/configura`. |
| `DEFAULT_MATCH_DURATION_MIN` | `60` | Durata partita se non impostata nel setup. |
| `OUTCOME_DELAY_HOURS` | `2` | Ore dopo la **fine** partita per chiedere l'esito. |
| `OUTCOME_TIMEOUT_HOURS` | `24` | Timeout senza risposta: 1 promemoria, poi "esito non registrato". |
| `REMINDER_HOUR` / `REMINDER_MINUTE` | `10` / `0` | Orario (Europe/Rome) del promemoria del giorno dopo. |
| `LOG_LEVEL` | `INFO` | Livello di logging. |

---

## 3. Avvio con Docker Compose

```bash
cp .env.example .env
# ... modifica .env inserendo BOT_TOKEN ...

docker compose up -d --build
docker compose logs -f          # segui i log
docker compose down             # ferma (il volume calcetto-data resta)
```

Il database SQLite vive nel volume `calcetto-data` (montato su `/data`) e
sopravvive a `down`, ricostruzioni dell'immagine e riavvii dell'host.

### Avvio senza Docker (sviluppo)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN=...            # oppure metti tutto in .env
python -m bot.main
```

---

## 4. Come si usa

1. **Aggiungi il bot al gruppo** e rendilo admin. Il bot chiede a un
   amministratore di lanciare `/configura`.
2. **`/configura`** (solo admin): wizard guidato → quante volte a settimana si
   gioca, per ognuna giorno + ora, numero giocatori, durata, info opzionali
   (campo, luogo, costo, note) e — se è stato indicato un campo/luogo — il
   **link Google Maps** da incollare (Maps → Condividi → Copia link); con `-`
   il bot genera da solo un link di ricerca dal nome del luogo. Al termine,
   riepilogo nel gruppo. Rilanciabile in qualsiasi momento per modificare.
3. **Nuovi utenti**: quando qualcuno entra nel gruppo il bot lo registra
   (voto iniziale 6.0) e gli chiede il ruolo con 4 pulsanti — Portiere,
   Difensore, Centrocampista, Attaccante.
   **Utenti già presenti prima del bot**: Telegram non consente a un bot di
   elencare i membri esistenti, quindi non c'è assegnazione in blocco. Ognuno
   lancia una volta **`/ruolo`** e sceglie dai 4 pulsanti (stesso comando serve
   anche per cambiare ruolo in seguito). Chi si iscrive con `/gioco` senza aver
   scelto il ruolo viene comunque accettato e riceve un promemoria a usare
   `/ruolo`; l'algoritmo squadre gestisce i ruoli mancanti.
4. **Iscrizioni**: `/gioco` per entrare, `/coda` quando è piena, ecc.
4b. **Utenti che lasciano il gruppo**: quando un utente esce o viene rimosso, il
   bot lo **elimina dal database** e da ogni partita a cui era iscritto. Se era
   titolare di una partita aperta, il primo in lista d'attesa viene promosso (o
   la partita torna ad accettare iscrizioni) e il gruppo riceve il recap
   aggiornato.
5. A ogni cambiamento il bot **ricalcola le squadre** e pubblica il riepilogo
   aggiornato (Squadra Bianca / Squadra Colorata, con nome e ruolo, `X/N iscritti`).
6. Raggiunti gli N iscritti: messaggio di **partita al completo** con la
   formazione finale; le iscrizioni successive vanno in `/coda`.
7. **2 ore dopo la fine** partita: il bot chiede in automatico quale squadra ha
   vinto (Bianca / Colorata / Pareggio). La **prima** risposta valida vince,
   le successive vengono ignorate con un avviso. In caso di vittoria,
   `partite_vinte` +1 per ogni giocatore della squadra vincente.
8. **Giorno dopo, ore 10:00**: se ieri si è giocato, promemoria con data della
   prossima partita e istruzioni per iscriversi.

I job programmati (promemoria e richieste esito) vengono **ricostruiti dal DB a
ogni riavvio** del bot, quindi un restart non perde nulla.

---

## 5. Comandi

| Comando | Chi | Cosa fa |
|---|---|---|
| `/gioco` | tutti | Iscrive alla prossima partita, se ci sono posti. |
| `/partita` | tutti | Recap della partita corrente: data, Squadra Bianca/Colorata aggiornate, lista d'attesa. Alias: `/recap`, `/squadre`. |
| `/ritiro` | tutti | Toglie dagli iscritti; promuove il primo in coda. |
| `/coda` | tutti | Entra in lista d'attesa (solo a partita piena). |
| `/ritirocoda` | tutti | Esce dalla lista d'attesa; ricalcola le posizioni. |
| `/vedicoda` | tutti | Mostra la lista d'attesa ordinata. |
| `/ruolo` | tutti | Imposta o cambia il proprio ruolo (Portiere/Difensore/Centrocampista/Attaccante). |
| `/statistiche` | tutti | Classifica dei giocatori per partite vinte (riepilogo di fine anno). |
| `/voto @utente <1-10>` | **admin** | Imposta il voto di un giocatore. Anche in *reply* a un suo messaggio: `/voto 7.5`. |
| `/configura` | **admin** | Avvia o rilancia la configurazione del gruppo. |
| `/help` | tutti | Elenco dei comandi. |
| `/annulla` | — | Esce dal wizard di `/configura`. |

---

## 6. Struttura del progetto

```
bot/
  config.py              variabili d'ambiente, timezone, costanti
  utils.py               date/fuso, parsing input, formattazione messaggi
  models.py              schema DB (SQLAlchemy): Group, User, Match, MatchParticipant
  db.py                  engine + session_scope() (transazioni)
  jobs.py                job programmati: promemoria, richiesta esito, reload_jobs()
  main.py                bootstrap: Application, registrazione handler, polling
  services/
    scheduling.py        prossima partita, get_or_create_open_match
    teams.py             algoritmo di formazione squadre (deterministico)
    queue.py             iscritti, coda, ricalcolo e persistenza squadre
    stats.py             incremento vittorie, classifica
  handlers/
    common.py            get-or-create, check admin, wrapper anti-crash
    setup.py             ConversationHandler di /configura
    onboarding.py        ingresso bot + scelta ruolo nuovi utenti
    signup.py            /gioco /ritiro /coda /ritirocoda /vedicoda
    votes.py             /voto
    stats.py             /statistiche /help
    outcome.py           callback "chi ha vinto?"
```

### Modello dati (sintesi)

- **Group** — una per chat: `telegram_chat_id`, `configured`, `schedule_slots`
  (lista JSON di `{weekday, hour, minute}`), `required_players`,
  `match_duration_min`, `field_name`, `location`, `maps_url`, `cost_per_person`,
  `notes`.
- **User** — una per **(gruppo, utente)**: PK surrogata `id` (non il solo
  `telegram_user_id`), `role` (enum a 4 valori, nullable finché non scelto),
  `voto` (float, default 6.0), `partite_vinte` (int), cache del ruolo admin.
- **Match** — `kickoff_at`, `end_at`, `state`
  (`open`/`complete`/`played`/`finished`/`outcome_missing`/`cancelled`),
  `winning_team` (nullable), `outcome_reminded`.
- **MatchParticipant** — ponte utente↔partita: `status` (`active`/`queued`),
  `queue_pos`, `team` (Bianca/Colorata).

Indici su `telegram_chat_id`, `telegram_user_id`, `(group_id, state)` dei match.
Tutti gli orari sono salvati in UTC (aware) e mostrati in `Europe/Rome`.

### Perché SQLite e come migrare a PostgreSQL

SQLite: zero setup, un file, ideale per un homelab con pochi gruppi. Il codice
usa **solo l'ORM**, ogni tabella ha PK intera e indici espliciti: per passare a
PostgreSQL basta impostare `DATABASE_URL` (es.
`postgresql+psycopg://user:pass@host/db`) e installare il driver. Un layer di
migrazioni (Alembic) è aggiungibile in seguito; qui lo schema è creato con
`create_all` all'avvio.

> All'avvio, oltre a `create_all`, `init_db()` esegue una **auto-migrazione
> minimale per SQLite**: confronta i modelli con lo schema reale e aggiunge con
> `ALTER TABLE ADD COLUMN` le colonne nuove (nullable / con default scalare).
> Copre l'evoluzione di questo progetto; per modifiche più complesse si passa ad
> Alembic. Su PostgreSQL l'auto-migrazione non gira: usare Alembic.

---

## 7. Note operative

- **Luogo nei recap**: se in `/configura` è stato impostato il campo e/o il
  luogo, ogni recap squadre (e il promemoria) mostra una riga `📍` cliccabile.
  Il link è quello Google Maps incollato in configurazione; se non è stato
  incollato, il bot genera un link di ricerca da luogo/nome campo. Senza campo
  né luogo, la riga viene omessa.
- **Formazione squadre**: ricalcolata da zero a ogni cambio iscritti, output
  deterministico. Vincolo rigido: max 1 portiere per squadra (1 solo portiere →
  va alla Bianca, segnalato; 3+ portieri → i sovrannumerari come giocatori di
  movimento, anomalia segnalata). Tra i giocatori di movimento: draft "a
  serpente" greedy che pareggia voto medio e distribuzione dei ruoli. Se manca
  qualche ruolo → split bilanciato solo per numero/voto.
- **Race condition iscrizioni**: PTB processa gli update in sequenza e il
  controllo "meno di N iscritti" + l'inserimento stanno nella stessa
  transazione; il vincolo unico `(match_id, user_id)` blocca i doppioni.
- **Self-check algoritmi**: `python -m bot.services.teams` e
  `python -m bot.services.scheduling` eseguono asserzioni di base senza
  dipendenze esterne.
