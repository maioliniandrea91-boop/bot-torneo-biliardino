"""Database SQLite del bot: tabelle base (tornei, squadre, stato) e letture comuni.
Le altre tabelle le creano i rispettivi moduli (statistiche, locandine, app tornei)."""
import sqlite3
from datetime import datetime

from config import CHIAVE_CHAT_GIOCO_LIBERO, DB_PATH, MAX_SQUADRE, NOME_TORNEO_INIZIALE


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tornei (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            creato_il TEXT NOT NULL,
            chiuso INTEGER NOT NULL DEFAULT 0,
            podio_primo TEXT,
            podio_secondo TEXT,
            podio_terzo TEXT,
            foto_file_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS squadre (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torneo_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            nome_squadra TEXT NOT NULL,
            iscritto_il TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stato (
            chiave TEXT PRIMARY KEY,
            valore TEXT
        )
    """)
    # massimo di coppie del singolo torneo (NULL = predefinito MAX_SQUADRE)
    if "max_coppie" not in [r[1] for r in conn.execute("PRAGMA table_info(tornei)")]:
        conn.execute("ALTER TABLE tornei ADD COLUMN max_coppie INTEGER")
    conn.commit()

    # Al primissimo avvio crea il torneo iniziale, se non esiste ancora nulla.
    esiste = conn.execute("SELECT COUNT(*) FROM tornei").fetchone()[0]
    if esiste == 0:
        conn.execute(
            "INSERT INTO tornei (nome, creato_il, chiuso) VALUES (?, ?, 0)",
            (NOME_TORNEO_INIZIALE, datetime.now().isoformat()),
        )
        conn.commit()

    return conn


def leggi_stato(conn, chiave: str):
    """Valore (testo) della chiave nella tabella stato, o None."""
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (chiave,)).fetchone()
    return row[0] if row else None


def scrivi_stato(conn, chiave: str, valore: str, commit: bool = True):
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        (chiave, valore),
    )
    if commit:
        conn.commit()


def get_torneo_attivo(conn):
    """Ritorna (id, nome) del torneo attualmente aperto (l'ultimo non chiuso)."""
    row = conn.execute(
        "SELECT id, nome FROM tornei WHERE chiuso = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row
    # Fallback di sicurezza: se per qualche motivo non c'è nessun torneo
    # aperto, ne crea uno nuovo al volo.
    conn.execute(
        "INSERT INTO tornei (nome, creato_il, chiuso) VALUES (?, ?, 0)",
        (NOME_TORNEO_INIZIALE, datetime.now().isoformat()),
    )
    conn.commit()
    return get_torneo_attivo(conn)


def iscrizioni_aperte(conn) -> bool:
    valore = leggi_stato(conn, "aperte")
    return valore is None or valore == "1"


def set_iscrizioni(conn, aperte: bool):
    scrivi_stato(conn, "aperte", "1" if aperte else "0")


def chat_gruppo_registrato(conn):
    """ID del gruppo registrato con /registragruppo (o None)."""
    valore = leggi_stato(conn, CHIAVE_CHAT_GIOCO_LIBERO)
    return int(valore) if valore is not None else None


def max_coppie(conn, torneo_id) -> int:
    """Coppie massime iscrivibili a quel torneo (predefinito MAX_SQUADRE)."""
    if torneo_id is None:
        return MAX_SQUADRE
    row = conn.execute("SELECT max_coppie FROM tornei WHERE id = ?", (torneo_id,)).fetchone()
    return row[0] if row and row[0] else MAX_SQUADRE
