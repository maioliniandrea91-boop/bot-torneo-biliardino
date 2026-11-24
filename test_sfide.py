"""Prova della scheda «Sfide» della Mini App via HTTP, con un Telegram finto:
python3 test_sfide.py  (database temporaneo)"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

CARTELLA = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:prova"
os.environ["ADMIN_IDS"] = "1,2"
os.environ["DB_PATH"] = os.path.join(CARTELLA, "sfide.db")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import app_tornei as A  # noqa: E402
import statistiche as ST  # noqa: E402
from config import CHIAVE_CHAT_GIOCO_LIBERO, is_admin  # noqa: E402
from database import db_connect, scrivi_stato  # noqa: E402


def init_data(uid):
    import hashlib, hmac, json, time
    from urllib.parse import urlencode
    campi = {"auth_date": str(int(time.time())), "query_id": "x",
             "user": json.dumps({"id": uid, "first_name": f"U{uid}"})}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()))
    chiave = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
    campi["hash"] = hmac.new(chiave, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(campi)


class BotFinto:
    def __init__(self):
        self.inviati, self.cancellati, self.n, self.tasti = [], [], 100, []
        self.username = "BotProva"

    async def send_message(self, chat_id, text, **k):
        self.n += 1
        self.inviati.append(("testo", chat_id, text))
        self.tasti.append(k.get("reply_markup"))
        return type("M", (), {"message_id": self.n})()

    async def delete_message(self, chat_id, message_id):
        self.cancellati.append((chat_id, message_id))

ADMIN, ADMIN2, UTENTE, ALTRO, GRUPPO = 1, 2, 50, 60, -100


async def main():
    A.registra(type("X", (), {"add_handler": lambda *a: None})(), db_connect,
               is_admin, None, None, os.environ["BOT_TOKEN"], {ADMIN, ADMIN2})
    conn = db_connect()
    ST.crea_tabelle(conn)
    scrivi_stato(conn, CHIAVE_CHAT_GIOCO_LIBERO, str(GRUPPO))
    for n in ("Giovanni", "Luca", "Matteo", "Paolo", "Michele"):
        ST.trova_o_crea(conn, n)
    conn.commit()
    bot = BotFinto()
    cli = TestClient(TestServer(A.crea_web_app(bot)))
    await cli.start_server()

    async def az(uid=UTENTE, **d):
        r = await cli.post("/api/sfide", json=d, headers={"X-Init-Data": init_data(uid)})
        return await r.json()

    async def stato(uid=UTENTE, q=""):
        r = await cli.get("/api/sfide" + q, headers={"X-Init-Data": init_data(uid)})
        assert r.status == 200
        return await r.json()

    # ---- solo da Telegram, ma per tutti (non solo admin)
    assert (await cli.get("/api/sfide")).status == 401
    s = await stato()
    assert not s["admin"] and s["gruppo"] and s["ultime"]["totale"] == 0
    assert {g["nome"] for g in s["giocatori"]} == {"Giovanni", "Luca", "Matteo", "Paolo", "Michele"}

    # ---- salva: nomi dall'elenco, classifica nuova nel gruppo
    r = await az(azione="salva", a=["luca", "Matteo"], b=["Paolo", "Michele"], va=5, vb=2)
    assert r["ok"] and "n° 1" in r["testo"], r
    assert bot.inviati[-1][1] == GRUPPO and "Registrata 1 sfida" in bot.inviati[-1][2] and "Dalla app" in bot.inviati[-1][2]
    url = bot.tasti[-1].inline_keyboard[0][0].url   # bottone sotto la classifica nel gruppo
    assert url == "https://t.me/BotProva/sfide?startapp=sfide", url
    prima = bot.n
    r = await az(azione="salva", a=["Luca", "Paolo"], b=["Matteo", "Michele"], va=3, vb=3)
    assert r["ok"] and (GRUPPO, prima) in bot.cancellati   # una sola classifica viva nel gruppo
    s = await stato()
    assert s["giocatori"][-1]["nome"] == "Giovanni"
    assert s["ultime"]["totale"] == 2 and s["ultime"]["sfide"][0]["va"] == 3
    assert s["classifica"]["righe"][0]["nome"] in ("Luca", "Matteo") and s["classifica"]["righe"][0]["g"] == 13

    # ---- doppio invio: stessa sfida entro pochi minuti (anche a coppie scambiate) rifiutata, nulla salvato
    inviati = len(bot.inviati)
    r = await az(azione="salva", a=["Luca", "Paolo"], b=["Matteo", "Michele"], va=3, vb=3)
    assert not r["ok"] and "già stata salvata" in r["errore"] and r["doppia"] == 2, r
    r = await az(azione="salva", a=["Paolo", "Michele"], b=["Matteo", "luca"], va=2, vb=5)
    assert not r["ok"] and r["doppia"] == 1, r
    assert (await stato())["ultime"]["totale"] == 2 and len(bot.inviati) == inviati
    # risultato diverso o sfida vecchia: si salva
    c = db_connect(); c.execute("UPDATE sfide SET inserita_il = ? WHERE id = 1",
                                ((datetime.now() - timedelta(minutes=ST.DOPPIA_MINUTI + 1)).isoformat(),)); c.commit()
    assert ST.sfida_doppia(c, *[[ST.cerca_giocatore(c, n)[0] for n in x] for x in (["Luca", "Matteo"], ["Paolo", "Michele"])],
                           5, 2, ST._oggi()) is None
    assert ST.sfida_doppia(c, *[[ST.cerca_giocatore(c, n)[0] for n in x] for x in (["Luca", "Paolo"], ["Matteo", "Michele"])],
                           3, 2, ST._oggi()) is None

    # ---- errori: giocatore doppio, 0-0, data futura, mancano giocatori
    for d, err in [
        (dict(a=["Luca", "luca"], b=["Paolo", "Michele"], va=1, vb=0), "due volte"),
        (dict(a=["Luca", "Matteo"], b=["Paolo", "Michele"], va=0, vb=0), "0-0"),
        (dict(a=["Luca", "Matteo"], b=["Paolo", "Michele"], va=1, vb=0,
              data=(ST._oggi() + timedelta(days=1)).isoformat()), "futuro"),
        (dict(a=["Luca", ""], b=["Paolo", "Michele"], va=1, vb=0), "quattro"),
    ]:
        r = await az(azione="salva", **d)
        assert not r["ok"] and err in r["errore"], (d, r)
    assert (await stato())["ultime"]["totale"] == 2

    # ---- nome nuovo: prima la domanda, con i nomi simili
    r = await az(azione="salva", a=["gio", "Luca"], b=["Paolo", "Michele"], va=4, vb=1)
    assert not r["ok"] and r["sconosciuti"][0]["nome"] == "Gio", r
    assert r["sconosciuti"][0]["simili"][0]["nome"] == "Giovanni"
    assert (await stato())["ultime"]["totale"] == 2   # niente salvato

    # soprannome di Giovanni: la sfida va a Giovanni, gli altri admin vengono avvisati
    gid = r["sconosciuti"][0]["simili"][0]["id"]
    avvisi = len([x for x in bot.inviati if x[1] == ADMIN2])
    r = await az(azione="salva", a=["gio", "Luca"], b=["Paolo", "Michele"], va=4, vb=1, scelte={"gio": gid})
    assert r["ok"], r
    assert ST.cerca_giocatore(db_connect(), "gio")[1] == "Giovanni"
    assert r["nomi"] == ["Giovanni", "Luca", "Paolo", "Michele"]
    assert (await stato())["ultime"]["sfide"][0]["a"] == ["Giovanni", "Luca"]
    msg = [x for x in bot.inviati if x[1] == ADMIN2]
    assert len(msg) == avvisi + 1 and "«Gio» → Giovanni" in msg[-1][2] and "/toglisoprannome" in msg[-1][2]
    assert any(x[1] == ADMIN and "soprannomi" in x[2] for x in bot.inviati)   # avvisati tutti gli admin

    # soprannome che si scontra con un giocatore già in sfida -> doppio
    r = await az(azione="salva", a=["Gianni", "Giovanni"], b=["Paolo", "Michele"], va=1, vb=0, scelte={"gianni": gid})
    assert not r["ok"] and "due volte" in r["errore"]
    assert ST.cerca_giocatore(db_connect(), "gianni") is None   # nulla salvato

    # giocatore nuovo
    r = await az(azione="salva", a=["Ugo", "Luca"], b=["Paolo", "Michele"], va=2, vb=1, scelte={"ugo": "nuovo"})
    assert r["ok"] and "🆕 Ugo" in r["testo"], r
    assert "Ugo" in {g["nome"] for g in (await stato())["giocatori"]}

    # data passata
    ieri = (ST._oggi() - timedelta(days=1)).isoformat()
    r = await az(azione="salva", a=["Luca", "Matteo"], b=["Paolo", "Michele"], va=1, vb=2, data=ieri)
    assert r["ok"] and "(oggi)" not in bot.inviati[-1][2]

    # ---- annulla: solo le proprie, l'admin tutte; classifica nuova nel gruppo
    sid = (await stato())["ultime"]["sfide"][0]["id"]
    r = await az(ALTRO, azione="annulla", id=sid)
    assert not r["ok"] and "solo le sfide inserite da te" in r["errore"]
    r = await az(azione="annulla", id=sid)
    assert r["ok"] and "annullata" in bot.inviati[-1][2] and bot.inviati[-1][1] == GRUPPO
    r = await az(azione="annulla", id=sid)
    assert not r["ok"]
    r = await az(ADMIN, azione="annulla", id=1)
    assert r["ok"]
    s = await stato(ADMIN)
    assert s["admin"] and s["ultime"]["totale"] == 3

    # ---- modifica del risultato: solo admin, stessa sfida, classifica nuova nel gruppo
    sid = (await stato())["ultime"]["sfide"][0]["id"]
    r = await az(azione="modifica", id=sid, va=4, vb=4)
    assert not r["ok"] and "Solo un admin" in r["errore"]
    for va, vb in ((0, 0), (100, 1)):
        r = await az(ADMIN, azione="modifica", id=sid, va=va, vb=vb)
        assert not r["ok"] and "non valido" in r["errore"], r
    prima = (await stato())["ultime"]["sfide"][0]
    r = await az(ADMIN, azione="modifica", id=sid, va=prima["va"], vb=prima["vb"])
    assert not r["ok"] and "è già" in r["errore"]
    r = await az(ADMIN, azione="modifica", id=sid, va=6, vb=1)
    assert r["ok"] and "corretta" in r["testo"], r
    g = bot.inviati[-1]
    assert g[1] == GRUPPO and "Prima:" in g[2] and " 6-1 " in g[2].split("Ora:")[1], g
    s = await stato(ADMIN)
    d0 = s["ultime"]["sfide"][0]
    assert d0["id"] == sid and (d0["va"], d0["vb"]) == (6, 1) and d0["modificata"] and d0["a"] == prima["a"]
    assert s["ultime"]["totale"] == 3
    r = await az(ADMIN, azione="modifica", id=1, va=1, vb=0)   # annullata
    assert not r["ok"] and "annullata" in r["errore"]

    # correzione totale: giocatori e data, numero invariato
    ieri = (ST._oggi() - timedelta(days=1)).isoformat()
    r = await az(ADMIN, azione="modifica", id=sid, a=["Giovanni", "Matteo"], b=["Paolo", "Michele"], va=3, vb=2, data=ieri)
    assert r["ok"], r
    d0 = (await stato(ADMIN))["ultime"]["sfide"][0]
    assert d0["id"] == sid and d0["a"] == ["Giovanni", "Matteo"] and (d0["va"], d0["vb"]) == (3, 2) and d0["data"] == ieri
    r = await az(ADMIN, azione="modifica", id=sid, a=["Luca", "Luca"], b=["Paolo", "Michele"], va=3, vb=2)
    assert not r["ok"] and "due volte" in r["errore"]
    r = await az(ADMIN, azione="modifica", id=sid, data=(ST._oggi() + timedelta(days=1)).isoformat())
    assert not r["ok"] and "futuro" in r["errore"]
    # nome nuovo nella correzione: stessa domanda soprannome/nuovo, poi salva
    r = await az(ADMIN, azione="modifica", id=sid, a=["Zeno", "Matteo"], b=["Paolo", "Michele"], va=3, vb=2)
    assert not r["ok"] and r["sconosciuti"][0]["nome"] == "Zeno"
    r = await az(ADMIN, azione="modifica", id=sid, a=["Zeno", "Matteo"], b=["Paolo", "Michele"], va=3, vb=2, scelte={"zeno": "nuovo"})
    assert r["ok"] and (await stato(ADMIN))["ultime"]["sfide"][0]["a"] == ["Zeno", "Matteo"]

    # ---- inserita dall'admin per conto di un giocatore: con /sonoio lui può annullarla
    r = await az(azione="salva", a=["Luca", "Matteo"], b=["Paolo", "Michele"], va=2, vb=0, per=0)
    assert not r["ok"] and "Solo un admin" in r["errore"]
    r = await az(ADMIN, azione="salva", a=["Luca", "Matteo"], b=["Paolo", "Michele"], va=2, vb=0, per=0)
    assert r["ok"] and "per Luca" in r["testo"] and "per Luca" in bot.inviati[-1][2], r
    nid = r["numero"]
    c = db_connect()
    c.execute("INSERT INTO giocatori_telegram (giocatore_id, user_id, collegato_il) VALUES (?, ?, 'x')",
              (ST.cerca_giocatore(c, "Luca")[0], ALTRO)); c.commit()
    s = await stato(ALTRO)
    x = [y for y in s["ultime"]["sfide"] if y["id"] == nid][0]
    assert x["per"] == "Luca" and x["per_uid"] == ALTRO
    r = await az(ALTRO, azione="annulla", id=nid)
    assert r["ok"], r
    # comando: logica di modifica_sfida con soli parametri del risultato
    ok, t, anni, _ = ST.modifica_sfida(db_connect(), sid, va=1, vb=1, user_id=ADMIN, admin=True)
    assert ok and anni == [int(ieri[:4])]

    # ---- paginazione, anni, medagliere
    vecchio = (datetime.now() - timedelta(hours=1)).isoformat()
    for _ in range(25):   # sfide uguali: ognuna "vecchia" di un'ora, sennò è un doppio invio
        assert (await az(azione="salva", a=["Luca", "Matteo"], b=["Paolo", "Michele"], va=1, vb=0))["ok"]
        c = db_connect(); c.execute("UPDATE sfide SET inserita_il = ?", (vecchio,)); c.commit()
    s = await stato(q="?pagina=2")
    assert s["ultime"]["pagina"] == 2 and s["ultime"]["pagine"] == 2 and len(s["ultime"]["sfide"]) == 8
    assert ST._oggi().year in s["anni"]
    s = await stato(q="?med=sempre")
    assert s["medagliere"]["anno"] is None and s["medagliere"]["righe"] == []

    # ---- classifica del lunedì: nel gruppo, con bottone, la vecchia sparisce
    vecchia = bot.n
    ST._db_connect = db_connect   # nel bot lo imposta statistiche.registra
    ctx = type("C", (), {"bot": bot})()
    await ST.classifica_settimanale(ctx)
    assert bot.inviati[-1][1] == GRUPPO and "Classifica del lunedì" in bot.inviati[-1][2]
    assert bot.tasti[-1].inline_keyboard[0][0].text == "📱 Segna una sfida"
    assert (GRUPPO, vecchia) in bot.cancellati

    # ---- comando /toglisoprannome: logica di base
    c = db_connect()
    assert ST.aggiungi_soprannome(c, "Luca", gid)   # già in anagrafica: errore
    assert ST.aggiungi_soprannome(c, "Nanni", 9999)  # giocatore inesistente: errore

    # ---- ricerca per giocatore nelle Ultime (per tutti): solo le sue sfide + bilancio
    c = db_connect()
    luca = ST.cerca_giocatore(c, "luca")[0]
    attese = c.execute("SELECT COUNT(*) FROM sfide WHERE annullata = 0 AND ? IN (a1, a2, b1, b2)", (luca,)).fetchone()[0]
    s = await stato(q="?cerca=LUCA")
    r = s["ultime"]["ricerca"]
    assert r["nomi"] == ["Luca"] and s["ultime"]["totale"] == r["sfide"] == attese > 0, r
    assert r["vinte"] + r["perse"] + r["pari"] == attese
    assert all("Luca" in x["a"] + x["b"] for x in s["ultime"]["sfide"])
    assert (await stato(q="?cerca=zzz"))["ultime"]["totale"] == 0
    parte = (await stato(q="?cerca=mi"))["ultime"]["ricerca"]   # parte del nome: Michele, ...
    assert "Michele" in parte["nomi"]
    assert "ricerca" not in (await stato())["ultime"] and (await stato())["io"] is None

    await cli.close()
    print("test_sfide: tutto OK")


if __name__ == "__main__":
    asyncio.run(main())
