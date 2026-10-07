"""
Comando /locandina — genera la locandina del torneo, te la mostra in privato
e la pubblica nel gruppo subito o all'orario che scegli tu.

FLUSSO (solo admin, in chat privata col bot):
  /locandina 16/10 volo                 -> data + formula (obbligatori)
  /locandina 16/10 rollerball 22:00 25€ -> ora e quota opzionali
  /locandina 16/10 tradizionale premi COPPE -> "premi ..." sempre in fondo
Il bot risponde con l'anteprima e quattro bottoni:
  📢 Pubblica ora   ⏰ Programma   ✏️ Aggiungi testo   ❌ Annulla
Con "Programma" ti chiede quando: es. 14/10 18:00, oppure domani 18:00.
Con "Aggiungi testo" scrivi qualcosa che va SOTTO la didascalia standard.

Altri comandi:
  /programmate            -> elenco locandine in attesa di pubblicazione
  /annullalocandina N     -> annulla la pubblicazione programmata n° N

Le programmazioni sono salvate nel database: se il bot si riavvia
(es. nuovo deploy su Railway) vengono ripristinate da sole.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from locandina import FORMULE, genera_locandina, testo_data

FUSO = ZoneInfo("Europe/Rome")
ORA_DEFAULT = "21:30"
QUOTA_DEFAULT = "20€"
PREMI_DEFAULT = "BV"
# Se il bot era spento all'orario previsto, pubblica comunque al riavvio
# solo se il ritardo è sotto questa soglia; oltre, avvisa e non pubblica.
RITARDO_MASSIMO = timedelta(hours=6)

# Vengono impostati da registra() con le funzioni del file principale
_db_connect = None
_is_admin = None
_chiave_gruppo = None
_admin_ids = set()


def _crea_tabella(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locandine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT,
            didascalia TEXT NOT NULL,
            stato TEXT NOT NULL,          -- bozza | programmata | pubblicata | annullata | scaduta
            pubblica_il TEXT,             -- ISO con fuso orario
            creato_il TEXT NOT NULL
        )
    """)
    colonne = [r[1] for r in conn.execute("PRAGMA table_info(locandine)")]
    if "extra" not in colonne:
        conn.execute("ALTER TABLE locandine ADD COLUMN extra TEXT")  # testo aggiunto a mano
    conn.commit()


def _conn():
    conn = _db_connect()
    _crea_tabella(conn)
    return conn


# ---------- lettura dati dal comando ----------

def _prossima_data(giorno: int, mese: int, anno: int | None) -> date:
    oggi = datetime.now(FUSO).date()
    if anno is not None:
        if anno < 100:
            anno += 2000
        return date(anno, mese, giorno)
    d = date(oggi.year, mese, giorno)
    if d < oggi:
        d = date(oggi.year + 1, mese, giorno)
    return d


def leggi_argomenti(args: list[str]):
    """Ritorna (dati, errore). dati = dict con data, formula, ora, quota, premi."""
    if not args:
        return None, None
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", args[0])
    if not m:
        return None, f"Non capisco la data '{args[0]}'. Scrivila così: 16/10"
    try:
        d = _prossima_data(int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    except ValueError:
        return None, f"La data '{args[0]}' non esiste."

    ora, quota, premi, formula = ORA_DEFAULT, QUOTA_DEFAULT, PREMI_DEFAULT, None
    resto = args[1:]
    if any(t.lower() == "premi" for t in resto):
        i = [t.lower() for t in resto].index("premi")
        premi = " ".join(resto[i + 1:]).strip().upper() or PREMI_DEFAULT
        resto = resto[:i]

    for t in resto:
        tl = t.lower()
        if re.fullmatch(r"\d{1,2}[:.]\d{2}", tl):
            h, mi = re.split(r"[:.]", tl)
            if int(h) > 23 or int(mi) > 59:
                return None, f"Orario '{t}' non valido."
            ora = f"{int(h):02d}:{mi}"
        elif re.fullmatch(r"\d+(?:[.,]\d+)?(?:€|euro)", tl):
            quota = re.sub(r"(?:€|euro)$", "", tl) + "€"
        elif tl in FORMULE:
            formula = tl
        elif tl in ("3", "tocchi"):
            formula = "rollerball"
        else:
            return None, f"Non capisco '{t}'."

    if formula is None:
        return None, "Manca la formula: scrivi rollerball, volo o tradizionale."
    return {"data": d, "formula": formula, "ora": ora, "quota": quota, "premi": premi}, None


def leggi_quando(testo: str):
    """'14/10 18:00', 'oggi 18:00', 'domani 18:00' -> datetime con fuso."""
    t = testo.strip().lower()
    adesso = datetime.now(FUSO)
    m = re.fullmatch(r"(oggi|domani|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?)\s+(?:ore\s+)?(\d{1,2})[:.](\d{2})", t)
    if not m:
        return None
    giorno, h, mi = m.groups()
    if giorno == "oggi":
        d = adesso.date()
    elif giorno == "domani":
        d = adesso.date() + timedelta(days=1)
    else:
        p = re.split(r"[/.-]", giorno)
        try:
            d = _prossima_data(int(p[0]), int(p[1]), int(p[2]) if len(p) > 2 else None)
        except ValueError:
            return None
    try:
        return datetime(d.year, d.month, d.day, int(h), int(mi), tzinfo=FUSO)
    except ValueError:
        return None


def _didascalia(dati) -> str:
    return (
        f"🏆 Torneo di biliardino — {testo_data(dati['data'], dati['ora']).capitalize()}\n"
        "Iscrivi la tua coppia con /iscrivi NomeSquadra"
    )


def _gruppo(conn):
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (_chiave_gruppo,)).fetchone()
    return int(row[0]) if row else None


# ---------- comandi ----------

async def locandina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usa /locandina in chat privata col bot.")
        return

    dati, errore = leggi_argomenti(context.args)
    if dati is None:
        await update.message.reply_text(
            (errore + "\n\n" if errore else "")
            + "Uso: /locandina 16/10 volo\n"
            "Opzionali: ora (22:00), quota (25€), premi in fondo (premi COPPE)\n"
            "Formule: rollerball, volo, tradizionale\n"
            f"Se non li scrivi: ore {ORA_DEFAULT}, {QUOTA_DEFAULT} a coppia, premi in {PREMI_DEFAULT}."
        )
        return

    png = await asyncio.to_thread(
        genera_locandina, dati["data"], dati["formula"], dati["ora"], dati["quota"], dati["premi"]
    )
    conn = _conn()
    didascalia = _didascalia(dati)
    cur = conn.execute(
        "INSERT INTO locandine (didascalia, stato, creato_il) VALUES (?, 'bozza', ?)",
        (didascalia, datetime.now(FUSO).isoformat()),
    )
    conn.commit()
    lid = cur.lastrowid

    msg = await update.message.reply_photo(
        photo=png,
        caption=_caption_anteprima(lid, didascalia),
        reply_markup=_tastiera(lid),
    )
    conn.execute("UPDATE locandine SET file_id = ? WHERE id = ?", (msg.photo[-1].file_id, lid))
    conn.commit()


LIMITE_DIDASCALIA = 1024  # limite Telegram per la didascalia di una foto


def _tastiera(lid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Pubblica ora", callback_data=f"loc:ora:{lid}"),
         InlineKeyboardButton("⏰ Programma", callback_data=f"loc:prog:{lid}")],
        [InlineKeyboardButton("✏️ Aggiungi testo", callback_data=f"loc:testo:{lid}"),
         InlineKeyboardButton("❌ Annulla", callback_data=f"loc:no:{lid}")],
    ])


def _testo_completo(didascalia: str, extra: str | None) -> str:
    return f"{didascalia}\n\n{extra}" if extra else didascalia


def _caption_anteprima(lid: int, testo: str) -> str:
    return f"Anteprima n° {lid}\n\nDidascalia nel gruppo:\n{testo}"


async def _pubblica(bot, lid: int) -> str:
    conn = _conn()
    row = conn.execute(
        "SELECT file_id, didascalia, stato, extra FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    if not row:
        return f"Locandina n° {lid} non trovata."
    file_id, didascalia, stato, extra = row
    didascalia = _testo_completo(didascalia, extra)
    if stato in ("pubblicata", "annullata"):
        return f"La locandina n° {lid} è già {stato}."
    gruppo = _gruppo(conn)
    if gruppo is None:
        return "Nessun gruppo registrato: manda /registragruppo dentro il gruppo del circolo."
    await bot.send_photo(chat_id=gruppo, photo=file_id, caption=didascalia)
    conn.execute("UPDATE locandine SET stato = 'pubblicata' WHERE id = ?", (lid,))
    conn.commit()
    return f"✅ Locandina n° {lid} pubblicata nel gruppo."


async def _job_pubblica(context: ContextTypes.DEFAULT_TYPE):
    lid = context.job.data
    esito = await _pubblica(context.bot, lid)
    for admin in _admin_ids:
        try:
            await context.bot.send_message(admin, esito)
        except Exception:
            pass


def _programma_job(job_queue, lid: int, quando: datetime):
    for j in job_queue.get_jobs_by_name(f"locandina_{lid}"):
        j.schedule_removal()
    job_queue.run_once(_job_pubblica, when=quando, data=lid, name=f"locandina_{lid}")


async def bottoni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("Riservato agli admin.", show_alert=True)
        return
    await q.answer()
    _, azione, lid = q.data.split(":")
    lid = int(lid)

    if azione == "ora":
        esito = await _pubblica(context.bot, lid)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(esito)
    elif azione == "prog":
        context.user_data["locandina_attesa"] = ("prog", lid)
        await q.message.reply_text(
            "Quando la pubblico? Scrivi ad esempio:\n14/10 18:00\noggi 20:30\ndomani 18:00\n"
            "(oppure 'annulla')"
        )
    elif azione == "testo":
        context.user_data["locandina_attesa"] = ("testo", lid)
        await q.message.reply_text(
            "Scrivi il testo da aggiungere sotto la didascalia (anche su più righe).\n"
            "Se lo riscrivi, sostituisce quello aggiunto prima.\n"
            "'nessuno' toglie il testo aggiunto, 'annulla' lascia tutto com'è."
        )
    elif azione == "no":
        conn = _conn()
        conn.execute("UPDATE locandine SET stato = 'annullata' WHERE id = ?", (lid,))
        conn.commit()
        for j in context.job_queue.get_jobs_by_name(f"locandina_{lid}"):
            j.schedule_removal()
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Locandina n° {lid} annullata.")


async def risposta_privata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Riceve l'orario (dopo Programma) o il testo da aggiungere (dopo
    Aggiungi testo). Se il bot non sta aspettando nulla, non fa niente."""
    attesa = context.user_data.get("locandina_attesa")
    if attesa is None or not _is_admin(update.effective_user.id):
        return
    modo, lid = attesa
    testo = (update.message.text or "").strip()
    if testo.lower() == "annulla":
        context.user_data.pop("locandina_attesa", None)
        await update.message.reply_text("Ok, nessuna modifica. L'anteprima resta lì se ti serve.")
        return
    if modo == "testo":
        await _salva_testo(update, context, lid, testo)
    else:
        await _salva_orario(update, context, lid, testo)


async def _salva_testo(update, context, lid: int, testo: str):
    conn = _conn()
    row = conn.execute("SELECT file_id, didascalia, stato FROM locandine WHERE id = ?", (lid,)).fetchone()
    if not row or row[2] in ("pubblicata", "annullata"):
        context.user_data.pop("locandina_attesa", None)
        await update.message.reply_text("Questa locandina non è più modificabile.")
        return
    file_id, didascalia, stato = row
    extra = None if testo.lower() == "nessuno" else testo
    completo = _testo_completo(didascalia, extra)
    if len(_caption_anteprima(lid, completo)) > LIMITE_DIDASCALIA:
        await update.message.reply_text(
            f"Troppo lungo: Telegram accetta al massimo circa {LIMITE_DIDASCALIA} caratteri di didascalia. Accorcialo."
        )
        return
    conn.execute("UPDATE locandine SET extra = ? WHERE id = ?", (extra, lid))
    conn.commit()
    context.user_data.pop("locandina_attesa", None)
    nota = ""
    if stato == "programmata":
        nota = "\n(è già programmata: uscirà con questo testo, non serve riprogrammarla)"
    await update.message.reply_photo(
        photo=file_id,
        caption=_caption_anteprima(lid, completo) + nota,
        reply_markup=_tastiera(lid),
    )


async def _salva_orario(update, context, lid: int, testo: str):
    quando = leggi_quando(testo)
    if quando is None:
        await update.message.reply_text("Non ho capito. Scrivi ad esempio 14/10 18:00 (oppure 'annulla').")
        return
    if quando <= datetime.now(FUSO):
        await update.message.reply_text("Quell'orario è già passato. Scrivine uno futuro.")
        return

    conn = _conn()
    conn.execute(
        "UPDATE locandine SET stato = 'programmata', pubblica_il = ? WHERE id = ?",
        (quando.isoformat(), lid),
    )
    conn.commit()
    _programma_job(context.job_queue, lid, quando)
    context.user_data.pop("locandina_attesa", None)
    gruppo_ok = _gruppo(conn) is not None
    await update.message.reply_text(
        f"⏰ Locandina n° {lid} programmata per {quando.strftime('%d/%m alle %H:%M')}."
        + ("" if gruppo_ok else "\n⚠️ Nessun gruppo registrato: manda /registragruppo nel gruppo, o non partirà.")
    )


async def programmate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    conn = _conn()
    righe = conn.execute(
        "SELECT id, pubblica_il, didascalia FROM locandine WHERE stato = 'programmata' ORDER BY pubblica_il"
    ).fetchall()
    if not righe:
        await update.message.reply_text("Nessuna locandina programmata.")
        return
    testo = "⏰ Locandine programmate:\n\n"
    for lid, quando, did in righe:
        q = datetime.fromisoformat(quando).astimezone(FUSO)
        testo += f"n° {lid} — {q.strftime('%d/%m %H:%M')} — {did.splitlines()[0]}\n"
    testo += "\nPer annullarne una: /annullalocandina N"
    await update.message.reply_text(testo)


async def annullalocandina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /annullalocandina N (il numero lo vedi con /programmate)")
        return
    lid = int(context.args[0])
    conn = _conn()
    n = conn.execute(
        "UPDATE locandine SET stato = 'annullata' WHERE id = ? AND stato = 'programmata'", (lid,)
    ).rowcount
    conn.commit()
    for j in context.job_queue.get_jobs_by_name(f"locandina_{lid}"):
        j.schedule_removal()
    await update.message.reply_text(
        f"Locandina n° {lid} annullata." if n else f"Nessuna locandina programmata con n° {lid}."
    )


async def ripristina_programmate(application):
    """All'avvio rimette in coda le pubblicazioni salvate nel database."""
    conn = _conn()
    adesso = datetime.now(FUSO)
    righe = conn.execute(
        "SELECT id, pubblica_il FROM locandine WHERE stato = 'programmata'"
    ).fetchall()
    for lid, quando_iso in righe:
        quando = datetime.fromisoformat(quando_iso)
        if quando > adesso:
            _programma_job(application.job_queue, lid, quando)
        elif adesso - quando <= RITARDO_MASSIMO:
            _programma_job(application.job_queue, lid, adesso + timedelta(seconds=10))
        else:
            conn.execute("UPDATE locandine SET stato = 'scaduta' WHERE id = ?", (lid,))
            conn.commit()
            for admin in _admin_ids:
                try:
                    await application.bot.send_message(
                        admin,
                        f"⚠️ La locandina n° {lid} doveva uscire il {quando.astimezone(FUSO).strftime('%d/%m %H:%M')} "
                        "ma il bot era spento. Non l'ho pubblicata: rifalla con /locandina se serve.",
                    )
                except Exception:
                    pass


def registra(app, db_connect, is_admin, chiave_gruppo, admin_ids):
    """Collega il comando al bot. Va chiamata da main() del file principale."""
    global _db_connect, _is_admin, _chiave_gruppo, _admin_ids
    _db_connect, _is_admin, _chiave_gruppo, _admin_ids = db_connect, is_admin, chiave_gruppo, set(admin_ids)
    app.add_handler(CommandHandler("locandina", locandina))
    app.add_handler(CommandHandler("programmate", programmate))
    app.add_handler(CommandHandler("annullalocandina", annullalocandina))
    app.add_handler(CallbackQueryHandler(bottoni, pattern=r"^loc:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, risposta_privata),
        group=2,
    )


COMANDI_LOCANDINA = [
    ("locandina", "[admin] Crea la locandina — /locandina 16/10 volo [22:00] [25€] [premi COPPE]"),
    ("programmate", "[admin] Elenco locandine programmate"),
    ("annullalocandina", "[admin] Annulla una locandina programmata — /annullalocandina N"),
]
