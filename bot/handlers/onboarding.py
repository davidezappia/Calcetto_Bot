"""Ingresso del bot nel gruppo e onboarding dei nuovi utenti.

Perche' il ruolo lo chiediamo IN GRUPPO con tastiera inline e non in privato:
un bot non puo' iniziare una chat privata con un utente che non ha mai premuto
Start sul bot, e un utente appena entrato quasi certamente non l'ha fatto. Un
messaggio nel gruppo con InlineKeyboardMarkup arriva sempre; ogni pulsante e'
legato all'id dell'utente destinatario, quindi solo lui puo' rispondere.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..db import session_scope
from ..jobs import cancel_match_jobs
from ..models import Role, User
from ..services.queue import purge_user
from ..utils import mention
from .common import (
    get_or_create_group,
    get_or_create_user,
    group_chat_only,
    safe_handler,
)

log = logging.getLogger(__name__)


def _role_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧤 Portiere", callback_data=f"role:{target_user_id}:PORTIERE"),
                InlineKeyboardButton("🛡️ Difensore", callback_data=f"role:{target_user_id}:DIFENSORE"),
            ],
            [
                InlineKeyboardButton("🎯 Centrocampista", callback_data=f"role:{target_user_id}:CENTROCAMPISTA"),
                InlineKeyboardButton("⚡ Attaccante", callback_data=f"role:{target_user_id}:ATTACCANTE"),
            ],
        ]
    )


@safe_handler
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    chat_id = update.effective_chat.id
    bot_id = context.bot.id

    with session_scope() as s:
        group = get_or_create_group(s, chat_id)
        group_configured = group.configured

        bot_added = False
        nuovi_umani: list[tuple[int, str | None, str | None]] = []
        for membro in msg.new_chat_members:
            if membro.id == bot_id:
                bot_added = True
                continue
            if membro.is_bot:
                continue
            u = get_or_create_user(s, group, membro)
            if u.role is None:
                nuovi_umani.append((membro.id, membro.first_name, membro.username))

    if bot_added:
        if group_configured:
            await context.bot.send_message(
                chat_id, "⚽ Sono di nuovo qui. Usate /gioco per iscrivervi alla prossima partita."
            )
        else:
            await context.bot.send_message(
                chat_id,
                "⚽ Ciao! Organizzo io le partite di calcetto.\n"
                "1. Rendetemi <b>amministratore</b> del gruppo (mi serve per "
                "leggere gli ingressi e sapere chi sono gli admin).\n"
                "2. Un amministratore lancia /configura per impostare giorni, "
                "orario e numero di giocatori.",
            )

    for uid, first_name, username in nuovi_umani:
        await context.bot.send_message(
            chat_id,
            f"{mention(uid, first_name, username)} benvenuto/a! "
            "Scegli il tuo ruolo in campo:",
            reply_markup=_role_keyboard(uid),
        )


@safe_handler
async def cmd_ruolo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Self-service: chi era gia' nel gruppo prima del bot (o chi vuole cambiare
    ruolo) lancia /ruolo e ottiene la stessa tastiera dei nuovi arrivati.
    Telegram non permette a un bot di elencare i membri esistenti, quindi non
    c'e' modo di assegnare i ruoli in blocco: ognuno usa /ruolo una volta.
    """
    if not group_chat_only(update):
        await update.effective_message.reply_text("Usa /ruolo nel gruppo del calcetto.")
        return

    tg_user = update.effective_user
    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        user = get_or_create_user(s, group, tg_user)
        attuale = user.role.value if user.role is not None else None

    testo = (
        f"{mention(tg_user.id, tg_user.first_name, tg_user.username)}, "
        + (f"ruolo attuale: <b>{attuale}</b>. Scegline un altro:" if attuale
           else "scegli il tuo ruolo in campo:")
    )
    await update.effective_message.reply_text(testo, reply_markup=_role_keyboard(tg_user.id))


@safe_handler
async def on_role_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        _, uid_str, role_name = query.data.split(":")
        target_uid = int(uid_str)
        role = Role[role_name]
    except (ValueError, KeyError):
        await query.answer("Dato non valido.", show_alert=True)
        return

    if query.from_user.id != target_uid:
        await query.answer("Questo pulsante non è per te. 🙂", show_alert=True)
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        user = get_or_create_user(s, group, query.from_user)
        user.role = role
        log.info("utente %s: ruolo impostato a %s", user.id, role.value)

    await query.answer("Ruolo salvato!")
    await query.edit_message_text(
        f"✅ {mention(target_uid, query.from_user.first_name, query.from_user.username)}: "
        f"ruolo <b>{role.value}</b>. Voto iniziale: <b>6.0</b>."
    )


@safe_handler
async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Un utente ha lasciato il gruppo (uscita volontaria o rimozione): lo si
    elimina dal DB e da ogni partita a cui era iscritto.

    Si basa sul messaggio di servizio `left_chat_member`, sempre recapitato per
    uscite e rimozioni. Un account cancellato lato Telegram non genera evento e
    resta finche' non lo si tocca: caso raro, non gestito qui.
    """
    msg = update.effective_message
    left = msg.left_chat_member if msg else None
    if left is None:
        return
    if left.id == context.bot.id:
        log.info("bot rimosso dal gruppo %s", update.effective_chat.id)
        return

    chat_id = update.effective_chat.id
    touched: list[dict] = []
    with session_scope() as s:
        group = get_or_create_group(s, chat_id)
        user = (
            s.execute(
                select(User).where(
                    User.group_id == group.id,
                    User.telegram_user_id == left.id,
                )
            )
            .scalars()
            .first()
        )
        if user is None:
            return
        touched = purge_user(s, group, user)

    for t in touched:
        if t["promoted_mention"]:
            await context.bot.send_message(
                chat_id,
                f"{t['promoted_mention']} entra dalla coda: un giocatore ha "
                "lasciato il gruppo. ✅\n\n" + t["summary"],
            )
        elif t["reverted"]:
            await context.bot.send_message(
                chat_id,
                "Un giocatore ha lasciato il gruppo: squadre aggiornate.\n\n"
                + t["summary"],
            )
        if t["reverted"]:
            cancel_match_jobs(context.job_queue, t["match_id"])
