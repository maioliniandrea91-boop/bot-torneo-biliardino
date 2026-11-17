"""Prova della sotto-scheda «Locandina» del pannello admin (Mini App) via HTTP,
con un Telegram finto: python3 test_locandina_app.py  (database temporaneo)"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

CARTELLA = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:prova"
os.environ["ADMIN_IDS"] = "1"
os.environ["DB_PATH"] = os.path.join(CARTELLA, "locandina.db")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import app_tornei as A  # noqa: E402
import comando_locandina as C  # noqa: E402
import locandina_app  # noqa: E402
import statistiche  # noqa: E402
from config import CHIAVE_CHAT_GIOCO_LIBERO, is_admin  # noqa: E402
from database import db_connect, scrivi_stato  # noqa: E402
from test_pannello import ADMIN, GRUPPO, TOKEN, UTENTE, BotFinto, init_data  # noqa: E402


class BotLoc(BotFinto):
    def __init__(self):
        super().__init__()
        self.didascalie, self.tastiere_tolte = [], []

    async def edit_message_caption(self, chat_id, message_id, caption, **k):
        self.didascalie.append((chat_id, message_id, caption))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.tastiere_tolte.append((chat_id, message_id))


class JobFinto:
    def __init__(self):
        self.lavori = {}

    def run_once(self, f, when, data=None, name=None):
        self.lavori[name] = when

    def get_jobs_by_name(self, name):
        if name not in self.lavori:
            return []
        lavori = self.lavori
        return [type("J", (), {"schedule_removal": lambda s: lavori.pop(name, None)})()]


async def main():
    A.registra(type("X", (), {"add_handler": lambda *a: None})(), db_connect,
               is_admin, None, None, TOKEN, {ADMIN})
    conn = db_connect()
    statistiche.crea_tabelle(conn)
    scrivi_stato(conn, CHIAVE_CHAT_GIOCO_LIBERO, str(GRUPPO))
    conn.commit()
    bot, jq = BotLoc(), JobFinto()
    cli = TestClient(TestServer(A.crea_web_app(bot, jq)))
    await cli.start_server()
    H = {"X-Init-Data": init_data(ADMIN)}

    async def az(uid=ADMIN, **d):
        r = await cli.post("/api/pannello/locandina", json=d, headers={"X-Init-Data": init_data(uid)})
        return r.status, await r.json()

    async def stato():
        r = await cli.get("/api/pannello/locandina", headers=H)
        assert r.status == 200
        return await r.json()

    # ---- sicurezza: solo admin
    assert (await cli.get("/api/pannello/locandina")).status == 401
    assert (await cli.get("/api/pannello/locandina", headers={"X-Init-Data": init_data(UTENTE)})).status == 403
    assert (await az(UTENTE, azione="anteprima"))[0] == 403

    s = await stato()
    p = s["predefiniti"]
    assert p["ora"] == "21:30" and p["quota"] == "20€" and p["premi"] == "BV" and p["max"] == 18
    assert datetime.fromisoformat(p["data"]).weekday() == 4   # venerdì
    assert s["bozza"] is None and s["ultima"] is None and s["gruppo"]

    data = (datetime.now(C.FUSO) + timedelta(days=10)).date()
    modulo = {"data": data.isoformat(), "formule": ["volo", "tradizionale"], "cinque_x": True,
              "ora": "22:00", "quota": "25", "premi": "coppe e medaglie", "max": 14,
              "extra": "Porta il tuo compagno!", "nuovo_torneo": True, "ripeti": True}

    # ---- controlli del modulo (stessi di /locandina)
    _, r = await az(azione="anteprima", **{**modulo, "formule": []})
    assert not r["ok"] and "formula" in r["errore"]
    _, r = await az(azione="anteprima", **{**modulo, "data": "2020-01-01"})
    assert not r["ok"] and "passata" in r["errore"]
    _, r = await az(azione="anteprima", **{**modulo, "max": 99})
    assert not r["ok"] and "Massimo" in r["errore"]
    _, r = await az(azione="anteprima", **{**modulo, "ora": "25:00"})
    assert not r["ok"]
    _, r = await az(azione="anteprima", **{**modulo, "extra": "x" * 1100})
    assert not r["ok"] and "lungo" in r["errore"]
    assert not bot.inviati   # niente in chat finché i dati non sono validi

    # ---- anteprima: immagine nel pannello + copia in privato con i bottoni
    _, r = await az(azione="anteprima", **modulo)
    assert r["ok"] and r["img"].startswith("data:image/png;base64,"), r
    b = r["bozza"]
    assert "Porta il tuo compagno!" in b["caption"] and "Massimo coppie: 14." in b["caption"]
    assert "22:00" in b["caption"] and b["nuovo_torneo"] and b["ripeti"]
    assert bot.inviati[-1][:2] == ("foto", ADMIN)
    nome = db_connect().execute("SELECT nome_torneo FROM locandine WHERE id = ?", (b["id"],)).fetchone()[0]
    assert nome == f"{data.day:02d}/{data.month:02d} 5X Volo-Tradizionale", nome
    img = await cli.get(f"/api/pannello/locandina/img?id={b['id']}", headers=H)
    assert img.status == 200 and (await img.read())[:4] == b"\x89PNG"
    assert (await stato())["bozza"]["id"] == b["id"]   # riaprendo la Mini App la ritrovi

    # anteprima rifatta: la vecchia bozza viene annullata (bottoni tolti in privato)
    _, r = await az(azione="anteprima", **{**modulo, "formule": ["rollerball"], "cinque_x": False},
                    sostituisce=b["id"])
    vecchia, b = b, r["bozza"]
    assert r["ok"] and b["id"] != vecchia["id"]
    stato_v = db_connect().execute("SELECT stato FROM locandine WHERE id = ?", (vecchia["id"],)).fetchone()[0]
    assert stato_v == "annullata" and bot.tastiere_tolte

    # ---- testo e interruttori sulla bozza (copia in privato aggiornata)
    _, r = await az(azione="testo", id=b["id"], testo="Si parte alle 22 in punto")
    assert r["ok"] and "Si parte alle 22" in r["bozza"]["caption"] and bot.didascalie
    _, r = await az(azione="ripeti", id=b["id"], valore=False)
    assert r["ok"] and not r["bozza"]["ripeti"] and "Ripubblicazione: no." in r["bozza"]["caption"]
    _, r = await az(azione="nuovo_torneo", id=b["id"], valore=False)
    assert r["ok"] and "nessun cambio" in r["bozza"]["caption"]
    await az(azione="nuovo_torneo", id=b["id"], valore=True)

    # ---- programma (orario passato rifiutato), poi annulla dall'elenco
    passato = (datetime.now(C.FUSO) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    _, r = await az(azione="programma", id=b["id"], quando=passato)
    assert not r["ok"] and "passato" in r["errore"]
    futuro = (datetime.now(C.FUSO) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    _, r = await az(azione="programma", id=b["id"], quando=futuro)
    assert r["ok"] and "programmata" in r["testo"] and f"locandina_{b['id']}" in jq.lavori
    s = await stato()
    assert [x["id"] for x in s["programmate"]] == [b["id"]] and s["bozza"] is None
    _, r = await az(azione="annulla", id=b["id"])
    assert r["ok"] and f"locandina_{b['id']}" not in jq.lavori
    assert (await stato())["programmate"] == []

    # ---- nuova anteprima e pubblicazione: apre il torneo, massimo applicato
    _, r = await az(azione="anteprima", **modulo)
    b = r["bozza"]
    _, r = await az(azione="pubblica", id=b["id"])
    assert r["ok"] and "pubblicata nel gruppo" in r["testo"] and "aperto" in r["testo"], r
    assert bot.inviati[-1][:2] == ("foto", GRUPPO) and "Porta il tuo compagno!" in bot.inviati[-1][2]
    att = db_connect().execute("SELECT nome, max_coppie FROM tornei WHERE chiuso = 0").fetchone()
    assert att == (f"{data.day:02d}/{data.month:02d} 5X Volo-Tradizionale", 14), att
    _, r = await az(azione="pubblica", id=b["id"])
    assert not r["ok"] and "già pubblicata" in r["errore"]
    _, r = await az(azione="testo", id=b["id"], testo="x")   # pubblicata: va col percorso /didascalia
    assert r["ok"] and bot.didascalie[-1][0] == GRUPPO and bot.didascalie[-1][2].startswith("🏆")
    s = await stato()
    u = s["ultima"]
    assert u["id"] == b["id"] and u["del_torneo_attivo"] and u["ripeti"] and u["prossimo"]

    # ---- ripubblica e ferma il giro
    _, r = await az(azione="ripubblica", id=b["id"])
    assert r["ok"] and "ripubblicata" in r["testo"], r
    _, r = await az(azione="ferma", id=b["id"])
    assert r["ok"] and "fermate" in r["testo"]
    assert not (await stato())["ultima"]["ripeti"]
    _, r = await az(azione="ferma", id=b["id"])
    assert not r["ok"]
    _, r = await az(azione="pubblica", id=9999)
    assert not r["ok"] and "non trovata" in r["errore"]

    # ---- argomenti dal modulo = stessi di /locandina
    dati, err = locandina_app.argomenti_da_modulo({**modulo, "quota": "25,50 €", "max": None})
    assert err is None and dati["quota"] == "25.50€" and dati["max_coppie"] is None and dati["premi"] == "COPPE E MEDAGLIE"

    await cli.close()
    print("test_locandina_app: tutto OK")


if __name__ == "__main__":
    asyncio.run(main())
