"""Aiuti per scrivere in Telegram tenendo pulita la chat del gruppo:
una sola lista squadre "viva", comandi cancellati, avvisi che spariscono."""
from telegram import Update
from telegram.ext import ContextTypes

from database import get_torneo_attivo, scrivi_stato, leggi_stato
from logica_tornei import conta_squadre, testo_squadre

SECONDI_MSG_TEMPORANEI = 20  # avvisi/errori nel gruppo spariscono dopo 20 s


def in_gruppo(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


async def cancella_comando(update: Update):
    """Nel gruppo cancella il messaggio con cui l'utente ha lanciato il comando.
    Serve che il bot sia admin del gruppo col permesso 'Elimina messaggi'."""
    if in_gruppo(update):
        try:
            await update.message.delete()
        except Exception:
            pass  # permesso mancante o messaggio già cancellato


async def _elimina_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, msg_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass


async def rispondi(update: Update, context: ContextTypes.DEFAULT_TYPE, testo: str):
    """Risposta breve (errori, avvisi): in privato resta, nel gruppo sparisce
    da sola dopo SECONDI_MSG_TEMPORANEI insieme al comando dell'utente."""
    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id=chat_id, text=testo)
    if in_gruppo(update):
        await cancella_comando(update)
        context.job_queue.run_once(
            _elimina_job, SECONDI_MSG_TEMPORANEI, data=(chat_id, msg.message_id)
        )


async def sostituisci_lista(bot, conn, chat_id: int, testo: str):
    """Invia la lista aggiornata e cancella la precedente in quella chat:
    resta sempre UNA sola lista. Prima invia la nuova, poi toglie la vecchia
    (se qualcosa va storto, la lista non sparisce mai del tutto)."""
    nuovo = await bot.send_message(chat_id=chat_id, text=testo)
    chiave = f"lista_msg_{chat_id}"
    vecchio = leggi_stato(conn, chiave)
    if vecchio is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(vecchio))
        except Exception:
            pass  # già cancellata a mano o troppo vecchia (>48h): pazienza
    scrivi_stato(conn, chiave, str(nuovo.message_id))


async def pubblica_lista(update: Update, context: ContextTypes.DEFAULT_TYPE, conn,
                         torneo_id: int, nome_torneo: str, intestazione: str = ""):
    """Lista dopo un comando: nel gruppo sostituisce la precedente e cancella
    il comando dell'utente; in privato risponde e basta."""
    testo = testo_squadre(conn, torneo_id, nome_torneo)
    if intestazione:
        testo = f"{intestazione}\n\n{testo}"
    if not in_gruppo(update):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=testo)
        return
    await sostituisci_lista(context.bot, conn, update.effective_chat.id, testo)
    await cancella_comando(update)


async def ripubblica_lista_gruppo(bot, conn, chat_id: int):
    """Chiamata dalla ripubblicazione della locandina: rimette la lista sotto
    la locandina, togliendo la precedente. Se non c'è nessuna iscritta, salta."""
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    if conta_squadre(conn, torneo_id):
        await sostituisci_lista(bot, conn, chat_id, testo_squadre(conn, torneo_id, nome_torneo))
