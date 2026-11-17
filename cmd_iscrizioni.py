"""Comandi delle iscrizioni al torneo attivo:
/iscrivi, /squadre, /ritira, /rimuovi, /rinomina, /soglia, /chiudi, /apri, /esporta, /reset.
La logica sta in logica_tornei: qui solo lettura del comando e risposta in chat."""
import io

from docx import Document
from telegram import Update
from telegram.ext import ContextTypes

import comando_locandina
import logica_tornei as L
from chat_telegram import pubblica_lista, rispondi
from config import is_admin
from database import db_connect, get_torneo_attivo, iscrizioni_aperte, set_iscrizioni

SOLO_ADMIN = "Comando riservato agli admin."


async def iscrivi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    user = update.effective_user
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    if not iscrizioni_aperte(conn):
        await rispondi(update, context, L.TESTO_ISCRIZIONI_CHIUSE)
        return
    if not context.args:
        await rispondi(update, context, "Uso corretto: /iscrivi NomeSquadra")
        return

    esito = L.iscrivi_coppia(conn, user.id, user.username or user.first_name,
                             " ".join(context.args), is_admin(user.id))
    if not esito.ok:
        await rispondi(update, context, esito.testo)
        return
    await pubblica_lista(update, context, conn, torneo_id, nome_torneo, esito.testo)


async def squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    await pubblica_lista(update, context, conn, torneo_id, nome_torneo)


async def ritira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    nome_indicato = " ".join(context.args).strip() if context.args else None
    esito = L.ritira_coppia(conn, update.effective_user.id, nome_indicato)
    if not esito.ok:
        await rispondi(update, context, esito.testo)
        return
    await pubblica_lista(update, context, conn, torneo_id, nome_torneo, esito.testo)


async def rimuovi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: rimuove QUALSIASI squadra dal torneo attivo, indipendentemente da chi
    l'ha iscritta. Diverso da /ritira, che è self-service e tocca solo le proprie."""
    if not is_admin(update.effective_user.id):
        await rispondi(update, context, SOLO_ADMIN)
        return
    if not context.args:
        await rispondi(update, context, "Uso corretto: /rimuovi NomeSquadra")
        return
    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    esito = L.rimuovi_coppia(conn, " ".join(context.args).strip())
    if not esito.ok:
        await rispondi(update, context, esito.testo)
        return
    await pubblica_lista(update, context, conn, torneo_id, nome_torneo, esito.testo)


async def rinomina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: corregge il nome di una squadra del torneo attivo.
      /rinomina 3 | Antonio-Eva          (3 = numero in /squadre)
      /rinomina Antonio e Eva | Antonio-Eva"""
    if not is_admin(update.effective_user.id):
        await rispondi(update, context, SOLO_ADMIN)
        return
    testo_args = " ".join(context.args) if context.args else ""
    parti = [p.strip() for p in testo_args.split("|")]
    if len(parti) != 2 or not parti[0] or not parti[1]:
        await rispondi(update, context,
                       "Uso: /rinomina NUMERO | Nuovo nome  (il numero è quello in /squadre)\n"
                       "oppure: /rinomina Nome attuale | Nuovo nome\n"
                       "Es. /rinomina 3 | Antonio-Eva")
        return
    conn = db_connect()
    esito = L.rinomina_coppia(conn, parti[0], parti[1])
    if not esito.ok:
        await rispondi(update, context, esito.testo)
        return
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    await pubblica_lista(update, context, conn, torneo_id, nome_torneo, esito.testo)


async def soglia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: coppie minime per confermare il torneo."""
    if not is_admin(update.effective_user.id):
        await rispondi(update, context, SOLO_ADMIN)
        return
    conn = db_connect()
    if not context.args:
        attuale = comando_locandina.get_soglia(conn)
        await rispondi(update, context, f"Soglia attuale: {attuale} coppie. Per cambiarla: /soglia N")
        return
    await rispondi(update, context, L.imposta_soglia(conn, context.args[0]).testo)


async def chiudi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    set_iscrizioni(db_connect(), False)
    await update.message.reply_text("🔒 Iscrizioni chiuse.")


async def apri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    set_iscrizioni(db_connect(), True)
    await update.message.reply_text("🔓 Iscrizioni riaperte.")


async def esporta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)

    documento = Document()
    documento.add_heading(nome_torneo, level=1)
    for _, nome_squadra in L.squadre_torneo(conn, torneo_id):
        documento.add_paragraph(nome_squadra)
    buffer = io.BytesIO()
    documento.save(buffer)
    buffer.seek(0)
    buffer.name = f"squadre_{nome_torneo}.docx"

    await context.bot.send_document(
        chat_id=update.effective_user.id,
        document=buffer,
        filename=f"squadre_{nome_torneo}.docx",
    )
    await update.message.reply_text("Elenco squadre inviato in privato (Word).")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    if not context.args or context.args[0] != "CONFERMA":
        await update.message.reply_text(
            "Questo cancella le iscrizioni del torneo ATTIVO (lo storico resta intatto). "
            "Per confermare: /reset CONFERMA"
        )
        return
    await update.message.reply_text(L.svuota_torneo_attivo(db_connect()).testo)

