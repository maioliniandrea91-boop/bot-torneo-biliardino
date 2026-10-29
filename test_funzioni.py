"""Test delle funzioni di servizio: annulla, completa gironi, PDF, cancellazioni
a tempo, avviso 'Da chiamare a voce', backup.  python3 test_funzioni.py"""
import asyncio
import json
import os
import sqlite3
import tempfile

os.environ.setdefault("BOT_TOKEN", "123:prova")
import app_tornei as A  # noqa: E402
import servizio as S  # noqa: E402
import test_app_tornei as T  # noqa: E402


def avvia():
    A.registra(type("X", (), {"add_handler": lambda *a: None})(), T.db_connect, lambda u: u == 1,
               None, None, T.TOKEN, {1})
    return A._conn()


def test_completa_gironi_e_pdf(conn):
    import tabellone_pdf
    for n, g in ((12, 3), (13, 4), (7, 2), (18, 3)):
        A.avvia_prova(conn, 1, n)
        A.esegui(conn, 1, {"azione": "crea_gironi", "n_gironi": g, "metodo": "sorteggio"})
        A.esegui(conn, 1, {"azione": "simula"})
        A.esegui(conn, 1, {"azione": "completa_gironi"})
        t = A.carica(conn)["t"]
        assert all(p["stato"] == "finita" for p in t["partite"].values() if p["tab"] == "G")
        assert tabellone_pdf.crea_pdf(t, "Prova", A.etichetta)[:4] == b"%PDF"
        A.esegui(conn, 1, {"azione": "crea_tabellone"})
        for _ in range(60):
            try:
                A.esegui(conn, 1, {"azione": "simula"})
            except ValueError:
                break
        t = A.carica(conn)["t"]
        assert tabellone_pdf.crea_pdf(t, "Prova", A.etichetta)[:4] == b"%PDF"
    print("completa gironi + PDF ok")


def test_annulla(conn):
    A.avvia_prova(conn, 1, 8)
    A.esegui(conn, 1, {"azione": "crea_gironi", "n_gironi": 2, "metodo": "sorteggio"})
    A.esegui(conn, 1, {"azione": "chiama"})
    prima = json.dumps(A.carica(conn)["t"], sort_keys=True)
    pid = next(p["id"] for p in A.carica(conn)["t"]["partite"].values() if p["stato"] == "in_corso")
    A.esegui(conn, 1, {"azione": "risultato", "pid": pid, "giochi": [[8, 3]]})
    assert "(8-3)" in A.ultima_annullabile(conn, A.ID_PROVA)
    A.esegui(conn, 1, {"azione": "annulla"})
    assert json.dumps(A.carica(conn)["t"], sort_keys=True) == prima
    for _ in range(30):
        try:
            A.esegui(conn, 1, {"azione": "simula"})
        except ValueError:
            break
    assert conn.execute("SELECT COUNT(*) FROM app_storia").fetchone()[0] <= A.STORIA_MAX
    A.esegui(conn, 1, {"azione": "ricomincia", "conferma": "RICOMINCIA"})
    A.esegui(conn, 1, {"azione": "annulla"})
    assert A.carica(conn)["t"]["fase"] != "iscrizioni"
    print("annulla ok")


def test_avviso_voce():
    righe = A._righe_da_chiamare({("A", "B", 1): False, ("B", "A", 1): False,
                                  ("C", "D", 2): True, ("D", "C", 2): False,
                                  ("E", "F", 3): True, ("F", "E", 3): True})
    assert righe == ["• A — B · biliardino 1 (entrambe)", "• D — C · biliardino 2 (solo D)"], righe
    print("avviso 'da chiamare a voce' ok")


class Bot:
    def __init__(self):
        self.cancellati, self.documenti, self.messaggi = [], [], []

    async def delete_message(self, chat, mid):
        self.cancellati.append((chat, mid))

    async def send_document(self, chat, f, filename=None, caption=None):
        self.documenti.append((chat, filename))

    async def send_message(self, chat, testo, **kw):
        self.messaggi.append((chat, testo))


async def test_cancellazioni(conn):
    bot = Bot()
    A._cancella_dopo(None, bot, 1, 99, 1)
    A.riprendi_cancellazioni(None, bot)                 # come dopo un riavvio
    await asyncio.sleep(2.5)
    assert (1, 99) in bot.cancellati
    assert conn.execute("SELECT COUNT(*) FROM app_cancella").fetchone()[0] == 0
    print("cancellazioni a tempo ok")


async def test_backup():
    db = os.path.join(tempfile.mkdtemp(), "b.db")

    def conn():
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE IF NOT EXISTS stato (chiave TEXT PRIMARY KEY, valore TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS tornei (id INTEGER)")
        return c
    S._db_connect, S._db_path, S._is_admin, S._admin_ids = conn, db, lambda u: True, {1}
    bot = Bot()
    assert await S.esegui_backup(bot) == "nessun canale"
    S._scrivi(conn(), S.CHIAVE_CANALE, "-100")
    assert await S.esegui_backup(bot) == "inviato"
    assert await S.esegui_backup(bot) == "invariato"
    c = conn()
    c.execute("INSERT INTO tornei VALUES (1)")
    c.commit()
    assert await S.esegui_backup(bot) == "inviato"
    print("backup ok")


if __name__ == "__main__":
    c = avvia()
    test_completa_gironi_e_pdf(c)
    test_annulla(c)
    test_avviso_voce()
    asyncio.run(test_cancellazioni(c))
    asyncio.run(test_backup())
    print("tutti i test superati")
