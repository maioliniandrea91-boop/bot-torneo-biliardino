"""Configurazione del bot: variabili d'ambiente e costanti condivise.

TOKEN e ADMIN_IDS vengono letti da variabili d'ambiente (impostate sulla
piattaforma di hosting, es. Railway) invece che scritti nel codice."""
import os
from datetime import time
from zoneinfo import ZoneInfo

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "iscrizioni.db")
MAX_SQUADRE = 18  # coppie massime predefinite: ogni torneo nuovo parte da qui
LIMITE_COPPIE_MIN, LIMITE_COPPIE_MAX = 4, 32   # massimo impostabile per torneo (locandina, /maxcoppie)
PER_PAGINA_STORICO = 10
NOME_TORNEO_INIZIALE = "Torneo"

# --- Sondaggio settimanale "gioco libero" ---
CHIAVE_CHAT_GIOCO_LIBERO = "chat_id_gioco_libero"   # = gruppo registrato con /registragruppo
CHIAVE_DOMANDA_SONDAGGIO = "sondaggio_domanda"
CHIAVE_OPZIONI_SONDAGGIO = "sondaggio_opzioni"
ORARIO_SONDAGGIO_GIOCO_LIBERO = time(hour=10, minute=0, tzinfo=ZoneInfo("Europe/Rome"))
DOMANDA_SONDAGGIO_DEFAULT = "Vieni al gioco libero di stasera? 🎱"
OPZIONI_SONDAGGIO_DEFAULT = ["Sì ✅", "No ❌"]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
