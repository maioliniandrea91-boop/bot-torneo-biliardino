"""Elenco dei comandi: /help, /start e menu nativo di Telegram (quello che
compare scrivendo "/"). Gli utenti vedono i comandi pubblici, gli admin tutti."""
import hashlib
import json

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeChatAdministrators, Update
from telegram.ext import ContextTypes

import app_tornei
import comando_locandina
import servizio
import statistiche
from config import ADMIN_IDS, CHIAVE_CHAT_GIOCO_LIBERO, is_admin
from database import db_connect

# ATTENZIONE: l'impronta di questa lista (versione_menu) decide quando rimandare il
# menu a Telegram: cambiarla è normale quando cambiano i comandi.
COMANDI = [
    ("iscrivi", "Iscrivi la tua squadra al torneo attivo — es. /iscrivi Rossi-Bianchi"),
    ("squadre", "Mostra l'elenco delle squadre iscritte al torneo attivo"),
    ("ritira", "Annulla una tua iscrizione (indica il nome se ne hai più di una)"),
    ("torneo", "Mostra il nome del torneo attualmente attivo"),
    ("storico", "Elenco tornei a pagine (/storico 2 = pagina 2), o dettagli di uno (/storico #N o /storico Nome)"),
    ("help", "Mostra questa lista di comandi"),
    ("podio", "[admin] Annuncia il podio — /podio [#N] Sq1 | Sq2 | Sq3 | descrizione (facoltativa)"),
    ("nometorneo", "[admin] Apre un nuovo torneo e archivia quello attivo (richiede CONFERMA)"),
    ("rimuovi", "[admin] Rimuove qualsiasi squadra dal torneo attivo, non solo le tue"),
    ("rinomina", "[admin] Corregge il nome di una squadra — /rinomina 3 | Antonio-Eva (3 = numero in /squadre)"),
    ("eliminatorneo", "[admin] Elimina un torneo archiviato — /eliminatorneo #N, poi #N CONFERMA"),
    ("soglia", "[admin] Coppie minime per confermare il torneo — /soglia 8"),
    ("chiudi", "[admin] Blocca nuove iscrizioni al torneo attivo"),
    ("apri", "[admin] Riapre le iscrizioni"),
    ("esporta", "[admin] Invia l'elenco squadre in Word (solo nomi) in privato"),
    ("reset", "[admin] Svuota le iscrizioni del torneo attivo (richiede conferma)"),
    ("registragruppo", "[admin] Registra QUESTO gruppo come destinazione del sondaggio gioco libero (una tantum)"),
    ("sondaggio", "[admin] Invia subito il sondaggio nel gruppo registrato"),
    ("vedisondaggio", "[admin] Mostra domanda e opzioni attuali del sondaggio, senza inviarlo"),
    ("anteprimasondaggio", "[admin] Ti manda il sondaggio in privato, così lo vedi prima che parta nel gruppo"),
    ("modificasondaggio", "[admin] Cambia domanda/opzioni — /modificasondaggio Domanda | Opzione1 | Opzione2"),
] + comando_locandina.COMANDI_LOCANDINA + statistiche.COMANDI_STATISTICHE + app_tornei.COMANDI_APP \
    + servizio.COMANDI_SERVIZIO


def comandi_pubblici():
    return [(n, d) for n, d in COMANDI if not d.startswith("[admin]")]


def testo_help(is_utente_admin: bool) -> str:
    comandi_da_mostrare = COMANDI if is_utente_admin else comandi_pubblici()
    testo = "🎱 Comandi disponibili:\n\n"
    for nome, descrizione in comandi_da_mostrare:
        testo += f"/{nome} — {descrizione}\n"
    return testo


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] == "app":   # link "Apri" dal gruppo
        await app_tornei.invia_bottone_app(update, context)
        return
    mostra_admin = is_admin(update.effective_user.id) and update.effective_chat.type == "private"
    await update.message.reply_text(testo_help(mostra_admin))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mostra_admin = is_admin(update.effective_user.id) and update.effective_chat.type == "private"
    await update.message.reply_text(testo_help(mostra_admin))


def versione_menu() -> str:
    return hashlib.md5(json.dumps(COMANDI).encode()).hexdigest()[:8]


async def imposta_menu_comandi(application):
    """Menu di default (chat private, o prima che venga rilevato un gruppo):
    solo i comandi pubblici."""
    comandi_default = [BotCommand(nome, descrizione[:256]) for nome, descrizione in comandi_pubblici()]
    await application.bot.set_my_commands(comandi_default)


async def imposta_menu_per_gruppo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """La prima volta che il bot riceve un messaggio in un gruppo, dà agli
    amministratori DI QUEL GRUPPO il menu completo (pubblici + admin).
    Gli altri membri continuano a vedere solo il menu pubblico."""
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return

    conn = db_connect()
    chiave = f"menu_admin_impostato_{chat.id}_{versione_menu()}"  # cambia se cambiano i comandi
    if conn.execute("SELECT 1 FROM stato WHERE chiave = ?", (chiave,)).fetchone():
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


async def aggiorna_menu_admin(application):
    """All'avvio dà il menu completo agli admin in chat privata col bot e agli
    amministratori dei gruppi già noti (i comandi nuovi compaiono subito)."""
    comandi_completi = [BotCommand(n, d[:256]) for n, d in COMANDI]
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(comandi_completi, scope=BotCommandScopeChat(admin_id))
        except Exception:
            pass  # admin che non ha mai avviato il bot in privato
    conn = db_connect()
    gruppi = {int(r[0]) for r in conn.execute(
        "SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_CHAT_GIOCO_LIBERO,))}
    gruppi |= {int(r[0].split("_")[3]) for r in conn.execute(
        "SELECT chiave FROM stato WHERE chiave LIKE 'menu_admin_impostato_%'")}
    for gid in gruppi:
        try:
            await application.bot.set_my_commands(
                comandi_completi, scope=BotCommandScopeChatAdministrators(gid))
            conn.execute(
                "INSERT INTO stato (chiave, valore) VALUES (?, '1') "
                "ON CONFLICT(chiave) DO UPDATE SET valore = '1'",
                (f"menu_admin_impostato_{gid}_{versione_menu()}",),
            )
        except Exception:
            pass
    conn.commit()
