"""Iscritti, lista d'attesa e ricalcolo squadre.

Tutte le funzioni lavorano su una Session gia' aperta (transazione del chiamante).
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    STATUS_ACTIVE,
    STATUS_QUEUED,
    Group,
    Match,
    MatchParticipant,
    MatchState,
    Team,
    User,
)
from ..utils import mention, teams_summary
from .teams import PlayerView, TeamsResult, form_teams

log = logging.getLogger(__name__)


def _by_status(session: Session, match: Match, status: str) -> list[MatchParticipant]:
    return list(
        session.execute(
            select(MatchParticipant).where(
                MatchParticipant.match_id == match.id,
                MatchParticipant.status == status,
            )
        ).scalars()
    )


def active_participants(session: Session, match: Match) -> list[MatchParticipant]:
    rows = _by_status(session, match, STATUS_ACTIVE)
    return sorted(rows, key=lambda p: (p.joined_at, p.id))


def queued_participants(session: Session, match: Match) -> list[MatchParticipant]:
    rows = _by_status(session, match, STATUS_QUEUED)
    return sorted(rows, key=lambda p: (p.queue_pos if p.queue_pos is not None else 1_000_000, p.id))


def find_participant(session: Session, match: Match, user: User) -> MatchParticipant | None:
    return (
        session.execute(
            select(MatchParticipant).where(
                MatchParticipant.match_id == match.id,
                MatchParticipant.user_id == user.id,
            )
        )
        .scalars()
        .first()
    )


def add_active(session: Session, match: Match, user: User) -> MatchParticipant:
    existing = find_participant(session, match, user)
    if existing is not None:
        return existing
    mp = MatchParticipant(match_id=match.id, user_id=user.id, status=STATUS_ACTIVE)
    session.add(mp)
    session.flush()
    return mp


def add_queued(session: Session, match: Match, user: User) -> MatchParticipant:
    existing = find_participant(session, match, user)
    if existing is not None:
        return existing
    max_pos = session.execute(
        select(func.max(MatchParticipant.queue_pos)).where(
            MatchParticipant.match_id == match.id,
            MatchParticipant.status == STATUS_QUEUED,
        )
    ).scalar()
    mp = MatchParticipant(
        match_id=match.id,
        user_id=user.id,
        status=STATUS_QUEUED,
        queue_pos=(max_pos or 0) + 1,
    )
    session.add(mp)
    session.flush()
    return mp


def renumber_queue(session: Session, match: Match) -> None:
    for i, mp in enumerate(queued_participants(session, match), start=1):
        mp.queue_pos = i
    session.flush()


def remove_participant(
    session: Session, match: Match, user: User
) -> tuple[str, User | None]:
    """Rimuove l'utente da titolari o coda.

    Ritorna (esito, promosso):
      esito in {"removed_active", "removed_queued", "not_found"};
      `promosso` = User promosso dalla coda a titolare (solo se si e' liberato
      un posto da titolare e la coda non era vuota), altrimenti None.
    """
    mp = find_participant(session, match, user)
    if mp is None:
        return "not_found", None

    was_active = mp.status == STATUS_ACTIVE
    session.delete(mp)
    session.flush()

    promoted: User | None = None
    if was_active:
        coda = queued_participants(session, match)
        if coda:
            primo = coda[0]
            primo.status = STATUS_ACTIVE
            primo.queue_pos = None
            promoted = primo.user
            session.flush()

    renumber_queue(session, match)
    return ("removed_active" if was_active else "removed_queued"), promoted


def _to_view(mp: MatchParticipant) -> PlayerView:
    u = mp.user
    name = u.first_name or (f"@{u.telegram_username}" if u.telegram_username else "Giocatore")
    return PlayerView(
        user_id=u.id,
        telegram_user_id=u.telegram_user_id,
        display_name=name,
        username=u.telegram_username,
        role=u.role,
        voto=u.voto if u.voto is not None else 6.0,
    )


def recompute_and_store_teams(session: Session, match: Match, group: Group) -> TeamsResult:
    """Ricalcola le squadre da zero e le persiste su MatchParticipant.team."""
    actives = active_participants(session, match)
    result = form_teams([_to_view(mp) for mp in actives], group.required_players)

    team_by_user: dict[int, Team] = {}
    for p in result.bianca:
        team_by_user[p.user_id] = Team.BIANCA
    for p in result.colorata:
        team_by_user[p.user_id] = Team.COLORATA

    for mp in actives:
        mp.team = team_by_user.get(mp.user_id)
    for mp in queued_participants(session, match):
        mp.team = None
    session.flush()

    log.info(
        "match %s: squadre ricalcolate (%d titolari)", match.id, len(actives)
    )
    return result


def purge_user(session: Session, group: Group, user: User) -> list[dict]:
    """Rimuove del tutto un utente che ha lasciato il gruppo.

    - Per ogni partita ancora aperta (OPEN/COMPLETE): lo toglie dagli iscritti,
      promuove il primo in coda se serve, ricalcola le squadre e, se la partita
      era COMPLETE ed e' scesa sotto il numero richiesto senza promozione, la
      riporta a OPEN.
    - Cancella tutte le altre righe MatchParticipant (storico partite passate).
    - Cancella la riga User.

    Ritorna una lista di dict (una per partita aperta modificata) con il testo
    gia' pronto per il gruppo: {match_id, promoted_mention, reverted, summary}.
    """
    touched: list[dict] = []

    open_matches = list(
        session.execute(
            select(Match).where(
                Match.group_id == group.id,
                Match.state.in_([MatchState.OPEN, MatchState.COMPLETE]),
            )
        ).scalars()
    )
    for match in open_matches:
        if find_participant(session, match, user) is None:
            continue
        _, promoted = remove_participant(session, match, user)
        result = recompute_and_store_teams(session, match, group)
        active_count = len(result.bianca) + len(result.colorata)

        reverted = (
            match.state == MatchState.COMPLETE
            and active_count < group.required_players
            and promoted is None
        )
        if reverted:
            match.state = MatchState.OPEN
            log.info("match %s: torna OPEN (giocatore uscito dal gruppo)", match.id)

        touched.append(
            {
                "match_id": match.id,
                "promoted_mention": (
                    mention(
                        promoted.telegram_user_id,
                        promoted.first_name,
                        promoted.telegram_username,
                    )
                    if promoted is not None
                    else None
                ),
                "reverted": reverted,
                "summary": teams_summary(
                    result,
                    group.required_players,
                    match.kickoff_at,
                    field_name=group.field_name,
                    location=group.location,
                    maps_link=group.maps_url,
                ),
            }
        )

    # Storico: partite passate / non aperte.
    session.query(MatchParticipant).filter(
        MatchParticipant.user_id == user.id
    ).delete(synchronize_session=False)
    session.delete(user)
    session.flush()
    log.info("utente %s eliminato dal gruppo %s", user.id, group.id)
    return touched
