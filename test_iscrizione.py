"""Prova della scheda «Iscrizione» della Mini App via HTTP, con un Telegram finto:
python3 test_iscrizione.py  (database temporaneo)"""
import asyncio
import os
import tempfile

CARTELLA = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:prova"
os.environ["ADMIN_IDS"] = "1,2"
os.environ["DB_PATH"] = os.path.join(CARTELLA, "iscrizione.db")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import app_tornei as A  # noqa: E402
import statistiche as ST  # noqa: E402
from chat_telegram import aggiorna_lista_gruppo  # noqa: E402
from config import CHIAVE_CHAT_GIOCO_LIBERO, is_admin  # noqa: E402
from database import db_connect, scrivi_stato, set_iscrizioni  # noqa: E402
from test_sfide import BotFinto, init_data  # noqa: E402

ADMIN, UTENTE, ALTRO, GRUPPO = 1, 50, 60, -100
URL = "https://t.me/BotProva/sfide?startapp=iscrizione"


async def main():
    A.registra(type("X", (), {"add_handler": lambda *a: None})(), db_connect,
               is_admin, None, None, os.environ["BOT_TOKEN"], {ADMIN})
    conn = db_connect()
    ST.crea_tabelle(conn)
    scrivi_stato(conn, CHIAVE_CHAT_GIOCO_LIBERO, str(GRUPPO))
    for n in ("Giovanni", "Luca", "Matteo", "Paolo"):
        ST.trova_o_crea(conn, n)
    conn.commit()
    bot = BotFinto()
    modificati = []

    async def edit_message_text(**k):
        modificati.append(k)
    bot.edit_message_text = edit_message_text
    cli = TestClient(TestServer(A.crea_web_app(bot)))
    await cli.start_server()

    async def az(uid=UTENTE, **d):
        r = await cli.post("/api/iscrizione", json=d, headers={"X-Init-Data": init_data(uid)})
        return await r.json()

    async def stato(uid=UTENTE):
        r = await cli.get("/api/iscrizione", headers={"X-Init-Data": init_data(uid)})
        assert r.status == 200
        return await r.json()

    assert (await cli.get("/api/iscrizione")).status == 401
    s = await stato()
    assert not s["admin"] and s["gruppo"] and s["torneo"]["aperte"] and s["squadre"] == []

    # iscrizione: nome ufficiale dall'anagrafica, lista nel gruppo col bottone
    r = await az(azione="iscrivi", g1="luca", g2="Matteo")
    assert r["ok"] and r["testo"] == "✅ Iscritta: Luca-Matteo", r
    assert bot.inviati[-1][1] == GRUPPO and "Luca-Matteo" in bot.inviati[-1][2] and "Dalla app — U50" in bot.inviati[-1][2]
    assert bot.tasti[-1].inline_keyboard[0][0].url == URL
    prima = bot.n

    # utente normale: una coppia a testa (messaggio senza /ritira)
    r = await az(azione="iscrivi", g1="Paolo", g2="Giovanni")
    assert not r["ok"] and "già iscritto" in r["errore"] and "/ritira" not in r["errore"], r

    # giocatore nuovo: niente domanda «soprannome», diventa giocatore nuovo
    r = await az(ALTRO, azione="iscrivi", g1="Paolo", g2="Gianni")
    assert r["ok"] and "🆕 Gianni" in r["testo"], r
    assert ST.cerca_giocatore(conn, "Gianni") and (GRUPPO, prima) in bot.cancellati   # una sola lista viva
    r = await az(ADMIN, azione="iscrivi", g1="X", g2="x")
    assert not r["ok"] and "due volte" in r["errore"], r

    s = await stato()
    assert [(c["nome"], c["mia"]) for c in s["squadre"]] == [("Luca-Matteo", True), ("Paolo-Gianni", False)]
    assert "Gianni" in s["giocatori"]

    # ritiro: solo le proprie
    r = await az(azione="ritira", nome="Paolo-Gianni")
    assert not r["ok"], r
    r = await az(azione="ritira", nome="Luca-Matteo")
    assert r["ok"] and "Ritirata" in r["testo"], r
    assert [c["nome"] for c in (await stato())["squadre"]] == ["Paolo-Gianni"]

    # admin: più coppie
    assert (await az(ADMIN, azione="iscrivi", g1="Luca", g2="Matteo"))["ok"]
    r = await az(ADMIN, azione="iscrivi", g1="Giovanni", g2="paolo")   # Paolo è già in Paolo-Gianni
    assert not r["ok"] and "Paolo è già iscritto in Paolo-Gianni" in r["errore"] and "Gianni G." in r["errore"], r
    assert (await az(ADMIN, azione="iscrivi", g1="Giovanni", g2="Nuovo"))["ok"]
    # iscrizioni chiuse
    set_iscrizioni(conn, False)
    r = await az(azione="iscrivi", g1="Giovanni", g2="Luca")
    assert not r["ok"] and not (await stato())["torneo"]["aperte"]
    set_iscrizioni(conn, True)

    # correzione silenziosa della lista: il bottone resta
    await aggiorna_lista_gruppo(bot, conn, GRUPPO)
    assert modificati and modificati[-1]["reply_markup"].inline_keyboard[0][0].url == URL

    # stesso controllo per il comando e il pannello admin (logica comune)
    import logica_tornei as L
    e = L.iscrivi_coppia(conn, ADMIN, "admin", "Luca-Zeno", True)   # Luca è in Luca-Matteo
    assert not e.ok and "Luca è già iscritto in Luca-Matteo" in e.testo, e
    assert L.iscrivi_coppia(conn, ADMIN, "admin", "Luca G.-Zeno", True).ok   # omonimo con iniziale
    r = await cli.post("/api/pannello", json={"azione": "iscrivi", "g1": "matteo", "g2": "Ugo"},
                       headers={"X-Init-Data": init_data(ADMIN)})
    r = await r.json()
    assert not r["ok"] and "Matteo è già iscritto" in r["errore"], r

    await cli.close()
    print("test_iscrizione: tutto OK")


asyncio.run(main())
