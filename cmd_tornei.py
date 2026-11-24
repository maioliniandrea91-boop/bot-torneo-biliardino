"""Comandi dei tornei: /torneo, /nometorneo, /storico, /eliminatorneo, /podio
(+ bottoni dell'anteprima del podio). La logica sta in logica_tornei."""
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import logica_tornei as L
from config import is_admin
from database import chat_gruppo_registrato, db_connect, get_torneo_attivo

SOLO_ADMIN = "Comando riservato agli admin."
NESSUN_GRUPPO = "Nessun gruppo registrato: scrivi /registragruppo dentro il gruppo, poi riprova."


async def torneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, nome_torneo = get_torneo_attivo(db_connect())
    await update.message.reply_text(f"🏓 Torneo attuale: #{tid} {nome_torneo}")


async def nometorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    conn = db_connect()
    if len(context.args) < 2 or context.args[-1] != "CONFERMA":
        _, attivo = get_torneo_attivo(conn)
        await update.message.reply_text(
            f"Questo chiude SUBITO il torneo attivo ('{attivo}') per tutti "
            "e ne apre uno nuovo.\n"
            "Uso corretto: /nometorneo Nome Del Torneo CONFERMA"
        )
        return
    esito = L.apri_nuovo_torneo(conn, " ".join(context.args[:-1]).strip())
    await update.message.reply_text(esito.testo)


async def storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/storico [pagina] -> elenco; /storico #N o Nome -> dettagli di un torneo."""
    conn = db_connect()
    if not context.args or (len(context.args) == 1 and context.args[0].isdigit()):
        pagina = int(context.args[0]) if context.args else 1
        await update.message.reply_text(L.testo_pagina_tornei(L.pagina_tornei(conn, pagina)))
        return
    await update.message.reply_text(L.dettaglio_torneo(conn, " ".join(context.args).strip()).testo)


async def eliminatorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: elimina definitivamente un torneo ARCHIVIATO dallo storico.
    /eliminatorneo #N (o Nome) -> riepilogo; + CONFERMA -> elimina.
    Il torneo attivo non si elimina, per sicurezza."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    args = list(context.args or [])
    conferma = bool(args) and args[-1] == "CONFERMA"
    if conferma:
        args = args[:-1]
    cercato = " ".join(args).strip()
    if not cercato:
        await update.message.reply_text(
            "Questo elimina PER SEMPRE un torneo dallo storico (squadre e podio inclusi).\n"
            "Uso: /eliminatorneo #N (numero in /storico) per vedere cosa elimina, "
            "poi /eliminatorneo #N CONFERMA.\n"
            "Funziona anche con il nome: /eliminatorneo NomeTorneo CONFERMA"
        )
        return
    conn = db_connect()
    esito = L.elimina_torneo(conn, cercato) if conferma else L.esamina_eliminazione(conn, cercato)
    await update.message.reply_text(esito.testo)


async def podio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/podio [#N] Sq1 | Sq2 | Sq3 [| descrizione]
    Come testo, in risposta a una foto o nella didascalia di una foto.
    Nel gruppo pubblica subito; in privato mostra l'anteprima con i bottoni."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    msg = update.message
    if msg.caption:
        # comando scritto nella didascalia della foto: tolgo "/podio" (o "/podio@NomeBot")
        testo_args = re.sub(r"^/podio(?:@\w+)?", "", msg.caption, count=1, flags=re.IGNORECASE)
    else:
        testo_args = " ".join(context.args) if context.args else ""
    num_torneo, squadre, descrizione = L.leggi_podio(testo_args)
    if squadre is None:
        await update.message.reply_text(
            "Uso corretto: /podio [#N] Squadra1 | Squadra2 | Squadra3 | descrizione (facoltativa)\n"
            "#N = numero del torneo in /storico (senza: torneo attivo).\n"
            "Per allegare la foto: scrivi il comando nella didascalia della foto, "
            "oppure rispondi alla foto con il comando."
        )
        return

    foto_file_id = None
    if msg.photo:
        foto_file_id = msg.photo[-1].file_id
    elif msg.reply_to_message and msg.reply_to_message.photo:
        foto_file_id = msg.reply_to_message.photo[-1].file_id

    conn = db_connect()
    privato = update.effective_chat.type == "private"
    esito = L.prepara_podio(conn, num_torneo, squadre, descrizione, foto_file_id,
                            sostituzione_ammessa=privato)
    if not esito.ok:
        await update.message.reply_text(esito.testo)
        return
    dati = esito.dati["podio"]

    if not privato:
        await pubblica_podio(context.bot, conn, update.effective_chat.id, dati)
        return

    # in privato: anteprima con bottoni, poi pubblicazione nel gruppo registrato
    if not chat_gruppo_registrato(conn):
        await update.message.reply_text(NESSUN_GRUPPO)
        return
    # codice dell'anteprima: i bottoni di un'anteprima vecchia non pubblicano quella nuova
    dati["codice"] = datetime.now().strftime("%H%M%S%f")
    context.user_data["podio_attesa"] = dati
    tastiera = InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Pubblica nel gruppo", callback_data=f"pod:ok:{dati['codice']}"),
        InlineKeyboardButton("❌ Annulla", callback_data=f"pod:no:{dati['codice']}"),
    ]])
    if esito.testo:   # avviso: c'è già un podio, verrà sostituito
        await update.message.reply_text(esito.testo)
    if foto_file_id:
        await update.message.reply_photo(photo=foto_file_id, caption=dati["caption"], reply_markup=tastiera)
    else:
        await update.message.reply_text(dati["caption"], reply_markup=tastiera)


async def pubblica_podio(bot, conn, chat_id: int, dati: dict):
    """Pubblica il podio in chat_id, poi lo salva sul torneo (se Telegram fallisce non cambia nulla).
    Se il torneo aveva già un podio pubblicato, il post vecchio viene tolto dal gruppo
    (Telegram lo permette solo entro 48 ore: dopo va tolto a mano)."""
    vecchio = conn.execute("SELECT podio_msg FROM tornei WHERE id = ?", (dati["torneo_id"],)).fetchone()
    if dati["foto"]:
        msg = await bot.send_photo(chat_id=chat_id, photo=dati["foto"], caption=dati["caption"])
        dati["foto"] = msg.photo[-1].file_id
    else:
        msg = await bot.send_message(chat_id=chat_id, text=dati["caption"])
    dati["msg"] = msg.message_id
    L.salva_podio(conn, dati)
    if vecchio and vecchio[0] and vecchio[0] != msg.message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=vecchio[0])
        except Exception:
            pass   # già tolto a mano o più vecchio di 48 ore


async def bottoni_podio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bottoni dell'anteprima del podio in privato."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Riservato agli admin.", show_alert=True)
        return
    _, azione, codice = (query.data.split(":") + ["", ""])[:3]
    dati = context.user_data.get("podio_attesa")
    if dati is not None and dati.get("codice") != codice:
        dati = None  # bottone di un'anteprima vecchia
    else:
        context.user_data.pop("podio_attesa", None)
    if azione == "no":
        esito = "❌ Podio annullato, non è stato pubblicato."
    elif dati is None:
        esito = "⚠️ Anteprima non più valida (già usata, sostituita da una più recente o bot riavviato). Rimanda /podio."
    else:
        conn = db_connect()
        chat_id = chat_gruppo_registrato(conn)
        if not chat_id:
            esito = NESSUN_GRUPPO
        else:
            try:
                await pubblica_podio(context.bot, conn, chat_id, dati)
                esito = "✅ Podio pubblicato nel gruppo."
            except Exception as e:
                context.user_data["podio_attesa"] = dati  # così puoi riprovare
                esito = f"⚠️ Pubblicazione non riuscita ({e}). Riprova tra poco."
    await query.answer()
    # tolgo i bottoni e aggiungo l'esito sotto l'anteprima
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n" + esito)
        else:
            await query.edit_message_text(text=(query.message.text or "") + "\n\n" + esito)
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=esito)
