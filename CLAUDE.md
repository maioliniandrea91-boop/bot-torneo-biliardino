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
- `python3 test_motore.py`, `python3 test_app_tornei.py` e `python3 test_funzioni.py` devono finire senza errori: li esegue anche il controllo GitHub, e se falliscono Railway non pubblica.
