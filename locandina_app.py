"""Pannello admin «Locandina» della Mini App (fase 3), sotto-scheda di Tornei.

Stessa logica di /locandina: tutto passa dalle funzioni di comando_locandina
(crea_bozza, imposta_testo, programma, annulla, ripubblica_locandina, _pubblica).
Qui solo: controllo admin, lettura del modulo, copia dell'anteprima nella chat
privata col bot (con i soliti bottoni, così funziona anche da lì).

API (solo admin):
  GET  /api/pannello/locandina          valori base, bozza in corso, programmate, ultima pubblicata
  POST azione «modulo» (dati per Modifica), «file» (PNG in chat privata), «anteprima» con «corregge»
  GET  /api/pannello/locandina/img?id=N immagine PNG della locandina
  POST /api/pannello/locandina          {"azione": ..., ...}  (JSON)
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta

from aiohttp import web

import comando_locandina as C
import pannello
from config import LIMITE_COPPIE_MAX, LIMITE_COPPIE_MIN, MAX_SQUADRE

FORMULE_MODULO = ["rollerball", "volo", "tradizionale"]
_png = {}   # lid -> PNG appena generato (poi si riprende da Telegram)


def _crea_colonne(conn):
    colonne = [r[1] for r in conn.execute("PRAGMA table_info(locandine)")]
    for nome in ("chat_privata", "msg_privato"):   # copia dell'anteprima in privato
        if nome not in colonne:
            conn.execute(f"ALTER TABLE locandine ADD COLUMN {nome} INTEGER")
    conn.commit()


def _db():
    conn = C._conn()
    _crea_colonne(conn)
    return conn


def prossimo_venerdi(oggi: date | None = None) -> date:
    oggi = oggi or datetime.now(C.FUSO).date()
    return oggi + timedelta(days=(4 - oggi.weekday()) % 7 or 7)


def argomenti_da_modulo(d: dict):
    """Modulo -> stessi argomenti di /locandina (una sola logica di controllo).
    Ritorna (dati, errore)."""
    try:
        giorno = date.fromisoformat(str(d.get("data", "")))
    except ValueError:
        return None, "Scegli la data del torneo."
    if giorno < datetime.now(C.FUSO).date():
        return None, "La data del torneo è già passata."
    formule = [f for f in FORMULE_MODULO if f in (d.get("formule") or [])]
    if not formule:
        return None, "Scegli almeno una formula."
    args = [giorno.strftime("%d/%m/%Y"), *formule]
    if d.get("cinque_x"):
        args.append("5x")
    ora = str(d.get("ora") or "").strip()
    if ora:
        args.append(ora)
    quota = str(d.get("quota") or "").strip().lower().replace(" ", "").removesuffix("euro").removesuffix("€").replace(",", ".")
    if quota:
        args.append(quota + "€")
    if d.get("max") not in (None, ""):
        args.append(f"max{d['max']}")
    premi = " ".join(str(d.get("premi") or "").split())
    if premi:
        args += ["premi", *premi.split()]
    try:
        return C.leggi_argomenti(args)
    except Exception:
        return None, "Dati non validi."


def _bozza_in_corso(conn):
    """Ultima bozza fatta dal pannello nelle ultime 48 ore (per riaprire la Mini App)."""
    limite = (datetime.now(C.FUSO) - timedelta(hours=48)).isoformat()
    row = conn.execute(
        "SELECT id FROM locandine WHERE stato = 'bozza' AND msg_privato IS NOT NULL AND creato_il >= ? "
        "ORDER BY id DESC LIMIT 1", (limite,)).fetchone()
    return _scheda(conn, row[0]) if row else None


def _scheda(conn, lid: int) -> dict:
    didascalia, extra, nuovo, ripeti, stato, quando = conn.execute(
        "SELECT didascalia, extra, nuovo_torneo, ripeti, stato, pubblica_il FROM locandine WHERE id = ?",
        (lid,)).fetchone()
    vid, vstato = C._sostituita(conn, lid)
    return {"id": lid, "extra": extra or "", "nuovo_torneo": bool(nuovo), "ripeti": bool(ripeti),
            "corregge": vid if vstato in ("pubblicata", "programmata") else None, "corregge_stato": vstato,
            "stato": stato, "caption": C._caption_anteprima(lid, C._testo_completo(didascalia, extra)),
            "quando": datetime.fromisoformat(quando).astimezone(C.FUSO).strftime("%d/%m %H:%M") if quando else None}


async def _aggiorna_privato(req, conn, lid: int, chiudi: bool = False):
    """Tiene allineata la copia in chat privata (didascalia e bottoni)."""
    bot = req.app.get("bot")
    row = conn.execute("SELECT chat_privata, msg_privato, didascalia, extra FROM locandine WHERE id = ?",
                       (lid,)).fetchone()
    if bot is None or not row or not row[1]:
        return
    try:
        if chiudi:
            await bot.edit_message_reply_markup(chat_id=row[0], message_id=row[1], reply_markup=None)
        else:
            await bot.edit_message_caption(chat_id=row[0], message_id=row[1],
                                           caption=C._caption_anteprima(lid, C._testo_completo(row[2], row[3])),
                                           reply_markup=C._tastiera(lid))
    except Exception:
        pass   # messaggio cancellato a mano o identico: non è un problema


# ======================= LETTURA =======================

async def h_stato(req):
    pannello._admin(req)
    conn = _db()
    oggi = datetime.now(C.FUSO).date()
    return web.json_response({
        "predefiniti": {"data": prossimo_venerdi(oggi).isoformat(), "oggi": oggi.isoformat(),
                        "ora": C.ORA_DEFAULT, "quota": C.QUOTA_DEFAULT, "premi": C.PREMI_DEFAULT,
                        "max": MAX_SQUADRE},
        "limiti": {"min": LIMITE_COPPIE_MIN, "max": LIMITE_COPPIE_MAX},
        "formule": FORMULE_MODULO,
        "bozza": _bozza_in_corso(conn),
        **C.elenco(conn),
        "gruppo": C._gruppo(conn) is not None,
    }, headers={"Cache-Control": "no-store"})


async def h_img(req):
    pannello._admin(req)
    try:
        lid = int(req.query.get("id", ""))
    except ValueError:
        raise web.HTTPBadRequest()
    png = _png.get(lid)
    if png is None:
        row = _db().execute("SELECT file_id FROM locandine WHERE id = ?", (lid,)).fetchone()
        bot = req.app.get("bot")
        if not row or not row[0] or bot is None:
            raise web.HTTPNotFound()
        try:
            png = bytes(await (await bot.get_file(row[0])).download_as_bytearray())
        except Exception:
            raise web.HTTPNotFound()
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ======================= AZIONI =======================

async def h_azione(req):
    uid = pannello._admin(req)
    try:
        d = await req.json()
    except ValueError:
        return pannello._json(False, "Richiesta non valida", 400)
    conn = _db()
    az = d.get("azione")
    bot, jq = req.app.get("bot"), req.app.get("jq")

    if az == "anteprima":
        dati, errore = argomenti_da_modulo(d)
        if dati is None:
            return pannello._json(False, errore or "Dati mancanti.")
        if bot is None:
            return pannello._json(False, "Bot non disponibile.")
        extra = str(d.get("extra") or "").strip() or None
        corregge = d.get("corregge")   # modifica di una locandina programmata o già pubblicata
        if corregge is not None:
            st = conn.execute("SELECT stato FROM locandine WHERE id = ?", (corregge,)).fetchone() \
                if isinstance(corregge, int) else None
            if not st or st[0] not in ("programmata", "pubblicata"):
                return pannello._json(False, "La locandina da modificare non è più programmata né pubblicata.")
        lid = C.crea_bozza(conn, dati, bool(d.get("nuovo_torneo", True)), bool(d.get("ripeti", True)), extra,
                           sostituisce=corregge)
        if len(C._caption_anteprima(lid, C._testo_completo(C._didascalia(dati), extra))) > C.LIMITE_DIDASCALIA:
            conn.execute("DELETE FROM locandine WHERE id = ?", (lid,))
            conn.commit()
            return pannello._json(False, f"Testo aggiunto troppo lungo (massimo circa {C.LIMITE_DIDASCALIA} "
                                         "caratteri di didascalia). Accorcialo.")
        png = await C.genera_png(dati)
        try:   # copia in privato: serve anche a Telegram per conservare l'immagine
            msg = await bot.send_photo(chat_id=uid, photo=png, caption=_scheda(conn, lid)["caption"],
                                       reply_markup=C._tastiera(lid))
        except Exception as e:
            conn.execute("DELETE FROM locandine WHERE id = ?", (lid,))
            conn.commit()
            return pannello._json(False, f"Non riesco a mandarti l'anteprima nella chat col bot ({e}). "
                                         "Apri la chat privata col bot e premi Avvia.")
        conn.execute("UPDATE locandine SET file_id = ?, chat_privata = ?, msg_privato = ? WHERE id = ?",
                     (msg.photo[-1].file_id, uid, msg.message_id, lid))
        conn.commit()
        _png.clear()
        _png[lid] = png
        vecchia = d.get("sostituisce")   # anteprima rifatta: la bozza precedente non serve più
        if isinstance(vecchia, int) and vecchia != lid:
            if conn.execute("UPDATE locandine SET stato = 'annullata' WHERE id = ? AND stato = 'bozza'",
                            (vecchia,)).rowcount:
                conn.commit()
                await _aggiorna_privato(req, conn, vecchia, chiudi=True)
        return pannello._json(True, "", bozza=_scheda(conn, lid),
                              img="data:image/png;base64," + base64.b64encode(png).decode())

    try:
        lid = int(d.get("id")) if d.get("id") is not None else None
    except (TypeError, ValueError):
        return pannello._json(False, "Locandina non valida", 400)
    esiste = lid is not None and conn.execute("SELECT 1 FROM locandine WHERE id = ?", (lid,)).fetchone()
    if az != "ripubblica" and not esiste:
        return pannello._json(False, "Locandina non trovata.")

    if az == "modulo":   # dati della locandina per riaprirla nel modulo (Modifica)
        dati = C.dati_salvati(conn, lid)
        extra, ripeti = conn.execute("SELECT extra, ripeti FROM locandine WHERE id = ?", (lid,)).fetchone()
        return pannello._json(True, "", modulo={
            "data": dati["data"].isoformat() if dati["data"] else None, "formule": dati["formula"],
            "cinque_x": bool(dati["cinque_x"]), "ora": dati["ora"], "quota": dati["quota"], "premi": dati["premi"],
            "max": dati["max_coppie"], "extra": extra or "", "ripeti": bool(ripeti), "parziale": dati["parziale"]})

    if az == "file":   # PNG originale nella chat privata col bot, da salvare o inoltrare
        if bot is None:
            return pannello._json(False, "Bot non disponibile.")
        dati = C.dati_salvati(conn, lid)
        file_id, nome = conn.execute("SELECT file_id, nome_torneo FROM locandine WHERE id = ?", (lid,)).fetchone()
        try:
            if not dati["parziale"]:
                png = await C.genera_png(dati)   # rigenerata: piena qualità
            else:   # locandina vecchia: quota e premi non salvati, uso l'immagine uscita
                png = bytes(await (await bot.get_file(file_id)).download_as_bytearray())
            nome_file = "locandina_" + "".join(c if c.isalnum() else "_" for c in (nome or str(lid))) + ".png"
            await bot.send_document(chat_id=uid, document=png, filename=nome_file,
                                    caption=f"Locandina n° {lid} {nome or ''}".strip())
        except Exception as e:
            return pannello._json(False, f"Non riesco a mandartela in chat ({e}). Apri la chat col bot e premi Avvia.")
        return pannello._json(True, "📥 Locandina mandata nella chat col bot.")

    if az == "testo":
        stato = conn.execute("SELECT stato FROM locandine WHERE id = ?", (lid,)).fetchone()[0]
        testo = str(d.get("testo") or "").strip()
        if stato == "pubblicata":   # come /didascalia: modifica il post nel gruppo
            esito = await C._modifica_pubblicata(bot, lid, testo or "nessuno")
            return pannello._json(esito.startswith("✅"), esito.split("\n")[0] if esito.startswith("✅") else esito)
        esito, _ = C.imposta_testo(lid, testo)
        if esito == "chiusa":
            return pannello._json(False, "Questa locandina non è più modificabile.")
        if esito == "lungo":
            return pannello._json(False, f"Troppo lungo: massimo circa {C.LIMITE_DIDASCALIA} caratteri. Accorcialo.")
        await _aggiorna_privato(req, conn, lid)
        return pannello._json(True, "✏️ Testo salvato.", bozza=_scheda(conn, lid))

    if az in ("nuovo_torneo", "ripeti"):
        if not C.modificabile(conn, lid):
            return pannello._json(False, "Questa locandina non è più modificabile.")
        conn.execute(f"UPDATE locandine SET {az} = ? WHERE id = ?", (int(bool(d.get("valore"))), lid))
        conn.commit()
        await _aggiorna_privato(req, conn, lid)
        return pannello._json(True, "", bozza=_scheda(conn, lid))

    if az == "pubblica":
        if bot is None:
            return pannello._json(False, "Bot non disponibile.")
        esito = await C._pubblica(bot, lid, jq)
        ok = esito.startswith("✅")
        if ok:
            for j in (jq.get_jobs_by_name(f"locandina_{lid}") if jq else []):   # era programmata
                j.schedule_removal()
            await _aggiorna_privato(req, conn, lid, chiudi=True)
        return pannello._json(ok, esito)

    if az == "programma":
        try:
            quando = datetime.fromisoformat(str(d.get("quando", ""))).replace(tzinfo=C.FUSO)
        except ValueError:
            return pannello._json(False, "Scegli giorno e ora.")
        if jq is None:
            return pannello._json(False, "Bot non disponibile.")
        ok, testo = C.programma(jq, lid, quando)
        if ok:
            await _aggiorna_privato(req, conn, lid, chiudi=True)
        return pannello._json(ok, testo)

    if az in ("annulla", "ferma"):
        testo = C.annulla(jq, lid, anche_bozza=(az == "annulla"))
        ok = not testo.startswith("Nessuna")
        if ok:
            await _aggiorna_privato(req, conn, lid, chiudi=True)
        return pannello._json(ok, testo)

    if az == "ripubblica":
        if bot is None:
            return pannello._json(False, "Bot non disponibile.")
        esito = await C.ripubblica_locandina(bot, jq, lid, bool(d.get("con_48h")))
        return pannello._json(esito.startswith("✅"), esito)

    return pannello._json(False, "Azione sconosciuta", 400)


def aggiungi_rotte(app: web.Application) -> None:
    """Dopo pannello.aggiungi_rotte (usa il suo controllo admin)."""
    app.router.add_get("/api/pannello/locandina", h_stato)
    app.router.add_get("/api/pannello/locandina/img", h_img)
    app.router.add_post("/api/pannello/locandina", h_azione)
