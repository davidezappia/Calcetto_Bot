"""Statistiche: incremento vittorie e classifica di gruppo."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    STATUS_ACTIVE,
    Group,
    Match,
    MatchParticipant,
    Outcome,
    Team,
    User,
)

log = logging.getLogger(__name__)

_OUTCOME_TO_TEAM = {Outcome.BIANCA: Team.BIANCA, Outcome.COLORATA: Team.COLORATA}


def increment_wins(session: Session, match: Match, outcome: Outcome) -> int:
    """+1 a partite_vinte per ogni titolare della squadra vincente. Ritorna il conteggio.

    In caso di pareggio non fa nulla e ritorna 0.
    """
    team = _OUTCOME_TO_TEAM.get(outcome)
    if team is None:
        return 0

    winners = list(
        session.execute(
            select(MatchParticipant).where(
                MatchParticipant.match_id == match.id,
                MatchParticipant.status == STATUS_ACTIVE,
                MatchParticipant.team == team,
            )
        ).scalars()
    )
    for mp in winners:
        mp.user.partite_vinte = (mp.user.partite_vinte or 0) + 1
    session.flush()
    log.info("match %s: +1 vittoria a %d giocatori (%s)", match.id, len(winners), team.value)
    return len(winners)


def leaderboard(session: Session, group: Group, limit: int = 30) -> list[User]:
    """Giocatori del gruppo ordinati per vittorie (poi voto, poi nome)."""
    return list(
        session.execute(
            select(User)
            .where(User.group_id == group.id)
            .order_by(
                User.partite_vinte.desc(),
                User.voto.desc(),
                User.first_name.asc(),
            )
            .limit(limit)
        ).scalars()
    )
