"""Sondaggio settimanale del gioco libero (lunedì) e registrazione del gruppo:
/registragruppo, /sondaggio, /vedisondaggio, /anteprimasondaggio, /modificasondaggio."""
import json

from telegram import Update
from telegram.ext import ContextTypes

from config import (CHIAVE_CHAT_GIOCO_LIBERO, CHIAVE_DOMANDA_SONDAGGIO, CHIAVE_OPZIONI_SONDAGGIO,
                    DOMANDA_SONDAGGIO_DEFAULT, OPZIONI_SONDAGGIO_DEFAULT,
                    ORARIO_SONDAGGIO_GIOCO_LIBERO, is_admin)
from database import chat_gruppo_registrato, db_connect, leggi_stato, scrivi_stato

SOLO_ADMIN = "Comando riservato agli admin."


def ottieni_config_sondaggio(conn) -> tuple[str, list[str]]:
    """Domanda/opzioni salvate; se mai personalizzate, i default di config.py."""
    domanda = leggi_stato(conn, CHIAVE_DOMANDA_SONDAGGIO)
    opzioni = leggi_stato(conn, CHIAVE_OPZIONI_SONDAGGIO)
    return (domanda if domanda is not None else DOMANDA_SONDAGGIO_DEFAULT,
            json.loads(opzioni) if opzioni is not None else list(OPZIONI_SONDAGGIO_DEFAULT))


def imposta_config_sondaggio(conn, domanda: str, opzioni: list[str]):
    scrivi_stato(conn, CHIAVE_DOMANDA_SONDAGGIO, domanda, commit=False)
    scrivi_stato(conn, CHIAVE_OPZIONI_SONDAGGIO, json.dumps(opzioni))


async def invia_sondaggio_gioco_libero(bot, chat_id: int, domanda: str, opzioni: list[str]):
    """Poll non anonimo: Telegram mostra da solo chi ha votato cosa,
    non serve salvare nulla nel DB."""
    await bot.send_poll(
        chat_id=chat_id,
        question=domanda,
        options=opzioni,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def vedisondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    domanda, opzioni = ottieni_config_sondaggio(db_connect())
    testo_opzioni = "\n".join(f"  • {o}" for o in opzioni)
    await update.message.reply_text(
        f"📋 Domanda attuale:\n{domanda}\n\nOpzioni:\n{testo_opzioni}\n\n"
        "Per vederlo renderizzato come poll vero: /anteprimasondaggio\n"
        "Per cambiarlo: /modificasondaggio Domanda | Opzione1 | Opzione2"
    )


async def anteprimasondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manda il poll in privato a chi lo richiede, così lo si vede come apparirà."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    domanda, opzioni = ottieni_config_sondaggio(db_connect())
    try:
        await invia_sondaggio_gioco_libero(context.bot, update.effective_user.id, domanda, opzioni)
        if update.effective_chat.type != "private":
            await update.message.reply_text("Anteprima inviata in privato, controlla la chat col bot.")
    except Exception:
        await update.message.reply_text(
            "Non riesco a scriverti in privato: apri prima una chat diretta col bot "
            "(cercalo su Telegram e premi Start), poi riprova."
        )


async def modificasondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    testo_args = " ".join(context.args) if context.args else ""
    parti = [p.strip() for p in testo_args.split("|") if p.strip()]
    if len(parti) < 3:
        domanda, opzioni = ottieni_config_sondaggio(db_connect())
        await update.message.reply_text(
            "Uso corretto: /modificasondaggio Domanda | Opzione1 | Opzione2 [| Opzione3 ...]\n"
            "Servono una domanda e almeno 2 opzioni (max 10, limite di Telegram).\n\n"
            f"Attuale — Domanda: {domanda}\nOpzioni: {', '.join(opzioni)}"
        )
        return
    domanda, *opzioni = parti
    if len(opzioni) > 10:
        await update.message.reply_text("Troppe opzioni: Telegram permette al massimo 10 per un sondaggio.")
        return
    imposta_config_sondaggio(db_connect(), domanda, opzioni)
    await update.message.reply_text(
        f"✅ Sondaggio aggiornato.\nDomanda: {domanda}\nOpzioni: {', '.join(opzioni)}\n\n"
        "Verifica con /anteprimasondaggio prima che parta davvero lunedì."
    )


async def registragruppo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin, UNA TANTUM dentro il gruppo giusto: lo registra come destinazione di
    sondaggio, podio e locandina."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Usa questo comando dentro il gruppo da registrare, non in privato.")
        return
    scrivi_stato(db_connect(), CHIAVE_CHAT_GIOCO_LIBERO, str(update.effective_chat.id))
    await update.message.reply_text(
        "✅ Questo gruppo è registrato. Ogni lunedì alle "
        f"{ORARIO_SONDAGGIO_GIOCO_LIBERO.strftime('%H:%M')} partirà qui il sondaggio "
        "del gioco libero. Puoi anche lanciarlo a mano con /sondaggio."
    )


async def sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: invia subito il sondaggio nel gruppo registrato (anche dal privato)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(SOLO_ADMIN)
        return
    conn = db_connect()
    destinazione = chat_gruppo_registrato(conn)
    if destinazione is None:
        await update.message.reply_text(
            "Nessun gruppo registrato. Vai nel gruppo giusto e manda /registragruppo (una tantum)."
        )
        return
    domanda, opzioni = ottieni_config_sondaggio(conn)
    await invia_sondaggio_gioco_libero(context.bot, destinazione, domanda, opzioni)
    if update.effective_chat.id != destinazione:
        await update.message.reply_text("Sondaggio inviato nel gruppo registrato.")


async def sondaggio_automatico(context: ContextTypes.DEFAULT_TYPE):
    """Gira ogni lunedì all'orario impostato."""
    conn = db_connect()
    destinazione = chat_gruppo_registrato(conn)
    if destinazione is not None:
        domanda, opzioni = ottieni_config_sondaggio(conn)
        await invia_sondaggio_gioco_libero(context.bot, destinazione, domanda, opzioni)
