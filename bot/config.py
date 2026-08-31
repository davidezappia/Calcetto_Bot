"""Configurazione runtime letta dalle variabili d'ambiente (.env).

Nessun segreto e' hardcoded: il token del bot arriva SOLO da ambiente.
In sviluppo si usa un file .env (vedi .env.example) caricato da python-dotenv;
in produzione (Docker Compose) le stesse variabili arrivano da `env_file`.
"""
from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Carica .env se presente (in Docker le variabili ci sono gia' nell'ambiente).
load_dotenv()

# Fuso orario unico per TUTTI gli orari mostrati e per ogni job schedulato.
# zoneinfo (standard library, Python 3.9+) legge il database IANA: su immagini
# "slim" serve il pacchetto pip `tzdata` (incluso in requirements.txt).
TZ = ZoneInfo("Europe/Rome")

# Token del bot Telegram (da @BotFather). Obbligatorio: controllato in main().
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()

# Percorso del file SQLite. In Docker punta a un volume montato su /data cosi'
# il database sopravvive a ricreazioni del container.
DB_PATH: str = os.environ.get("DB_PATH", "./data/calcetto.sqlite3")
# Si puo' anche passare un DATABASE_URL completo (es. postgresql+psycopg://...):
# il codice usa solo l'ORM, quindi per migrare a PostgreSQL basta questa variabile.
DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

# Formato partita di default: 10 giocatori = 5 vs 5.
# NON e' una costante di dominio: e' solo il valore proposto in fase di setup,
# poi ogni gruppo salva il proprio numero in Group.required_players e puo'
# cambiarlo con /configura (7 vs 7, 8 vs 8, ...).
DEFAULT_REQUIRED_PLAYERS: int = int(os.environ.get("DEFAULT_REQUIRED_PLAYERS", "10"))

# Durata partita di default (minuti) se il gruppo non la imposta in configurazione.
DEFAULT_MATCH_DURATION_MIN: int = int(os.environ.get("DEFAULT_MATCH_DURATION_MIN", "60"))

# La richiesta dell'esito viene inviata questo numero di ore DOPO la fine partita
# (fine = inizio + durata configurata).
OUTCOME_DELAY_HOURS: int = int(os.environ.get("OUTCOME_DELAY_HOURS", "2"))

# Se nessuno risponde entro queste ore: un solo promemoria, poi "esito non registrato".
OUTCOME_TIMEOUT_HOURS: int = int(os.environ.get("OUTCOME_TIMEOUT_HOURS", "24"))

# Orario (Europe/Rome) del promemoria automatico del giorno dopo la partita.
REMINDER_HOUR: int = int(os.environ.get("REMINDER_HOUR", "10"))
REMINDER_MINUTE: int = int(os.environ.get("REMINDER_MINUTE", "0"))

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()


def setup_logging() -> None:
    """Logging strutturato su stdout (catturato da `docker logs`)."""
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, LOG_LEVEL, logging.INFO),
    )
    # Meno rumore dalle librerie HTTP sottostanti.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
