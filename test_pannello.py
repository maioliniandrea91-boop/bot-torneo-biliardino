"""Prova del pannello admin «Tornei» della Mini App via HTTP, con un Telegram finto:
python3 test_pannello.py  (database temporaneo)"""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from urllib.parse import urlencode

CARTELLA = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:prova"
os.environ["ADMIN_IDS"] = "1"
os.environ["DB_PATH"] = os.path.join(CARTELLA, "pannello.db")
from aiohttp import FormData  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import app_tornei as A  # noqa: E402
import statistiche  # noqa: E402
from config import CHIAVE_CHAT_GIOCO_LIBERO, is_admin  # noqa: E402
from database import db_connect, scrivi_stato  # noqa: E402

TOKEN = os.environ["BOT_TOKEN"]
ADMIN, UTENTE, GRUPPO = 1, 50, -100


def init_data(uid):
    campi = {"auth_date": str(int(time.time())), "query_id": "x",
             "user": json.dumps({"id": uid, "first_name": f"U{uid}"})}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()))
    chiave = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    campi["hash"] = hmac.new(chiave, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(campi)


class Msg:
    def __init__(self, mid, foto=False):
        self.message_id = mid
        self.photo = [type("F", (), {"file_id": f"FOTO{mid}"})()] if foto else []


class BotFinto:
    """Registra quello che il bot manderebbe a Telegram."""
    def __init__(self):
        self.inviati, self.cancellati, self.modificati, self.n = [], [], [], 100

    async def send_message(self, chat_id, text, **k):
        self.n += 1
        self.inviati.append(("testo", chat_id, text))
        return Msg(self.n)

    async def send_photo(self, chat_id, photo, caption=None, **k):
        self.n += 1
        self.inviati.append(("foto", chat_id, caption, len(photo)))
        return Msg(self.n, foto=True)

    async def send_document(self, chat_id, document, filename=None, **k):
        self.n += 1
        self.inviati.append(("doc", chat_id, filename))
        return Msg(self.n)

    async def delete_message(self, chat_id, message_id):
        self.cancellati.append((chat_id, message_id))

    async def edit_message_text(self, chat_id, message_id, text, **k):
        self.modificati.append((chat_id, message_id, text))


async def main():
    A.registra(type("X", (), {"add_handler": lambda *a: None})(), db_connect,
               is_admin, None, None, TOKEN, {ADMIN})
    conn = db_connect()
    statistiche.crea_tabelle(conn)
    scrivi_stato(conn, CHIAVE_CHAT_GIOCO_LIBERO, str(GRUPPO))
    statistiche.trova_o_crea(conn, "Gianni")
    conn.commit()
    bot = BotFinto()
    cli = TestClient(TestServer(A.crea_web_app(bot)))
    await cli.start_server()
    H = {"X-Init-Data": init_data(ADMIN)}

    async def az(uid=ADMIN, **d):
        r = await cli.post("/api/pannello", json=d, headers={"X-Init-Data": init_data(uid)})
        return r.status, await r.json()

    async def stato(pagina=1):
        r = await cli.get(f"/api/pannello?pagina={pagina}", headers=H)
        assert r.status == 200
        return await r.json()

    # ---- sicurezza: solo admin, solo da Telegram
    assert (await cli.get("/api/pannello")).status == 401
    assert (await cli.get("/api/pannello", headers={"X-Init-Data": init_data(UTENTE)})).status == 403
    assert (await az(UTENTE, azione="iscrivi", g1="A", g2="B"))[0] == 403

    s = await stato()
    assert s["attivo"]["squadre"] == [] and s["attivo"]["max"] == 18 and s["gruppo"]
    assert "Gianni" in s["giocatori"]

    # ---- iscrivi: nome dall'anagrafica (maiuscole), lista nuova nel gruppo
    st, r = await az(azione="iscrivi", g1="gianni", g2=" pinna 🔥")
    assert r["ok"] and "Gianni-Pinna" in r["testo"], r
    assert bot.inviati[-1][1] == GRUPPO and "Gianni-Pinna" in bot.inviati[-1][2]
    lista1 = bot.n
    _, r = await az(azione="iscrivi", g1="Luca", g2="Matteo")
    assert r["ok"] and (GRUPPO, lista1) in bot.cancellati   # resta una sola lista
    _, r = await az(azione="iscrivi", g1="gianni", g2="PINNA")
    assert not r["ok"] and "già" in r["errore"]
    _, r = await az(azione="iscrivi", g1="Solo", g2="")
    assert not r["ok"]
    s = await stato()
    assert s["attivo"]["squadre"] == ["Gianni-Pinna", "Luca-Matteo"]
    assert {"Pinna", "Luca", "Matteo"} <= set(s["giocatori"])

    # ---- rinomina per posizione, rimuovi
    _, r = await az(azione="rinomina", pos=2, g1="Luca", g2="Paolo")
    assert r["ok"] and "Luca-Matteo → Luca-Paolo" in r["testo"], r
    _, r = await az(azione="rinomina", pos=9, g1="X", g2="Y")
    assert not r["ok"]
    _, r = await az(azione="rimuovi", nome="Luca-Paolo")
    assert r["ok"] and "Rimossa" in bot.inviati[-1][2]
    assert (await stato())["attivo"]["squadre"] == ["Gianni-Pinna"]

    # ---- iscrizioni chiuse bloccano anche il pannello (come /iscrivi)
    _, r = await az(azione="iscrizioni", aperte=False)
    assert r["ok"] and not (await stato())["attivo"]["aperte"]
    _, r = await az(azione="iscrivi", g1="Ada", g2="Eva")
    assert not r["ok"] and "chiuse" in r["errore"]
    await az(azione="iscrizioni", aperte=True)

    # ---- massimo e soglia (lista corretta in silenzio, senza messaggi nuovi)
    inviati = len(bot.inviati)
    _, r = await az(azione="max", valore=12)
    assert r["ok"] and (await stato())["attivo"]["max"] == 12
    _, r = await az(azione="max", valore=99)
    assert not r["ok"]
    _, r = await az(azione="soglia", valore=6)
    assert r["ok"] and (await stato())["attivo"]["soglia"] == 6
    assert len(bot.inviati) == inviati and bot.modificati

    # ---- Word in privato
    _, r = await az(azione="esporta")
    assert r["ok"] and bot.inviati[-1][0] == "doc" and bot.inviati[-1][1] == ADMIN

    # ---- podio: attivo con squadre -> proposto l'attivo
    tid = s["attivo"]["id"]
    assert (await stato())["podio_proposto"] == tid
    form = FormData()
    for k, v in {"modo": "anteprima", "torneo_id": str(tid), "primo": "gianni pinna",
                 "secondo": "Ada-Eva", "terzo": "Ugo-Rino", "descrizione": "Grande serata"}.items():
        form.add_field(k, v)
    r = await (await cli.post("/api/pannello/podio", data=form, headers=H)).json()
    assert r["ok"] and "🥇 Gianni-Pinna" in r["caption"] and "Grande serata" in r["caption"], r
    assert not any(t[0] == "foto" for t in bot.inviati)   # l'anteprima non pubblica

    form = FormData()
    for k, v in {"modo": "pubblica", "torneo_id": str(tid), "primo": "Gianni-Pinna",
                 "secondo": "Ada-Eva", "terzo": "Ugo-Rino"}.items():
        form.add_field(k, v)
    form.add_field("foto", b"\xff\xd8finto-jpeg" * 100, filename="podio.jpg", content_type="image/jpeg")
    r = await (await cli.post("/api/pannello/podio", data=form, headers=H)).json()
    assert r["ok"], r
    assert bot.inviati[-1][0] == "foto" and bot.inviati[-1][1] == GRUPPO
    row = db_connect().execute("SELECT podio_primo, foto_file_id FROM tornei WHERE id = ?", (tid,)).fetchone()
    assert row[0] == "Gianni-Pinna" and row[1].startswith("FOTO")

    # podio già presente: l'anteprima avvisa che verrà sostituito
    form = FormData()
    for k, v in {"modo": "anteprima", "torneo_id": str(tid), "primo": "A-B", "secondo": "C-D", "terzo": ""}.items():
        form.add_field(k, v)
    r = await (await cli.post("/api/pannello/podio", data=form, headers=H)).json()
    assert not r["ok"]   # manca il terzo
    form = FormData()
    for k, v in {"modo": "anteprima", "torneo_id": str(tid), "primo": "A-B", "secondo": "C-D", "terzo": "E-F"}.items():
        form.add_field(k, v)
    r = await (await cli.post("/api/pannello/podio", data=form, headers=H)).json()
    assert r["ok"] and "già un podio" in r["avviso"] and r["caption"].startswith("🔄 Podio aggiornato")

    # ---- vedi e modifica il podio pubblicato: tiene la foto, post vecchio tolto, avviso in testa
    _, r = await az(azione="dettaglio", id=tid)
    pp = r["torneo"]["podio_pubblicato"]
    assert pp["primo"] == "Gianni-Pinna" and pp["foto"] and pp["nel_gruppo"] and pp["descrizione"] == "", pp
    msg_vecchio = db_connect().execute("SELECT podio_msg FROM tornei WHERE id = ?", (tid,)).fetchone()[0]
    form = FormData()
    for k, v in {"modo": "pubblica", "torneo_id": str(tid), "primo": "Gianni-Pinna", "secondo": "Ugo-Rino",
                 "terzo": "Ada-Eva", "descrizione": "Corretto il 2° posto", "tieni_foto": "1"}.items():
        form.add_field(k, v)
    r = await (await cli.post("/api/pannello/podio", data=form, headers=H)).json()
    assert r["ok"] and "aggiornato" in r["testo"], r
    assert bot.inviati[-1][0] == "foto" and bot.inviati[-1][2].startswith("🔄 Podio aggiornato\n\n🏆 Podio")
    assert (GRUPPO, msg_vecchio) in bot.cancellati
    row = db_connect().execute("SELECT podio_secondo, podio_descrizione, foto_file_id, podio_msg FROM tornei WHERE id = ?",
                               (tid,)).fetchone()
    assert row[0] == "Ugo-Rino" and row[1] == "Corretto il 2° posto" and row[2].startswith("FOTO") and row[3] != msg_vecchio

    # ---- storico per giocatore: tornei, compagno, piazzamento e medaglie
    r = await (await cli.get("/api/pannello?cerca=pinna", headers=H)).json()
    sr = r["storico_ricerca"]
    assert sr["nomi"] == ["Pinna"] and sr["tornei"][0]["id"] == tid, sr
    assert sr["tornei"][0]["compagno"] == "Gianni" and sr["tornei"][0]["pos"] == 0 and sr["medaglie"][0] == 1, sr
    assert (await stato())["storico_ricerca"] is None
    assert (await (await cli.get("/api/pannello?cerca=zzz", headers=H)).json())["storico_ricerca"]["nomi"] == []

    # ---- nuovo torneo vuoto: il podio propone il precedente
    import logica_tornei as L
    assert L.apri_nuovo_torneo(db_connect(), "Venerdì 21/11").ok
    s = await stato()
    assert s["podio_proposto"] == tid and s["attivo"]["id"] != tid

    # ---- storico: dettaglio, elimina (mai l'attivo)
    _, r = await az(azione="dettaglio", id=tid)
    assert r["ok"] and r["torneo"]["squadre"] == ["Gianni-Pinna"] and not r["torneo"]["attivo"]
    _, r = await az(azione="elimina_esamina", id=s["attivo"]["id"])
    assert not r["ok"] and "ATTIVO" in r["errore"]
    _, r = await az(azione="elimina_esamina", id=tid)
    assert r["ok"] and "CONFERMA" not in r["testo"] and "medagliere" in r["testo"], r
    _, r = await az(azione="elimina", id=tid)
    assert r["ok"]
    _, r = await az(azione="dettaglio", id=tid)
    assert not r["ok"]
    _, r = await az(azione="dettaglio", id="x")
    assert not r["ok"]
    assert [t["id"] for t in (await stato())["storico"]["tornei"]] == [s["attivo"]["id"]]

    # ---- riordino numeri: anteprima, blocchi, backup obbligatorio, rinumera tutto
    from datetime import datetime as DT
    import servizio
    c = db_connect()
    for nome in ("T3", "T4"):
        assert L.apri_nuovo_torneo(c, nome).ok
    att = c.execute("SELECT id FROM tornei WHERE chiuso = 0").fetchone()[0]
    c.execute("INSERT INTO squadre (torneo_id, user_id, nome_squadra, iscritto_il) VALUES (?, 1, 'A-B', 'x')", (att,))
    c.execute("INSERT INTO app_storia (torneo_id, stato, proposte, utenti, azione, quando) VALUES (?, '{}', '{}', '{}', 'x', 'x')", (att,))
    c.commit()
    vecchi = [r[0] for r in c.execute("SELECT id FROM tornei ORDER BY id")]
    assert vecchi != list(range(1, len(vecchi) + 1)), vecchi
    _, r = await az(azione="riordina_esamina")
    assert r["ok"] and f"#{att} → #{len(vecchi)}" in r["testo"], r
    assert L.blocco_riordino(c, DT(2026, 11, 27, 21)) and L.blocco_riordino(c, DT(2026, 11, 28, 4))
    assert L.blocco_riordino(c, DT(2026, 11, 24, 21)) is None
    servizio._db_connect, servizio._db_path = db_connect, os.environ["DB_PATH"]
    _, r = await az(azione="riordina")   # canale backup non collegato: non si tocca nulla
    assert not r["ok"] and "Backup" in r["errore"], r
    assert [r[0] for r in db_connect().execute("SELECT id FROM tornei ORDER BY id")] == vecchi
    servizio._scrivi(db_connect(), servizio.CHIAVE_CANALE, "-100999")
    docs = len([x for x in bot.inviati if x[0] == "doc"])
    _, r = await az(azione="riordina")
    assert r["ok"] and "rinumerati" in r["testo"], r
    assert len([x for x in bot.inviati if x[0] == "doc"]) == docs + 1   # backup prima
    c = db_connect()
    assert [r[0] for r in c.execute("SELECT id FROM tornei ORDER BY id")] == list(range(1, len(vecchi) + 1))
    nuovo = len(vecchi)
    assert c.execute("SELECT torneo_id FROM squadre WHERE nome_squadra = 'A-B'").fetchone()[0] == nuovo
    assert c.execute("SELECT COUNT(*) FROM app_storia WHERE torneo_id = ?", (nuovo,)).fetchone()[0] == 1
    assert c.execute("SELECT id FROM tornei WHERE chiuso = 0").fetchone()[0] == nuovo
    assert (await stato())["attivo"]["id"] == nuovo
    assert L.apri_nuovo_torneo(c, "T5").ok
    assert c.execute("SELECT MAX(id) FROM tornei").fetchone()[0] == nuovo + 1   # il prossimo segue
    _, r = await az(azione="riordina_esamina")
    assert not r["ok"] and "già in ordine" in r["errore"]

    await cli.close()
    print("test_pannello: tutto OK")


if __name__ == "__main__":
    asyncio.run(main())
