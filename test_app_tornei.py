"""Prova end-to-end dell'app tornei via HTTP, senza Telegram:
python3 test_app_tornei.py  (crea un database temporaneo)"""
import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3
import tempfile
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123:prova")
os.environ["PUBLIC_URL"] = "https://esempio.test"
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import app_tornei as A  # noqa: E402

TOKEN = os.environ["BOT_TOKEN"]
ADMIN = 1
DB = os.path.join(tempfile.mkdtemp(), "prova.db")


def db_connect():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS tornei (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, "
                 "creato_il TEXT, chiuso INTEGER DEFAULT 0, podio_primo TEXT, podio_secondo TEXT, "
                 "podio_terzo TEXT, foto_file_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS squadre (id INTEGER PRIMARY KEY AUTOINCREMENT, torneo_id INTEGER, "
                 "user_id INTEGER, username TEXT, nome_squadra TEXT, iscritto_il TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS stato (chiave TEXT PRIMARY KEY, valore TEXT)")
    return conn


def init_data(uid):
    campi = {"auth_date": str(int(time.time())), "query_id": "x",
             "user": json.dumps({"id": uid, "first_name": f"U{uid}"})}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()))
    chiave = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    campi["hash"] = hmac.new(chiave, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(campi)


NOMI = ["Luca", "Matteo", "Paolo", "Michele", "Ada", "Eva", "Ugo", "Rino", "Sara", "Dino",
        "Gino", "Lia", "Teo", "Ivo", "Noa", "Pia", "Elio", "Zeno", "Remo", "Olga", "Ezio", "Nina",
        "Aldo", "Bice", "Carlo", "Dora", "Enzo", "Fede", "Gaia", "Igor", "Leo", "Mara", "Nico", "Otto",
        "Piero", "Rita"]


async def main(n_coppie=11, n_gironi=3):
    A.registra(type("X", (), {"add_handler": lambda *a: None})(), db_connect,
               lambda u: u == ADMIN, None, None, TOKEN, {ADMIN})
    conn = db_connect()
    conn.execute("INSERT INTO tornei (nome, creato_il) VALUES ('Torneo prova', '2026-10-28')")
    coppie = [f"{NOMI[2 * i]}-{NOMI[2 * i + 1]}" for i in range(n_coppie)]
    for i, c in enumerate(coppie):
        conn.execute("INSERT INTO squadre (torneo_id, user_id, nome_squadra, iscritto_il) VALUES (1, ?, ?, ?)",
                     (100 + i, c, f"2026-10-2{i % 10}"))
    conn.commit()
    utente = {c: 100 + i for i, c in enumerate(coppie)}

    cli = TestClient(TestServer(A.crea_web_app()))
    await cli.start_server()

    async def az(uid, **d):
        r = await cli.post("/api/azione", json=d, headers={"X-Init-Data": init_data(uid)})
        return r.status, await r.json()

    async def stato(uid=ADMIN):
        r = await cli.get("/api/stato", headers={"X-Init-Data": init_data(uid)})
        return await r.json()

    # sicurezza
    r = await cli.get("/api/stato", headers={"X-Init-Data": "user=%7B%22id%22%3A1%7D&hash=abc"})
    assert r.status == 401
    assert (await az(100, azione="prepara"))[0] == 403
    s0 = await stato()
    assert s0["fase"] == "nessuna" and len(s0["iscritte"]) == n_coppie

    assert (await az(ADMIN, azione="prepara"))[1]["ok"]
    _, prev = await az(ADMIN, azione="anteprima_gironi", n_gironi=n_gironi, metodo="teste")
    print("stima durata:", prev["minuti"], "min · gironi:", prev["gironi"])
    _, prev2 = await az(ADMIN, azione="anteprima_gironi", n_gironi=n_gironi, metodo="sorteggio", rimescola=True)
    st, r = await az(ADMIN, azione="crea_gironi", n_gironi=n_gironi, metodo="sorteggio")
    assert st == 200, r
    s = await stato()
    assert [g for g in s["coppie"]] and s["fase"] == "gironi"
    gironi_creati = [ [r["coppia"] for r in g["classifica"]] for g in s["gironi"]]
    assert sorted(map(sorted, gironi_creati)) == sorted(map(sorted, prev2["gironi"])), "anteprima ≠ creazione"
    assert db_connect().execute("SELECT valore FROM stato WHERE chiave='aperte'").fetchone()[0] == "0"

    tv = A.token_tv(db_connect())
    assert (await cli.get("/api/stato?tv=sbagliato")).status == 404
    assert (await cli.get(f"/tv/{tv}")).status == 200
    assert (await cli.get(f"/api/stato?tv={tv}")).status == 200

    rng = random.Random(4)
    gol = s["config"]["gol"]

    # ---- /sonoio: il compagno (account 900) gestisce la coppia 0
    c0 = coppie[0]
    secondo = c0.split("-")[1]
    ok, msg = A.collega(A._conn(), 900, secondo)
    assert ok, msg
    assert not A.collega(A._conn(), 901, secondo)[0]          # già preso da 900
    assert c0 in (await stato(900))["ruolo"]["coppie"]
    utente_extra = {c0: 900}

    # ---- stand-by e assenti
    st, r = await az(ADMIN, azione="chiama")
    assert st == 200, r
    s = await stato()
    in_campo = [p for p in s["partite"] if p["stato"] == "in_corso"]
    p1 = in_campo[0]
    st, r = await az(ADMIN, azione="sospendi", pid=p1["id"])
    assert st == 200, r
    s = await stato()
    q = {p["id"]: p for p in s["partite"]}
    assert q[p1["id"]]["stato"] == "attesa" and p1["id"] in s["sospese"]
    nuova = [p for p in s["partite"] if p["stato"] == "in_corso" and p["biliardino"] == p1["biliardino"]]
    assert nuova and nuova[0]["id"] != p1["id"], "il campo doveva passare alla partita dopo"
    p2 = nuova[0]
    assente = p2["a"]
    st, r = await az(ADMIN, azione="assente", coppia=assente, giocatore=assente.split("-")[0])
    assert st == 200, r
    s = await stato()
    assert all(assente not in (p["a"], p["b"]) for p in s["partite"] if p["stato"] == "in_corso")
    assert all(p["id"] != p1["id"] for p in s["partite"] if p["stato"] == "in_corso")
    await az(ADMIN, azione="chiama")
    s = await stato()
    assert all(assente not in (p["a"], p["b"]) for p in s["partite"] if p["stato"] == "in_corso")
    await az(ADMIN, azione="assente", coppia=assente, giocatore=assente.split("-")[0], assente=False)
    await az(ADMIN, azione="riattiva", pid=p1["id"])
    s = await stato()
    assert not s["assenti"] and not s["sospese"]
    print("stand-by, assenti e /sonoio ok")

    def punteggio(bo, vant=0):
        """Punteggi reali: in finale il punteggio di 'a' parte dal vantaggio."""
        serve, va, vb, g = bo // 2 + 1, 0, 0, []
        while va < serve and vb < serve:
            if rng.random() < .5:
                g.append([gol, rng.choice([gol - 1, rng.randint(0, gol - 1)])])
            else:
                g.append([rng.choice([gol - 1, rng.randint(vant, gol - 1)]), gol])
            va += g[-1][0] > g[-1][1]
            vb += g[-1][1] > g[-1][0]
        return g

    passi = proposte = contestate = 0
    while True:
        s = await stato()
        if s["fase"] == "finito":
            break
        if s["fase"] == "gironi" and not any(p["stato"] != "finita" for p in s["partite"] if p["tab"] == "G"):
            st, r = await az(ADMIN, azione="crea_tabellone")
            assert st == 200, r
            await az(ADMIN, azione="imposta", valori={"vantaggio_finale": 2})
            continue
        st, r = await az(ADMIN, azione="chiama")
        in_corso = [p for p in (await stato())["partite"] if p["stato"] == "in_corso"]
        assert in_corso, (st, r)
        for p in in_corso:
            g = punteggio(p["regole"]["al_meglio_di"], p["regole"]["vantaggio_a"])
            passi += 1
            modo = passi % 4
            if modo == 0:            # admin
                st, r = await az(ADMIN, azione="risultato", pid=p["id"], giochi=g)
            elif modo in (1, 2):     # coppia A propone, B conferma
                estraneo = next(u for c, u in utente.items() if c not in (p["a"], p["b"]))
                assert (await az(estraneo, azione="proponi", pid=p["id"], giochi=g))[0] == 403
                chi = utente_extra.get(p["a"], utente[p["a"]])     # il compagno da /sonoio
                st, r = await az(chi, azione="proponi", pid=p["id"], giochi=g)
                assert st == 200, (r, p, g)
                st2, r2 = await az(utente[p["a"]], azione="conferma", pid=p["id"])
                assert st2 == 400                        # non può confermare da sola
                st, r = await az(utente[p["b"]], azione="conferma", pid=p["id"])
                proposte += 1
            else:                    # disaccordo -> decide l'admin
                await az(utente[p["a"]], azione="proponi", pid=p["id"], giochi=g)
                await az(utente[p["b"]], azione="contesta", pid=p["id"])
                sp = next(x for x in (await stato())["partite"] if x["id"] == p["id"])
                assert sp["proposta"]["contestata"]
                st, r = await az(ADMIN, azione="risultato", pid=p["id"], giochi=g)
                contestate += 1
            assert st == 200, (r, p, g)
        # estraneo non può toccare
    s = await stato()
    print("podio:", s["podio"], f"· partite {passi}, conferme coppie {proposte}, contestate {contestate}")
    # correzione girone a tabellone fatto: rifiutata
    g0 = next(p for p in s["partite"] if p["tab"] == "G")
    st, r = await az(ADMIN, azione="risultato", pid=g0["id"], giochi=[[gol, 0]])
    assert st == 400, r
    await cli.close()
    print("ok")


if __name__ == "__main__":
    for n, g in ((11, 3), (8, 2), (18, 3)):
        asyncio.run(main(n, g))
        os.remove(DB)
