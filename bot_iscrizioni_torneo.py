"""
Bot Telegram — Iscrizioni squadre torneo biliardino
=====================================================

COSA FA:
- /help                   -> mostra la lista di tutti i comandi con descrizione
- /iscrivi NomeSquadra   -> registra la squadra al torneo attivo (1 per
                            utente normale; gli admin possono iscriverne
                            quante ne vogliono)
- /squadre               -> mostra elenco squadre iscritte al torneo attivo
- /ritira NomeSquadra    -> annulla un'iscrizione (nome obbligatorio solo se
                            hai più di una squadra iscritta, es. admin)
- /rimuovi NomeSquadra   -> (solo admin) rimuove qualsiasi squadra dal
                            torneo attivo, indipendentemente da chi l'ha
                            iscritta (moderazione contro iscrizioni fasulle)
- /eliminatorneo Nome CONFERMA -> (solo admin) elimina per sempre un torneo
                            ARCHIVIATO dallo storico (non funziona sul
                            torneo attivo, per sicurezza)
- /torneo                -> mostra il nome del torneo attuale
- /nometorneo Nome CONFERMA -> (solo admin) apre un nuovo torneo con questo
                            nome e archivia automaticamente quello precedente
                            (squadre incluse, restano consultabili in /storico)
- /storico                -> mostra gli ultimi 10 tornei con conteggio squadre
- /storico Nome           -> mostra le squadre e il podio di un torneo passato
- /podio Sq1 | Sq2 | Sq3  -> (solo admin) registra e annuncia il podio del
                            torneo attivo nel gruppo. Se usato in risposta a
                            una foto, la ripubblica come immagine del podio.
- /chiudi                -> (solo admin) blocca nuove iscrizioni
- /apri                  -> (solo admin) riapre le iscrizioni
- /esporta               -> (solo admin) invia CSV delle squadre in privato
- /reset                 -> (solo admin) svuota le squadre del torneo attivo
                            (richiede conferma; lo storico non viene toccato)

SETUP:
1. pip install python-telegram-bot --upgrade
2. Sostituisci TOKEN con quello di @BotFather
3. Sostituisci ADMIN_IDS con gli ID Telegram numerici degli admin
   (per scoprire il proprio ID, scrivi a @userinfobot su Telegram)
4. (opzionale) Imposta DB_PATH per salvare il database in un percorso
   specifico, es. su un volume persistente (vedi nota HOSTING in fondo).
   Se non impostata, usa "iscrizioni.db" nella cartella corrente.
5. python bot_iscrizioni_torneo.py

Il bot va lasciato in esecuzione (polling). Per hosting 24/7 vedi
nota in fondo al file.
"""

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import BotCommand, BotCommandScopeChatAdministrators, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============ CONFIGURAZIONE ============
# TOKEN e ADMIN_IDS vengono letti da variabili d'ambiente (impostate sulla
# piattaforma di hosting, es. Railway) invece che scritti qui nel codice.
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "iscrizioni.db")
MAX_SQUADRE = 18  # limite massimo di coppie iscrivibili al torneo
NOME_TORNEO_INIZIALE = "Torneo"

# --- Sondaggio settimanale "gioco libero" ---
CHIAVE_CHAT_GIOCO_LIBERO = "chat_id_gioco_libero"
CHIAVE_DOMANDA_SONDAGGIO = "sondaggio_domanda"
CHIAVE_OPZIONI_SONDAGGIO = "sondaggio_opzioni"
ORARIO_SONDAGGIO_GIOCO_LIBERO = time(hour=12, minute=0, tzinfo=ZoneInfo("Europe/Rome"))
DOMANDA_SONDAGGIO_DEFAULT = "Vieni al gioco libero di stasera? 🎱"
OPZIONI_SONDAGGIO_DEFAULT = ["Sì ✅", "No ❌"]

# Elenco comandi con descrizione breve, usato sia da /help sia dal menu
# nativo di Telegram (quello che compare scrivendo "/").
COMANDI = [
    ("iscrivi", "Iscrivi la tua squadra al torneo attivo — es. /iscrivi Rossi-Bianchi"),
    ("squadre", "Mostra l'elenco delle squadre iscritte al torneo attivo"),
    ("ritira", "Annulla una tua iscrizione (indica il nome se ne hai più di una)"),
    ("torneo", "Mostra il nome del torneo attualmente attivo"),
    ("storico", "Mostra gli ultimi tornei, o i dettagli di uno (/storico Nome)"),
    ("help", "Mostra questa lista di comandi"),
    ("podio", "[admin] Registra e annuncia il podio — /podio Sq1 | Sq2 | Sq3"),
    ("nometorneo", "[admin] Apre un nuovo torneo e archivia quello attivo (richiede CONFERMA)"),
    ("rimuovi", "[admin] Rimuove qualsiasi squadra dal torneo attivo, non solo le tue"),
    ("eliminatorneo", "[admin] Elimina per sempre un torneo archiviato dallo storico"),
    ("chiudi", "[admin] Blocca nuove iscrizioni al torneo attivo"),
    ("apri", "[admin] Riapre le iscrizioni"),
    ("esporta", "[admin] Invia il CSV delle squadre del torneo attivo in privato"),
    ("reset", "[admin] Svuota le iscrizioni del torneo attivo (richiede conferma)"),
    ("registragruppo", "[admin] Registra QUESTO gruppo come destinazione del sondaggio gioco libero (una tantum)"),
    ("sondaggio", "[admin] Invia subito il sondaggio nel gruppo registrato"),
    ("vedisondaggio", "[admin] Mostra domanda e opzioni attuali del sondaggio, senza inviarlo"),
    ("anteprimasondaggio", "[admin] Ti manda il sondaggio in privato, così lo vedi prima che parta nel gruppo"),
    ("modificasondaggio", "[admin] Cambia domanda/opzioni — /modificasondaggio Domanda | Opzione1 | Opzione2"),
]
# =========================================


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tornei (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            creato_il TEXT NOT NULL,
            chiuso INTEGER NOT NULL DEFAULT 0,
            podio_primo TEXT,
            podio_secondo TEXT,
            podio_terzo TEXT,
            foto_file_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS squadre (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torneo_id INTEGER NOT NULL,
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

    # Al primissimo avvio crea il torneo iniziale, se non esiste ancora nulla.
    esiste = conn.execute("SELECT COUNT(*) FROM tornei").fetchone()[0]
    if esiste == 0:
        conn.execute(
            "INSERT INTO tornei (nome, creato_il, chiuso) VALUES (?, ?, 0)",
            (NOME_TORNEO_INIZIALE, datetime.now().isoformat()),
        )
        conn.commit()

    return conn


def get_torneo_attivo(conn):
    """Ritorna (id, nome) del torneo attualmente aperto (l'ultimo non chiuso)."""
    row = conn.execute(
        "SELECT id, nome FROM tornei WHERE chiuso = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row
    # Fallback di sicurezza: se per qualche motivo non c'è nessun torneo
    # aperto, ne crea uno nuovo al volo.
    conn.execute(
        "INSERT INTO tornei (nome, creato_il, chiuso) VALUES (?, ?, 0)",
        (NOME_TORNEO_INIZIALE, datetime.now().isoformat()),
    )
    conn.commit()
    return get_torneo_attivo(conn)


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


def testo_squadre(conn, torneo_id: int, nome_torneo: str) -> str:
    righe = conn.execute(
        "SELECT nome_squadra FROM squadre WHERE torneo_id = ? ORDER BY iscritto_il",
        (torneo_id,),
    ).fetchall()

    if not righe:
        return f"Nessuna squadra ancora iscritta a {nome_torneo}."

    testo = f"🏆 Squadre iscritte — {nome_torneo}\n\n"
    for i, (nome,) in enumerate(righe, start=1):
        testo += f"{i}. {nome}\n"
    testo += f"\nTotale: {len(righe)}/{MAX_SQUADRE}"
    return testo


async def iscrivi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    user = update.effective_user
    torneo_id, nome_torneo = get_torneo_attivo(conn)

    if not iscrizioni_aperte(conn):
        await update.message.reply_text("Le iscrizioni sono chiuse. Contatta un admin.")
        return

    if not context.args:
        await update.message.reply_text("Uso corretto: /iscrivi NomeSquadra")
        return

    nome_squadra = " ".join(context.args).strip()

    # Il limite di 1 squadra per persona vale solo per gli utenti normali,
    # e solo all'interno del torneo attivo corrente.
    if not is_admin(user.id):
        esistente = conn.execute(
            "SELECT nome_squadra FROM squadre WHERE user_id = ? AND torneo_id = ?",
            (user.id, torneo_id),
        ).fetchone()
        if esistente:
            await update.message.reply_text(
                f"Sei già iscritto come '{esistente[0]}'. Usa /ritira prima di reiscriverti."
            )
            return

    duplicato = conn.execute(
        "SELECT 1 FROM squadre WHERE torneo_id = ? AND LOWER(nome_squadra) = LOWER(?)",
        (torneo_id, nome_squadra),
    ).fetchone()
    if duplicato:
        await update.message.reply_text(
            f"Il nome '{nome_squadra}' è già stato preso in questo torneo. Scegline un altro."
        )
        return

    totale_attuale = conn.execute(
        "SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (torneo_id,)
    ).fetchone()[0]
    if totale_attuale >= MAX_SQUADRE:
        await update.message.reply_text(
            f"Torneo al completo: raggiunto il limite di {MAX_SQUADRE} coppie. "
            "Contatta un admin se vuoi essere messo in lista d'attesa."
        )
        return

    conn.execute(
        "INSERT INTO squadre (torneo_id, user_id, username, nome_squadra, iscritto_il) "
        "VALUES (?, ?, ?, ?, ?)",
        (torneo_id, user.id, user.username or user.first_name, nome_squadra, datetime.now().isoformat()),
    )
    conn.commit()

    totale = totale_attuale + 1
    await update.message.reply_text(
        f"✅ Squadra '{nome_squadra}' iscritta a {nome_torneo}! (n° {totale} nell'elenco)"
    )
    await update.message.reply_text(testo_squadre(conn, torneo_id, nome_torneo))


async def squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    await update.message.reply_text(testo_squadre(conn, torneo_id, nome_torneo))


async def ritira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    user = update.effective_user
    torneo_id, _ = get_torneo_attivo(conn)
    proprie = conn.execute(
        "SELECT id, nome_squadra FROM squadre WHERE user_id = ? AND torneo_id = ?",
        (user.id, torneo_id),
    ).fetchall()

    if not proprie:
        await update.message.reply_text("Non risulti iscritto a nessuna squadra in questo torneo.")
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


async def rimuovi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando admin: rimuove QUALSIASI squadra dal torneo attivo,
    indipendentemente da chi l'ha iscritta. Diverso da /ritira, che è
    self-service e tocca solo le proprie squadre."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if not context.args:
        await update.message.reply_text("Uso corretto: /rimuovi NomeSquadra")
        return

    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    nome_cercato = " ".join(context.args).strip()

    match = conn.execute(
        "SELECT id, nome_squadra FROM squadre WHERE torneo_id = ? AND LOWER(nome_squadra) = LOWER(?)",
        (torneo_id, nome_cercato),
    ).fetchone()

    if not match:
        await update.message.reply_text(
            f"Nessuna squadra '{nome_cercato}' trovata in {nome_torneo}. Controlla /squadre."
        )
        return

    squadra_id, nome_reale = match
    conn.execute("DELETE FROM squadre WHERE id = ?", (squadra_id,))
    conn.commit()

    await update.message.reply_text(f"🗑️ Squadra '{nome_reale}' rimossa da {nome_torneo}.")
    await update.message.reply_text(testo_squadre(conn, torneo_id, nome_torneo))


async def eliminatorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando admin: elimina definitivamente un torneo ARCHIVIATO dallo
    storico (utile per ripulire test o tornei vuoti). Non permette di
    eliminare il torneo attualmente attivo, per evitare di cancellare
    per sbaglio quello su cui la gente si sta iscrivendo ora."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    if len(context.args) < 2 or context.args[-1] != "CONFERMA":
        await update.message.reply_text(
            "Questo elimina PER SEMPRE un torneo dallo storico (squadre incluse).\n"
            "Uso corretto: /eliminatorneo NomeTorneo CONFERMA"
        )
        return

    nome_cercato = " ".join(context.args[:-1]).strip()
    conn = db_connect()

    torneo_row = conn.execute(
        "SELECT id, chiuso FROM tornei WHERE LOWER(nome) = LOWER(?) ORDER BY id DESC LIMIT 1",
        (nome_cercato,),
    ).fetchone()

    if not torneo_row:
        await update.message.reply_text(f"Nessun torneo trovato con nome '{nome_cercato}'.")
        return

    torneo_id, chiuso = torneo_row
    if not chiuso:
        await update.message.reply_text(
            "Non puoi eliminare il torneo ATTIVO. Aprine prima uno nuovo con "
            "/nometorneo per archiviarlo, poi elimina quello archiviato."
        )
        return

    n_squadre = conn.execute(
        "SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (torneo_id,)
    ).fetchone()[0]

    conn.execute("DELETE FROM squadre WHERE torneo_id = ?", (torneo_id,))
    conn.execute("DELETE FROM tornei WHERE id = ?", (torneo_id,))
    conn.commit()

    await update.message.reply_text(
        f"🗑️ Torneo '{nome_cercato}' eliminato definitivamente dallo storico "
        f"({n_squadre} squadre rimosse con lui)."
    )


async def nometorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    conn = db_connect()
    _, vecchio_nome_preview = get_torneo_attivo(conn)

    if len(context.args) < 2 or context.args[-1] != "CONFERMA":
        await update.message.reply_text(
            f"Questo chiude SUBITO il torneo attivo ('{vecchio_nome_preview}') per tutti "
            "e ne apre uno nuovo.\n"
            "Uso corretto: /nometorneo Nome Del Torneo CONFERMA"
        )
        return

    nuovo_nome = " ".join(context.args[:-1]).strip()
    vecchio_id, vecchio_nome = get_torneo_attivo(conn)

    if nuovo_nome.lower() == vecchio_nome.lower():
        await update.message.reply_text(f"Il torneo attivo è già '{vecchio_nome}'.")
        return

    # Archivia il torneo corrente e ne apre uno nuovo: nessuna azione manuale
    # necessaria, le squadre già iscritte restano legate al torneo vecchio.
    conn.execute("UPDATE tornei SET chiuso = 1 WHERE id = ?", (vecchio_id,))
    conn.execute(
        "INSERT INTO tornei (nome, creato_il, chiuso) VALUES (?, ?, 0)",
        (nuovo_nome, datetime.now().isoformat()),
    )
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES ('aperte', '1') "
        "ON CONFLICT(chiave) DO UPDATE SET valore = '1'"
    )
    conn.commit()

    await update.message.reply_text(
        f"🏓 Nuovo torneo aperto: '{nuovo_nome}'.\n"
        f"'{vecchio_nome}' è stato archiviato ed è consultabile con /storico."
    )


async def torneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    _, nome_torneo = get_torneo_attivo(conn)
    await update.message.reply_text(f"🏓 Torneo attuale: {nome_torneo}")


async def storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()

    if not context.args:
        righe = conn.execute("""
            SELECT t.id, t.nome, t.chiuso, COUNT(s.id)
            FROM tornei t
            LEFT JOIN squadre s ON s.torneo_id = t.id
            GROUP BY t.id
            ORDER BY t.id DESC
            LIMIT 10
        """).fetchall()

        testo = "📜 Ultimi tornei:\n\n"
        for tid, nome, chiuso, n_squadre in righe:
            stato_icona = "🔵 (attivo)" if not chiuso else ""
            testo += f"• {nome} — {n_squadre} squadre {stato_icona}\n"
        testo += "\nDettagli di uno specifico: /storico NomeTorneo"
        await update.message.reply_text(testo)
        return

    nome_cercato = " ".join(context.args).strip()
    torneo_row = conn.execute(
        "SELECT id, nome, podio_primo, podio_secondo, podio_terzo FROM tornei "
        "WHERE LOWER(nome) = LOWER(?) ORDER BY id DESC LIMIT 1",
        (nome_cercato,),
    ).fetchone()

    if not torneo_row:
        await update.message.reply_text(f"Nessun torneo trovato con nome '{nome_cercato}'.")
        return

    tid, nome, primo, secondo, terzo = torneo_row
    righe_squadre = conn.execute(
        "SELECT nome_squadra FROM squadre WHERE torneo_id = ? ORDER BY iscritto_il", (tid,)
    ).fetchall()

    testo = f"📜 {nome}\n\n"
    if primo or secondo or terzo:
        testo += "🏆 Podio:\n"
        if primo:
            testo += f"🥇 {primo}\n"
        if secondo:
            testo += f"🥈 {secondo}\n"
        if terzo:
            testo += f"🥉 {terzo}\n"
        testo += "\n"

    testo += f"Squadre iscritte ({len(righe_squadre)}):\n"
    for i, (nome_sq,) in enumerate(righe_squadre, start=1):
        testo += f"{i}. {nome_sq}\n"

    await update.message.reply_text(testo)


async def podio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    testo_args = " ".join(context.args) if context.args else ""
    squadre_podio = [s.strip() for s in testo_args.split("|") if s.strip()]

    if len(squadre_podio) != 3:
        await update.message.reply_text(
            "Uso corretto: /podio Squadra1 | Squadra2 | Squadra3\n"
            "(rispondi a una foto del podio se vuoi allegarla all'annuncio)"
        )
        return

    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    primo, secondo, terzo = squadre_podio

    conn.execute(
        "UPDATE tornei SET podio_primo = ?, podio_secondo = ?, podio_terzo = ? WHERE id = ?",
        (primo, secondo, terzo, torneo_id),
    )

    foto_file_id = None
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        foto_file_id = update.message.reply_to_message.photo[-1].file_id
        conn.execute("UPDATE tornei SET foto_file_id = ? WHERE id = ?", (foto_file_id, torneo_id))

    conn.commit()

    caption = (
        f"🏆 Podio — {nome_torneo}\n\n"
        f"🥇 {primo}\n"
        f"🥈 {secondo}\n"
        f"🥉 {terzo}"
    )

    if foto_file_id:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id, photo=foto_file_id, caption=caption
        )
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=caption)


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


def ottieni_config_sondaggio(conn) -> tuple[str, list[str]]:
    """Legge domanda/opzioni salvate; se non sono mai state personalizzate
    ritorna i default definiti in cima al file."""
    riga_domanda = conn.execute(
        "SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_DOMANDA_SONDAGGIO,)
    ).fetchone()
    riga_opzioni = conn.execute(
        "SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_OPZIONI_SONDAGGIO,)
    ).fetchone()

    domanda = riga_domanda[0] if riga_domanda else DOMANDA_SONDAGGIO_DEFAULT
    opzioni = json.loads(riga_opzioni[0]) if riga_opzioni else list(OPZIONI_SONDAGGIO_DEFAULT)
    return domanda, opzioni


def imposta_config_sondaggio(conn, domanda: str, opzioni: list[str]):
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        (CHIAVE_DOMANDA_SONDAGGIO, domanda),
    )
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        (CHIAVE_OPZIONI_SONDAGGIO, json.dumps(opzioni)),
    )
    conn.commit()


async def invia_sondaggio_gioco_libero(bot, chat_id: int, domanda: str, opzioni: list[str]):
    """Invia il poll non anonimo: Telegram mostra da solo, nell'interfaccia
    del sondaggio, chi ha votato cosa — non serve salvare nulla nel DB."""
    await bot.send_poll(
        chat_id=chat_id,
        question=domanda,
        options=opzioni,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def vedisondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    conn = db_connect()
    domanda, opzioni = ottieni_config_sondaggio(conn)
    testo_opzioni = "\n".join(f"  • {o}" for o in opzioni)
    await update.message.reply_text(
        f"📋 Domanda attuale:\n{domanda}\n\nOpzioni:\n{testo_opzioni}\n\n"
        "Per vederlo renderizzato come poll vero: /anteprimasondaggio\n"
        "Per cambiarlo: /modificasondaggio Domanda | Opzione1 | Opzione2"
    )


async def anteprimasondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manda il poll in privato a chi lo richiede (non nel gruppo), così lo
    si vede esattamente come apparirà, senza disturbare nessuno."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    conn = db_connect()
    domanda, opzioni = ottieni_config_sondaggio(conn)
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
        await update.message.reply_text("Comando riservato agli admin.")
        return

    testo_args = " ".join(context.args) if context.args else ""
    parti = [p.strip() for p in testo_args.split("|") if p.strip()]

    if len(parti) < 3:
        conn = db_connect()
        domanda, opzioni = ottieni_config_sondaggio(conn)
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

    conn = db_connect()
    imposta_config_sondaggio(conn, domanda, opzioni)
    await update.message.reply_text(
        f"✅ Sondaggio aggiornato.\nDomanda: {domanda}\nOpzioni: {', '.join(opzioni)}\n\n"
        "Verifica con /anteprimasondaggio prima che parta davvero lunedì."
    )


async def registragruppo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando admin, da eseguire UNA TANTUM dentro il gruppo giusto: salva
    l'ID di quel gruppo come destinazione del sondaggio settimanale."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Usa questo comando dentro il gruppo da registrare, non in privato.")
        return

    conn = db_connect()
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        (CHIAVE_CHAT_GIOCO_LIBERO, str(update.effective_chat.id)),
    )
    conn.commit()
    await update.message.reply_text(
        "✅ Questo gruppo è registrato. Ogni lunedì alle "
        f"{ORARIO_SONDAGGIO_GIOCO_LIBERO.strftime('%H:%M')} partirà qui il sondaggio "
        "del gioco libero. Puoi anche lanciarlo a mano con /sondaggio."
    )


async def sondaggio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando admin: invia subito il sondaggio nel gruppo registrato con
    /registragruppo (puoi lanciarlo anche da chat privata con il bot)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    conn = db_connect()
    row = conn.execute(
        "SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_CHAT_GIOCO_LIBERO,)
    ).fetchone()
    if not row:
        await update.message.reply_text(
            "Nessun gruppo registrato. Vai nel gruppo giusto e manda /registragruppo (una tantum)."
        )
        return

    chat_id_destinazione = int(row[0])
    domanda, opzioni = ottieni_config_sondaggio(conn)
    await invia_sondaggio_gioco_libero(context.bot, chat_id_destinazione, domanda, opzioni)
    if update.effective_chat.id != chat_id_destinazione:
        await update.message.reply_text("Sondaggio inviato nel gruppo registrato.")


async def sondaggio_automatico(context: ContextTypes.DEFAULT_TYPE):
    """Callback schedulato: gira ogni lunedì all'orario impostato."""
    conn = db_connect()
    row = conn.execute(
        "SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_CHAT_GIOCO_LIBERO,)
    ).fetchone()
    if row:
        domanda, opzioni = ottieni_config_sondaggio(conn)
        await invia_sondaggio_gioco_libero(context.bot, int(row[0]), domanda, opzioni)


async def esporta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    righe = conn.execute(
        "SELECT nome_squadra, username, iscritto_il FROM squadre WHERE torneo_id = ? ORDER BY iscritto_il",
        (torneo_id,),
    ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Squadra", "Username", "Iscritto il"])
    writer.writerows(righe)
    buffer.seek(0)

    dati_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
    dati_bytes.name = f"squadre_{nome_torneo}.csv"

    await context.bot.send_document(
        chat_id=update.effective_user.id,
        document=dati_bytes,
        filename=f"squadre_{nome_torneo}.csv",
    )
    await update.message.reply_text("CSV inviato in privato.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if not context.args or context.args[0] != "CONFERMA":
        await update.message.reply_text(
            "Questo cancella le iscrizioni del torneo ATTIVO (lo storico resta intatto). "
            "Per confermare: /reset CONFERMA"
        )
        return
    conn = db_connect()
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    conn.execute("DELETE FROM squadre WHERE torneo_id = ?", (torneo_id,))
    conn.commit()
    await update.message.reply_text(f"Elenco squadre di '{nome_torneo}' svuotato.")


def comandi_pubblici():
    return [(n, d) for n, d in COMANDI if not d.startswith("[admin]")]


def testo_help(is_utente_admin: bool) -> str:
    comandi_da_mostrare = COMANDI if is_utente_admin else comandi_pubblici()
    testo = "🎱 Comandi disponibili:\n\n"
    for nome, descrizione in comandi_da_mostrare:
        testo += f"/{nome} — {descrizione}\n"
    return testo


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mostra_admin = is_admin(update.effective_user.id) and update.effective_chat.type == "private"
    await update.message.reply_text(testo_help(mostra_admin))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mostra_admin = is_admin(update.effective_user.id) and update.effective_chat.type == "private"
    await update.message.reply_text(testo_help(mostra_admin))


async def imposta_menu_comandi(application):
    """Registra i comandi nel menu nativo di Telegram (quello che compare
    scrivendo '/'). Il menu di default (chat private, o prima che venga
    rilevato un gruppo) mostra solo i comandi pubblici."""
    comandi_default = [BotCommand(nome, descrizione[:256]) for nome, descrizione in comandi_pubblici()]
    await application.bot.set_my_commands(comandi_default)


async def imposta_menu_per_gruppo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """La prima volta che il bot riceve un messaggio in un dato gruppo,
    imposta per gli amministratori DI QUEL GRUPPO il menu completo
    (comandi pubblici + admin). Gli altri membri continuano a vedere
    solo il menu pubblico di default."""
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return

    conn = db_connect()
    chiave = f"menu_admin_impostato_{chat.id}"
    gia_impostato = conn.execute(
        "SELECT 1 FROM stato WHERE chiave = ?", (chiave,)
    ).fetchone()
    if gia_impostato:
        return

    comandi_completi = [BotCommand(nome, descrizione[:256]) for nome, descrizione in COMANDI]
    await context.bot.set_my_commands(
        comandi_completi, scope=BotCommandScopeChatAdministrators(chat.id)
    )
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, '1') "
        "ON CONFLICT(chiave) DO UPDATE SET valore = '1'",
        (chiave,),
    )
    conn.commit()


def main():
    app = Application.builder().token(TOKEN).post_init(imposta_menu_comandi).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("iscrivi", iscrivi))
    app.add_handler(CommandHandler("squadre", squadre))
    app.add_handler(CommandHandler("ritira", ritira))
    app.add_handler(CommandHandler("rimuovi", rimuovi))
    app.add_handler(CommandHandler("eliminatorneo", eliminatorneo))
    app.add_handler(CommandHandler("nometorneo", nometorneo))
    app.add_handler(CommandHandler("torneo", torneo))
    app.add_handler(CommandHandler("storico", storico))
    app.add_handler(CommandHandler("podio", podio))
    app.add_handler(CommandHandler("chiudi", chiudi))
    app.add_handler(CommandHandler("apri", apri))
    app.add_handler(CommandHandler("esporta", esporta))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("registragruppo", registragruppo))
    app.add_handler(CommandHandler("sondaggio", sondaggio))
    app.add_handler(CommandHandler("vedisondaggio", vedisondaggio))
    app.add_handler(CommandHandler("anteprimasondaggio", anteprimasondaggio))
    app.add_handler(CommandHandler("modificasondaggio", modificasondaggio))
    app.add_handler(MessageHandler(filters.ALL, imposta_menu_per_gruppo), group=1)

    app.job_queue.run_daily(
        sondaggio_automatico,
        time=ORARIO_SONDAGGIO_GIOCO_LIBERO,
        days=(0,),  # 0 = lunedì (convenzione python-telegram-bot: 0=lun ... 6=dom)
        name="sondaggio_gioco_libero_lunedi",
    )

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
