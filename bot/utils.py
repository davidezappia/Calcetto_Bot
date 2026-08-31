"""Funzioni di utilita' pure: date/fuso orario, parsing input, formattazione testo.

Nessuna dipendenza da telegram o dal DB: qui vive solo logica riusabile e testabile.
"""
from __future__ import annotations

import datetime as dt
import html
from urllib.parse import quote_plus

from .config import TZ

UTC = dt.timezone.utc

# Nomi dei giorni in italiano, indice 0 = lunedi' (come datetime.weekday()).
GIORNI_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]

# Lookup tollerante per il parsing: con e senza accento + abbreviazioni comuni.
GIORNI_LOOKUP: dict[str, int] = {}
for _i, _nome in enumerate(GIORNI_IT):
    GIORNI_LOOKUP[_nome] = _i
    GIORNI_LOOKUP[_nome.replace("ì", "i")] = _i
GIORNI_LOOKUP.update({"lun": 0, "mar": 1, "mer": 2, "gio": 3, "ven": 4, "sab": 5, "dom": 6})


def now_utc() -> dt.datetime:
    """Adesso, timezone-aware in UTC. Unico modo consentito di leggere l'ora corrente."""
    return dt.datetime.now(UTC)


def to_local(d: dt.datetime) -> dt.datetime:
    """Converte a Europe/Rome. Un datetime naive viene assunto UTC."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(TZ)


def fmt_dt(d: dt.datetime) -> str:
    """Formato leggibile in italiano, es. 'venerdì 05/09 alle 20:30' (ora di Roma)."""
    loc = to_local(d)
    return f"{GIORNI_IT[loc.weekday()]} {loc:%d/%m} alle {loc:%H:%M}"


def parse_day(text: str) -> int | None:
    """'lunedì' / 'lunedi' / 'lun' -> 0 ... 'domenica' -> 6. None se non riconosciuto."""
    return GIORNI_LOOKUP.get(text.strip().lower())


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """'20:30' o '20.30' -> (20, 30). None se non valido."""
    text = text.strip().replace(".", ":")
    if ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def esc(s: str | None) -> str:
    """Escape HTML per interpolare testo dinamico nei messaggi (parse_mode=HTML)."""
    return html.escape(s or "")


def mention(telegram_user_id: int, name: str | None, username: str | None) -> str:
    """Menzione Telegram cliccabile. Funziona anche senza username."""
    label = name or (f"@{username}" if username else "giocatore")
    return f'<a href="tg://user?id={telegram_user_id}">{esc(label)}</a>'


def maps_url(query: str) -> str:
    """Link di ricerca su Google Maps per una stringa di luogo/indirizzo."""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


def place_line(
    field_name: str | None,
    location: str | None,
    maps_link: str | None = None,
) -> str | None:
    """Riga '📍 campo — luogo' con link a Maps. None se non c'e' nulla da mostrare.

    Il link e' quello incollato in configurazione (`maps_link`); in mancanza si
    genera un link di ricerca da luogo o nome campo.
    """
    if not field_name and not location and not maps_link:
        return None
    testo = " — ".join(esc(x) for x in (field_name, location) if x) or "Mappa"
    href = maps_link or maps_url(location or field_name)
    return f'📍 <a href="{esc(href)}">{testo}</a>'


# --- Riepilogo squadre -------------------------------------------------------
# Accetta un oggetto "TeamsResult-like" (attributi .bianca, .colorata, .notes con
# elementi che hanno .display_name e .role); tenuto qui per non far dipendere i
# services dalla formattazione dei messaggi.

def _team_block(titolo: str, giocatori) -> str:
    righe = [f"<b>{titolo}</b> ({len(giocatori)})"]
    if not giocatori:
        righe.append(" • (nessuno)")
    for p in giocatori:
        ruolo = p.role.value if p.role is not None else "ruolo non impostato"
        righe.append(f" • {esc(p.display_name)} — {ruolo}")
    return "\n".join(righe)


def teams_summary(
    result,
    required_players: int,
    kickoff_at: dt.datetime,
    *,
    field_name: str | None = None,
    location: str | None = None,
    maps_link: str | None = None,
) -> str:
    iscritti = len(result.bianca) + len(result.colorata)
    parti = [f"📅 Partita di {fmt_dt(kickoff_at)}"]
    luogo = place_line(field_name, location, maps_link)
    if luogo:
        parti.append(luogo)
    parti += [
        "",
        "🏳️ " + _team_block("Squadra Bianca", result.bianca),
        "",
        "🎽 " + _team_block("Squadra Colorata", result.colorata),
        "",
        f"👥 <b>{iscritti}/{required_players} iscritti</b>",
    ]
    if result.notes:
        parti.append("")
        parti.extend("⚠️ " + esc(n) for n in result.notes)
    return "\n".join(parti)
