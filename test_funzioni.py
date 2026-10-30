"""Test delle funzioni di servizio: annulla, completa gironi, PDF, cancellazioni
a tempo, avviso 'Da chiamare a voce', backup.  python3 test_funzioni.py"""
import asyncio
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

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
    assert await S.esegui_backup(bot) == "invariato"
    # niente modifiche da una settimana: parte lo stesso il backup settimanale
    vecchio = (datetime.now(S.ROMA) - timedelta(days=7)).isoformat()
    S._scrivi(conn(), S.CHIAVE_ULTIMO, vecchio)
    assert await S.esegui_backup(bot) == "inviato"
    assert await S.esegui_backup(bot) == "invariato"
    print("backup ok")


def test_ripristino():
    """File valido -> riepilogo; file non valido -> errore; ripristino sostituisce i dati."""
    d = tempfile.mkdtemp()
    buono, vivo = os.path.join(d, "buono.db"), os.path.join(d, "vivo.db")
    for p, n in ((buono, 3), (vivo, 1)):
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE tornei (id INTEGER PRIMARY KEY, nome TEXT, chiuso INTEGER)")
        c.execute("CREATE TABLE squadre (torneo_id INTEGER)")
        c.execute("CREATE TABLE stato (chiave TEXT PRIMARY KEY, valore TEXT)")
        c.executemany("INSERT INTO tornei (nome, chiuso) VALUES (?, 1)", [(f"T{i}",) for i in range(n)])
        c.commit()
        c.close()
    assert "3 tornei" in S._riepilogo(buono)
    falso = os.path.join(d, "falso.db")
    open(falso, "wb").write(b"non sono un database" * 50)
    for p in (falso,):
        try:
            S._riepilogo(p)
            raise SystemExit("ERRORE: file non valido accettato")
        except ValueError:
            pass
    src, dst = sqlite3.connect(buono), sqlite3.connect(vivo)
    src.backup(dst)
    assert dst.execute("SELECT COUNT(*) FROM tornei").fetchone()[0] == 3
    print("ripristino ok")


def test_nomi_squadra():
    import bot_iscrizioni_torneo as B
    casi = {
        "Gianni🔥Pinna": "Gianni-Pinna",
        "gianni 🔥 pinna": "Gianni-Pinna",
        "Marco❤️Lirio": "Marco-Lirio",
        "👍🏻Eugenio e Flavia": "Eugenio-Flavia",
        "Maio-Socio": "Maio-Socio",
        "antonio e eva": "Antonio-Eva",
        "Niccolò/Zoë": "Niccolò-Zoë",
        "D'Angelo Rossi": "D'Angelo-Rossi",
        "🔥": "",
    }
    for dato, atteso in casi.items():
        got = B.normalizza_nome_squadra(dato)
        assert got == atteso, f"{dato!r}: {got!r} invece di {atteso!r}"
    print("nomi squadra ok")


if __name__ == "__main__":
    c = avvia()
    test_completa_gironi_e_pdf(c)
    test_annulla(c)
    test_avviso_voce()
    asyncio.run(test_cancellazioni(c))
    asyncio.run(test_backup())
    test_ripristino()
    test_nomi_squadra()
    print("tutti i test superati")
