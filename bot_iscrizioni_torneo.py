"""
Bot Telegram — Iscrizioni squadre torneo biliardino
=====================================================

COSA FA:
- /iscrivi NomeSquadra   -> registra la squadra (1 per utente normale;
                            gli admin possono iscriverne quante ne vogliono)
- /squadre               -> mostra elenco squadre iscritte (visibile a tutti)
- /ritira NomeSquadra    -> annulla un'iscrizione (nome obbligatorio solo se
                            hai più di una squadra iscritta, es. admin)
- /chiudi                -> (solo admin) blocca nuove iscrizioni
- /apri                  -> (solo admin) riapre le iscrizioni
- /esporta               -> (solo admin) invia CSV delle squadre in privato
- /reset                 -> (solo admin) svuota tutte le iscrizioni (richiede conferma)

SETUP:
1. pip install python-telegram-bot --upgrade
2. Sostituisci TOKEN con quello di @BotFather
3. Sostituisci ADMIN_IDS con gli ID Telegram numerici degli admin
   (per scoprire il proprio ID, scrivi a @userinfobot su Telegram)
4. python bot_iscrizioni_torneo.py

Il bot va lasciato in esecuzione (polling). Per hosting 24/7 vedi
nota in fondo al file.
"""

import csv
import io
import os
import sqlite3
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ============ CONFIGURAZIONE ============
# TOKEN e ADMIN_IDS vengono letti da variabili d'ambiente (impostate sulla
# piattaforma di hosting, es. Railway) invece che scritti qui nel codice.
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = "iscrizioni.db"
MAX_SQUADRE = 18  # limite massimo di coppie iscrivibili al torneo
# =========================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS squadre (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            nome_squadra TEXT NOT NULL,
            iscritto_il TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stato (
            chiave TEXT PRIMARY KEY,
            valore TEXT
        )
    """)
    conn.commit()
    return conn


def iscrizioni_aperte(conn) -> bool:
    row = conn.execute(
        "SELECT valore FROM stato WHERE chiave = 'aperte'"
    ).fetchone()
    return row is None or row[0] == "1"


def set_iscrizioni(conn, aperte: bool):
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES ('aperte', ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        ("1" if aperte else "0",),
    )
    conn.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def iscrivi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    user = update.effective_user

    if not iscrizioni_aperte(conn):
        await update.message.reply_text("Le iscrizioni sono chiuse. Contatta un admin.")
        return

    if not context.args:
        await update.message.reply_text("Uso corretto: /iscrivi NomeSquadra")
        return

    nome_squadra = " ".join(context.args).strip()

    # Il limite di 1 squadra per persona vale solo per gli utenti normali.
    # Gli admin possono iscrivere più squadre (es. per conto di chi non usa Telegram).
    if not is_admin(user.id):
        esistente = conn.execute(
            "SELECT nome_squadra FROM squadre WHERE user_id = ?", (user.id,)
        ).fetchone()
        if esistente:
            await update.message.reply_text(
                f"Sei già iscritto come '{esistente[0]}'. Usa /ritira prima di reiscriverti."
            )
            return

    duplicato = conn.execute(
        "SELECT 1 FROM squadre WHERE LOWER(nome_squadra) = LOWER(?)", (nome_squadra,)
    ).fetchone()
    if duplicato:
        await update.message.reply_text(
            f"Il nome '{nome_squadra}' è già stato preso. Scegline un altro."
        )
        return

    totale_attuale = conn.execute("SELECT COUNT(*) FROM squadre").fetchone()[0]
    if totale_attuale >= MAX_SQUADRE:
        await update.message.reply_text(
            f"Torneo al completo: raggiunto il limite di {MAX_SQUADRE} coppie. "
            "Contatta un admin se vuoi essere messo in lista d'attesa."
        )
        return

    conn.execute(
        "INSERT INTO squadre (user_id, username, nome_squadra, iscritto_il) VALUES (?, ?, ?, ?)",
        (user.id, user.username or user.first_name, nome_squadra, datetime.now().isoformat()),
    )
    conn.commit()

    totale = conn.execute("SELECT COUNT(*) FROM squadre").fetchone()[0]
    await update.message.reply_text(
        f"✅ Squadra '{nome_squadra}' iscritta! (n° {totale} nell'elenco)"
    )


async def squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    righe = conn.execute(
        "SELECT nome_squadra, username FROM squadre ORDER BY iscritto_il"
    ).fetchall()

    if not righe:
        await update.message.reply_text("Nessuna squadra iscritta finora.")
        return

    testo = "🏆 Squadre iscritte:\n\n"
    for i, (nome, username) in enumerate(righe, start=1):
        testo += f"{i}. {nome} (@{username})\n"
    testo += f"\nTotale: {len(righe)}/{MAX_SQUADRE}"
    await update.message.reply_text(testo)


async def ritira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    user = update.effective_user
    proprie = conn.execute(
        "SELECT id, nome_squadra FROM squadre WHERE user_id = ?", (user.id,)
    ).fetchall()

    if not proprie:
        await update.message.reply_text("Non risulti iscritto a nessuna squadra.")
        return

    nome_indicato = " ".join(context.args).strip() if context.args else None

    if len(proprie) == 1 and not nome_indicato:
        squadra_id, nome = proprie[0]
    elif nome_indicato:
        match = next((p for p in proprie if p[1].lower() == nome_indicato.lower()), None)
        if not match:
            elenco = ", ".join(n for _, n in proprie)
            await update.message.reply_text(
                f"Non trovo '{nome_indicato}' tra le tue squadre. Le tue: {elenco}"
            )
            return
        squadra_id, nome = match
    else:
        elenco = ", ".join(n for _, n in proprie)
        await update.message.reply_text(
            f"Hai più squadre iscritte. Specifica quale: /ritira NomeSquadra\nLe tue: {elenco}"
        )
        return

    conn.execute("DELETE FROM squadre WHERE id = ?", (squadra_id,))
    conn.commit()
    await update.message.reply_text(f"Iscrizione di '{nome}' annullata.")


async def chiudi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    conn = db_connect()
    set_iscrizioni(conn, False)
    await update.message.reply_text("🔒 Iscrizioni chiuse.")


async def apri(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    conn = db_connect()
    set_iscrizioni(conn, True)
    await update.message.reply_text("🔓 Iscrizioni riaperte.")


async def esporta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    conn = db_connect()
    righe = conn.execute(
        "SELECT nome_squadra, username, iscritto_il FROM squadre ORDER BY iscritto_il"
    ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Squadra", "Username", "Iscritto il"])
    writer.writerows(righe)
    buffer.seek(0)

    dati_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
    dati_bytes.name = "squadre_torneo.csv"

    await context.bot.send_document(
        chat_id=update.effective_user.id,
        document=dati_bytes,
        filename="squadre_torneo.csv",
    )
    await update.message.reply_text("CSV inviato in privato.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if not context.args or context.args[0] != "CONFERMA":
        await update.message.reply_text(
            "Questo cancella TUTTE le iscrizioni. Per confermare: /reset CONFERMA"
        )
        return
    conn = db_connect()
    conn.execute("DELETE FROM squadre")
    conn.commit()
    await update.message.reply_text("Elenco squadre svuotato.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot iscrizioni torneo attivo.\n\n"
        "/iscrivi NomeSquadra — iscrivi la tua squadra\n"
        "/squadre — vedi l'elenco\n"
        "/ritira [NomeSquadra] — annulla un'iscrizione"
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("iscrivi", iscrivi))
    app.add_handler(CommandHandler("squadre", squadre))
    app.add_handler(CommandHandler("ritira", ritira))
    app.add_handler(CommandHandler("chiudi", chiudi))
    app.add_handler(CommandHandler("apri", apri))
    app.add_handler(CommandHandler("esporta", esporta))
    app.add_handler(CommandHandler("reset", reset))

    print("Bot avviato. Premi Ctrl+C per fermarlo.")
    app.run_polling()


if __name__ == "__main__":
    main()

# ============ HOSTING 24/7 ============
# Per farlo girare stabilmente senza tenere il PC acceso, opzioni pratiche:
# - Railway.app o Render.com (piano free/hobby, deploy diretto da questo file)
# - Un Raspberry Pi o mini-PC di casa con `screen`/`tmux` + riavvio automatico
# - VPS economico (Hetzner ~4€/mese) con systemd per farlo ripartire da solo
# =======================================
