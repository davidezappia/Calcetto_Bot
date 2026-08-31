"""Callback della tastiera "chi ha vinto?".

Chi puo' rispondere: chiunque nel gruppo. Il risultato e' noto a tutti i
giocatori; vincolarlo a un admin rischia di lasciarlo non registrato se nessun
admin ha giocato. Le risposte discordanti sono risolte cosi': vince la PRIMA
risposta valida. python-telegram-bot processa gli update in sequenza e qui, nella
stessa transazione, ricontrolliamo che `winning_team` sia ancora NULL; i click
successivi ricevono un popup che segnala che l'esito e' gia' stato registrato.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..db import session_scope
from ..models import Match, MatchState, Outcome
from ..services.stats import increment_wins
from ..utils import fmt_dt
from .common import safe_handler

log = logging.getLogger(__name__)


@safe_handler
async def on_outcome_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        _, match_id_str, outcome_name = query.data.split(":")
        match_id = int(match_id_str)
        outcome = Outcome[outcome_name]
    except (ValueError, KeyError):
        await query.answer("Dato non valido.", show_alert=True)
        return

    fallback_chat_id: int | None = None

    with session_scope() as s:
        match = s.get(Match, match_id)
        if match is None:
            await query.answer("Partita non trovata.", show_alert=True)
            return

        if match.winning_team is not None or match.state == MatchState.FINISHED:
            gia = match.winning_team.value if match.winning_team else "—"
            await query.answer(f"Esito già registrato: {gia}.", show_alert=True)
            return
        if match.state not in (MatchState.PLAYED, MatchState.COMPLETE):
            await query.answer("Questa partita non è in attesa di esito.", show_alert=True)
            return

        match.winning_team = outcome
        match.state = MatchState.FINISHED
        winners = increment_wins(s, match, outcome) if outcome != Outcome.PAREGGIO else 0
        kickoff = match.kickoff_at
        fallback_chat_id = match.group.telegram_chat_id
        log.info("match %s: esito = %s (%d vincitori)", match_id, outcome.value, winners)

    await query.answer("Esito registrato!")
    if outcome == Outcome.PAREGGIO:
        text = f"🤝 Partita di {fmt_dt(kickoff)}: <b>Pareggio</b> registrato."
    else:
        text = (
            f"🏁 Partita di {fmt_dt(kickoff)}: ha vinto <b>{outcome.value}</b>. "
            f"+1 vittoria per {winners} giocatori. 🏆"
        )
    try:
        await query.edit_message_text(text)
    except Exception:
        await context.bot.send_message(fallback_chat_id, text)
