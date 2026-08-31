"""Bootstrap del bot: build Application, registra handler, avvia il polling."""
from __future__ import annotations

import logging

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

from .config import BOT_TOKEN, setup_logging
from .db import init_db
from .handlers import onboarding, outcome, signup, votes
from .handlers import setup as setup_handlers
from .handlers import stats as stats_handlers
from .jobs import reload_jobs

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("gioco", "Iscriviti alla prossima partita"),
    BotCommand("partita", "Recap partita: data, squadre, coda"),
    BotCommand("ritiro", "Ritirati dalla partita"),
    BotCommand("coda", "Mettiti in lista d'attesa (partita piena)"),
    BotCommand("ritirocoda", "Esci dalla lista d'attesa"),
    BotCommand("vedicoda", "Mostra la lista d'attesa"),
    BotCommand("ruolo", "Imposta o cambia il tuo ruolo in campo"),
    BotCommand("statistiche", "Classifica vittorie del gruppo"),
    BotCommand("voto", "(admin) Imposta il voto di un giocatore"),
    BotCommand("configura", "(admin) Configura il gruppo"),
    BotCommand("help", "Elenco dei comandi"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS)
    # I job non sono persistenti: li ricostruiamo dal DB a ogni avvio.
    reload_jobs(app)
    log.info("bot pronto")


async def _on_error(update: object, context) -> None:
    log.exception("errore non gestito nell'update", exc_info=context.error)


def build_app() -> Application:
    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_init(_post_init)
        .build()
    )

    # ConversationHandler di setup: registrato per primo cosi' intercetta
    # /configura e i messaggi di testo del wizard.
    app.add_handler(setup_handlers.build_conversation_handler())

    app.add_handler(CommandHandler("gioco", signup.cmd_gioco))
    app.add_handler(CommandHandler(["partita", "recap", "squadre"], signup.cmd_partita))
    app.add_handler(CommandHandler("ritiro", signup.cmd_ritiro))
    app.add_handler(CommandHandler("coda", signup.cmd_coda))
    app.add_handler(CommandHandler("ritirocoda", signup.cmd_ritirocoda))
    app.add_handler(CommandHandler("vedicoda", signup.cmd_vedicoda))
    app.add_handler(CommandHandler("ruolo", onboarding.cmd_ruolo))
    app.add_handler(CommandHandler("statistiche", stats_handlers.cmd_statistiche))
    app.add_handler(CommandHandler(["help", "start", "comandi"], stats_handlers.cmd_help))
    app.add_handler(CommandHandler("voto", votes.cmd_voto))

    app.add_handler(CallbackQueryHandler(onboarding.on_role_choice, pattern=r"^role:"))
    app.add_handler(CallbackQueryHandler(outcome.on_outcome_choice, pattern=r"^outcome:"))

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, onboarding.on_new_members)
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, onboarding.on_left_member)
    )

    app.add_error_handler(_on_error)
    return app


def main() -> None:
    setup_logging()
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN mancante: impostalo nel file .env (vedi .env.example).")

    init_db()
    app = build_app()
    log.info("avvio polling")
    # allowed_updates esplicito: servono anche i messaggi di servizio (nuovi membri).
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
