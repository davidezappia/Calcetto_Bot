"""Job programmati: promemoria giornaliero e richiesta esito partita.

Come i job sopravvivono a un riavvio
-----------------------------------
La JobQueue di python-telegram-bot NON e' persistente: a ogni avvio parte vuota.
`reload_jobs(app)` viene chiamato in `post_init` (vedi main.py) e ricostruisce
tutto leggendo dal DB:

  * un `run_daily` di promemoria per OGNI gruppo con `configured = True`;
  * un `run_once` di richiesta esito per OGNI partita gia' iniziata e ancora
    senza `winning_team` (stati COMPLETE/PLAYED). Se l'orario in cui la domanda
    andava fatta e' passato mentre il bot era spento, la si fa subito.

Cosi' spegnere/riaggiornare il container non perde ne' i promemoria ne' le
richieste di esito.
"""
from __future__ import annotations

import datetime as dt
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes
from sqlalchemy import select

from .config import (
    OUTCOME_DELAY_HOURS,
    OUTCOME_TIMEOUT_HOURS,
    REMINDER_HOUR,
    REMINDER_MINUTE,
    TZ,
)
from .db import session_scope
from .models import Group, Match, MatchState
from .services.scheduling import next_match_datetime
from .utils import fmt_dt, now_utc, place_line, to_local

log = logging.getLogger(__name__)
UTC = dt.timezone.utc

REMINDER_JOB = "reminder:{}"
OUTCOME_JOB = "outcome:{}"
OUTCOME_TIMEOUT_JOB = "outcome_timeout:{}"


def _remove_jobs(job_queue, name: str) -> None:
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def outcome_keyboard(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏳️ Squadra Bianca", callback_data=f"outcome:{match_id}:BIANCA"),
                InlineKeyboardButton("🎽 Squadra Colorata", callback_data=f"outcome:{match_id}:COLORATA"),
            ],
            [InlineKeyboardButton("🤝 Pareggio", callback_data=f"outcome:{match_id}:PAREGGIO")],
        ]
    )


# --- pianificazione ----------------------------------------------------------

def schedule_group_reminder(job_queue, group_id: int, chat_id: int) -> None:
    name = REMINDER_JOB.format(group_id)
    _remove_jobs(job_queue, name)
    job_queue.run_daily(
        daily_reminder,
        time=dt.time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TZ),
        data={"group_id": group_id, "chat_id": chat_id},
        name=name,
    )
    log.info(
        "promemoria giornaliero pianificato per gruppo %s alle %02d:%02d (Europe/Rome)",
        group_id, REMINDER_HOUR, REMINDER_MINUTE,
    )


def schedule_outcome_request(job_queue, match_id: int, chat_id: int, end_at: dt.datetime) -> None:
    """Programma la domanda sull'esito a `end_at + OUTCOME_DELAY_HOURS`.

    Se quel momento e' gia' passato (es. riavvio del bot), la programma subito.
    """
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=UTC)
    when = end_at + dt.timedelta(hours=OUTCOME_DELAY_HOURS)
    now = now_utc()
    run_at = when if when > now else now + dt.timedelta(seconds=10)

    name = OUTCOME_JOB.format(match_id)
    _remove_jobs(job_queue, name)
    job_queue.run_once(
        outcome_request,
        when=run_at,
        data={"match_id": match_id, "chat_id": chat_id},
        name=name,
    )
    log.info("richiesta esito match %s pianificata @ %s", match_id, run_at.isoformat())


def cancel_match_jobs(job_queue, match_id: int) -> None:
    _remove_jobs(job_queue, OUTCOME_JOB.format(match_id))
    _remove_jobs(job_queue, OUTCOME_TIMEOUT_JOB.format(match_id))


# --- callback dei job ------------------------------------------------------

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ogni giorno alle REMINDER_HOUR: se IERI c'era una partita, annuncia la prossima."""
    data = context.job.data
    group_id, chat_id = data["group_id"], data["chat_id"]

    with session_scope() as s:
        group = s.get(Group, group_id)
        if group is None or not group.configured:
            return
        slots = list(group.schedule_slots or [])
        luogo = place_line(group.field_name, group.location, group.maps_url)

        # Finestra "ieri" in ora locale, convertita in UTC per il confronto.
        ieri = (to_local(now_utc()) - dt.timedelta(days=1)).date()
        start = dt.datetime.combine(ieri, dt.time.min, tzinfo=TZ).astimezone(UTC)
        end = start + dt.timedelta(days=1)
        c_ieri = (
            s.execute(
                select(Match).where(
                    Match.group_id == group_id,
                    Match.kickoff_at >= start,
                    Match.kickoff_at < end,
                    Match.state.in_(
                        [
                            MatchState.COMPLETE,
                            MatchState.PLAYED,
                            MatchState.FINISHED,
                            MatchState.OUTCOME_MISSING,
                        ]
                    ),
                )
            )
            .scalars()
            .first()
        )
        c_ieri_presente = c_ieri is not None

    if not c_ieri_presente:
        return  # ieri non si e' giocato: niente messaggio

    prossima = next_match_datetime(slots, now_utc())
    if prossima is None:
        return
    await context.bot.send_message(
        chat_id,
        f"⚽ Prossima partita: <b>{fmt_dt(prossima)}</b>.\n"
        + (luogo + "\n" if luogo else "")
        + "• /gioco per iscriverti\n"
        "• /coda se i posti sono esauriti\n"
        "• /vedicoda per la lista d'attesa",
    )


async def outcome_request(context: ContextTypes.DEFAULT_TYPE) -> None:
    """2h dopo la fine partita: chiede quale squadra ha vinto."""
    data = context.job.data
    match_id, chat_id = data["match_id"], data["chat_id"]

    with session_scope() as s:
        match = s.get(Match, match_id)
        if match is None:
            return
        if match.winning_team is not None or match.state in (
            MatchState.FINISHED,
            MatchState.CANCELLED,
            MatchState.OUTCOME_MISSING,
        ):
            return
        match.state = MatchState.PLAYED
        kickoff = match.kickoff_at

    await context.bot.send_message(
        chat_id,
        f"🏟️ La partita di <b>{fmt_dt(kickoff)}</b> è finita.\nChi ha vinto?",
        reply_markup=outcome_keyboard(match_id),
    )

    name = OUTCOME_TIMEOUT_JOB.format(match_id)
    _remove_jobs(context.job_queue, name)
    context.job_queue.run_once(
        outcome_timeout,
        when=now_utc() + dt.timedelta(hours=OUTCOME_TIMEOUT_HOURS),
        data={"match_id": match_id, "chat_id": chat_id},
        name=name,
    )


async def outcome_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Se nessuno risponde: UN solo promemoria, poi 'esito non registrato'."""
    data = context.job.data
    match_id, chat_id = data["match_id"], data["chat_id"]

    invia_promemoria = False
    rinuncia = False
    kickoff = None

    with session_scope() as s:
        match = s.get(Match, match_id)
        if match is None:
            return
        if match.winning_team is not None or match.state != MatchState.PLAYED:
            return  # nel frattempo qualcuno ha risposto
        kickoff = match.kickoff_at
        if not match.outcome_reminded:
            match.outcome_reminded = True
            invia_promemoria = True
        else:
            match.state = MatchState.OUTCOME_MISSING
            rinuncia = True

    if invia_promemoria:
        await context.bot.send_message(
            chat_id,
            f"⏰ Nessuno ha ancora indicato l'esito della partita di {fmt_dt(kickoff)}. "
            "Usate i pulsanti qui sopra.",
        )
        name = OUTCOME_TIMEOUT_JOB.format(match_id)
        _remove_jobs(context.job_queue, name)
        context.job_queue.run_once(
            outcome_timeout,
            when=now_utc() + dt.timedelta(hours=OUTCOME_TIMEOUT_HOURS),
            data={"match_id": match_id, "chat_id": chat_id},
            name=name,
        )
    elif rinuncia:
        await context.bot.send_message(
            chat_id,
            f"La partita di {fmt_dt(kickoff)} resta senza esito registrato.",
        )


def reload_jobs(app: Application) -> None:
    """Ricostruisce tutti i job dallo stato del DB. Chiamata all'avvio."""
    jq = app.job_queue

    with session_scope() as s:
        gruppi = [
            (g.id, g.telegram_chat_id)
            for g in s.execute(
                select(Group).where(Group.configured.is_(True))
            ).scalars()
        ]
        pendenti = [
            (m.id, m.group.telegram_chat_id, m.end_at)
            for m in s.execute(
                select(Match).where(
                    Match.state.in_([MatchState.COMPLETE, MatchState.PLAYED]),
                    Match.winning_team.is_(None),
                )
            ).scalars()
        ]

    for group_id, chat_id in gruppi:
        schedule_group_reminder(jq, group_id, chat_id)
    for match_id, chat_id, end_at in pendenti:
        schedule_outcome_request(jq, match_id, chat_id, end_at)

    log.info(
        "reload_jobs: %d gruppi, %d partite in attesa di esito", len(gruppi), len(pendenti)
    )
