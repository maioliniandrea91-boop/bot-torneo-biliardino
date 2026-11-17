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
- /rinomina 3 | Nuovo nome -> (solo admin) corregge il nome di una squadra
                            (numero da /squadre, oppure nome attuale | nuovo)
- /eliminatorneo #N CONFERMA -> (solo admin) elimina per sempre un torneo
                            (anche per nome; senza CONFERMA mostra cosa elimina)
                            ARCHIVIATO dallo storico (non funziona sul
                            torneo attivo, per sicurezza)
- /torneo                -> mostra il nome del torneo attuale
- /nometorneo Nome CONFERMA -> (solo admin) apre un nuovo torneo con questo
                            nome e archivia automaticamente quello precedente
                            (squadre incluse, restano consultabili in /storico)
- /storico [pagina]       -> elenco di tutti i tornei, 10 per pagina
- /storico #N | Nome      -> mostra le squadre e il podio di un torneo passato
                            (#N = numero del torneo nell'elenco, fisso nel tempo)
- /podio [#N] Sq1 | Sq2 | Sq3 | descrizione -> (solo admin) registra e annuncia
                            il podio nel gruppo; senza #N è il torneo attivo, con #N
                            un torneo dello storico. La descrizione (facoltativa)
                            va sotto il podio, non nello storico. Se il torneo
                            attivo è vuoto chiede di indicare #N; un podio già
                            presente si sostituisce solo dall'anteprima in privato.
                            Con foto: comando nella didascalia della foto,
                            oppure in risposta alla foto. In privato col bot
                            mostra l'anteprima e, su "Pubblica", lo manda nel
                            gruppo registrato con /registragruppo.
- /chiudi                -> (solo admin) blocca nuove iscrizioni
- /apri                  -> (solo admin) riapre le iscrizioni
- /esporta               -> (solo admin) invia elenco squadre in Word (solo nomi) in privato
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

Il bot va lasciato in esecuzione (polling): in produzione gira su Railway.

STRUTTURA DEL CODICE (questo file avvia soltanto il bot)
- config.py          variabili d'ambiente, costanti, is_admin
- database.py        tabelle base (tornei, squadre, stato) e letture comuni
- logica_tornei.py   logica di iscrizioni, tornei, storico, podio — senza
                     Telegram: la usano i comandi e la userà il pannello admin
- chat_telegram.py   chat pulita: lista unica, avvisi che spariscono
- cmd_iscrizioni.py  /iscrivi /squadre /ritira /rimuovi /rinomina /soglia
                     /chiudi /apri /esporta /reset
- cmd_tornei.py      /torneo /nometorneo /storico /eliminatorneo /podio
- cmd_sondaggio.py   sondaggio del lunedì e /registragruppo
- menu.py            /help /start e menu dei comandi di Telegram
- statistiche.py     sfidine, classifica Elo, medagliere, giocatori
- comando_locandina.py + locandina.py   locandina del torneo
- app_tornei.py + motore_torneo.py      Mini App, pagina TV, gironi e doppio KO
- servizio.py        backup, ripristino, notifica errori
"""

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import app_tornei
import comando_locandina
import servizio
import statistiche
from chat_telegram import cancella_comando, rispondi, ripubblica_lista_gruppo
from cmd_iscrizioni import apri, chiudi, esporta, iscrivi, reset, rimuovi, rinomina, ritira, soglia, squadre
from cmd_sondaggio import (anteprimasondaggio, modificasondaggio, registragruppo, sondaggio,
                           sondaggio_automatico, vedisondaggio)
from cmd_tornei import bottoni_podio, eliminatorneo, nometorneo, podio, storico, torneo
from config import (ADMIN_IDS, CHIAVE_CHAT_GIOCO_LIBERO, DB_PATH, MAX_SQUADRE,
                    ORARIO_SONDAGGIO_GIOCO_LIBERO, TOKEN, is_admin)
from database import db_connect
from menu import aggiorna_menu_admin, help_cmd, imposta_menu_comandi, imposta_menu_per_gruppo, start


async def all_avvio(application):
    await imposta_menu_comandi(application)
    await aggiorna_menu_admin(application)
    await comando_locandina.ripristina_programmate(application)
    await app_tornei.avvia_server(application)


def main():
    app = (Application.builder().token(TOKEN).post_init(all_avvio)
           .post_shutdown(app_tornei.ferma_server).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("iscrivi", iscrivi))
    app.add_handler(CommandHandler("squadre", squadre))
    app.add_handler(CommandHandler("ritira", ritira))
    app.add_handler(CommandHandler("rimuovi", rimuovi))
    app.add_handler(CommandHandler("rinomina", rinomina))
    app.add_handler(CommandHandler("eliminatorneo", eliminatorneo))
    app.add_handler(CommandHandler("nometorneo", nometorneo))
    app.add_handler(CommandHandler("torneo", torneo))
    app.add_handler(CommandHandler("storico", storico))
    app.add_handler(CommandHandler("podio", podio))
    # i comandi scritti nella didascalia di una foto non li vede CommandHandler
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.CaptionRegex(r"(?i)^/podio(?:@\w+)?(?:\s|$)"), podio
    ))
    app.add_handler(CallbackQueryHandler(bottoni_podio, pattern=r"^pod:"))
    app.add_handler(CommandHandler("soglia", soglia))
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
    statistiche.registra(app, db_connect, is_admin, rispondi, cancella_comando)
    comando_locandina.registra(app, db_connect, is_admin, CHIAVE_CHAT_GIOCO_LIBERO, ADMIN_IDS, MAX_SQUADRE,
                               ripubblica_lista=ripubblica_lista_gruppo)
    app_tornei.registra(app, db_connect, is_admin, rispondi, cancella_comando, TOKEN, ADMIN_IDS)
    servizio.registra(app, db_connect, DB_PATH, is_admin, ADMIN_IDS)

    app.job_queue.run_daily(
        sondaggio_automatico,
        time=ORARIO_SONDAGGIO_GIOCO_LIBERO,
        days=(1,),  # ATTENZIONE: dalla versione 20.0 di python-telegram-bot la
                    # convenzione è cambiata: 0=domenica ... 6=sabato (non più
                    # 0=lunedì). Quindi lunedì è 1, non 0. Verificato nel
                    # sorgente della libreria (docstring di JobQueue.run_daily).
        name="sondaggio_gioco_libero_lunedi",
    )

    print("Bot avviato. Premi Ctrl+C per fermarlo.")
    app.run_polling()


if __name__ == "__main__":
    main()
