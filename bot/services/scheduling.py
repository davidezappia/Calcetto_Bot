"""Calcolo della prossima partita e gestione della riga Match "aperta".

Non importa nulla da `jobs` ne dagli handler: e' logica pura sul DB, cosi'
`jobs.py` puo' importarla senza cicli.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import TZ
from ..models import Group, Match, MatchState
from ..utils import now_utc

log = logging.getLogger(__name__)
UTC = dt.timezone.utc


def next_match_datetime(
    slots: list[dict], after: dt.datetime | None = None
) -> dt.datetime | None:
    """Prima occorrenza (UTC, aware) tra tutti gli slot configurati, > `after`.

    `slots`: lista di {"weekday": 0-6 (lun=0), "hour": 0-23, "minute": 0-59}.
    """
    if not slots:
        return None
    after = after or now_utc()
    after_local = after.astimezone(TZ)

    best: dt.datetime | None = None
    for slot in slots:
        wd, hh, mm = int(slot["weekday"]), int(slot["hour"]), int(slot["minute"])
        days_ahead = (wd - after_local.weekday()) % 7
        cand = (after_local + dt.timedelta(days=days_ahead)).replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        if cand <= after_local:
            cand += dt.timedelta(days=7)
        if best is None or cand < best:
            best = cand

    # .astimezone(UTC) su un datetime gia' aware in Europe/Rome: la conversione
    # gestisce da sola l'ora legale.
    return best.astimezone(UTC)


def get_or_create_open_match(session: Session, group: Group) -> Match | None:
    """La partita a cui si riferiscono /gioco e /coda.

    Se ne esiste una OPEN/COMPLETE futura la restituisce. Se il suo kickoff e'
    gia' passato la chiude (COMPLETE -> PLAYED, altrimenti CANCELLED) e ne apre
    una nuova per la prossima data in calendario.
    """
    now = now_utc()

    existing = (
        session.execute(
            select(Match)
            .where(
                Match.group_id == group.id,
                Match.state.in_([MatchState.OPEN, MatchState.COMPLETE]),
            )
            .order_by(Match.kickoff_at.asc())
        )
        .scalars()
        .first()
    )

    if existing is not None:
        if existing.kickoff_at > now:
            return existing
        existing.state = (
            MatchState.PLAYED
            if existing.state == MatchState.COMPLETE
            else MatchState.CANCELLED
        )
        log.info("match %s scaduto -> %s", existing.id, existing.state.value)

    kickoff = next_match_datetime(group.schedule_slots, now)
    if kickoff is None:
        return None

    match = Match(
        group_id=group.id,
        kickoff_at=kickoff,
        end_at=kickoff + dt.timedelta(minutes=group.match_duration_min),
        state=MatchState.OPEN,
    )
    session.add(match)
    session.flush()
    log.info("creato match %s per gruppo %s @ %s", match.id, group.id, kickoff.isoformat())
    return match


def _demo() -> None:
    # venerdi' 20:30, riferimento un lunedi'
    slots = [{"weekday": 4, "hour": 20, "minute": 30}]
    ref = dt.datetime(2024, 9, 2, 12, 0, tzinfo=UTC)  # lun 2 set 2024
    nxt = next_match_datetime(slots, ref)
    assert nxt is not None and nxt.astimezone(TZ).weekday() == 4
    assert nxt > ref
    # se il riferimento e' dopo lo slot dello stesso giorno -> settimana dopo
    ref2 = dt.datetime(2024, 9, 6, 22, 0, tzinfo=UTC)
    nxt2 = next_match_datetime(slots, ref2)
    assert (nxt2 - ref2).days >= 6
    assert next_match_datetime([], ref) is None
    print("scheduling._demo OK")


if __name__ == "__main__":
    _demo()
