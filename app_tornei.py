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
- Coppia = l'utente Telegram che l'ha iscritta con /iscrivi (squadre.user_id).
- TV: link con token segreto (/tv), sola lettura, nessun login.

VARIABILI D'AMBIENTE
- PORT        porta del server web (Railway la imposta da sola), default 8080
- PUBLIC_URL  indirizzo pubblico https (se manca usa RAILWAY_PUBLIC_DOMAIN)

COMANDI
- /app  -> apre la Mini App (in privato bottone diretto; nel gruppo rimanda
           alla chat privata col bot)
- /tv   -> (admin, in privato) link della pagina TV; "/tv nuovo" lo rigenera
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import random
import secrets
import time
from datetime import date, datetime
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
PORTA = int(os.environ.get("PORT", "8080"))
VALIDITA_INIT_DATA = 24 * 3600          # secondi
# bonus Elo per medaglie dell'anno in corso (oro, argento, bronzo), solo per
# le teste di serie: valori provvisori, da decidere
BONUS_MEDAGLIE = (20, 10, 5)
PROVE_STIMA = 8                          # tornei simulati per la stima durata
CHIAVE_TV = "app_tv_token"

AZIONI_ADMIN = {"prepara", "anteprima_gironi", "crea_gironi", "imposta", "formato",
                "avvia", "ferma", "chiama", "risultato", "ritira", "crea_tabellone",
                "ricomincia"}
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


def carica(conn) -> dict:
    """Stato dell'app per il torneo attivo. 't' è None se non preparato."""
    tid, nome = torneo_attivo(conn) or (None, None)
    s = {"torneo_id": tid, "nome": nome, "t": None, "proposte": {}, "utenti": {}, "versione": 0}
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


def _versione_globale(conn, s: dict) -> str:
    """Cambia a ogni salvataggio, al cambio di torneo e, prima che l'app sia
    preparata, a ogni nuova iscrizione."""
    if s["t"] is not None:
        return f'{s["torneo_id"]}:{s["versione"]}'
    n = conn.execute("SELECT COUNT(*), MAX(id) FROM squadre WHERE torneo_id = ?",
                     (s["torneo_id"],)).fetchone() if s["torneo_id"] else (0, 0)
    return f'{s["torneo_id"]}:i{n[0]}-{n[1]}'


# ======================= TESTE DI SERIE E STIMA =======================

def forza_coppie(conn, coppie: list[str]) -> dict[str, float]:
    """Media dei due giocatori: Elo sfidine anno in corso (1000 se assente)
    + bonus medaglie dell'anno in corso."""
    anno = date.today().year
    elo = {r["nome"].lower(): r["elo"] for r in statistiche.calcola_classifica(conn, anno)}
    med, _, _ = statistiche.calcola_medagliere(conn, anno)
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
           "fase": t["fase"] if t else "nessuna", "url_tv": None}
    if uid is not None:
        out["ruolo"] = {"admin": bool(_is_admin(uid)),
                        "coppie": sorted(c for c, u in s["utenti"].items() if u == uid)}
    if t is None:
        return out
    out.update({
        "config": t["config"], "coppie": t["coppie"], "ritirate": t["ritirate"],
        "podio": t["podio"], "seme": t["seme"],
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
        if p[lato] and s["utenti"].get(p[lato]) == uid:
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
        conn.execute("DELETE FROM app_tornei WHERE torneo_id = ?", (s["torneo_id"],))
        conn.commit()
        return risposta

    if az in ("anteprima_gironi", "crea_gironi"):
        if t["fase"] != "iscrizioni":
            raise ValueError("Gironi già creati")
        n, metodo = int(d.get("n_gironi", 0)), d.get("metodo", "teste")
        if d.get("rimescola"):
            t["seme"] = random.randrange(10**9)
        forza = forza_coppie(conn, t["coppie"])
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
        conn.execute("INSERT INTO stato (chiave, valore) VALUES ('aperte', '0') "
                     "ON CONFLICT(chiave) DO UPDATE SET valore = '0'")

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
                u = s["utenti"].get(altra)
                if u and u != uid:
                    risposta["avvisi"].append((u, f"🎱 {mia} ha inserito il risultato contro di voi: "
                                                  f"{_testo_giochi(giochi)}. Confermalo nell'app."))
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
    avvisi = []
    for p in partite:
        for c, avv in ((p["a"], p["b"]), (p["b"], p["a"])):
            u = s["utenti"].get(c)
            if u:
                avvisi.append((u, f"🎱 Tocca a voi! {c} contro {avv} — biliardino {p['biliardino']}"))
    return avvisi


def _avvisi_admin(s, p, motivo) -> list:
    return [(a, f"⚠️ {p['a']} – {p['b']}: {motivo}. Decidi tu dall'app.") for a in _admin_ids]


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
    for user_id, testo in r.pop("avvisi", []):
        if bot is None:
            continue
        try:
            await bot.send_message(chat_id=user_id, text=testo, reply_markup=_bottone_app())
        except Exception:
            pass  # utente che non ha mai avviato il bot in privato
    return web.json_response(r)


def crea_web_app(bot=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    app.router.add_get("/app", h_miniapp)
    app.router.add_get("/tv/{token}", h_tv)
    app.router.add_get("/api/stato", h_stato)
    app.router.add_post("/api/azione", h_azione)
    return app


async def avvia_server(application):
    runner = web.AppRunner(crea_web_app(application.bot))
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


COMANDI_APP = [
    ("app", "Apre l'app torneo: gironi, partite, risultati"),
    ("tv", "[admin] Link della pagina TV del tabellone (/tv nuovo lo cambia)"),
]


def registra(app, db_connect, is_admin, rispondi, cancella_comando, token, admin_ids):
    global _db_connect, _is_admin, _rispondi, _cancella_comando, _token, _admin_ids
    _db_connect, _is_admin, _rispondi, _cancella_comando, _token, _admin_ids = (
        db_connect, is_admin, rispondi, cancella_comando, token, set(admin_ids))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("tv", cmd_tv))
