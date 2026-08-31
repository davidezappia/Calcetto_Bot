"""Comandi /statistiche e /help."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..db import session_scope
from ..services.stats import leaderboard
from ..utils import esc
from .common import get_or_create_group, group_chat_only, safe_handler

log = logging.getLogger(__name__)

HELP_TEXT = (
    "⚽ <b>CalcettoBot — comandi</b>\n\n"
    "<b>Iscrizioni</b>\n"
    "/gioco — iscriviti alla prossima partita\n"
    "/partita — recap: data, squadre aggiornate, coda\n"
    "/ritiro — ritirati dalla partita\n"
    "/coda — mettiti in lista d'attesa (solo a partita piena)\n"
    "/ritirocoda — esci dalla lista d'attesa\n"
    "/vedicoda — mostra la lista d'attesa\n"
    "/ruolo — imposta o cambia il tuo ruolo in campo\n\n"
    "<b>Statistiche</b>\n"
    "/statistiche — classifica vittorie del gruppo\n\n"
    "<b>Admin</b>\n"
    "/voto @username 7.5 — imposta il voto di un giocatore (anche in reply)\n"
    "/configura — imposta o modifica giorni, orario, numero giocatori, ecc.\n"
)


@safe_handler
async def cmd_statistiche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_chat_only(update):
        await update.effective_message.reply_text("Usa /statistiche nel gruppo del calcetto.")
        return

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        righe_utenti = leaderboard(s, group)
        if not righe_utenti:
            text = "Ancora nessun giocatore registrato."
        else:
            righe = ["🏆 <b>Classifica vittorie</b>"]
            for i, u in enumerate(righe_utenti, start=1):
                nome = esc(u.first_name or u.telegram_username or "Giocatore")
                righe.append(
                    f"{i}. {nome} — <b>{u.partite_vinte}</b> vittorie (voto {u.voto:.1f})"
                )
            text = "\n".join(righe)

    await update.effective_message.reply_text(text)


@safe_handler
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)
