"""Formazione automatica delle squadre.

L'algoritmo viene rieseguito DA ZERO sull'intero elenco iscritti a ogni
cambiamento (nuova iscrizione, ritiro, promozione da coda). E' deterministico:
stesso insieme di iscritti -> stesso risultato. Non si aggiorna in modo
incrementale, cosi' le squadre non "saltano" tra un ricalcolo e l'altro.

Priorita' dei vincoli (dalla piu' forte):

1. Portieri (VINCOLO RIGIDO): massimo 1 portiere per squadra.
   - 2 portieri  -> uno per squadra.
   - 1 portiere  -> assegnato alla Squadra Bianca; la Colorata resta senza
                    portiere dedicato (segnalato nel riepilogo).
   - >=3 portieri -> il 3o e successivi diventano giocatori di movimento nel
                    bilanciamento; anomalia segnalata (serve scelta manuale).
   - 0 portieri  -> segnalato.

2. Bilanciamento voto/ruolo (tutti hanno ruolo e voto): draft "a serpente"
   realizzato in modo greedy. Si ordina per voto decrescente e ogni giocatore
   va alla squadra con la SOMMA VOTI corrente piu' bassa; a parita' di somma si
   sceglie la squadra con meno giocatori dello stesso ruolo, poi quella con meno
   giocatori, poi la Bianca. Questo pareggia sia il voto medio sia la
   distribuzione dei ruoli, adattandosi quando i voti non sono uniformi (un puro
   snake fisso b,c,c,b,... non lo farebbe).

3. Fallback (a qualcuno manca il ruolo): niente bilanciamento per ruolo, si
   applica solo lo split bilanciato per numero e per somma voti (voto mancante
   trattato come 6.0). Il vincolo rigido sui portieri resta valido per i
   giocatori di cui il ruolo Portiere e' noto.

Determinismo: ogni ordinamento ha come ultimo criterio telegram_user_id, quindi
non ci sono pareggi "casuali".
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..models import Role, Team


@dataclass
class PlayerView:
    """Vista immutabile di un giocatore per l'algoritmo (nessun oggetto ORM)."""

    user_id: int
    telegram_user_id: int
    display_name: str
    username: str | None
    role: Role | None
    voto: float


@dataclass
class TeamsResult:
    bianca: list[PlayerView] = field(default_factory=list)
    colorata: list[PlayerView] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def team_of(self, user_id: int) -> Team | None:
        if any(p.user_id == user_id for p in self.bianca):
            return Team.BIANCA
        if any(p.user_id == user_id for p in self.colorata):
            return Team.COLORATA
        return None


def _sort_key(p: PlayerView):
    # voto decrescente, poi ruolo (nome), poi id Telegram -> ordine totale stabile.
    return (-p.voto, p.role.value if p.role is not None else "", p.telegram_user_id)


def form_teams(players: list[PlayerView], required_players: int) -> TeamsResult:
    notes: list[str] = []
    n = len(players)

    # Capienza per lato. Con meno iscritti del richiesto si divide comunque a
    # meta' (l'eventuale dispari va alla Bianca).
    cap_bianca = min((required_players + 1) // 2, (n + 1) // 2)
    cap_colorata = min(required_players // 2, n // 2)
    while cap_bianca + cap_colorata < n:  # non deve mai restare qualcuno fuori
        cap_bianca += 1

    keepers = sorted((p for p in players if p.role == Role.PORTIERE), key=_sort_key)
    others = [p for p in players if p.role != Role.PORTIERE]
    # `roles_complete` guarda i soli giocatori di movimento "veri": gli eventuali
    # portieri in eccesso (vedi sotto) vengono aggiunti dopo con role azzerato.
    roles_complete = all(p.role is not None for p in others)

    bianca: list[PlayerView] = []
    colorata: list[PlayerView] = []
    sum_b = 0.0
    sum_c = 0.0

    # --- Vincolo 1: portieri ------------------------------------------------
    if len(keepers) >= 1:
        bianca.append(keepers[0])
        sum_b += keepers[0].voto
    if len(keepers) >= 2:
        colorata.append(keepers[1])
        sum_c += keepers[1].voto
    if len(keepers) == 0:
        notes.append("Nessun portiere tra gli iscritti.")
    elif len(keepers) == 1:
        notes.append(
            "Solo 1 portiere: assegnato alla Squadra Bianca. "
            "La Squadra Colorata gioca senza portiere dedicato."
        )
    if len(keepers) >= 3:
        notes.append(
            f"{len(keepers)} portieri iscritti: dal 3° in poi sono trattati come "
            "giocatori di movimento (ruolo azzerato per il bilanciamento). "
            "Serve una scelta manuale."
        )
        # role=None: non contano come "secondo portiere" di nessuna squadra.
        others = others + [replace(p, role=None) for p in keepers[2:]]

    if not roles_complete:
        notes.append(
            "Ad alcuni giocatori manca il ruolo: divisione bilanciata solo per "
            "numero e voto medio."
        )

    # --- Vincoli 2/3: greedy snake draft ---------------------------------
    for p in sorted(others, key=_sort_key):
        candidates = []
        if len(bianca) < cap_bianca:
            candidates.append("b")
        if len(colorata) < cap_colorata:
            candidates.append("c")
        if not candidates:  # entrambe piene: non dovrebbe accadere (cap somma >= n)
            candidates = ["b", "c"]

        def score(team: str):
            if team == "b":
                somma, conteggio, pool = sum_b, len(bianca), bianca
            else:
                somma, conteggio, pool = sum_c, len(colorata), colorata
            same_role = (
                sum(1 for x in pool if x.role == p.role)
                if roles_complete and p.role is not None
                else 0
            )
            # L'ordine della tupla = ordine di priorita'.
            return (somma, same_role, conteggio, 0 if team == "b" else 1)

        scelta = min(candidates, key=score)
        if scelta == "b":
            bianca.append(p)
            sum_b += p.voto
        else:
            colorata.append(p)
            sum_c += p.voto

    return TeamsResult(bianca=bianca, colorata=colorata, notes=notes)


# --- self-check (nessun framework): `python -m bot.services.teams` --------
def _demo() -> None:
    def mk(uid, role, voto):
        return PlayerView(uid, 1000 + uid, f"G{uid}", None, role, voto)

    roles = [Role.DIFENSORE, Role.CENTROCAMPISTA, Role.ATTACCANTE]
    squad = [mk(0, Role.PORTIERE, 6), mk(1, Role.PORTIERE, 6)]
    squad += [mk(i, roles[i % 3], 5 + (i % 5)) for i in range(2, 10)]

    r1 = form_teams(squad, 10)
    r2 = form_teams(list(reversed(squad)), 10)  # stesso insieme, ordine diverso
    assert [p.user_id for p in r1.bianca] == [p.user_id for p in r2.bianca], "non deterministico"
    assert len(r1.bianca) == 5 and len(r1.colorata) == 5, "squadre non 5+5"
    gk_b = [p for p in r1.bianca if p.role == Role.PORTIERE]
    gk_c = [p for p in r1.colorata if p.role == Role.PORTIERE]
    assert len(gk_b) == 1 and len(gk_c) == 1, "vincolo portieri violato"

    one_gk = [mk(0, Role.PORTIERE, 6)] + [mk(i, roles[i % 3], 6) for i in range(1, 10)]
    r3 = form_teams(one_gk, 10)
    assert any("senza portiere dedicato" in note for note in r3.notes), "manca la nota 1-portiere"

    three_gk = [mk(i, Role.PORTIERE, 6) for i in range(3)] + [mk(i, roles[i % 3], 6) for i in range(3, 10)]
    r4 = form_teams(three_gk, 10)
    assert sum(1 for p in r4.bianca if p.role == Role.PORTIERE) <= 1
    assert sum(1 for p in r4.colorata if p.role == Role.PORTIERE) <= 1
    assert any("scelta manuale" in note for note in r4.notes)

    print("teams._demo OK")


if __name__ == "__main__":
    _demo()
