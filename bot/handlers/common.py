"""Helper condivisi dagli handler: get-or-create, check admin, wrapper errori."""
from __future__ import annotations

import datetime as dt
import functools
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from ..models import Group, User
from ..utils import now_utc

log = logging.getLogger(__name__)

# Per quanto tempo fidarsi della cache del ruolo admin prima di richiamare l'API.
ADMIN_CACHE_TTL = dt.timedelta(hours=1)


def get_or_create_group(session: Session, chat_id: int) -> Group:
    group = (
        session.execute(select(Group).where(Group.telegram_chat_id == chat_id))
        .scalars()
        .first()
    )
    if group is None:
        group = Group(telegram_chat_id=chat_id, configured=False, schedule_slots=[])
        session.add(group)
        session.flush()
        log.info("nuovo gruppo %s (chat %s)", group.id, chat_id)
    return group


def get_or_create_user(session: Session, group: Group, tg_user) -> User:
    """Registra l'utente al volo se non esiste; tiene aggiornati nome e username."""
    user = (
        session.execute(
            select(User).where(
                User.group_id == group.id,
                User.telegram_user_id == tg_user.id,
            )
        )
        .scalars()
        .first()
    )
    if user is None:
        user = User(
            group_id=group.id,
            telegram_user_id=tg_user.id,
            telegram_username=tg_user.username,
            first_name=tg_user.first_name,
        )
        session.add(user)
        session.flush()
        log.info("nuovo utente %s (tg %s) nel gruppo %s", user.id, tg_user.id, group.id)
    else:
        user.telegram_username = tg_user.username
        user.first_name = tg_user.first_name
    return user


async def is_group_admin(
    context: ContextTypes.DEFAULT_TYPE, group: Group, user: User
) -> bool:
    """True se l'utente e' amministratore della chat Telegram.

    Verifica via getChatAdministrators (fonte di verita'), con cache di 1h sulla
    riga User. Se l'API fallisce si ripiega sull'ultimo valore noto.
    """
    now = now_utc()
    if user.admin_checked_at and (now - user.admin_checked_at) < ADMIN_CACHE_TTL:
        return user.is_admin_cached

    try:
        admins = await context.bot.get_chat_administrators(group.telegram_chat_id)
        is_admin = any(a.user.id == user.telegram_user_id for a in admins)
    except Exception:
        log.warning("getChatAdministrators fallita per chat %s", group.telegram_chat_id)
        return user.is_admin_cached

    user.is_admin_cached = is_admin
    user.admin_checked_at = now
    return is_admin


def group_chat_only(update: Update) -> bool:
    return bool(
        update.effective_chat
        and update.effective_chat.type in ("group", "supergroup")
    )


def safe_handler(func):
    """Ogni handler passa da qui: un input sbagliato non deve mai far crashare il bot."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception:
            log.exception("errore nell'handler %s", getattr(func, "__name__", "?"))
            try:
                if update and update.effective_chat:
                    await context.bot.send_message(
                        update.effective_chat.id,
                        "⚠️ Si è verificato un errore. Riprova tra poco.",
                    )
            except Exception:
                pass

    return wrapper
