"""Schema del database (SQLAlchemy ORM).

Scelta del DB: SQLite.
  - Zero setup, un solo file, banale da persistere su un volume Docker.
  - Piu' che sufficiente per un homelab con pochi gruppi: pochissime scritture
    concorrenti, dataset minuscolo.
Perche' resta facile migrare a PostgreSQL se un giorno servisse scalare:
  - si accede al DB SOLO tramite questi modelli ORM (niente SQL grezzo, niente
    PRAGMA SQLite sparsi nella logica applicativa);
  - ogni tabella ha una chiave primaria surrogata intera e indici espliciti
    sulle colonne di lookup frequente;
  - per cambiare motore basta impostare DATABASE_URL, il resto del codice non
    cambia. Un layer di migrazioni (Alembic) si puo' aggiungere in seguito:
    all'avvio qui usiamo `create_all`, che crea le tabelle mancanti e basta.

Modello di identita' dell'utente: UNA riga User per ogni coppia (gruppo, utente).
  - ruolo, voto e partite_vinte sono concetti "di comunita'": la stessa persona
    puo' essere portiere in un gruppo e attaccante in un altro, e i voti hanno
    senso solo dentro un gruppo. Righe per-gruppo mantengono ogni query
    monogruppo, senza join extra.
  - la chiave concettuale NON e' telegram_user_id: la PK e' un intero surrogato
    (`id`), cosi' in futuro si possono agganciare altri identificatori
    (account collegato, roster importato, ...) senza toccare le foreign key.
    telegram_user_id resta una colonna indicizzata, non la chiave.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

UTC = dt.timezone.utc


def _utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """DateTime sempre timezone-aware in UTC.

    SQLite non ha un tipo datetime nativo con fuso: senza questo decoratore i
    valori tornerebbero "naive" e i confronti con gli orari (aware) dei job
    esploderebbero. Qui: in scrittura normalizziamo a UTC naive, in lettura
    riattacchiamo tzinfo=UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


# --- Enum di dominio -------------------------------------------------------

class Role(str, enum.Enum):
    """Ruolo in campo. Insieme CHIUSO: nessun altro valore e' ammesso."""

    PORTIERE = "Portiere"
    DIFENSORE = "Difensore"
    CENTROCAMPISTA = "Centrocampista"
    ATTACCANTE = "Attaccante"


class MatchState(str, enum.Enum):
    OPEN = "open"                        # iscrizioni aperte / squadre in formazione
    COMPLETE = "complete"                # raggiunti gli N iscritti
    PLAYED = "played"                    # orario passato, in attesa dell'esito
    FINISHED = "finished"               # esito registrato
    OUTCOME_MISSING = "outcome_missing"  # nessuno ha risposto entro il timeout
    CANCELLED = "cancelled"


class Team(str, enum.Enum):
    BIANCA = "Squadra Bianca"
    COLORATA = "Squadra Colorata"


class Outcome(str, enum.Enum):
    BIANCA = "Squadra Bianca"
    COLORATA = "Squadra Colorata"
    PAREGGIO = "Pareggio"


# Stato di un partecipante dentro una partita (stringa semplice: due soli valori).
STATUS_ACTIVE = "active"
STATUS_QUEUED = "queued"


# --- Tabelle -------------------------------------------------------------

class Group(Base):
    """Una riga per ogni chat di gruppo in cui il bot e' attivo."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    configured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Piu' occorrenze a settimana: lista di dict
    #   {"weekday": 0-6 (lun=0), "hour": 0-23, "minute": 0-59}
    # JSON invece di una tabella figlia: non facciamo query "per slot", ci serve
    # solo leggere/riscrivere l'intera lista. Se un giorno servisse interrogarli,
    # una tabella GroupSchedule e' la naturale evoluzione.
    schedule_slots: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Numero di giocatori per partita completa. Parametro, non costante.
    required_players: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    # Durata partita in minuti (serve per calcolare l'orario di fine).
    match_duration_min: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    # Info opzionali raccolte in configurazione.
    field_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Link Google Maps incollato in fase di setup. Se assente, i recap generano
    # comunque un link di RICERCA da field_name/location.
    maps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cost_per_person: Mapped[str | None] = mapped_column(String(60), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="group")
    matches: Mapped[list["Match"]] = relationship(back_populates="group")


class User(Base):
    """Un giocatore, per-gruppo (vedi nota in cima al file)."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("group_id", "telegram_user_id", name="uq_user_group_tg"),
        Index("ix_users_group_tg", "group_id", "telegram_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True, nullable=False)

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # None finche' il giocatore non sceglie il ruolo in onboarding.
    role: Mapped[Role | None] = mapped_column(Enum(Role), nullable=True)
    voto: Mapped[float] = mapped_column(Float, default=6.0, nullable=False)
    partite_vinte: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Cache del ruolo admin: verificato a runtime via getChatAdministrators,
    # qui memorizziamo l'ultimo esito per non chiamare l'API a ogni comando.
    is_admin_cached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    admin_checked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    group: Mapped[Group] = relationship(back_populates="users")


class Match(Base):
    """Una riga per ogni partita organizzata."""

    __tablename__ = "matches"
    __table_args__ = (
        Index("ix_matches_group_state", "group_id", "state"),
        Index("ix_matches_group_kickoff", "group_id", "kickoff_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True, nullable=False)

    kickoff_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True, nullable=False)
    end_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)

    state: Mapped[MatchState] = mapped_column(
        Enum(MatchState), default=MatchState.OPEN, index=True, nullable=False
    )
    # None finche' non registrato; poi BIANCA / COLORATA / PAREGGIO.
    winning_team: Mapped[Outcome | None] = mapped_column(Enum(Outcome), nullable=True)
    # True dopo che e' partito l'unico promemoria di richiesta esito.
    outcome_reminded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    group: Mapped[Group] = relationship(back_populates="matches")
    participants: Mapped[list["MatchParticipant"]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )


class MatchParticipant(Base):
    """Tabella ponte utente <-> partita."""

    __tablename__ = "match_participants"
    __table_args__ = (
        UniqueConstraint("match_id", "user_id", name="uq_participant_match_user"),
        Index("ix_participant_match_status", "match_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)

    # STATUS_ACTIVE (titolare) oppure STATUS_QUEUED (lista d'attesa).
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, nullable=False)
    # Posizione in coda (1 = primo). NULL per i titolari.
    queue_pos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Squadra assegnata dall'algoritmo (solo per i titolari).
    team: Mapped[Team | None] = mapped_column(Enum(Team), nullable=True)

    joined_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    match: Mapped[Match] = relationship(back_populates="participants")
    user: Mapped[User] = relationship()
