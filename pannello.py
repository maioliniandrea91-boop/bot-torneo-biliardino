"""Pannello admin «Tornei» dentro la Mini App (fase 1).

Stessa logica dei comandi: tutto passa da logica_tornei (una sola logica, due
ingressi). Qui solo: controllo admin, lettura della richiesta, effetti su
Telegram (lista nel gruppo, podio, file Word in privato).

API (solo admin, autenticazione Telegram come il resto della Mini App):
  GET  /api/pannello?pagina=N      stato del torneo attivo + storico
  POST /api/pannello               {"azione": ..., ...}  (JSON)
  POST /api/pannello/podio         multipart: anteprima o pubblicazione, foto facoltativa
"""
from __future__ import annotations

import io

from aiohttp import web

import comando_locandina
import logica_tornei as L
import statistiche
from chat_telegram import aggiorna_lista_gruppo, pubblica_lista_gruppo
from database import (chat_gruppo_registrato, get_torneo_attivo, iscrizioni_aperte, max_coppie,
                      set_iscrizioni)

MAX_FOTO = 10 * 1024 * 1024   # limite Telegram per le foto

_utente = None      # req -> dict utente (alza 401 se non valido)
_is_admin = None
_conn = None
NESSUN_GRUPPO = "Nessun gruppo registrato: scrivi /registragruppo dentro il gruppo."


def _json(ok: bool, testo: str = "", status: int = 200, **dati):
    return web.json_response({"ok": ok, "testo" if ok else "errore": testo, **dati},
                             status=status, headers={"Cache-Control": "no-store"})


def _admin(req) -> int:
    u = _utente(req)
    uid = int(u["id"])
    if not _is_admin(uid):
        raise web.HTTPForbidden(text='{"ok": false, "errore": "Riservato agli admin"}',
                                content_type="application/json")
    return uid


def _db():
    conn = _conn()
    statistiche.crea_tabelle(conn)   # anagrafica giocatori (per i suggerimenti)
    return conn


# ======================= LETTURA =======================

def _proposta_podio(conn, torneo_id: int) -> int:
    """Torneo da proporre per il podio: l'attivo, ma se è vuoto (appena aperto
    con la locandina) il precedente — stessa regola di /podio."""
    if L.conta_squadre(conn, torneo_id):
        return torneo_id
    prec = conn.execute("SELECT id FROM tornei WHERE id < ? ORDER BY id DESC LIMIT 1",
                        (torneo_id,)).fetchone()
    return prec[0] if prec else torneo_id


def stato_pannello(conn, pagina: int = 1) -> dict:
    tid, nome = get_torneo_attivo(conn)
    squadre = [n for _, n in L.squadre_torneo(conn, tid)]
    massimo = max_coppie(conn, tid)
    giocatori = [r[0] for r in conn.execute("SELECT nome FROM giocatori ORDER BY nome COLLATE NOCASE")]
    return {
        "attivo": {"id": tid, "nome": nome, "squadre": squadre, "max": massimo,
                   "aperte": iscrizioni_aperte(conn), "soglia": comando_locandina.get_soglia(conn),
                   "conferma": L.riga_conferma(conn, len(squadre), massimo)},
        "giocatori": giocatori,
        "storico": L.pagina_tornei(conn, pagina),
        "podio_proposto": _proposta_podio(conn, tid),
        "gruppo": chat_gruppo_registrato(conn) is not None,
        "limiti": {"min": L.LIMITE_COPPIE_MIN, "max": L.LIMITE_COPPIE_MAX},
    }


async def h_stato(req):
    _admin(req)
    try:
        pagina = int(req.query.get("pagina", "1"))
    except ValueError:
        pagina = 1
    return web.json_response(stato_pannello(_db(), pagina), headers={"Cache-Control": "no-store"})


# ======================= AZIONI =======================

def nome_coppia(conn, g1: str, g2: str) -> str | None:
    """Due giocatori -> 'Nome1-Nome2', con il nome ufficiale dell'anagrafica
    se esiste (maiuscole come in archivio)."""
    g1, g2 = (" ".join(L.togli_simboli(g or "").split()) for g in (g1, g2))
    if not g1 or not g2:
        return None
    return f"{statistiche.nome_canonico(conn, g1)}-{statistiche.nome_canonico(conn, g2)}"


async def _lista_nel_gruppo(req, conn, intestazione: str) -> str:
    """Lista nuova in fondo al gruppo. Ritorna un avviso se non è stato possibile."""
    bot = req.app.get("bot")
    gruppo = chat_gruppo_registrato(conn)
    if bot is None or gruppo is None:
        return "" if bot is None else NESSUN_GRUPPO
    try:
        await pubblica_lista_gruppo(bot, conn, gruppo, intestazione)
        return ""
    except Exception as e:
        return f"Lista nel gruppo non aggiornata ({e})."


async def _lista_silenziosa(req, conn):
    bot, gruppo = req.app.get("bot"), chat_gruppo_registrato(conn)
    if bot is not None and gruppo is not None:
        await aggiorna_lista_gruppo(bot, conn, gruppo)


async def h_azione(req):
    uid = _admin(req)
    try:
        d = await req.json()
    except ValueError:
        return _json(False, "Richiesta non valida", 400)
    conn = _db()
    az = d.get("azione")

    if az in ("iscrivi", "rinomina"):
        nuovo = nome_coppia(conn, d.get("g1", ""), d.get("g2", ""))
        if not nuovo:
            return _json(False, "Scrivi entrambi i giocatori.")
        if az == "iscrivi":
            u = _utente(req)
            esito = L.iscrivi_coppia(conn, uid, u.get("username") or u.get("first_name"), nuovo, admin=True)
        else:
            esito = L.rinomina_coppia(conn, str(d.get("pos", "")).strip(), nuovo)
        if not esito.ok:
            return _json(False, esito.testo)
        return _json(True, esito.testo, avviso=await _lista_nel_gruppo(req, conn, esito.testo))

    if az == "rimuovi":
        esito = L.rimuovi_coppia(conn, str(d.get("nome", "")))
        if not esito.ok:
            return _json(False, esito.testo)
        return _json(True, esito.testo, avviso=await _lista_nel_gruppo(req, conn, esito.testo))

    if az == "iscrizioni":
        aperte = bool(d.get("aperte"))
        set_iscrizioni(conn, aperte)
        return _json(True, "🔓 Iscrizioni aperte." if aperte else "🔒 Iscrizioni chiuse.")

    if az in ("max", "soglia"):
        valore = str(d.get("valore", ""))
        esito = L.imposta_max_coppie(conn, valore) if az == "max" else L.imposta_soglia(conn, valore)
        if not esito.ok:
            return _json(False, esito.testo)
        await _lista_silenziosa(req, conn)
        return _json(True, esito.testo)

    if az == "esporta":
        bot = req.app.get("bot")
        if bot is None:
            return _json(False, "Bot non disponibile.")
        contenuto, nome_file = L.word_squadre(conn)
        buffer = io.BytesIO(contenuto)
        buffer.name = nome_file
        try:
            await bot.send_document(chat_id=uid, document=buffer, filename=nome_file)
        except Exception as e:
            return _json(False, f"Invio non riuscito ({e}).")
        return _json(True, "📄 Word inviato nella chat col bot.")

    if az in ("dettaglio", "elimina_esamina", "elimina") and not str(d.get("id", "")).isdigit():
        return _json(False, "Torneo non valido", 400)

    if az == "dettaglio":
        esito = L.dettaglio_torneo(conn, f"#{int(d.get('id', 0))}")
        if not esito.ok:
            return _json(False, esito.testo)
        chiuso = conn.execute("SELECT chiuso FROM tornei WHERE id = ?", (esito.dati["id"],)).fetchone()[0]
        return _json(True, "", torneo={**esito.dati, "attivo": not chiuso})

    if az in ("elimina_esamina", "elimina"):
        cercato = f"#{int(d.get('id', 0))}"
        esito = L.elimina_torneo(conn, cercato) if az == "elimina" else L.esamina_eliminazione(conn, cercato)
        if not esito.ok:
            return _json(False, esito.testo)
        testo = esito.testo
        if az == "elimina_esamina":   # nel pannello si conferma col bottone, non con CONFERMA
            testo = testo.split("\nPer confermare:")[0]
        return _json(True, testo)

    if az == "riordina_esamina":
        esito = L.esamina_riordino(conn)
        return _json(esito.ok, esito.testo)

    if az == "riordina":
        if not L.esamina_riordino(conn).ok:
            return _json(False, L.esamina_riordino(conn).testo)
        bot = req.app.get("bot")
        try:   # backup prima di toccare i numeri: senza backup non si procede
            import servizio
            backup = await servizio.esegui_backup(bot, forzato=True, nota="🔢 prima del riordino dei numeri tornei") \
                if bot is not None else "nessun bot"
        except Exception as e:
            return _json(False, f"Backup non riuscito ({e}): riordino annullato.")
        if backup != "inviato":
            return _json(False, f"Backup non inviato ({backup}): riordino annullato. Collega il canale con /backup.")
        esito = L.riordina_tornei(conn)
        return _json(esito.ok, esito.testo)

    return _json(False, "Azione sconosciuta", 400)


# ======================= PODIO =======================

async def h_podio(req):
    """Campi: modo (anteprima|pubblica), torneo_id, primo, secondo, terzo,
    descrizione, foto (file, facoltativa)."""
    _admin(req)
    try:
        modulo = await req.post()   # multipart (con foto) o modulo semplice
    except Exception:
        return _json(False, "Richiesta non valida", 400)
    foto = None
    f = modulo.get("foto")
    if isinstance(f, web.FileField):
        foto = f.file.read()
        if len(foto) > MAX_FOTO:
            return _json(False, "Foto troppo grande (max 10 MB).")
    campi = {k: v.strip() for k, v in modulo.items() if isinstance(v, str)}
    conn = _db()
    try:
        torneo_id = int(campi.get("torneo_id", ""))
    except ValueError:
        return _json(False, "Scegli il torneo.")
    squadre = [campi.get(k, "") for k in ("primo", "secondo", "terzo")]
    if not all(squadre):
        return _json(False, "Servono tutte e tre le squadre del podio.")
    squadre = [L.normalizza_nome_squadra(s) for s in squadre]
    esito = L.prepara_podio(conn, torneo_id, squadre, campi.get("descrizione", ""),
                            "foto" if foto else None, sostituzione_ammessa=True)
    if not esito.ok:
        return _json(False, esito.testo)
    dati = esito.dati["podio"]
    if campi.get("modo") != "pubblica":
        return _json(True, "", caption=dati["caption"], avviso=esito.testo,
                     nome_torneo=esito.dati["nome_torneo"])

    bot = req.app.get("bot")
    gruppo = chat_gruppo_registrato(conn)
    if bot is None or gruppo is None:
        return _json(False, NESSUN_GRUPPO if bot else "Bot non disponibile.")
    try:   # prima si pubblica, poi si salva: se Telegram fallisce non cambia nulla
        if foto:
            msg = await bot.send_photo(chat_id=gruppo, photo=foto, caption=dati["caption"])
            dati["foto"] = msg.photo[-1].file_id
        else:
            dati["foto"] = None
            await bot.send_message(chat_id=gruppo, text=dati["caption"])
    except Exception as e:
        return _json(False, f"Pubblicazione non riuscita ({e}). Riprova tra poco.")
    L.salva_podio(conn, dati)
    return _json(True, "✅ Podio pubblicato nel gruppo.")


def aggiungi_rotte(app: web.Application, utente, is_admin, conn) -> None:
    global _utente, _is_admin, _conn
    _utente, _is_admin, _conn = utente, is_admin, conn
    app.router.add_get("/api/pannello", h_stato)
    app.router.add_post("/api/pannello", h_azione)
    app.router.add_post("/api/pannello/podio", h_podio)
