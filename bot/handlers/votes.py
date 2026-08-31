"""Comando /voto (solo admin)."""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import ContextTypes

from ..db import session_scope
from ..models import User
from ..utils import esc
from .common import (
    get_or_create_group,
    get_or_create_user,
    group_chat_only,
    is_group_admin,
    safe_handler,
)

log = logging.getLogger(__name__)

USO = (
    "Uso:\n"
    "• <code>/voto @username 7.5</code>\n"
    "• oppure rispondi a un messaggio dell'utente con <code>/voto 7.5</code>\n"
    "Valore ammesso: da 1 a 10."
)


@safe_handler
async def cmd_voto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await update.effective_message.reply_text("Usa /voto nel gruppo del calcetto.")
        return

    msg = update.effective_message
    args = context.args or []

    target_username: str | None = None
    reply_user = None
    value_str: str | None = None

    if msg.reply_to_message and len(args) >= 1:
        reply_user = msg.reply_to_message.from_user
        value_str = args[0]
    elif len(args) >= 2:
        target_username = args[0].lstrip("@")
        value_str = args[1]
    else:
        await msg.reply_text(USO)
        return

    try:
        value = float(value_str.replace(",", "."))
    except ValueError:
        await msg.reply_text("Valore non valido: serve un numero tra 1 e 10.")
        return
    if not (1.0 <= value <= 10.0):
        await msg.reply_text("Il voto deve essere compreso tra 1 e 10.")
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        invoker = get_or_create_user(s, group, update.effective_user)

        if not await is_group_admin(context, group, invoker):
            await msg.reply_text("Solo gli amministratori del gruppo possono cambiare i voti.")
            return

        if reply_user is not None:
            target = get_or_create_user(s, group, reply_user)
        else:
            target = (
                s.execute(
                    select(User).where(
                        User.group_id == group.id,
                        func.lower(User.telegram_username) == target_username.lower(),
                    )
                )
                .scalars()
                .first()
            )
            if target is None:
                await msg.reply_text(
                    f"Utente @{esc(target_username)} non trovato. Deve aver gia' "
                    "scritto nel gruppo, oppure usa il comando in reply a un suo messaggio."
                )
                return

        old = target.voto
        target.voto = value
        nome = esc(target.first_name or target.telegram_username or "giocatore")
        log.info(
            "voto utente %s: %.1f -> %.1f (admin tg %s)",
            target.id, old, value, invoker.telegram_user_id,
        )

    await msg.reply_text(f"Voto di {nome} aggiornato: {old:.1f} → {value:.1f} ✅")
