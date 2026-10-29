"""Servizio: errori notificati agli admin e backup del database su un canale privato.

- Errori: ogni eccezione non gestita (bot, job, server dell'app) arriva in privato
  agli admin, al massimo una volta ogni 10 minuti per lo stesso errore.
- Backup: ogni giorno alle ORARIO_BACKUP il bot invia il file del database nel
  canale privato collegato, solo se qualcosa è cambiato dall'ultimo backup.
  /backup -> backup subito (o istruzioni per collegare il canale)
  /backup canale -> collega un altro canale
"""
import hashlib
import os
import sqlite3
import tempfile
import time as _time
import traceback
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import ApplicationHandlerStop, CommandHandler, ContextTypes, MessageHandler, filters

ROMA = ZoneInfo("Europe/Rome")
ORARIO_BACKUP = time(hour=6, minute=0, tzinfo=ROMA)   # dopo la fine dei tornei (sab 05)
CHIAVE_CANALE = "backup_canale"
CHIAVE_HASH = "backup_hash"
SILENZIO_ERRORE_S = 600

_db_connect = None
_db_path = None
_is_admin = None
_admin_ids: set[int] = set()
_ultimi_errori: dict[str, float] = {}


# ======================= ERRORI =======================

async def notifica_errore(bot, dove: str, exc: BaseException) -> None:
    traccia = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"ERRORE ({dove}):\n{traccia}")
    firma = f"{dove}|{type(exc).__name__}|{exc}"
    adesso = _time.monotonic()
    if adesso - _ultimi_errori.get(firma, -SILENZIO_ERRORE_S) < SILENZIO_ERRORE_S:
        return
    _ultimi_errori[firma] = adesso
    testo = (f"⚠️ Errore nel bot ({dove}) alle {datetime.now(ROMA):%H:%M}\n"
             f"{type(exc).__name__}: {exc}\n\n…{traccia[-2500:]}")
    for a in _admin_ids:
        try:
            await bot.send_message(a, testo[:4000])
        except Exception:
            pass


async def gestore_errori(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    e = context.error
    # rete instabile e doppia istanza durante un deploy: normali, non si segnalano
    if isinstance(e, (NetworkError, TimedOut, Conflict)):
        print("Errore di rete ignorato:", e)
        return
    dove = "comando"
    if isinstance(update, Update) and update.effective_message and update.effective_message.text:
        dove = f"messaggio «{update.effective_message.text[:40]}»"
    elif update is None:
        dove = "attività programmata"
    await notifica_errore(context.bot, dove, e)


# ======================= BACKUP =======================

def _leggi(conn, chiave):
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (chiave,)).fetchone()
    return row[0] if row else None


def _scrivi(conn, chiave, valore):
    conn.execute("INSERT INTO stato (chiave, valore) VALUES (?, ?) "
                 "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore", (chiave, valore))
    conn.commit()


def _impronta(conn) -> str:
    """Impronta del contenuto (esclusa la riga con l'impronta stessa)."""
    h = hashlib.sha256()
    for riga in conn.iterdump():
        if f"'{CHIAVE_HASH}'" not in riga:
            h.update(riga.encode())
    return h.hexdigest()


async def esegui_backup(bot, forzato: bool = False) -> str:
    conn = _db_connect()
    canale = _leggi(conn, CHIAVE_CANALE)
    if not canale:
        return "nessun canale"
    impronta = _impronta(conn)
    if not forzato and impronta == _leggi(conn, CHIAVE_HASH):
        return "invariato"
    adesso = datetime.now(ROMA)
    percorso = os.path.join(tempfile.mkdtemp(), f"biliardino_{adesso:%Y-%m-%d_%H%M}.db")
    dest = sqlite3.connect(percorso)
    with dest:
        sqlite3.connect(_db_path).backup(dest)   # copia coerente anche col bot in funzione
    dest.close()
    tornei = conn.execute("SELECT COUNT(*) FROM tornei").fetchone()[0]
    with open(percorso, "rb") as f:
        await bot.send_document(int(canale), f, filename=os.path.basename(percorso),
                                caption=f"💾 Backup {adesso:%d/%m/%Y %H:%M} · {tornei} tornei · "
                                        f"{os.path.getsize(percorso) // 1024} KB")
    os.remove(percorso)
    _scrivi(conn, CHIAVE_HASH, impronta)
    return "inviato"


async def job_backup(context: ContextTypes.DEFAULT_TYPE):
    await esegui_backup(context.bot)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id) or update.effective_chat.type != "private":
        return
    conn = _db_connect()
    if not _leggi(conn, CHIAVE_CANALE) or (context.args and context.args[0].lower() == "canale"):
        context.user_data["attesa_canale_backup"] = True
        await update.message.reply_text(
            "💾 Per collegare il canale dei backup:\n"
            "1. Crea un canale Telegram privato (solo tu).\n"
            "2. Aggiungi il bot come amministratore con il permesso di pubblicare.\n"
            "3. Scrivi un messaggio qualsiasi nel canale e inoltralo qui.")
        return
    esito = await esegui_backup(context.bot, forzato=True)
    await update.message.reply_text("💾 Backup inviato nel canale." if esito == "inviato"
                                    else f"Backup non inviato ({esito}).")


async def ricevi_inoltro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Messaggio inoltrato dal canale: ne ricava l'id e lo collega."""
    if not context.user_data.get("attesa_canale_backup") or not _is_admin(update.effective_user.id):
        return
    origine = update.message.forward_origin
    chat = getattr(origine, "chat", None)
    if chat is None or chat.type != "channel":
        await update.message.reply_text("Serve un messaggio inoltrato da un canale. Riprova.")
        raise ApplicationHandlerStop
    try:
        await context.bot.send_message(chat.id, "✅ Canale collegato: qui arriveranno i backup del bot.")
    except Exception:
        await update.message.reply_text(
            "Non riesco a scrivere nel canale: aggiungi il bot come amministratore "
            "con il permesso di pubblicare, poi inoltrami di nuovo un messaggio.")
        raise ApplicationHandlerStop
    conn = _db_connect()
    _scrivi(conn, CHIAVE_CANALE, str(chat.id))
    context.user_data.pop("attesa_canale_backup", None)
    esito = await esegui_backup(context.bot, forzato=True)
    await update.message.reply_text(
        f"✅ Canale «{chat.title}» collegato. Primo backup {esito}. "
        f"Da ora un backup al giorno alle {ORARIO_BACKUP:%H:%M}, solo se qualcosa è cambiato.")
    raise ApplicationHandlerStop


COMANDI_SERVIZIO = [("backup", "[admin] Backup del database nel canale privato (/backup canale per cambiarlo)")]


def registra(app, db_connect, db_path, is_admin, admin_ids):
    global _db_connect, _db_path, _is_admin, _admin_ids
    _db_connect, _db_path, _is_admin, _admin_ids = db_connect, db_path, is_admin, set(admin_ids)
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.FORWARDED, ricevi_inoltro), group=-1)
    app.add_error_handler(gestore_errori)
    app.job_queue.run_daily(job_backup, time=ORARIO_BACKUP, name="backup_giornaliero")
