"""Scheda «Iscrizione» della Mini App: iscriversi e ritirarsi con due tocchi.

Per tutti i membri, come /iscrivi e /ritira (stessa logica in logica_tornei:
utente normale una coppia a testa, admin quante vuole; si ritirano solo le coppie
iscritte da sé). Giocatori scelti dall'anagrafica; un nome nuovo diventa un
giocatore nuovo, SENZA domanda «soprannome di…» (scelta di Andrea: i doppioni
li accorpa lui con /unisci). Si apre dal bottone «📝 Iscriviti» sotto la lista
squadre e sotto la locandina (t.me/<bot>/sfide?startapp=iscrizione).

API (autenticazione Telegram come il resto della Mini App):
  GET  /api/iscrizione
  POST /api/iscrizione {"azione": "iscrivi", "g1": "...", "g2": "..."}
                       {"azione": "ritira", "nome": "Nome1-Nome2"}
Dopo iscrivi e ritira la lista nuova va in fondo al gruppo (la vecchia sparisce).
"""
from __future__ import annotations

from aiohttp import web

import logica_tornei as L
import statistiche as ST
from chat_telegram import pubblica_lista_gruppo
from database import chat_gruppo_registrato, get_torneo_attivo, iscrizioni_aperte, max_coppie  # noqa: F401

_utente = None
_is_admin = None
_conn = None


def _json(ok: bool, testo: str = "", status: int = 200, **dati):
    return web.json_response({"ok": ok, "testo" if ok else "errore": testo, **dati},
                             status=status, headers={"Cache-Control": "no-store"})


def _db():
    conn = _conn()
    ST.crea_tabelle(conn)
    return conn


def nome_coppia(conn, g1: str, g2: str) -> str | None:
    """Due giocatori -> 'Nome1-Nome2' col nome ufficiale dell'anagrafica (come il pannello)."""
    g1, g2 = (" ".join(L.togli_simboli(g or "").split()) for g in (g1, g2))
    if not g1 or not g2:
        return None
    return f"{ST.nome_canonico(conn, g1)}-{ST.nome_canonico(conn, g2)}"


def stato_iscrizione(conn, uid: int) -> dict:
    tid, nome = get_torneo_attivo(conn)
    righe = conn.execute("SELECT nome_squadra, user_id FROM squadre WHERE torneo_id = ? ORDER BY id",
                         (tid,)).fetchall()
    massimo = max_coppie(conn, tid)
    return {
        "torneo": {"id": tid, "nome": nome, "aperte": iscrizioni_aperte(conn), "max": massimo,
                   "conferma": L.riga_conferma(conn, len(righe), massimo)},
        "squadre": [{"nome": n, "mia": u == uid} for n, u in righe],
        "giocatori": [r[0] for r in conn.execute("SELECT nome FROM giocatori ORDER BY nome COLLATE NOCASE")],
        "admin": bool(_is_admin(uid)),
        "gruppo": chat_gruppo_registrato(conn) is not None,
    }


async def h_stato(req):
    uid = int(_utente(req)["id"])
    return web.json_response(stato_iscrizione(_db(), uid), headers={"Cache-Control": "no-store"})


async def _lista_nel_gruppo(req, conn, intestazione: str) -> str:
    bot, gruppo = req.app.get("bot"), chat_gruppo_registrato(conn)
    if bot is None:
        return ""
    if gruppo is None:
        return "Nessun gruppo registrato: la lista non è andata nel gruppo."
    try:
        await pubblica_lista_gruppo(bot, conn, gruppo, intestazione)
        return ""
    except Exception as e:
        return f"Lista nel gruppo non aggiornata ({e})."


async def h_azione(req):
    u = _utente(req)
    uid = int(u["id"])
    try:
        d = await req.json()
    except ValueError:
        return _json(False, "Richiesta non valida", 400)
    conn = _db()
    chi = u.get("first_name") or u.get("username") or "?"
    az = d.get("azione")
    if az == "iscrivi":
        coppia = nome_coppia(conn, d.get("g1", ""), d.get("g2", ""))
        if not coppia:
            return _json(False, "Scegli entrambi i giocatori.")
        g1, g2 = coppia.split("-", 1)   # doppioni e giocatori già in coppia: li blocca logica_tornei
        nuovi = [g for g in (g1, g2) if not ST.cerca_giocatore(conn, g)]
        esito = L.iscrivi_coppia(conn, uid, u.get("username") or u.get("first_name"), coppia,
                                 bool(_is_admin(uid)))
    elif az == "ritira":
        nuovi = []
        esito = L.ritira_coppia(conn, uid, str(d.get("nome", "")).strip() or None)
    else:
        return _json(False, "Azione sconosciuta", 400)
    if not esito.ok:
        return _json(False, esito.testo.replace("/ritira", "«Ritira»"))
    testo = esito.testo + (" · 🆕 " + ", ".join(nuovi) if nuovi else "")
    avviso = await _lista_nel_gruppo(req, conn, f"{testo}\n📱 Dalla app — {chi}")
    return _json(True, testo, avviso=avviso)


def aggiungi_rotte(app: web.Application, utente, is_admin, conn) -> None:
    global _utente, _is_admin, _conn
    _utente, _is_admin, _conn = utente, is_admin, conn
    app.router.add_get("/api/iscrizione", h_stato)
    app.router.add_post("/api/iscrizione", h_azione)
