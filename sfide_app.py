"""Scheda «Sfide» della Mini App (fase 2): sfidine con giocatori scelti da elenco.

Per tutti i membri, come /sfida. Stessa logica dei comandi (statistiche: una
sola logica, due ingressi). Qui solo: lettura della richiesta, scelta dei nomi
nuovi (giocatore nuovo o soprannome di uno esistente) ed effetti su Telegram.

API (autenticazione Telegram come il resto della Mini App):
  GET  /api/sfide?pagina=N&anno=A&med=A|sempre   elenco giocatori, ultime sfide,
                                                  classifica Elo, medagliere
  POST /api/sfide  {"azione": "salva", "a": [g1, g2], "b": [g3, g4], "va": 5, "vb": 2,
                    "data": "AAAA-MM-GG" (facoltativa),
                    "scelte": {"gio": "nuovo" | id_giocatore}}  (solo per i nomi nuovi)
                   {"azione": "annulla", "id": N}
                   {"azione": "modifica", "id": N, "a": [..], "b": [..], "va": 5, "vb": 3,
                    "data": "AAAA-MM-GG", "scelte": {...}}   (solo admin: correzione totale,
                    la sfida tiene il numero; a/b/data mancanti = restano com'erano)
  In "salva" l'admin può aggiungere "per": 0-3 = giocatore per conto del quale inserisce
  (con /sonoio quel giocatore può poi annullarla lui).

Nomi non in anagrafica senza scelta: risponde {"ok": false, "sconosciuti": [...]}
con i nomi simili, e la Mini App chiede «è un soprannome di…? / è nuovo».
Dopo salva e annulla la classifica nuova va in fondo al gruppo (la vecchia sparisce),
come /sfida scritto nel gruppo. Ogni soprannome aggiunto viene segnalato agli admin.
"""
from __future__ import annotations

from datetime import date
from html import escape

from aiohttp import web

import statistiche as ST
from database import chat_gruppo_registrato

_utente = None      # req -> dict utente (alza 401 se non valido)
_is_admin = None
_conn = None
_admin_ids: set = set()
MAX_VINTE = ST.MAX_VINTE


def _json(ok: bool, testo: str = "", status: int = 200, **dati):
    return web.json_response({"ok": ok, "testo" if ok else "errore": testo, **dati},
                             status=status, headers={"Cache-Control": "no-store"})


def _db():
    conn = _conn()
    ST.crea_tabelle(conn)
    return conn


# ======================= LETTURA =======================

def stato_sfide(conn, uid: int, pagina: int = 1, anno: int | None = None, med: str = "",
                cerca: str = "") -> dict:
    oggi = ST._oggi()
    anno = anno or oggi.year
    anno_med = None if med == "sempre" else (int(med) if med.isdigit() else oggi.year)
    righe_med, contati, _ = ST.calcola_medagliere(conn, anno_med)
    return {
        "giocatori": ST.giocatori_per_uso(conn),
        "ultime": ST.pagina_sfide(conn, pagina, giocatore=cerca),
        "io": (lambda r: r[0] if r else None)(conn.execute(   # giocatore collegato con /sonoio («Le mie»)
            "SELECT g.nome FROM giocatori_telegram t JOIN giocatori g ON g.id = t.giocatore_id WHERE t.user_id = ?",
            (uid,)).fetchone()),
        "classifica": {"anno": anno, "righe": [
            {"nome": r["nome"], "elo": round(r["elo"]), "g": r["g"], "v": r["v"], "perc": round(r["perc"])}
            for r in ST.calcola_classifica(conn, anno)]},
        "medagliere": {"anno": anno_med, "contati": contati,
                       "righe": [{"nome": n, "m": m} for n, m in righe_med]},
        "anni": ST.anni_con_dati(conn),
        "oggi": oggi.isoformat(),
        "uid": uid,
        "admin": bool(_is_admin(uid)),
        "gruppo": chat_gruppo_registrato(conn) is not None,
    }


async def h_stato(req):
    uid = int(_utente(req)["id"])
    q = req.query
    try:
        pagina = int(q.get("pagina", "1"))
        anno = int(q["anno"]) if q.get("anno") else None
    except ValueError:
        pagina, anno = 1, None
    return web.json_response(stato_sfide(_db(), uid, pagina, anno, q.get("med", ""), q.get("cerca", "")[:60]),
                             headers={"Cache-Control": "no-store"})


# ======================= EFFETTI SU TELEGRAM =======================

async def _classifica_nel_gruppo(req, conn, intestazione: str, anno: int) -> str:
    """Classifica nuova in fondo al gruppo, la vecchia sparisce. Ritorna un avviso se non riesce."""
    bot, gruppo = req.app.get("bot"), chat_gruppo_registrato(conn)
    if bot is None:
        return ""
    if gruppo is None:
        return "Nessun gruppo registrato: la classifica non è andata nel gruppo."
    try:
        await ST._sostituisci(bot, conn, gruppo, "classifica_msg",
                              escape(intestazione) + "\n\n" + ST.testo_classifica(conn, anno))
        return ""
    except Exception as e:
        return f"Classifica nel gruppo non aggiornata ({e})."


async def _avvisa_admin(req, testo: str, salta: int):
    bot = req.app.get("bot")
    if bot is None:
        return
    for aid in _admin_ids:
        if aid == salta:
            continue
        try:
            await bot.send_message(chat_id=aid, text=testo)
        except Exception:
            pass   # admin che non ha mai scritto al bot: non blocca la sfida


# ======================= AZIONI =======================

def _leggi_data(testo, oggi: date):
    if not testo:
        return oggi, None
    try:
        d = date.fromisoformat(str(testo))
    except ValueError:
        return None, "Data non valida."
    if d > oggi:
        return None, "La data è nel futuro."
    return d, None


def _leggi_nomi(d: dict):
    return [ST.pulisci_nome(str(n or "")) for n in list(d.get("a") or [])[:2] + list(d.get("b") or [])[:2]]


def _risolvi_nomi(conn, nomi: list[str], d: dict):
    """Nomi non in anagrafica: serve una scelta (nuovo o soprannome).
    Ritorna (risposta d'errore o None, soprannomi [(nome, id)], chiavi dei 4 giocatori)."""
    scelte = {str(k).lower(): v for k, v in (d.get("scelte") or {}).items()}
    sconosciuti, soprannomi = [], []
    for n in dict.fromkeys(nomi):   # stesso nome due volte: una domanda sola
        if ST.cerca_giocatore(conn, n):
            continue
        scelta = scelte.get(n.lower())
        if scelta is None:
            sconosciuti.append({"nome": n, "simili": ST.nomi_simili(conn, n)})
        elif scelta != "nuovo":
            try:
                soprannomi.append((n, int(scelta)))
            except (TypeError, ValueError):
                return _json(False, "Scelta non valida."), [], []
    if sconosciuti:
        return _json(False, "", sconosciuti=sconosciuti), [], []
    # chiave di ogni giocatore dopo le scelte: id se esiste o se è soprannome, altrimenti il nome nuovo
    al_sop = dict(soprannomi)
    chiavi = []
    for n in nomi:
        g = ST.cerca_giocatore(conn, n)
        chiavi.append(g[0] if g else al_sop.get(n, n.lower()))
    if len(set(chiavi)) != 4:
        return _json(False, "Lo stesso giocatore compare due volte."), [], []
    return None, soprannomi, chiavi


def _chi(req) -> str:
    u = _utente(req)
    return u.get("first_name") or u.get("username") or "?"


async def _segnala_soprannomi(req, conn, uid, soprannomi, numero):
    if not soprannomi:
        return
    nomi_id = dict(conn.execute("SELECT id, nome FROM giocatori").fetchall())
    righe = "\n".join(f"• «{n}» → {nomi_id.get(gid, '?')}" for n, gid in soprannomi)
    await _avvisa_admin(req, f"🔗 {_chi(req)} ha aggiunto dei soprannomi dalla Mini App (sfida n° {numero}):\n"
                             f"{righe}\nSe uno è sbagliato: /toglisoprannome Nome, poi annulla e rifai la sfida.",
                        salta=uid)


async def _salva(req, conn, uid: int, d: dict):
    nomi = _leggi_nomi(d)
    if len(nomi) != 4 or not all(nomi):
        return _json(False, "Scegli tutti e quattro i giocatori.")
    try:
        va, vb = int(d.get("va")), int(d.get("vb"))
    except (TypeError, ValueError):
        return _json(False, "Risultato non valido.")
    if not (0 <= va <= MAX_VINTE and 0 <= vb <= MAX_VINTE) or va + vb == 0:
        return _json(False, "Risultato 0-0: nessuna partita.")
    oggi = ST._oggi()
    data_sfida, errore = _leggi_data(d.get("data"), oggi)
    if errore:
        return _json(False, errore)
    per = d.get("per")
    if per is not None:
        if not _is_admin(uid):
            return _json(False, "Solo un admin può inserire una sfida per conto di un altro.")
        if per not in (0, 1, 2, 3):
            return _json(False, "Giocatore «per conto di» non valido.")

    errore, soprannomi, chiavi = _risolvi_nomi(conn, nomi, d)
    if errore is not None:   # una risposta aiohttp vuota vale False: serve "is not None"
        return errore

    # doppio invio (tocco ripetuto o due persone che segnano la stessa sfida): stessa sfida pochi minuti fa = rifiutata
    if all(isinstance(c, int) for c in chiavi):
        gia = ST.sfida_doppia(conn, chiavi[:2], chiavi[2:], va, vb, data_sfida)
        if gia:
            return _json(False, f"⚠️ Questa sfida è già stata salvata poco fa (n° {gia}). Se l'avete giocata davvero un'altra volta, riprova tra qualche minuto.", doppia=gia)

    for n, gid in soprannomi:
        errore = ST.aggiungi_soprannome(conn, n, gid)
        if errore:
            conn.rollback()
            return _json(False, errore)
    numeri, nuovi = ST.salva_sfide(conn, [(nomi[:2], nomi[2:], va, vb)], data_sfida, uid)   # commit
    ufficiali = [ST.nome_canonico(conn, n) for n in nomi]   # «Gio» torna «Giovanni» nelle caselle

    chi = _chi(req)
    firma = f"\n📱 Dalla app — {chi}"
    if per is not None:
        ST.imposta_per_conto(conn, numeri[0], ST.cerca_giocatore(conn, nomi[per])[0])
        firma += f" per {ufficiali[per]}"
    intest = ST.intestazione_sfide(numeri, nuovi, data_sfida, oggi) + firma
    avviso = await _classifica_nel_gruppo(req, conn, intest, data_sfida.year)
    await _segnala_soprannomi(req, conn, uid, soprannomi, numeri[0])
    testo = f"✅ Sfida n° {numeri[0]} salvata" + (f" per {ufficiali[per]}" if per is not None else "")
    if nuovi:
        testo += " · 🆕 " + ", ".join(nuovi)
    return _json(True, testo, avviso=avviso, numero=numeri[0], nomi=ufficiali)


async def _modifica(req, conn, uid: int, d: dict):
    """Correzione totale (solo admin): giocatori, risultato, data. La sfida tiene il numero."""
    if not _is_admin(uid):
        return _json(False, "Solo un admin può correggere una sfida.")
    try:
        sid = int(d.get("id"))
        va = int(d["va"]) if d.get("va") is not None else None
        vb = int(d["vb"]) if d.get("vb") is not None else None
    except (TypeError, ValueError, KeyError):
        return _json(False, "Dati non validi.")
    data_sfida = None
    if d.get("data"):
        data_sfida, errore = _leggi_data(d.get("data"), ST._oggi())
        if errore:
            return _json(False, errore)
    a = b = None
    soprannomi = []
    if d.get("a") is not None or d.get("b") is not None:
        nomi = _leggi_nomi(d)
        if len(nomi) != 4 or not all(nomi):
            return _json(False, "Scegli tutti e quattro i giocatori.")
        errore, soprannomi, _ = _risolvi_nomi(conn, nomi, d)
        if errore is not None:
            return errore
        for n, gid in soprannomi:
            errore = ST.aggiungi_soprannome(conn, n, gid)
            if errore:
                conn.rollback()
                return _json(False, errore)
        a, b = nomi[:2], nomi[2:]
    ok, testo, anni, _ = ST.modifica_sfida(conn, sid, a, b, va, vb, data_sfida, uid, True)
    if not ok:
        conn.rollback()   # soprannomi aggiunti qui sopra non restano
        return _json(False, testo)
    avviso = await _classifica_nel_gruppo(req, conn, f"{testo}\n📱 Dalla app — {_chi(req)}", anni[0])
    if len(anni) > 1:
        avviso = (avviso + " " if avviso else "") + f"Cambiato anno: anche la classifica {anni[1]} è stata ricalcolata."
    await _segnala_soprannomi(req, conn, uid, soprannomi, sid)
    return _json(True, testo.split("\n")[0], avviso=avviso)


async def _annulla(req, conn, uid: int, d: dict):
    try:
        sid = int(d.get("id"))
    except (TypeError, ValueError):
        return _json(False, "Sfida non valida.")
    ok, testo, anno = ST.annulla_sfida(conn, sid, uid, bool(_is_admin(uid)))
    if not ok:
        return _json(False, testo)
    u = _utente(req)
    chi = u.get("first_name") or u.get("username") or "?"
    avviso = await _classifica_nel_gruppo(req, conn, f"{testo}\n📱 Dalla app — {chi}", anno)
    return _json(True, testo, avviso=avviso)


async def h_azione(req):
    uid = int(_utente(req)["id"])
    try:
        d = await req.json()
    except ValueError:
        return _json(False, "Richiesta non valida", 400)
    conn = _db()
    az = d.get("azione")
    if az == "salva":
        return await _salva(req, conn, uid, d)
    if az == "annulla":
        return await _annulla(req, conn, uid, d)
    if az == "modifica":
        return await _modifica(req, conn, uid, d)
    return _json(False, "Azione sconosciuta", 400)


def aggiungi_rotte(app: web.Application, utente, is_admin, conn, admin_ids) -> None:
    global _utente, _is_admin, _conn, _admin_ids
    _utente, _is_admin, _conn, _admin_ids = utente, is_admin, conn, set(admin_ids or ())
    app.router.add_get("/api/sfide", h_stato)
    app.router.add_post("/api/sfide", h_azione)
