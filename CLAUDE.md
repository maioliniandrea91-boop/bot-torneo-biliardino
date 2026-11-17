# Bot torneo biliardino — regole di lavoro

## Rami e deploy
- `main` = produzione: Railway (servizio "worker") pubblica automaticamente ogni push su `main`.
- `app-tornei` = sviluppo. Si lavora qui; in produzione si va solo unendo `app-tornei` in `main`.

## Finestra vietata ai deploy
- **Mai push/merge su `main` dal venerdì alle 20:00 al sabato alle 05:00 (ora di Roma)**: c'è il torneo e un deploy riavvia il bot a metà serata.
- Prima di ogni push su `main` controllare data e ora attuali (Europe/Rome). Se si è nella finestra: push solo su `app-tornei` e rimandare il merge.
- Il blocco è anche automatico: il controllo GitHub `Finestra torneo` fallisce in quella fascia e Railway ("Wait for CI") non pubblica. Un push bloccato NON si pubblica da solo dopo: va ripubblicato (nuovo push o Redeploy su Railway).
- Evitare deploy anche mentre Andrea sta facendo prove (chiedere se in dubbio).

## Test prima di pubblicare
- `python3 test_motore.py`, `python3 test_app_tornei.py`, `python3 test_funzioni.py`, `python3 test_comandi.py`, `python3 test_pannello.py` e `python3 test_sfide.py` devono finire senza errori: li esegue anche il controllo GitHub, e se falliscono Railway non pubblica.
- `test_comandi.py` fa girare i comandi più usati con un Telegram finto, passando dal vero `main()`. Per un riordino che non deve cambiare nulla: `python3 test_comandi.py --trascrizione prima.json` sul codice vecchio e `--trascrizione dopo.json` sul nuovo, poi confrontare i due file (devono essere identici).

## Struttura del codice
- `bot_iscrizioni_torneo.py` avvia soltanto il bot (Procfile): registra i gestori, in quest'ordine.
- Logica senza Telegram in `logica_tornei.py` (iscrizioni, tornei, storico, podio): comandi e futuro pannello admin della Mini App devono chiamare queste funzioni, non duplicarle. Ritornano un `Esito` (ok, testo, dati).
- Comandi per argomento: `cmd_iscrizioni.py`, `cmd_tornei.py`, `cmd_sondaggio.py`, `menu.py` (help e menu Telegram); `config.py` e `database.py` in comune; `chat_telegram.py` per la chat pulita.
- `pannello.py`: pannello admin «Tornei» della Mini App (scheda Tornei in `webapp/miniapp.html`), API `/api/pannello*` solo admin; chiama `logica_tornei` come i comandi. Dal pannello iscrivi/togli/rinomina mettono la lista nuova in fondo al gruppo; massimo e soglia la correggono in silenzio; il podio si pubblica prima su Telegram e poi si salva.
- `sfide_app.py`: scheda «Sfide» della Mini App, per tutti i membri (API `/api/sfide`): nuova sfida con giocatori scelti da elenco, ultime sfide con annulla, classifica Elo, medagliere. Chiama le funzioni di `statistiche.py` (`salva_sfide`, `annulla_sfida`, `pagina_sfide`…) come /sfida e /annullasfida. Nome non in anagrafica: la Mini App chiede «soprannome di… / giocatore nuovo»; ogni soprannome viene segnalato agli admin (correzione: /toglisoprannome). Dopo salva e annulla la classifica nuova va in fondo al gruppo. Mini App collegata su BotFather con nome breve `sfide` (link diretto `t.me/Presenzatorneo_bot/sfide?startapp=sfide`, apre la scheda Sfide): bottone «📱 Segna una sfida» sotto ogni classifica nel gruppo e nel messaggio di /app. Ogni lunedì alle 21 la classifica viene ripubblicata in automatico nel gruppo (`classifica_settimanale`, saltata se nell'anno non ci sono sfide).
- Cambiare la lista `COMANDI` in `menu.py` cambia la sua impronta: all'avvio il menu viene rimandato a Telegram (normale).
