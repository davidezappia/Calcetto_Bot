"""Comandi di iscrizione / ritiro / coda."""
from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from ..db import session_scope
from ..jobs import cancel_match_jobs, schedule_outcome_request
from ..models import STATUS_ACTIVE, STATUS_QUEUED, Match, MatchState
from ..services.queue import (
    active_participants,
    add_active,
    add_queued,
    find_participant,
    queued_participants,
    recompute_and_store_teams,
    remove_participant,
)
from ..services.scheduling import get_or_create_open_match, next_match_datetime
from ..utils import esc, fmt_dt, mention, place_line, teams_summary
from .common import (
    get_or_create_group,
    get_or_create_user,
    group_chat_only,
    safe_handler,
)

log = logging.getLogger(__name__)


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text)


def _open_match(session, group) -> Match | None:
    return (
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


@safe_handler
async def cmd_gioco(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await _reply(update, "Usa questo comando nel gruppo del calcetto.")
        return

    summary_text: str | None = None
    completion_text: str | None = None
    outcome_job: tuple[int, object] | None = None
    chat_id = update.effective_chat.id

    with session_scope() as s:
        group = get_or_create_group(s, chat_id)
        if not group.configured:
            await _reply(update, "Gruppo non ancora configurato. Un admin usi /configura.")
            return
        user = get_or_create_user(s, group, update.effective_user)

        match = get_or_create_open_match(s, group)
        if match is None:
            await _reply(update, "Nessun giorno/orario configurato. Un admin usi /configura.")
            return

        actives = active_participants(s, match)
        if any(mp.user_id == user.id for mp in actives):
            await _reply(update, "Sei già iscritto a questa partita. ✅")
            return
        if len(actives) >= group.required_players:
            await _reply(update, "Partita piena. Usa /coda per la lista d'attesa.")
            return

        add_active(s, match, user)
        result = recompute_and_store_teams(s, match, group)
        active_count = len(actives) + 1

        nudge = "" if user.role is not None else "\n\nℹ️ Non hai ancora un ruolo: usa /ruolo."
        summary_text = (
            f"{mention(user.telegram_user_id, user.first_name, user.telegram_username)} "
            "iscritto! ✅"
            + nudge
            + "\n\n"
            + teams_summary(result, group.required_players, match.kickoff_at, field_name=group.field_name, location=group.location, maps_link=group.maps_url)
        )

        if active_count >= group.required_players and match.state != MatchState.COMPLETE:
            match.state = MatchState.COMPLETE
            log.info("match %s COMPLETO", match.id)
            completion_text = (
                "🎉 <b>Squadre al completo!</b> Ci siamo tutti.\n\n"
                + teams_summary(result, group.required_players, match.kickoff_at, field_name=group.field_name, location=group.location, maps_link=group.maps_url)
            )
            outcome_job = (match.id, match.end_at)

    await _reply(update, summary_text)
    if completion_text:
        await context.bot.send_message(chat_id, completion_text)
    if outcome_job:
        schedule_outcome_request(context.job_queue, outcome_job[0], chat_id, outcome_job[1])


@safe_handler
async def cmd_ritiro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await _reply(update, "Usa questo comando nel gruppo del calcetto.")
        return

    chat_id = update.effective_chat.id
    summary_text: str | None = None
    reverted_match_id: int | None = None

    with session_scope() as s:
        group = get_or_create_group(s, chat_id)
        if not group.configured:
            await _reply(update, "Gruppo non configurato.")
            return
        user = get_or_create_user(s, group, update.effective_user)

        match = _open_match(s, group)
        if match is None:
            await _reply(update, "Non c'è nessuna partita aperta.")
            return

        esito, promoted = remove_participant(s, match, user)
        if esito == "not_found":
            await _reply(update, "Non risultavi iscritto a questa partita.")
            return

        result = recompute_and_store_teams(s, match, group)
        active_count = len(result.bianca) + len(result.colorata)

        if (
            match.state == MatchState.COMPLETE
            and active_count < group.required_players
            and promoted is None
        ):
            match.state = MatchState.OPEN
            reverted_match_id = match.id
            log.info("match %s: torna OPEN dopo un ritiro", match.id)

        if promoted is not None:
            summary_text = (
                f"{mention(promoted.telegram_user_id, promoted.first_name, promoted.telegram_username)} "
                "entra dalla coda! ✅\n\n"
                + teams_summary(result, group.required_players, match.kickoff_at, field_name=group.field_name, location=group.location, maps_link=group.maps_url)
            )
        else:
            summary_text = (
                "Iscritto ritirato.\n\n"
                + teams_summary(result, group.required_players, match.kickoff_at, field_name=group.field_name, location=group.location, maps_link=group.maps_url)
            )

    await _reply(update, "Ti sei ritirato dalla partita. 👋")
    await context.bot.send_message(chat_id, summary_text)
    if reverted_match_id is not None:
        cancel_match_jobs(context.job_queue, reverted_match_id)


@safe_handler
async def cmd_coda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await _reply(update, "Usa questo comando nel gruppo del calcetto.")
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        if not group.configured:
            await _reply(update, "Gruppo non configurato.")
            return
        user = get_or_create_user(s, group, update.effective_user)

        match = _open_match(s, group)
        if match is None:
            await _reply(update, "Non c'è nessuna partita aperta.")
            return

        actives = active_participants(s, match)
        if len(actives) < group.required_players:
            liberi = group.required_players - len(actives)
            await _reply(update, f"Ci sono ancora {liberi} posti liberi. Usa /gioco.")
            return

        existing = find_participant(s, match, user)
        if existing and existing.status == STATUS_ACTIVE:
            await _reply(update, "Sei già tra i titolari di questa partita.")
            return
        if existing and existing.status == STATUS_QUEUED:
            await _reply(update, f"Sei già in coda, posizione {existing.queue_pos}.")
            return

        pos = add_queued(s, match, user).queue_pos

    await _reply(update, f"Aggiunto in coda. Posizione: {pos}. 📋")


@safe_handler
async def cmd_ritirocoda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await _reply(update, "Usa questo comando nel gruppo del calcetto.")
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        if not group.configured:
            await _reply(update, "Gruppo non configurato.")
            return
        user = get_or_create_user(s, group, update.effective_user)

        match = _open_match(s, group)
        if match is None:
            await _reply(update, "Non c'è nessuna partita aperta.")
            return

        mp = find_participant(s, match, user)
        if mp is None or mp.status != STATUS_QUEUED:
            await _reply(update, "Non sei in coda.")
            return

        remove_participant(s, match, user)  # rimuove e rinumera la coda

    await _reply(update, "Rimosso dalla coda. ✅")


@safe_handler
async def cmd_vedicoda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await _reply(update, "Usa questo comando nel gruppo del calcetto.")
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        if not group.configured:
            await _reply(update, "Gruppo non configurato.")
            return

        match = _open_match(s, group)
        if match is None:
            await _reply(update, "Non c'è nessuna partita aperta.")
            return

        coda = queued_participants(s, match)
        if not coda:
            text = "Coda vuota. 📭"
        else:
            righe = ["📋 <b>Lista d'attesa</b>"]
            for mp in coda:
                u = mp.user
                nome = esc(
                    u.first_name
                    or (f"@{u.telegram_username}" if u.telegram_username else "Giocatore")
                )
                righe.append(f"{mp.queue_pos}. {nome}")
            text = "\n".join(righe)

    await _reply(update, text)


_STATO_LABEL = {
    MatchState.OPEN: "iscrizioni aperte",
    MatchState.COMPLETE: "squadre al completo",
}


@safe_handler
async def cmd_partita(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recap della partita corrente: data, squadre aggiornate, coda."""
    if not group_chat_only(update):
        await _reply(update, "Usa questo comando nel gruppo del calcetto.")
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        if not group.configured:
            await _reply(update, "Gruppo non configurato. Un admin usi /configura.")
            return

        match = _open_match(s, group)
        if match is None:
            prossima = next_match_datetime(group.schedule_slots, None)
            if prossima is None:
                await _reply(update, "Nessuna partita in programma.")
            else:
                luogo = place_line(group.field_name, group.location, group.maps_url)
                await _reply(
                    update,
                    f"Nessuna partita aperta. Prossima: <b>{fmt_dt(prossima)}</b>.\n"
                    + (luogo + "\n" if luogo else "")
                    + "Usa /gioco per aprire le iscrizioni.",
                )
            return

        # Ricalcolo deterministico dallo stato attuale (stesso risultato del live).
        result = recompute_and_store_teams(s, match, group)
        coda = queued_participants(s, match)

        parti = [
            f"📋 <b>Recap partita</b> — {_STATO_LABEL.get(match.state, match.state.value)}",
            "",
            teams_summary(result, group.required_players, match.kickoff_at, field_name=group.field_name, location=group.location, maps_link=group.maps_url),
        ]
        if coda:
            parti.append("")
            parti.append("⏳ <b>In coda</b>")
            for mp in coda:
                u = mp.user
                nome = esc(
                    u.first_name
                    or (f"@{u.telegram_username}" if u.telegram_username else "Giocatore")
                )
                parti.append(f"{mp.queue_pos}. {nome}")
        text = "\n".join(parti)

    await _reply(update, text)
