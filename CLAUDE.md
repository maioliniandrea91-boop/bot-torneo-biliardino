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
- `python3 test_motore.py`, `python3 test_app_tornei.py`, `python3 test_funzioni.py`, `python3 test_comandi.py` e `python3 test_pannello.py` devono finire senza errori: li esegue anche il controllo GitHub, e se falliscono Railway non pubblica.
- `test_comandi.py` fa girare i comandi più usati con un Telegram finto, passando dal vero `main()`. Per un riordino che non deve cambiare nulla: `python3 test_comandi.py --trascrizione prima.json` sul codice vecchio e `--trascrizione dopo.json` sul nuovo, poi confrontare i due file (devono essere identici).

## Struttura del codice
- `bot_iscrizioni_torneo.py` avvia soltanto il bot (Procfile): registra i gestori, in quest'ordine.
- Logica senza Telegram in `logica_tornei.py` (iscrizioni, tornei, storico, podio): comandi e futuro pannello admin della Mini App devono chiamare queste funzioni, non duplicarle. Ritornano un `Esito` (ok, testo, dati).
- Comandi per argomento: `cmd_iscrizioni.py`, `cmd_tornei.py`, `cmd_sondaggio.py`, `menu.py` (help e menu Telegram); `config.py` e `database.py` in comune; `chat_telegram.py` per la chat pulita.
- `pannello.py`: pannello admin «Tornei» della Mini App (scheda Tornei in `webapp/miniapp.html`), API `/api/pannello*` solo admin; chiama `logica_tornei` come i comandi. Dal pannello iscrivi/togli/rinomina mettono la lista nuova in fondo al gruppo; massimo e soglia la correggono in silenzio; il podio si pubblica prima su Telegram e poi si salva.
- Cambiare la lista `COMANDI` in `menu.py` cambia la sua impronta: all'avvio il menu viene rimandato a Telegram (normale).
