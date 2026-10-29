"""
App tornei: collega motore_torneo al database del bot e serve la Mini App
Telegram (scrittura) e la pagina TV (sola lettura) dallo stesso processo.

ARCHITETTURA
- Un solo processo: bot (polling) + piccolo server web aiohttp sulla porta
  PORT. Stesso database SQLite del bot (DB_PATH sul volume Railway).
- Stato del torneo = dizionario del motore, salvato in JSON nella tabella
  app_tornei (una riga per torneo, legata a tornei.id). Ogni salvataggio
  incrementa 'versione': Mini App e TV si aggiornano solo se cambia.
- Ogni azione lavora su una copia: se il motore rifiuta, nulla viene salvato.

ACCESSO
- Mini App: Telegram firma i dati dell'utente (initData); il server verifica
  la firma col token del bot. Admin = ADMIN_IDS del bot.
- Coppia = chi l'ha iscritta con /iscrivi (squadre.user_id) + gli account
  collegati ai due giocatori con /sonoio (vale per tutti i tornei futuri).
- TV: link con token segreto (/tv), sola lettura, nessun login.

VARIABILI D'AMBIENTE
- PORT        porta del server web (Railway la imposta da sola), default 8080
- PUBLIC_URL  indirizzo pubblico https (se manca usa RAILWAY_PUBLIC_DOMAIN)

COMANDI
- /app  -> apre la Mini App (in privato bottone diretto; nel gruppo rimanda
           alla chat privata col bot)
- /sonoio Nome  -> collega il proprio account al giocatore Nome
- /scollega Nome -> (admin) toglie il collegamento di un giocatore
- /provatorneo N -> (admin, in privato) torneo finto di N coppie per provare
                    l'app, isolato dal torneo vero; /fineprova lo chiude
- /avvisabili -> (admin) coppie raggiungibili in privato alle chiamate
- /tv   -> (admin, in privato) link della pagina TV; "/tv nuovo" lo rigenera
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import random
import secrets
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import CommandHandler, ContextTypes

import motore_torneo as M
import statistiche

CARTELLA_WEB = Path(__file__).parent / "webapp"
CARTELLA_ASSETS = Path(__file__).parent / "assets"     # font della locandina
PORTA = int(os.environ.get("PORT", "8080"))
VALIDITA_INIT_DATA = 24 * 3600          # secondi
# bonus Elo per medaglie dell'anno in corso (oro, argento, bronzo), solo per
# le teste di serie. Vale fino a BONUS_FINO_AL: poi le partite dei tornei
# entrano nell'Elo e il bonus conterebbe due volte le stesse vittorie.
BONUS_MEDAGLIE = (20, 10, 5)
BONUS_FINO_AL = 2026
PROVE_STIMA = 8                          # tornei simulati per la stima durata
CHIAVE_TV = "app_tv_token"
# "Da chiamare a voce" in privato all'admin: si cancella da solo dopo questi minuti
CANCELLA_DA_CHIAMARE_MIN = 15
# Modalità prova: torneo finto con id negativo (mai presente nella tabella
# tornei: statistiche, medagliere, Elo, storico e iscrizioni non lo vedono).
ID_PROVA = -1
CHIAVE_PROVA = "app_prova"            # valore = uid admin che l'ha avviata
CHIAVE_PROVA_DAL = "app_prova_dal"    # ISO: scade da sola dopo DURATA_PROVA
DURATA_PROVA_ORE = 12
NOMI_PROVA = ["Alfa", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel",
              "India", "Juliett", "Kilo", "Lima", "Mike", "November", "Oscar", "Papa",
              "Quebec", "Romeo", "Sierra", "Tango", "Uniform", "Victor", "Whiskey", "Xray",
              "Yankee", "Zulu", "Ares", "Bora", "Cielo", "Duna", "Etna", "Faro", "Gelo",
              "Iris", "Luna", "Neve"]

AZIONI_ADMIN = {"prepara", "anteprima_gironi", "crea_gironi", "imposta", "formato",
                "avvia", "ferma", "chiama", "risultato", "ritira", "crea_tabellone",
                "ricomincia", "sospendi", "riattiva", "assente", "simula",
                "annulla_gironi", "annulla_tabellone"}
AZIONI_COPPIA = {"proponi", "conferma", "contesta"}

_db_connect = None
_is_admin = None
_rispondi = None
_cancella_comando = None
_token = None
_admin_ids: set[int] = set()


def url_pubblico() -> str | None:
    url = os.environ.get("PUBLIC_URL") or ""
    if not url and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        url = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    return url.rstrip("/") or None


# ======================= DATABASE =======================

def crea_tabelle(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_tornei (
            torneo_id INTEGER PRIMARY KEY,
            stato TEXT NOT NULL,
            proposte TEXT NOT NULL DEFAULT '{}',
            utenti TEXT NOT NULL DEFAULT '{}',
            versione INTEGER NOT NULL DEFAULT 1,
            aggiornato_il TEXT
        )
    """)
    # avvisi privati da cancellare quando non servono più (vedi pulisci_messaggi)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_messaggi (
            torneo_id INTEGER, pid TEXT, tipo TEXT,      -- chiamata | risultato
            chat_id INTEGER, message_id INTEGER, creato_il TEXT
        )
    """)
    conn.commit()


def _conn():
    conn = _db_connect()
    statistiche.crea_tabelle(conn)
    crea_tabelle(conn)
    return conn


def torneo_attivo(conn):
    return conn.execute(
        "SELECT id, nome FROM tornei WHERE chiuso = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def prova_attiva(conn) -> int | None:
    """uid dell'admin se la modalità prova è attiva (e non scaduta)."""
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_PROVA,)).fetchone()
    if not row:
        return None
    dal = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_PROVA_DAL,)).fetchone()
    if dal and (datetime.now() - datetime.fromisoformat(dal[0])).total_seconds() > DURATA_PROVA_ORE * 3600:
        chiudi_prova(conn)
        return None
    return int(row[0])


def avvia_prova(conn, uid: int, n: int) -> None:
    conn.execute("DELETE FROM app_tornei WHERE torneo_id = ?", (ID_PROVA,))
    for k, v in ((CHIAVE_PROVA, str(uid)), (CHIAVE_PROVA_DAL, datetime.now().isoformat())):
        conn.execute("INSERT INTO stato (chiave, valore) VALUES (?, ?) "
                     "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore", (k, v))
    nomi = NOMI_PROVA[:]
    random.shuffle(nomi)
    coppie = [f"{nomi[2 * i]}-{nomi[2 * i + 1]}" for i in range(n)]
    t = M.nuovo_torneo(coppie)
    # l'admin è membro della prima coppia: così vede anche la vista giocatore
    s = {"torneo_id": ID_PROVA, "t": t, "proposte": {}, "utenti": {coppie[0]: uid}, "versione": 0}
    salva(conn, s)


def chiudi_prova(conn) -> bool:
    n = conn.execute("DELETE FROM app_tornei WHERE torneo_id = ?", (ID_PROVA,)).rowcount
    n += conn.execute("DELETE FROM stato WHERE chiave IN (?, ?)", (CHIAVE_PROVA, CHIAVE_PROVA_DAL)).rowcount
    conn.commit()
    return bool(n)


def carica(conn) -> dict:
    """Stato dell'app per il torneo attivo (o per la prova, se attiva).
    't' è None se non preparato."""
    prova = prova_attiva(conn)
    if prova is not None:
        tid, nome = ID_PROVA, "🧪 PROVA"
    else:
        tid, nome = torneo_attivo(conn) or (None, None)
    s = {"torneo_id": tid, "nome": nome, "t": None, "proposte": {}, "utenti": {}, "versione": 0,
         "prova": prova}
    if tid is None:
        return s
    row = conn.execute(
        "SELECT stato, proposte, utenti, versione FROM app_tornei WHERE torneo_id = ?", (tid,)
    ).fetchone()
    if row:
        s["t"] = json.loads(row[0])
        s["proposte"] = json.loads(row[1])
        s["utenti"] = json.loads(row[2])
        s["versione"] = row[3]
    return s


def salva(conn, s: dict) -> None:
    s["versione"] += 1
    conn.execute(
        "INSERT INTO app_tornei (torneo_id, stato, proposte, utenti, versione, aggiornato_il) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(torneo_id) DO UPDATE SET "
        "stato = excluded.stato, proposte = excluded.proposte, utenti = excluded.utenti, "
        "versione = excluded.versione, aggiornato_il = excluded.aggiornato_il",
        (s["torneo_id"], json.dumps(s["t"]), json.dumps(s["proposte"]),
         json.dumps(s["utenti"]), s["versione"], datetime.now().isoformat()),
    )
    conn.commit()


def squadre_iscritte(conn, torneo_id: int) -> list[tuple[str, int]]:
    return conn.execute(
        "SELECT nome_squadra, user_id FROM squadre WHERE torneo_id = ? ORDER BY iscritto_il",
        (torneo_id,),
    ).fetchall()


def mappa_utenti(conn, s: dict) -> dict[str, set[int]]:
    """Coppia -> account Telegram che la gestiscono: chi l'ha iscritta +
    account collegati ai due giocatori con /sonoio. Un admin che ha iscritto
    coppie altrui non ne diventa 'membro' (gestisce già tutto da admin e non
    deve ricevere gli avvisi di ogni chiamata)."""
    if s.get("prova") is not None:   # niente account veri: solo l'admin nella sua coppia
        return {c: ({s["utenti"][c]} if c in s["utenti"] else set()) for c in s["t"]["coppie"]}
    collegati = dict(conn.execute("SELECT giocatore_id, user_id FROM giocatori_telegram").fetchall())
    coppie = s["t"]["coppie"] if s["t"] else [n for n, _ in squadre_iscritte(conn, s["torneo_id"])]
    mappa = {}
    for c in coppie:
        isc = s["utenti"].get(c)
        utenti = {isc} if isc is not None and not _is_admin(isc) else set()
        for g in statistiche.dividi_coppia(c) or []:
            row = statistiche.cerca_giocatore(conn, g)
            if row and row[0] in collegati:
                utenti.add(collegati[row[0]])
        mappa[c] = utenti
    return mappa


def _versione_globale(conn, s: dict) -> str:
    """Cambia a ogni salvataggio, al cambio di torneo e, prima che l'app sia
    preparata, a ogni nuova iscrizione."""
    link = conn.execute("SELECT COUNT(*), MAX(collegato_il) FROM giocatori_telegram").fetchone()
    if s["t"] is not None:
        return f'{s["torneo_id"]}:{s["versione"]}:{link[0]}{link[1]}'
    n = conn.execute("SELECT COUNT(*), MAX(id) FROM squadre WHERE torneo_id = ?",
                     (s["torneo_id"],)).fetchone() if s["torneo_id"] else (0, 0)
    return f'{s["torneo_id"]}:i{n[0]}-{n[1]}'


# ======================= TESTE DI SERIE E STIMA =======================

def forza_coppie(conn, coppie: list[str], prova: bool = False) -> dict[str, float]:
    """Media dei due giocatori: Elo sfidine anno in corso (1000 se assente)
    + bonus medaglie dell'anno in corso."""
    if prova:
        rng = random.Random(",".join(coppie))
        return {c: round(rng.gauss(1000, 80)) for c in coppie}
    anno = date.today().year
    elo = {r["nome"].lower(): r["elo"] for r in statistiche.calcola_classifica(conn, anno)}
    med = statistiche.calcola_medagliere(conn, anno)[0] if anno <= BONUS_FINO_AL else []
    bonus = {n.lower(): o * BONUS_MEDAGLIE[0] + a * BONUS_MEDAGLIE[1] + b * BONUS_MEDAGLIE[2]
             for n, (o, a, b) in med}

    def giocatore(n):
        k = statistiche.nome_canonico(conn, n).lower()
        return elo.get(k, statistiche.ELO_BASE) + bonus.get(k, 0)

    forza = {}
    for c in coppie:
        parti = statistiche.dividi_coppia(c) or [c]
        forza[c] = sum(giocatore(p) for p in parti) / len(parti)
    return forza


def _simula(t: dict, forza: dict, rng: random.Random) -> int:
    """Gioca un torneo già con i gironi creati; ritorna i minuti totali."""
    import heapq
    ora, coda = 0, []
    durata = t["config"]["minuti_partita"]
    while t["fase"] != "finito":
        if t["fase"] == "gironi" and not coda and all(
                p["stato"] == "finita" for p in t["partite"].values() if p["tab"] == "G"):
            M.crea_tabellone(t)
            continue
        for p in M.chiama_partite(t):
            r = p["regole"]
            giochi, va, vb, serve = [], 0, 0, r["al_meglio_di"] // 2 + 1
            fa, fb = forza.get(p["a"], 1000), forza.get(p["b"], 1000)
            while va < serve and vb < serve:
                a, b = r["vantaggio_a"], 0
                while a < r["gol"] and b < r["gol"]:
                    if rng.random() < fa / (fa + fb):
                        a += 1
                    else:
                        b += 1
                giochi.append((a, b))
                va += a > b
                vb += b > a
            heapq.heappush(coda, (ora + durata * len(giochi), p["id"], giochi))
        if not coda:
            raise RuntimeError("Stallo nella simulazione")
        ora, pid, giochi = heapq.heappop(coda)
        M.registra(t, pid, giochi)
    return ora


def stima_minuti(t: dict, n_gironi: int, forza: dict) -> int:
    tot = 0
    for s in range(PROVE_STIMA):
        prova = copy.deepcopy(t)
        prova["seme"] = s
        prova["partite"], prova["gironi"], prova["fase"] = {}, [], "iscrizioni"
        M.crea_gironi(prova, n_gironi, "teste", forza)
        tot += _simula(prova, forza, random.Random(s))
    return round(tot / PROVE_STIMA)


# ======================= VISTA (JSON per Mini App e TV) =======================

NOMI_GIRONI = "ABCDEFGHIJ"


def etichetta(p: dict, t: dict) -> str:
    if p["tab"] == "G":
        return f"Girone {NOMI_GIRONI[p['girone']]} · giornata {p['turno']}"
    if p["tab"] == "F":
        return "Finale"
    turni_w = max(q["turno"] for q in t["partite"].values() if q["tab"] == "W")
    turni_l = max((q["turno"] for q in t["partite"].values() if q["tab"] == "L"), default=0)
    if p["tab"] == "W":
        return "Finale vincenti" if p["turno"] == turni_w else f"Vincenti · turno {p['turno']}"
    return "Finale perdenti" if p["turno"] == turni_l else f"Perdenti · turno {p['turno']}"


def vista(s: dict, uid: int | None = None) -> dict:
    t = s["t"]
    out = {"torneo": s["nome"], "torneo_id": s["torneo_id"], "versione": s["versione"],
           "fase": t["fase"] if t else "nessuna", "url_tv": None, "prova": s.get("prova") is not None}
    if uid is not None:
        out["ruolo"] = {"admin": bool(_is_admin(uid)),
                        "coppie": sorted(c for c, u in s.get("accessi", {}).items() if uid in u)}
    if t is None:
        return out
    out.update({
        "config": t["config"], "coppie": t["coppie"], "ritirate": t["ritirate"],
        "podio": t["podio"], "seme": t["seme"],
        "sospese": t.get("sospese", []), "assenti": t.get("assenti", {}),
        "giocatori": {c: statistiche.dividi_coppia(c) or [c] for c in t["coppie"]},
    })
    out["gironi"] = [
        {"nome": f"Girone {NOMI_GIRONI[gi]}", "classifica": M.classifica_girone(t, gi)}
        for gi in range(len(t["gironi"]))
    ]
    partite = []
    for p in sorted(t["partite"].values(), key=lambda x: x["ordine"]):
        regole = p["regole"] or {
            "gol": t["config"]["gol"], "al_meglio_di": M._al_meglio_di(t, p),
            "vantaggio_a": t["config"]["vantaggio_finale"] if p["tab"] == "F" else 0}
        partite.append({
            "id": p["id"], "tab": p["tab"], "turno": p["turno"], "girone": p["girone"],
            "chiave_turno": M._chiave_turno(p), "etichetta": etichetta(p, t),
            "a": p["a"], "b": p["b"], "stato": p["stato"], "biliardino": p["biliardino"],
            "giochi": p["giochi"], "vincitore": p["vincitore"], "tavolino": p["tavolino"],
            "bye": M.BYE in (p["a"], p["b"]), "regole": regole, "finita_il": p.get("finita_il"),
            "proposta": s["proposte"].get(p["id"]),
        })
    out["partite"] = partite
    out["pronte"] = [p["id"] for p in M.partite_pronte(t)]
    occupati = {p["biliardino"] for p in t["partite"].values() if p["stato"] == "in_corso"}
    out["biliardini_liberi"] = [b for b in range(1, t["config"]["biliardini"] + 1) if b not in occupati]
    if t["fase"] in ("ko", "finito"):
        chiavi = []
        for p in sorted(t["partite"].values(), key=lambda x: x["ordine"]):
            k = M._chiave_turno(p)
            # solo turni con partite vere non ancora iniziate (i finiti non si cambiano)
            if p["tab"] != "G" and k not in chiavi and p["stato"] == "attesa" \
                    and M.BYE not in (p["a"], p["b"]):
                chiavi.append(k)
        out["formati"] = {k: M._al_meglio_di(t, next(
            p for p in t["partite"].values() if M._chiave_turno(p) == k)) for k in chiavi}
    return out


# ======================= AZIONI =======================

class Vietato(Exception):
    pass


def _giochi(d) -> list[tuple[int, int]]:
    try:
        g = [(int(a), int(b)) for a, b in d["giochi"]]
    except (KeyError, TypeError, ValueError):
        raise ValueError("Punteggio non leggibile")
    if not g:
        raise ValueError("Inserisci almeno una partita")
    return g


def _coppia_di(s: dict, uid: int, p: dict) -> str | None:
    for lato in ("a", "b"):
        if p[lato] and uid in s["accessi"].get(p[lato], ()):
            return p[lato]
    return None


def esegui(conn, uid: int, d: dict) -> dict:
    """Esegue un'azione. Ritorna {'ok':..., 'avvisi': [(user_id, testo)], ...}.
    ValueError = errore di regolamento/dati; Vietato = permesso negato."""
    az = d.get("azione")
    admin = bool(_is_admin(uid))
    if az in AZIONI_ADMIN and not admin:
        raise Vietato("Azione riservata agli admin")
    if az not in AZIONI_ADMIN | AZIONI_COPPIA:
        raise ValueError("Azione sconosciuta")
    s = carica(conn)
    if s["torneo_id"] is None:
        raise ValueError("Nessun torneo attivo")
    prova = s.get("prova") is not None
    if az == "simula" and not prova:
        raise ValueError("Simulazione disponibile solo in modalità prova")
    if az == "prepara" and prova:
        raise ValueError("In prova le coppie sono già caricate")
    s["accessi"] = mappa_utenti(conn, s) if s["t"] is not None or not prova else {}
    if az == "prepara":
        return _prepara(conn, s)
    if s["t"] is None:
        raise ValueError("Torneo non ancora preparato nell'app")

    t = copy.deepcopy(s["t"])
    proposte = copy.deepcopy(s["proposte"])
    risposta = {"ok": True, "avvisi": []}

    if az == "ricomincia":
        if d.get("conferma") != "RICOMINCIA":
            raise ValueError("Conferma mancante")
        if prova:   # stesse coppie finte, si riparte da zero
            s["t"], s["proposte"] = M.nuovo_torneo(t["coppie"]), {}
            salva(conn, s)
            return risposta
        conn.execute("DELETE FROM app_tornei WHERE torneo_id = ?", (s["torneo_id"],))
        if t["fase"] != "iscrizioni":   # i gironi le avevano chiuse: si riaprono
            conn.execute("INSERT INTO stato (chiave, valore) VALUES ('aperte', '1') "
                         "ON CONFLICT(chiave) DO UPDATE SET valore = '1'")
        conn.commit()
        return risposta

    if az in ("anteprima_gironi", "crea_gironi"):
        if t["fase"] != "iscrizioni":
            raise ValueError("Gironi già creati")
        n, metodo = int(d.get("n_gironi", 0)), d.get("metodo", "teste")
        if d.get("rimescola"):
            t["seme"] = random.randrange(10**9)
        forza = forza_coppie(conn, t["coppie"], s.get("prova") is not None)
        manuale = d.get("manuale")
        M.crea_gironi(t, n, metodo, forza, manuale)
        if az == "anteprima_gironi":
            risposta["gironi"] = t["gironi"]
            risposta["forza"] = {c: round(f) for c, f in forza.items()}
            risposta["minuti"] = stima_minuti(s["t"], n, forza)
            if d.get("rimescola"):                 # il nuovo sorteggio resta valido
                s["t"]["seme"] = t["seme"]
                salva(conn, s)
            return risposta
        if not prova:   # chiude le iscrizioni del torneo vero
            conn.execute("INSERT INTO stato (chiave, valore) VALUES ('aperte', '0') "
                         "ON CONFLICT(chiave) DO UPDATE SET valore = '0'")

    elif az == "simula":
        _simula_prova(t, proposte, s)

    elif az == "annulla_tabellone":
        M.annulla_tabellone(t)
        proposte = {k: v for k, v in proposte.items() if k in t["partite"]}

    elif az == "annulla_gironi":
        # torna alla composizione: stesse coppie, impostazioni e sorteggio;
        # gironi e risultati azzerati. Le iscrizioni restano come sono.
        if t["fase"] != "gironi":
            raise ValueError("Si può tornare indietro solo durante i gironi (dopo, usa Ricomincia)")
        nuovo = M.nuovo_torneo(t["coppie"])
        nuovo["config"], nuovo["seme"] = t["config"], t["seme"]
        t, proposte = nuovo, {}

    elif az == "imposta":
        valori = {k: int(v) for k, v in (d.get("valori") or {}).items()}
        M.imposta(t, **valori)

    elif az == "formato":
        M.imposta_formato(t, d["turno"], int(d["al_meglio_di"]))

    elif az == "avvia":
        p = t["partite"][d["pid"]]
        b = int(d["biliardino"])
        occupati = {q["biliardino"] for q in t["partite"].values() if q["stato"] == "in_corso"}
        if not 1 <= b <= t["config"]["biliardini"] or b in occupati:
            raise ValueError(f"Biliardino {b} non disponibile")
        impegnate = {c for q in t["partite"].values() if q["stato"] == "in_corso" for c in (q["a"], q["b"])}
        if {p["a"], p["b"]} & impegnate:
            raise ValueError("Una delle due coppie sta già giocando")
        M.avvia(t, p["id"], b)
        risposta["avvisi"] += _avvisi_chiamata(s, [p])

    elif az == "ferma":
        p = t["partite"][d["pid"]]
        if p["stato"] != "in_corso":
            raise ValueError("La partita non è in corso")
        p["stato"], p["biliardino"], p["regole"] = "attesa", None, None

    elif az in ("sospendi", "riattiva", "assente"):
        if t["fase"] not in ("gironi", "ko"):
            raise ValueError("Torneo non in corso")
        if az == "sospendi":
            libero = M.sospendi(t, d["pid"])
        elif az == "riattiva":
            M.riattiva(t, d["pid"])
            libero = None
        else:
            libero = M.segna_assente(t, d["coppia"], d["giocatore"], bool(d.get("assente", True)))
        if libero:   # il campo liberato passa subito alla prossima partita pronta
            pronte = M.partite_pronte(t)
            if pronte:
                M.avvia(t, pronte[0]["id"], libero)
                risposta["chiamate"] = [pronte[0]["id"]]
                risposta["avvisi"] += _avvisi_chiamata(s, [pronte[0]])

    elif az == "chiama":
        chiamate = M.chiama_partite(t)
        if not chiamate:
            raise ValueError("Nessuna partita da chiamare (biliardini occupati o coppie impegnate)")
        risposta["chiamate"] = [p["id"] for p in chiamate]
        risposta["avvisi"] += _avvisi_chiamata(s, chiamate)

    elif az == "risultato":
        M.registra(t, d["pid"], _giochi(d))
        proposte.pop(d["pid"], None)

    elif az == "ritira":
        if d.get("coppia") not in t["coppie"]:
            raise ValueError("Coppia sconosciuta")
        if t["fase"] == "iscrizioni":
            raise ValueError("Prima dei gironi basta togliere la coppia con /rimuovi e ricaricare")
        M.ritira(t, d["coppia"])

    elif az == "crea_tabellone":
        M.crea_tabellone(t)

    elif az in AZIONI_COPPIA:
        p = t["partite"].get(d.get("pid"))
        if p is None:
            raise ValueError("Partita sconosciuta")
        mia = _coppia_di(s, uid, p)
        if mia is None:
            raise Vietato("Puoi gestire solo le partite della tua coppia")
        if p["stato"] == "finita":
            raise ValueError("Risultato già registrato: per correggerlo chiedi all'admin")
        if p["a"] is None or p["b"] is None:
            raise ValueError("Partita non ancora definita")
        altra = p["b"] if mia == p["a"] else p["a"]
        prop = proposte.get(p["id"])

        if az == "proponi":
            giochi = _giochi(d)
            prova = copy.deepcopy(t)
            M.registra(prova, p["id"], giochi)          # solo validazione
            if prop and prop["da"] == altra and [list(g) for g in giochi] == prop["giochi"]:
                M.registra(t, p["id"], giochi)          # stesso risultato: confermato
                proposte.pop(p["id"])
            elif prop and prop["da"] == altra:
                prop["contestata"] = True
                prop["controproposta"] = [list(g) for g in giochi]
                risposta["avvisi"] += _avvisi_admin(s, p, "risultati diversi dalle due coppie")
            else:
                proposte[p["id"]] = {"giochi": [list(g) for g in giochi], "da": mia,
                                     "contestata": False, "quando": datetime.now().isoformat()}
                for u in s["accessi"].get(altra, ()):
                    if u != uid:
                        risposta["avvisi"].append((u, f"🎱 {mia} ha inserito il risultato contro di voi: "
                                                      f"{_testo_giochi(giochi)}. Confermalo nell'app.",
                                                   {"pid": p["id"], "tipo": "risultato"}))
        elif az == "conferma":
            if not prop or prop["da"] != altra:
                raise ValueError("Nessun risultato dell'altra coppia da confermare")
            M.registra(t, p["id"], [tuple(g) for g in prop["giochi"]])
            proposte.pop(p["id"])
        elif az == "contesta":
            if not prop or prop["da"] != altra:
                raise ValueError("Nessun risultato dell'altra coppia da contestare")
            prop["contestata"] = True
            risposta["avvisi"] += _avvisi_admin(s, p, f"{mia} contesta il risultato")

    # ora di fine (per "ultimi risultati" sulla TV)
    adesso = datetime.now().isoformat(timespec="seconds")
    for pid, p in t["partite"].items():
        prima = s["t"]["partite"].get(pid)
        if p["stato"] == "finita" and (prima is None or prima["stato"] != "finita" or prima["giochi"] != p["giochi"]):
            p["finita_il"] = adesso
        elif p["stato"] != "finita":
            p.pop("finita_il", None)
    # risultati registrati: via le proposte rimaste su partite finite
    for pid in [k for k in proposte if t["partite"][k]["stato"] == "finita"]:
        proposte.pop(pid)
    s["t"], s["proposte"] = t, proposte
    salva(conn, s)
    return risposta


def _simula_prova(t: dict, proposte: dict, s: dict) -> None:
    """Solo prova: chiama le partite e mette punteggi casuali a quelle in campo.
    La partita della coppia dell'admin riceve una proposta dell'avversario (da
    confermare dalla vista giocatore); una su tre delle altre resta proposta
    (da confermare come admin); le altre sono registrate."""
    if t["fase"] not in ("gironi", "ko"):
        raise ValueError("Crea prima i gironi (o il tabellone)")
    M.chiama_partite(t)
    in_campo = sorted((p for p in t["partite"].values() if p["stato"] == "in_corso"),
                      key=lambda p: p["ordine"])
    if not in_campo:
        raise ValueError("Nessuna partita da giocare: crea il tabellone se i gironi sono finiti")
    mie = set(s["utenti"])
    rng = random.Random()
    for i, p in enumerate(in_campo):
        if p["id"] in proposte:
            continue
        a_giochi = []
        regole = p["regole"] or {"gol": t["config"]["gol"], "al_meglio_di": M._al_meglio_di(t, p),
                                 "vantaggio_a": 0}
        gol, bo, vant = regole["gol"], regole["al_meglio_di"], regole.get("vantaggio_a", 0)
        va = vb = 0
        while max(va, vb) < bo // 2 + 1:
            if rng.random() < .5:
                a_giochi.append((gol, rng.randint(0, gol - 1))); va += 1
            else:
                a_giochi.append((rng.randint(vant, gol - 1), gol)); vb += 1
        if {p["a"], p["b"]} & mie:
            da = p["b"] if p["a"] in mie else p["a"]
        elif i % 3 == 0:
            da = p["a"]
        else:
            M.registra(t, p["id"], a_giochi)
            continue
        prova_t = copy.deepcopy(t)
        M.registra(prova_t, p["id"], a_giochi)        # solo validazione
        proposte[p["id"]] = {"giochi": [list(g) for g in a_giochi], "da": da,
                             "contestata": False, "quando": datetime.now().isoformat()}


def _prepara(conn, s: dict) -> dict:
    """Crea (o ricarica, finché i gironi non ci sono) le coppie dal torneo attivo."""
    if s["t"] is not None and s["t"]["fase"] != "iscrizioni":
        raise ValueError("Gironi già creati: per ripartire usa 'Ricomincia'")
    righe = squadre_iscritte(conn, s["torneo_id"])
    coppie = [n for n, _ in righe]
    vecchio = s["t"]
    t = M.nuovo_torneo(coppie)
    if vecchio:                                     # mantiene impostazioni e sorteggio
        t["config"], t["seme"] = vecchio["config"], vecchio["seme"]
    s["t"], s["proposte"] = t, {}
    s["utenti"] = {n: u for n, u in righe}
    for n in coppie:
        statistiche.registra_coppia(conn, n)
    salva(conn, s)
    return {"ok": True, "avvisi": []}


def _testo_giochi(giochi) -> str:
    return ", ".join(f"{a}-{b}" for a, b in giochi)


def _avvisi_chiamata(s, partite) -> list:
    """Avvisi (user_id, testo, meta). meta["chiamata"] = (coppia, avversario,
    biliardino): serve a h_azione per dire all'admin chi non è stato avvisato.
    Una coppia senza account collegati compare con user_id None."""
    avvisi = []
    for p in partite:
        for c, avv in ((p["a"], p["b"]), (p["b"], p["a"])):
            if c == M.BYE or avv == M.BYE:
                continue
            meta = {"chiamata": (c, avv, p["biliardino"]), "pid": p["id"], "tipo": "chiamata"}
            utenti = s["accessi"].get(c, ())
            if not utenti:
                avvisi.append((None, None, meta))
            for u in utenti:
                avvisi.append((u, f"🎱 Tocca a voi! {c} contro {avv} — biliardino {p['biliardino']}", meta))
    return avvisi


def _avvisi_admin(s, p, motivo) -> list:
    return [(a, f"⚠️ {p['a']} – {p['b']}: {motivo}. Decidi tu dall'app.",
             {"pid": p["id"], "tipo": "risultato"}) for a in _admin_ids]


# ======================= AUTENTICAZIONE TELEGRAM =======================

def verifica_init_data(init_data: str, token: str) -> dict | None:
    """Verifica la firma dei dati che Telegram passa alla Mini App.
    Ritorna l'utente (dict) o None."""
    try:
        campi = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    firma = campi.pop("hash", None)
    if not firma:
        return None
    controllo = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()))
    chiave = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    atteso = hmac.new(chiave, controllo.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(atteso, firma):
        return None
    if time.time() - int(campi.get("auth_date", 0)) > VALIDITA_INIT_DATA:
        return None
    try:
        return json.loads(campi["user"])
    except (KeyError, ValueError):
        return None


def token_tv(conn, nuovo: bool = False) -> str:
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_TV,)).fetchone()
    if row and not nuovo:
        return row[0]
    tok = secrets.token_urlsafe(12)
    conn.execute("INSERT INTO stato (chiave, valore) VALUES (?, ?) "
                 "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore", (CHIAVE_TV, tok))
    conn.commit()
    return tok


# ======================= SERVER WEB =======================

NO_CACHE = {"Cache-Control": "no-store"}


def _utente(req) -> dict:
    u = verifica_init_data(req.headers.get("X-Init-Data", ""), _token)
    if not u:
        raise web.HTTPUnauthorized(text="Apri l'app da Telegram")
    return u


async def h_miniapp(req):
    return web.FileResponse(CARTELLA_WEB / "miniapp.html", headers=NO_CACHE)


async def h_tv(req):
    conn = _conn()
    if not hmac.compare_digest(req.match_info["token"], token_tv(conn)):
        raise web.HTTPNotFound()
    return web.FileResponse(CARTELLA_WEB / "tv.html", headers=NO_CACHE)


async def h_stato(req):
    conn = _conn()
    tv = req.query.get("tv")
    if tv is not None:
        if not hmac.compare_digest(tv, token_tv(conn)):
            raise web.HTTPNotFound()
        uid = None
    else:
        uid = int(_utente(req)["id"])
    s = carica(conn)
    versione = _versione_globale(conn, s)
    if req.query.get("v") == versione:
        return web.json_response({"invariato": True}, headers=NO_CACHE)
    if s["torneo_id"]:
        s["accessi"] = mappa_utenti(conn, s)
    v = vista(s, uid)
    v["versione"] = versione
    if s["t"] is None and s["torneo_id"]:
        v["iscritte"] = [n for n, _ in squadre_iscritte(conn, s["torneo_id"])]
    if uid is not None and _is_admin(uid) and url_pubblico():
        v["url_tv"] = f"{url_pubblico()}/tv/{token_tv(conn)}"
    return web.json_response(v, headers=NO_CACHE)


async def h_azione(req):
    uid = int(_utente(req)["id"])
    try:
        d = await req.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Richiesta non valida")
    conn = _conn()
    try:
        r = esegui(conn, uid, d)
    except Vietato as e:
        return web.json_response({"ok": False, "errore": str(e)}, status=403)
    except (ValueError, KeyError) as e:
        msg = str(e) if isinstance(e, ValueError) else "Dati mancanti"
        return web.json_response({"ok": False, "errore": msg}, status=400)
    bot = req.app.get("bot")
    if bot is not None:
        avvisata: dict[tuple, bool] = {}   # chiamata -> almeno un account raggiunto
        tid = carica(conn)["torneo_id"]
        for user_id, testo, *resto in r.pop("avvisi", []):
            meta = resto[0] if resto else {}
            ok = False
            if user_id is not None:
                try:
                    msg = await bot.send_message(chat_id=user_id, text=testo, reply_markup=_bottone_app())
                    ok = True
                    if meta.get("pid"):   # da cancellare quando la partita è chiusa
                        conn.execute("INSERT INTO app_messaggi VALUES (?, ?, ?, ?, ?, ?)",
                                     (tid, meta["pid"], meta["tipo"], user_id, msg.message_id,
                                      datetime.now().isoformat()))
                except Exception:
                    pass  # utente che non ha mai avviato il bot in privato
            if meta.get("chiamata"):
                avvisata[meta["chiamata"]] = avvisata.get(meta["chiamata"], False) or ok
        conn.commit()
        await pulisci_messaggi(bot, conn)
        mancate = [c for c, ok in avvisata.items() if not ok]
        if mancate:   # A: l'admin sa chi chiamare a voce
            testo = "📣 Da chiamare a voce (avviso privato non arrivato):\n" + "\n".join(
                f"• {c} contro {avv} — biliardino {b}" for c, avv, b in mancate)
            scade = datetime.now(ZoneInfo("Europe/Rome")) + timedelta(minutes=CANCELLA_DA_CHIAMARE_MIN)
            testo += f"\n\n🕒 Si cancella da solo alle {scade:%H:%M}"
            for a in _admin_ids:
                try:
                    msg = await bot.send_message(chat_id=a, text=testo)
                except Exception:
                    continue
                _cancella_dopo(req.app.get("jq"), bot, a, msg.message_id, CANCELLA_DA_CHIAMARE_MIN * 60)
        if d.get("azione") in ("prepara", "crea_gironi"):   # B: riepilogo copertura
            testo = await riepilogo_avvisabili(bot, conn)
            for a in _admin_ids:
                try:
                    await bot.send_message(chat_id=a, text=testo)
                except Exception:
                    pass
    else:
        r.pop("avvisi", None)
    return web.json_response(r)


async def pulisci_messaggi(bot, conn) -> None:
    """Cancella gli avvisi privati che non servono più:
    - "Tocca a voi" quando la partita non è più in campo (finita, ferma, stand-by);
    - avvisi sul risultato quando la partita è finita o la proposta non c'è più;
    - tutti quelli di un torneo non più attivo (prova chiusa, Ricomincia, ecc.)."""
    righe = conn.execute("SELECT rowid, torneo_id, pid, tipo, chat_id, message_id FROM app_messaggi").fetchall()
    if not righe:
        return
    s = carica(conn)
    partite = s["t"]["partite"] if s["t"] else {}
    via = []
    for rowid, tid, pid, tipo, chat_id, mid in righe:
        p = partite.get(pid) if tid == s["torneo_id"] else None
        if tipo == "chiamata":
            scaduto = p is None or p["stato"] != "in_corso"
        else:
            scaduto = p is None or p["stato"] == "finita" or pid not in s["proposte"]
        if scaduto:
            via.append(rowid)
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass   # già cancellato a mano o più vecchio di 48 ore
    if via:
        conn.executemany("DELETE FROM app_messaggi WHERE rowid = ?", [(r,) for r in via])
        conn.commit()


async def _raggiungibile(bot, user_id: int) -> bool:
    """Telegram non dice se un utente ha avviato il bot: si prova un'azione
    invisibile ("sta scrivendo..." per pochi secondi nella sua chat col bot)."""
    try:
        await bot.send_chat_action(chat_id=user_id, action="typing")
        return True
    except Exception:
        return False


async def riepilogo_avvisabili(bot, conn) -> str:
    """B: quali coppie del torneo attivo riceveranno le chiamate in privato."""
    s = carica(conn)
    if s["torneo_id"] is None:
        return "Nessun torneo attivo."
    accessi = mappa_utenti(conn, s)
    collegati = {gid for (gid,) in conn.execute("SELECT giocatore_id FROM giocatori_telegram")}
    cache: dict[int, bool] = {}
    ok, ko = [], []
    for c in sorted(accessi):
        raggiunti = []
        for u in accessi[c]:
            if u not in cache:
                cache[u] = await _raggiungibile(bot, u)
            if cache[u]:
                raggiunti.append(u)
        if raggiunti:
            ok.append(c)
            continue
        if accessi[c]:
            ko.append(f"• {c}: collegato ma non ha mai avviato il bot in privato (deve premere Avvia)")
            continue
        senza = []
        for g in statistiche.dividi_coppia(c) or [c]:
            row = statistiche.cerca_giocatore(conn, g)
            if not row or row[0] not in collegati:
                senza.append(g)
        ko.append(f"• {c}: nessun account → serve /sonoio " + " o /sonoio ".join(senza))
    tot = len(accessi)
    testo = f"📲 Avvisi in privato — {s['nome']}: {len(ok)}/{tot} coppie avvisabili."
    if s.get("prova") is not None:
        testo += "\n(Prova: le coppie finte non hanno account, è normale che risultino non avvisabili.)"
    if ko:
        testo += "\n\nNon avvisabili (da chiamare a voce):\n" + "\n".join(ko)
    else:
        testo += "\nTutte raggiungibili ✅"
    return testo


async def cmd_provatorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/provatorneo [N]: torneo finto per provare l'app (solo admin, in privato)."""
    uid = update.effective_user.id
    if not _is_admin(uid):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    if update.effective_chat.type != "private":
        await _rispondi(update, context, "Usalo in chat privata con il bot.")
        return
    arg = (context.args or ["12"])[0]
    if not arg.isdigit() or not 4 <= int(arg) <= 18:
        await update.message.reply_text("Uso: /provatorneo N (coppie finte, da 4 a 18; predefinito 12)")
        return
    conn = _conn()
    avvia_prova(conn, uid, int(arg))
    mia = next(iter(carica(conn)["utenti"]))
    await update.message.reply_text(
        f"🧪 Prova avviata: {arg} coppie finte. Tu giochi in «{mia}».\n\n"
        "• L'app e la pagina TV mostrano la PROVA al posto del torneo vero, per tutti.\n"
        "• Il torneo vero non viene toccato: iscrizioni, lista, statistiche, Elo e medagliere restano come sono.\n"
        "• Gli avvisi arrivano solo a te.\n"
        "• In Gestione trovi «🎲 Simula giro» (punteggi casuali) e «👤 Vista giocatore».\n"
        f"• Chiudi con /fineprova (si chiude da sola dopo {DURATA_PROVA_ORE} ore).",
        reply_markup=_bottone_app(),
    )


async def cmd_fineprova(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    conn = _conn()
    chiusa = chiudi_prova(conn)
    await pulisci_messaggi(context.bot, conn)
    await _rispondi(update, context, "🧪 Prova chiusa: l'app torna sul torneo vero." if chiusa
                    else "Nessuna prova attiva.")


async def cmd_avvisabili(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    await _rispondi(update, context, await riepilogo_avvisabili(context.bot, _conn()))


def _cancella_dopo(jq, bot, chat_id: int, message_id: int, secondi: int) -> None:
    """Cancella un messaggio del bot dopo 'secondi'. Con la job queue del bot
    se c'è; altrimenti un task asyncio. Se il bot si riavvia nel frattempo il
    messaggio resta (pazienza: è solo un promemoria)."""
    if jq is not None:
        jq.run_once(_elimina, secondi, data=(chat_id, message_id))
        return

    async def _dopo():
        await asyncio.sleep(secondi)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    asyncio.get_running_loop().create_task(_dopo())


def crea_web_app(bot=None, jq=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["jq"] = jq
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    app.router.add_get("/app", h_miniapp)
    app.router.add_get("/tv/{token}", h_tv)
    app.router.add_get("/api/stato", h_stato)
    app.router.add_post("/api/azione", h_azione)
    app.router.add_static("/static/", CARTELLA_WEB)          # mascotte ridotta
    app.router.add_static("/assets/", CARTELLA_ASSETS)       # font Bebas e Yellowtail
    return app


async def avvia_server(application):
    runner = web.AppRunner(crea_web_app(application.bot, application.job_queue))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORTA).start()
    application.bot_data["web_runner"] = runner
    url = url_pubblico()
    print(f"Server web app tornei sulla porta {PORTA} — {url or 'PUBLIC_URL non impostata'}")
    if url:   # bottone "Torneo" accanto alla tastiera nelle chat private col bot
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp("Torneo", WebAppInfo(f"{url}/app")))
        except Exception as e:
            print("Menu button non impostato:", e)


async def ferma_server(application):
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


# ======================= COMANDI TELEGRAM =======================

def _bottone_app():
    url = url_pubblico()
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎱 Apri l'app torneo", web_app=WebAppInfo(f"{url}/app"))]])


async def invia_bottone_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usata da /app in privato e da /start app (link dal gruppo)."""
    tastiera = _bottone_app()
    if tastiera is None:
        await update.message.reply_text("App non ancora configurata (manca PUBLIC_URL).")
        return
    await update.message.reply_text("Tabellone, partite e risultati:", reply_markup=tastiera)


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await invia_bottone_app(update, context)
        return
    # nei gruppi Telegram non permette i bottoni Mini App: si passa dalla chat privata
    await _cancella_comando(update)
    nome = context.bot.username
    msg = await context.bot.send_message(
        update.effective_chat.id, "🎱 App torneo: apri la chat col bot",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "Apri", url=f"https://t.me/{nome}?start=app")]]))
    context.job_queue.run_once(_elimina, 60, data=(update.effective_chat.id, msg.message_id))


async def _elimina(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.delete_message(*context.job.data)
    except Exception:
        pass


async def cmd_tv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    if update.effective_chat.type != "private":
        await _rispondi(update, context, "Il link TV te lo mando solo in privato: scrivimi /tv lì.")
        return
    url = url_pubblico()
    if not url:
        await update.message.reply_text("PUBLIC_URL non impostata: genera il dominio su Railway.")
        return
    conn = _conn()
    tok = token_tv(conn, nuovo=bool(context.args and context.args[0].lower() == "nuovo"))
    await update.message.reply_text(
        f"📺 Pagina TV (sola lettura, si aggiorna da sola):\n{url}/tv/{tok}\n\n"
        "Chi ha il link vede il tabellone. Per invalidarlo: /tv nuovo")


def collega(conn, user_id: int, nome: str) -> tuple[bool, str]:
    """/sonoio: collega l'account al giocatore. Ritorna (ok, messaggio)."""
    g = statistiche.cerca_giocatore(conn, nome)
    if not g:
        return False, (f"Giocatore «{nome}» non trovato. Scrivi il nome come nelle iscrizioni "
                       "(es. /sonoio Luca); l'elenco è in /giocatori.")
    gid, gnome = g
    occupato = conn.execute("SELECT user_id FROM giocatori_telegram WHERE giocatore_id = ?", (gid,)).fetchone()
    if occupato and occupato[0] != user_id:
        return False, f"«{gnome}» è già collegato a un altro account. Se è un errore chiedi all'admin (/scollega {gnome})."
    if occupato:
        return True, f"Sei già collegato come «{gnome}»."
    precedente = conn.execute(
        "SELECT g.nome FROM giocatori_telegram t JOIN giocatori g ON g.id = t.giocatore_id WHERE t.user_id = ?",
        (user_id,)).fetchone()
    conn.execute("DELETE FROM giocatori_telegram WHERE user_id = ?", (user_id,))
    conn.execute("INSERT INTO giocatori_telegram (giocatore_id, user_id, collegato_il) VALUES (?, ?, ?)",
                 (gid, user_id, datetime.now().isoformat()))
    conn.commit()
    extra = f" (prima eri «{precedente[0]}»)" if precedente else ""
    return True, (f"✅ Ora sei «{gnome}»{extra}: nell'app puoi inserire e confermare i risultati "
                  "di ogni coppia in cui giochi.")


async def cmd_sonoio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    uid = update.effective_user.id
    nome = " ".join(context.args or []).strip()
    if not nome:
        row = conn.execute(
            "SELECT g.nome FROM giocatori_telegram t JOIN giocatori g ON g.id = t.giocatore_id "
            "WHERE t.user_id = ?", (uid,)).fetchone()
        stato = f"Sei collegato come «{row[0]}»." if row else "Non sei ancora collegato a nessun giocatore."
        await _rispondi(update, context, f"{stato}\nUso: /sonoio TuoNome (come nelle iscrizioni, es. /sonoio Luca)")
        return
    _, msg = collega(conn, uid, nome)
    await _rispondi(update, context, msg)


async def cmd_scollega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    conn = _conn()
    nome = " ".join(context.args or []).strip()
    g = statistiche.cerca_giocatore(conn, nome) if nome else None
    if not g:
        await _rispondi(update, context, "Uso: /scollega Nome (giocatore esistente, vedi /giocatori)")
        return
    n = conn.execute("DELETE FROM giocatori_telegram WHERE giocatore_id = ?", (g[0],)).rowcount
    conn.commit()
    await _rispondi(update, context, f"🔓 «{g[1]}» scollegato." if n else f"«{g[1]}» non era collegato.")


COMANDI_APP = [
    ("app", "Apre l'app torneo: gironi, partite, risultati"),
    ("sonoio", "Collega il tuo account al tuo nome giocatore — es. /sonoio Luca"),
    ("scollega", "[admin] Toglie il collegamento /sonoio di un giocatore — /scollega Luca"),
    ("provatorneo", "[admin] Torneo finto per provare l'app — /provatorneo 12 (chiudi con /fineprova)"),
    ("fineprova", "[admin] Chiude la prova: l'app torna sul torneo vero"),
    ("avvisabili", "[admin] Quali coppie ricevono le chiamate in privato e chi deve fare /sonoio"),
    ("tv", "[admin] Link della pagina TV del tabellone (/tv nuovo lo cambia)"),
]


def registra(app, db_connect, is_admin, rispondi, cancella_comando, token, admin_ids):
    global _db_connect, _is_admin, _rispondi, _cancella_comando, _token, _admin_ids
    _db_connect, _is_admin, _rispondi, _cancella_comando, _token, _admin_ids = (
        db_connect, is_admin, rispondi, cancella_comando, token, set(admin_ids))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("tv", cmd_tv))
    app.add_handler(CommandHandler("sonoio", cmd_sonoio))
    app.add_handler(CommandHandler("scollega", cmd_scollega))
    app.add_handler(CommandHandler("avvisabili", cmd_avvisabili))
    app.add_handler(CommandHandler("provatorneo", cmd_provatorneo))
    app.add_handler(CommandHandler("fineprova", cmd_fineprova))
