"""Engine e sessioni SQLAlchemy.

`session_scope()` incapsula una transazione: commit se tutto ok, rollback in caso
di errore. E' anche il meccanismo che protegge il limite di N giocatori dalle
iscrizioni quasi simultanee: il controllo del numero e l'inserimento avvengono
nella stessa transazione.

Nota sulla concorrenza: python-telegram-bot processa gli update in sequenza (non
impostiamo `concurrent_updates`), quindi due /gioco non vengono mai eseguiti
davvero in parallelo. Il vincolo di unicita' (match_id, user_id) evita comunque
i doppioni. Per un deploy multi-processo servirebbe un lock esplicito
(BEGIN IMMEDIATE su SQLite, oppure SELECT ... FOR UPDATE su PostgreSQL).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL
from .models import Base

log = logging.getLogger(__name__)

# check_same_thread=False: la JobQueue di PTB gira su un thread diverso da quello
# che serve gli update; serve solo a SQLite.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _sqlite_add_missing_columns() -> None:
    """Auto-migrazione minimale per SQLite: aggiunge le colonne nuove.

    `create_all` crea le tabelle mancanti ma non tocca quelle esistenti, quindi
    una colonna aggiunta a un modello non comparirebbe su un DB gia' popolato.
    Qui confrontiamo i modelli con lo schema reale e facciamo `ALTER TABLE ADD
    COLUMN` per il mancante. Funziona solo per colonne nullable / con default
    scalare (SQLite non consente ADD COLUMN NOT NULL senza default): sufficiente
    per l'evoluzione di questo progetto. Per cambi piu' complessi -> Alembic.
    """
    if engine.dialect.name != "sqlite":
        return
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                log.warning("migrazione: aggiunta colonna %s.%s", table.name, col.name)
            except Exception:
                log.exception("migrazione: impossibile aggiungere %s.%s", table.name, col.name)


def init_db() -> None:
    """Crea le tabelle mancanti e allinea le colonne. Idempotente."""
    Base.metadata.create_all(engine)
    _sqlite_add_missing_columns()
    log.info("schema DB pronto (%s)", DATABASE_URL.split("://", 1)[0])


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
