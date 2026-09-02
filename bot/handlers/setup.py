"""Configurazione guidata del gruppo (ConversationHandler).

Entry point: /configura (solo admin). Serve sia al primo setup sia a modifiche
successive. Il bot appena aggiunto NON avvia da solo il wizard: driverebbe una
conversazione a piu' passi in una chat di gruppo affollata (fragile) e non
puo' scrivere in privato a chi non ha fatto Start. Percio' quando entra si
limita a chiedere che un admin lanci /configura (vedi handlers/onboarding.py).

Passi: frequenza settimanale -> (giorno + ora) per ogni occorrenza ->
numero giocatori -> durata -> info opzionali -> riepilogo e conferma.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..config import DEFAULT_MATCH_DURATION_MIN, DEFAULT_REQUIRED_PLAYERS
from ..db import session_scope
from ..jobs import cancel_match_jobs, schedule_group_reminder
from ..models import Match, MatchState
from ..utils import GIORNI_IT, esc, parse_day, parse_hhmm
from .common import (
    get_or_create_group,
    get_or_create_user,
    group_chat_only,
    is_group_admin,
    safe_handler,
)

log = logging.getLogger(__name__)

FREQ, SLOT, REQUIRED, DURATION, EXTRA, MAPS, CONFIRM = range(7)


def _slots_text(slots: list[dict]) -> str:
    return "\n".join(
        f"  • {GIORNI_IT[s['weekday']]} {s['hour']:02d}:{s['minute']:02d}" for s in slots
    )


_CONFIRM_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Confermo", callback_data="setup:ok"),
            InlineKeyboardButton("🔁 Ricomincia", callback_data="setup:redo"),
        ]
    ]
)


async def _ask_confirm(message, setup: dict):
    """Mostra il riepilogo e la tastiera di conferma. Ritorna lo stato CONFIRM."""
    campo, luogo = setup.get("field"), setup.get("location")
    maps = setup.get("maps")
    if maps:
        maps_txt = esc(maps)
    elif campo or luogo:
        maps_txt = "(generato automaticamente)"
    else:
        maps_txt = "—"

    riepilogo = (
        "📋 <b>Riepilogo configurazione</b>\n"
        f"{_slots_text(setup['slots'])}\n"
        f"Giocatori per partita: <b>{setup['required']}</b>\n"
        f"Durata: <b>{setup['duration']} min</b>\n"
        f"Campo: {esc(campo) or '—'}\n"
        f"Luogo: {esc(luogo) or '—'}\n"
        f"Link Maps: {maps_txt}\n"
        f"Costo: {esc(setup.get('cost')) or '—'}\n"
        f"Note: {esc(setup.get('notes')) or '—'}\n\n"
        "Confermi?"
    )
    await message.reply_text(riepilogo, reply_markup=_CONFIRM_KB)
    return CONFIRM


@safe_handler
async def cmd_configura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_chat_only(update):
        await update.effective_message.reply_text("Usa /configura nel gruppo del calcetto.")
        return ConversationHandler.END

    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)
        invoker = get_or_create_user(s, group, update.effective_user)
        if not await is_group_admin(context, group, invoker):
            await update.effective_message.reply_text(
                "Solo un amministratore del gruppo può usare /configura."
            )
            return ConversationHandler.END

    context.user_data["setup"] = {"slots": []}
    await update.effective_message.reply_text(
        "🛠️ <b>Configurazione gruppo</b>\n"
        "Quante volte a settimana si gioca? (un numero, es. 1)\n"
        "Scrivi /annulla in qualsiasi momento per uscire."
    )
    return FREQ


@safe_handler
async def on_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 7):
        await update.effective_message.reply_text("Scrivi un numero da 1 a 7.")
        return FREQ
    context.user_data["setup"] = {"freq": int(txt), "slots": []}
    await update.effective_message.reply_text(
        "Occorrenza 1: scrivi <b>giorno e ora</b>, es. <code>venerdì 20:30</code>"
    )
    return SLOT


@safe_handler
async def on_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data["setup"]
    parti = (update.effective_message.text or "").strip().split()
    if len(parti) < 2:
        await update.effective_message.reply_text(
            "Formato: <code>giorno HH:MM</code>, es. <code>martedì 21:00</code>"
        )
        return SLOT

    giorno = parse_day(parti[0])
    ora = parse_hhmm(parti[1])
    if giorno is None or ora is None:
        await update.effective_message.reply_text(
            "Non ho capito. Giorno per esteso (lunedì…domenica) e ora in formato HH:MM."
        )
        return SLOT

    setup["slots"].append({"weekday": giorno, "hour": ora[0], "minute": ora[1]})
    if len(setup["slots"]) < setup["freq"]:
        await update.effective_message.reply_text(
            f"Occorrenza {len(setup['slots']) + 1}: giorno e ora?"
        )
        return SLOT

    await update.effective_message.reply_text(
        f"Quanti giocatori servono per una partita completa?\n"
        f"Scrivi un numero pari, oppure <code>-</code> per il default "
        f"({DEFAULT_REQUIRED_PLAYERS}, cioè {DEFAULT_REQUIRED_PLAYERS // 2} vs "
        f"{DEFAULT_REQUIRED_PLAYERS // 2}). Modificabile in futuro con /configura."
    )
    return REQUIRED


@safe_handler
async def on_required(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    setup = context.user_data["setup"]
    if txt == "-":
        setup["required"] = DEFAULT_REQUIRED_PLAYERS
    elif txt.isdigit() and 2 <= int(txt) <= 40 and int(txt) % 2 == 0:
        setup["required"] = int(txt)
    else:
        await update.effective_message.reply_text(
            "Serve un numero pari tra 2 e 40, oppure <code>-</code> per il default."
        )
        return REQUIRED
    await update.effective_message.reply_text(
        f"Durata di una partita in minuti? Numero, oppure <code>-</code> per il "
        f"default ({DEFAULT_MATCH_DURATION_MIN})."
    )
    return DURATION


@safe_handler
async def on_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    setup = context.user_data["setup"]
    if txt == "-":
        setup["duration"] = DEFAULT_MATCH_DURATION_MIN
    elif txt.isdigit() and 20 <= int(txt) <= 240:
        setup["duration"] = int(txt)
    else:
        await update.effective_message.reply_text(
            "Minuti tra 20 e 240, oppure <code>-</code> per il default."
        )
        return DURATION
    await update.effective_message.reply_text(
        "Info extra opzionali, su una riga sola separate da <code>|</code>:\n"
        "<code>nome campo | luogo/indirizzo | costo a persona | note</code>\n"
        "Scrivi <code>-</code> per saltare."
    )
    return EXTRA


@safe_handler
async def on_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    setup = context.user_data["setup"]

    campo = luogo = costo = note = None
    if txt != "-":
        bits = [b.strip() or None for b in txt.split("|")]
        bits += [None] * (4 - len(bits))
        campo, luogo, costo, note = bits[:4]
    setup.update(field=campo, location=luogo, cost=costo, notes=note, maps=None)

    # Il link Maps ha senso solo se c'e' un campo/luogo a cui riferirlo.
    if campo or luogo:
        await update.effective_message.reply_text(
            "Incolla il <b>link Google Maps</b> del campo (apri Maps → Condividi → "
            "Copia link).\nScrivi <code>-</code> per far generare al bot un link "
            "di ricerca dal nome del luogo."
        )
        return MAPS
    return await _ask_confirm(update.effective_message, setup)


@safe_handler
async def on_maps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    setup = context.user_data["setup"]

    if txt != "-":
        if not (txt.startswith("http://") or txt.startswith("https://")):
            await update.effective_message.reply_text(
                "Non sembra un link valido. Incolla un URL che inizia con http(s)://, "
                "oppure <code>-</code> per saltare."
            )
            return MAPS
        setup["maps"] = txt

    return await _ask_confirm(update.effective_message, setup)


@safe_handler
async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "setup:redo":
        context.user_data["setup"] = {"slots": []}
        await query.edit_message_text("Ricominciamo. Quante volte a settimana si gioca?")
        return FREQ

    setup = context.user_data.get("setup", {})
    stale_match_ids: list[int] = []
    with session_scope() as s:
        group = get_or_create_group(s, update.effective_chat.id)

        # Riconfigurazione: la partita aperta si riferisce ancora ai vecchi
        # slot e ai vecchi iscritti. La eliminiamo (i partecipanti spariscono
        # via cascade) cosi' la prossima /gioco ne apre una pulita sul nuovo
        # calendario.
        for m in s.execute(
            select(Match).where(
                Match.group_id == group.id,
                Match.state.in_([MatchState.OPEN, MatchState.COMPLETE]),
            )
        ).scalars():
            stale_match_ids.append(m.id)
            s.delete(m)

        group.schedule_slots = setup["slots"]
        group.required_players = setup["required"]
        group.match_duration_min = setup["duration"]
        group.field_name = setup.get("field")
        group.location = setup.get("location")
        group.maps_url = setup.get("maps")
        group.cost_per_person = setup.get("cost")
        group.notes = setup.get("notes")
        group.configured = True
        group_id, chat_id = group.id, group.telegram_chat_id

    for mid in stale_match_ids:
        cancel_match_jobs(context.job_queue, mid)
    schedule_group_reminder(context.job_queue, group_id, chat_id)
    context.user_data.pop("setup", None)
    log.info("gruppo %s configurato", group_id)

    await query.edit_message_text(
        "✅ <b>Configurazione salvata!</b>\n"
        f"{_slots_text(setup['slots'])}\n"
        f"Giocatori per partita: <b>{setup['required']}</b> "
        f"({setup['required'] // 2} vs {setup['required'] // 2})\n"
        f"Durata: {setup['duration']} min\n\n"
        "Usate /gioco per iscrivervi alla prossima partita."
    )
    return ConversationHandler.END


@safe_handler
async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("setup", None)
    await update.effective_message.reply_text("Configurazione annullata.")
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    text_only = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[CommandHandler("configura", cmd_configura)],
        states={
            FREQ: [MessageHandler(text_only, on_freq)],
            SLOT: [MessageHandler(text_only, on_slot)],
            REQUIRED: [MessageHandler(text_only, on_required)],
            DURATION: [MessageHandler(text_only, on_duration)],
            EXTRA: [MessageHandler(text_only, on_extra)],
            MAPS: [MessageHandler(text_only, on_maps)],
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^setup:")],
        },
        fallbacks=[CommandHandler("annulla", on_cancel)],
        per_chat=True,
        per_user=True,
        conversation_timeout=600,
        name="setup_conv",
    )
