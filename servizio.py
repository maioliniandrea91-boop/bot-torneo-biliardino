"""Servizio: errori notificati agli admin e backup del database su un canale privato.

- Errori: ogni eccezione non gestita (bot, job, server dell'app) arriva in privato
  agli admin, al massimo una volta ogni 10 minuti per lo stesso errore.
- Backup: ogni giorno alle ORARIO_BACKUP il bot invia il file del database nel
  canale privato collegato, se qualcosa è cambiato dall'ultimo backup
  e comunque almeno uno a settimana (canale con cancellazione automatica a 1 mese).
  /backup -> backup subito (o istruzioni per collegare il canale)
  /backup canale -> collega un altro canale
- Ripristino: inoltra al bot (in privato) un file di backup .db dal canale →
  il bot lo controlla, mostra cosa contiene e chiede conferma; prima di
  sostituire fa un backup di sicurezza del database attuale, poi si riavvia.
"""
import asyncio
import hashlib
import os
import sqlite3
import tempfile
import time as _time
import traceback
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

ROMA = ZoneInfo("Europe/Rome")
ORARIO_BACKUP = time(hour=6, minute=0, tzinfo=ROMA)   # dopo la fine dei tornei (sab 05)
CHIAVE_CANALE = "backup_canale"
CHIAVE_HASH = "backup_hash"
CHIAVE_ULTIMO = "backup_ultimo"   # data dell'ultimo backup inviato
# Anche senza modifiche, un backup almeno ogni settimana: col canale a
# cancellazione automatica di 1 mese ne restano sempre almeno 4.
SETTIMANALE = timedelta(days=6, hours=12)
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
        if f"'{CHIAVE_HASH}'" not in riga and f"'{CHIAVE_ULTIMO}'" not in riga:
            h.update(riga.encode())
    return h.hexdigest()


async def esegui_backup(bot, forzato: bool = False, nota: str = "") -> str:
    conn = _db_connect()
    canale = _leggi(conn, CHIAVE_CANALE)
    if not canale:
        return "nessun canale"
    impronta = _impronta(conn)
    adesso = datetime.now(ROMA)
    invariato = impronta == _leggi(conn, CHIAVE_HASH)
    if not forzato and invariato:
        ultimo = _leggi(conn, CHIAVE_ULTIMO)
        if ultimo and adesso - datetime.fromisoformat(ultimo) < SETTIMANALE:
            return "invariato"
        nota = nota or "🗓 backup settimanale (nessuna modifica)"
    percorso = os.path.join(tempfile.mkdtemp(), f"biliardino_{adesso:%Y-%m-%d_%H%M}.db")
    dest = sqlite3.connect(percorso)
    with dest:
        sqlite3.connect(_db_path).backup(dest)   # copia coerente anche col bot in funzione
    dest.close()
    tornei = conn.execute("SELECT COUNT(*) FROM tornei").fetchone()[0]
    with open(percorso, "rb") as f:
        await bot.send_document(int(canale), f, filename=os.path.basename(percorso),
                                caption=f"💾 Backup {adesso:%d/%m/%Y %H:%M} · {tornei} tornei · "
                                        f"{os.path.getsize(percorso) // 1024} KB" + (f"\n{nota}" if nota else ""))
    os.remove(percorso)
    _scrivi(conn, CHIAVE_HASH, impronta)
    _scrivi(conn, CHIAVE_ULTIMO, adesso.isoformat())
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
        f"Da ora un backup al giorno alle {ORARIO_BACKUP:%H:%M}, se qualcosa è cambiato, e comunque almeno uno a settimana.")
    raise ApplicationHandlerStop


# ======================= RIPRISTINO =======================

def _riepilogo(percorso: str) -> str:
    """Controlla che il file sia un database del bot integro; ritorna un riepilogo.
    ValueError se non va bene."""
    try:
        c = sqlite3.connect(f"file:{percorso}?mode=ro", uri=True)
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("file danneggiato")
        tabelle = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"tornei", "squadre", "stato"} <= tabelle:
            raise ValueError("non è un database del bot")
        n_tornei = c.execute("SELECT COUNT(*) FROM tornei").fetchone()[0]
        ultimo = c.execute("SELECT nome, chiuso FROM tornei ORDER BY id DESC LIMIT 1").fetchone()
        squadre = c.execute("SELECT COUNT(*) FROM squadre WHERE torneo_id = "
                            "(SELECT MAX(id) FROM tornei)").fetchone()[0]
    except sqlite3.DatabaseError:
        raise ValueError("non è un database valido")
    righe = [f"• {n_tornei} tornei in archivio"]
    if ultimo:
        righe.append(f"• ultimo torneo: «{ultimo[0]}» ({'chiuso' if ultimo[1] else 'attivo'}, "
                     f"{squadre} coppie iscritte)")
    if "giocatori" in tabelle:
        righe.append(f"• {c.execute('SELECT COUNT(*) FROM giocatori').fetchone()[0]} giocatori in anagrafica")
    if "sfide" in tabelle:
        righe.append(f"• {c.execute('SELECT COUNT(*) FROM sfide').fetchone()[0]} sfidine registrate")
    return "\n".join(righe)


async def cmd_ripristina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id) or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "♻️ Per ripristinare: apri il canale dei backup, scegli il backup da recuperare "
        "e inoltralo qui. Ti mostro cosa contiene e ti chiedo conferma prima di toccare qualcosa.")


async def ricevi_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    doc = update.message.document
    percorso = os.path.join(tempfile.mkdtemp(), "ripristino.db")
    f = await context.bot.get_file(doc.file_id)
    await f.download_to_drive(percorso)
    try:
        riepilogo = _riepilogo(percorso)
    except ValueError as e:
        await update.message.reply_text(f"❌ Non posso usare questo file: {e}.")
        raise ApplicationHandlerStop
    context.user_data["ripristino"] = percorso
    await update.message.reply_text(
        f"♻️ Backup «{doc.file_name}»:\n{riepilogo}\n\n"
        "Se confermi, il database attuale viene SOSTITUITO da questo (prima ne salvo una copia "
        "nel canale) e il bot si riavvia (circa 1 minuto).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ripristina", callback_data="ripr:ok"),
            InlineKeyboardButton("Annulla", callback_data="ripr:no")]]))
    raise ApplicationHandlerStop


async def conferma_ripristino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    percorso = context.user_data.pop("ripristino", None)
    if not _is_admin(q.from_user.id) or q.data != "ripr:ok" or not percorso or not os.path.exists(percorso):
        await q.edit_message_text("Ripristino annullato." if q.data == "ripr:no"
                                  else "Ripristino non più valido: inoltra di nuovo il backup.")
        return
    conn = _db_connect()
    canale = _leggi(conn, CHIAVE_CANALE)
    try:
        sicurezza = await esegui_backup(context.bot, forzato=True, nota="⚠️ copia di sicurezza prima di un ripristino")
    except Exception as e:
        await q.edit_message_text(f"❌ Backup di sicurezza non riuscito ({e}): ripristino annullato.")
        return
    origine = sqlite3.connect(percorso)
    destinazione = sqlite3.connect(_db_path)
    origine.backup(destinazione)          # sostituisce il contenuto in modo atomico
    origine.close()
    destinazione.close()
    conn = _db_connect()
    if canale:                            # il canale dei backup resta quello attuale
        _scrivi(conn, CHIAVE_CANALE, canale)
    conn.execute("DELETE FROM stato WHERE chiave = ?", (CHIAVE_HASH,))
    conn.commit()
    await q.edit_message_text(
        f"✅ Database ripristinato (copia di sicurezza: {sicurezza}). Riavvio il bot: "
        "tra circa un minuto è di nuovo operativo.")
    # riavvio: Railway riavvia il servizio uscito con errore (riprogramma locandine, avvisi, ecc.)
    asyncio.get_running_loop().call_later(3, os._exit, 1)


COMANDI_SERVIZIO = [
    ("backup", "[admin] Backup del database nel canale privato (/backup canale per cambiarlo)"),
    ("ripristina", "[admin] Come ripristinare un backup (inoltra il file .db al bot)"),
]


def registra(app, db_connect, db_path, is_admin, admin_ids):
    global _db_connect, _db_path, _is_admin, _admin_ids
    _db_connect, _db_path, _is_admin, _admin_ids = db_connect, db_path, is_admin, set(admin_ids)
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("ripristina", cmd_ripristina))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.FileExtension("db"),
                                   ricevi_backup), group=-2)
    app.add_handler(CallbackQueryHandler(conferma_ripristino, pattern=r"^ripr:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.FORWARDED, ricevi_inoltro), group=-1)
    app.add_error_handler(gestore_errori)
    app.job_queue.run_daily(job_backup, time=ORARIO_BACKUP, name="backup_giornaliero")
