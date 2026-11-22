"""
Statistiche del circolo: anagrafica giocatori, sfidine fuori torneo con
classifica Elo annuale, medagliere dei tornei per giocatore.

ANAGRAFICA (base comune per sfidine, medagliere e future app tornei)
  - Un giocatore = un nome. Maiuscole/minuscole non contano.
  - I nomi nuovi vengono creati in automatico (il bot avvisa con 🆕).
  - /giocatori                        -> elenco con partite giocate
  - /rinominagiocatore Vecchio | Nuovo -> (admin) corregge un nome
  - /unisci Doppione | Giusto          -> (admin) fonde due giocatori: il
                                          doppione diventa un soprannome
                                          del giusto, con tutte le sue sfide

SFIDINE (sempre 2 vs 2, si segna quante partite ha vinto ciascuna coppia)
  /sfida Luca-Matteo Paolo-Michele 5-2
  Più sfide in un messaggio, una per riga. Data facoltativa in testa
  (per caricare serate passate):
      /sfida 26/10
      Luca-Matteo Paolo-Michele 5-2
      Luca-Paolo Matteo-Michele 3-3
  Nomi con spazi: separa le coppie con "vs" (Pier Luigi-Ugo vs Ada-Eva 4-1).
  Pareggio ammesso. Chiunque può inserire, senza conferma.
  - /sfide               -> ultime sfide inserite, con il loro numero
  - /annullasfida N      -> annulla la sfida N (chi l'ha inserita o admin)
  - /classifica [anno]   -> classifica Elo dell'anno solare (default: anno
                            in corso). Elo partita per partita, 1000 a inizio
                            anno, K=32, forza coppia = media dei due.

MEDAGLIERE (tornei, per giocatore, ordine olimpico: ori > argenti > bronzi)
  - /medagliere [anno|sempre] -> default anno in corso
  - Il podio viene letto da /podio (nomi squadra "Nome1-Nome2").
  - Esclusi in automatico i tornei con "5X" nel nome.
  - /escluditorneo Nome, /includitorneo Nome -> (admin) forza l'esclusione
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime
from datetime import time as dtime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

FUSO = ZoneInfo("Europe/Rome")
ELO_BASE = 1000.0
ELO_K = 32.0
MAX_MSG = 4000  # Telegram: 4096 caratteri per messaggio, teniamo margine

# Impostati da registra()
_db_connect = None
_is_admin = None
_rispondi = None
_cancella_comando = None


# ======================= DATABASE =======================

def crea_tabelle(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS giocatori (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            creato_il TEXT NOT NULL
        )
    """)
    # ogni nome con cui un giocatore può essere scritto (minuscolo) -> id.
    # Il nome principale è sempre anche un alias di se stesso.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS giocatori_alias (
            alias TEXT PRIMARY KEY,
            giocatore_id INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sfide (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            a1 INTEGER NOT NULL, a2 INTEGER NOT NULL,
            b1 INTEGER NOT NULL, b2 INTEGER NOT NULL,
            vinte_a INTEGER NOT NULL, vinte_b INTEGER NOT NULL,
            inserita_da INTEGER,
            inserita_il TEXT NOT NULL,
            annullata INTEGER NOT NULL DEFAULT 0
        )
    """)
    # account Telegram collegato a un giocatore (/sonoio): uno a uno
    conn.execute("""
        CREATE TABLE IF NOT EXISTS giocatori_telegram (
            giocatore_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE,
            collegato_il TEXT NOT NULL
        )
    """)
    colonne = [r[1] for r in conn.execute("PRAGMA table_info(tornei)")]
    if "escluso_medagliere" not in colonne:
        # NULL = automatico (5X escluso), 1 = escluso a mano, 0 = incluso a mano
        conn.execute("ALTER TABLE tornei ADD COLUMN escluso_medagliere INTEGER")
    if "data_torneo" not in colonne:
        conn.execute("ALTER TABLE tornei ADD COLUMN data_torneo TEXT")
    conn.commit()


def _conn():
    conn = _db_connect()
    crea_tabelle(conn)
    return conn


# ======================= GIOCATORI =======================

def pulisci_nome(nome: str) -> str:
    nome = " ".join(nome.split()).strip(" .,;")
    return " ".join(w[:1].upper() + w[1:] for w in nome.split(" "))


def cerca_giocatore(conn, nome: str):
    row = conn.execute(
        "SELECT g.id, g.nome FROM giocatori_alias a JOIN giocatori g ON g.id = a.giocatore_id "
        "WHERE a.alias = ?", (nome.strip().lower(),)
    ).fetchone()
    return row


def trova_o_crea(conn, nome: str):
    """Ritorna (id, nome, nuovo?)."""
    nome = pulisci_nome(nome)
    row = cerca_giocatore(conn, nome)
    if row:
        return row[0], row[1], False
    cur = conn.execute(
        "INSERT INTO giocatori (nome, creato_il) VALUES (?, ?)", (nome, datetime.now().isoformat())
    )
    conn.execute("INSERT INTO giocatori_alias (alias, giocatore_id) VALUES (?, ?)",
                 (nome.lower(), cur.lastrowid))
    return cur.lastrowid, nome, True


def nome_canonico(conn, nome: str) -> str:
    """Nome ufficiale se il giocatore esiste, altrimenti il nome pulito."""
    row = cerca_giocatore(conn, nome)
    return row[1] if row else pulisci_nome(nome)


def giocatori_per_uso(conn) -> list[dict]:
    """Anagrafica per la scelta da elenco: prima chi ha giocato più di recente,
    poi gli altri in ordine alfabetico."""
    rows = conn.execute("""
        SELECT g.id, g.nome,
          (SELECT MAX(s.id) FROM sfide s WHERE s.annullata = 0 AND g.id IN (s.a1, s.a2, s.b1, s.b2))
        FROM giocatori g
    """).fetchall()
    rows.sort(key=lambda r: (r[2] is None, -(r[2] or 0), r[1].lower()))
    return [{"id": gid, "nome": nome} for gid, nome, _ in rows]


def nomi_simili(conn, nome: str, quanti: int = 4) -> list[dict]:
    """Giocatori con un nome simile (per «è un soprannome di…?»)."""
    chiave = pulisci_nome(nome).lower()
    punti = {}
    for a, gid, gnome in conn.execute(
            "SELECT a.alias, g.id, g.nome FROM giocatori_alias a JOIN giocatori g ON g.id = a.giocatore_id"):
        r = difflib.SequenceMatcher(None, chiave, a).ratio()
        if a.startswith(chiave) or chiave.startswith(a):
            r = max(r, 0.8)
        if r >= 0.5 and r > punti.get(gid, (0, ""))[0]:
            punti[gid] = (r, gnome)
    migliori = sorted(punti.items(), key=lambda kv: (-kv[1][0], kv[1][1].lower()))[:quanti]
    return [{"id": gid, "nome": gnome} for gid, (_, gnome) in migliori]


def aggiungi_soprannome(conn, soprannome: str, giocatore_id: int) -> str | None:
    """Collega un nome nuovo a un giocatore esistente. Ritorna un errore o None (senza commit)."""
    chiave = pulisci_nome(soprannome).lower()
    if not chiave:
        return "Soprannome vuoto."
    if cerca_giocatore(conn, chiave):
        return f"«{soprannome}» è già in anagrafica."
    if not conn.execute("SELECT 1 FROM giocatori WHERE id = ?", (giocatore_id,)).fetchone():
        return "Giocatore non trovato."
    conn.execute("INSERT INTO giocatori_alias (alias, giocatore_id) VALUES (?, ?)", (chiave, giocatore_id))
    return None


def dividi_coppia(testo: str):
    """'Luca-Matteo' -> ['Luca', 'Matteo'] (separatori - / & +, o 'e')."""
    parti = [p for p in re.split(r"\s*[-/&+]\s*|\s+(?:e|ed)\s+", testo.strip(), flags=re.I) if p.strip()]
    return parti if len(parti) == 2 else None


def registra_coppia(conn, nome_squadra: str):
    """Usata da /iscrivi e /podio: mette in anagrafica i due giocatori."""
    coppia = dividi_coppia(nome_squadra or "")
    if coppia:
        for n in coppia:
            trova_o_crea(conn, n)
        conn.commit()


# ======================= SFIDE: LETTURA =======================

RE_PUNTEGGIO = re.compile(r"(\d{1,2})\s*[-–:]\s*(\d{1,2})\s*$")
RE_DATA = re.compile(r"^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?$")


def leggi_data(testo: str, oggi: date):
    m = RE_DATA.match(testo.strip())
    if not m:
        return None
    g, mese, anno = int(m.group(1)), int(m.group(2)), m.group(3)
    if anno:
        anno = int(anno) + (2000 if len(anno) == 2 else 0)
    else:
        anno = oggi.year
    try:
        d = date(anno, mese, g)
    except ValueError:
        return None
    if not m.group(3) and d > oggi:
        d = date(anno - 1, mese, g)  # "28/12" scritto a gennaio = anno scorso
    return d


def leggi_riga(riga: str):
    """'Luca-Matteo Paolo-Michele 5-2' -> ((n1,n2),(n3,n4),5,2) oppure errore str."""
    m = RE_PUNTEGGIO.search(riga)
    if not m:
        return "manca il risultato in fondo (es. 5-2)"
    va, vb = int(m.group(1)), int(m.group(2))
    resto = riga[:m.start()].strip()
    pezzi = re.split(r"\s+(?:vs\.?|contro|v)\s+", resto, flags=re.I)
    if len(pezzi) != 2:
        pezzi = resto.split()
    if len(pezzi) != 2:
        return "non riconosco le due coppie (scrivile Nome1-Nome2 Nome3-Nome4, o separale con vs)"
    a, b = dividi_coppia(pezzi[0]), dividi_coppia(pezzi[1])
    if not a or not b:
        return "ogni coppia va scritta Nome1-Nome2"
    if va + vb == 0:
        return "risultato 0-0: nessuna partita"
    return a, b, va, vb


# ======================= SFIDE: SALVA E ANNULLA (senza Telegram) =======================
# Una sola logica, due ingressi: /sfida e /annullasfida, e la scheda «Sfide» della Mini App.

def salva_sfide(conn, lette, data_sfide: date, user_id) -> tuple[list[int], list[str]]:
    """lette = [((n1, n2), (n3, n4), vinte_a, vinte_b), ...] già controllate.
    Crea i giocatori mancanti e salva tutto. Ritorna (numeri sfide, nomi nuovi)."""
    nuovi, numeri = [], []
    for a, b, va, vb in lette:
        ids = []
        for n in list(a) + list(b):
            gid, nome, nuovo = trova_o_crea(conn, n)
            ids.append(gid)
            if nuovo:
                nuovi.append(nome)
        cur = conn.execute(
            "INSERT INTO sfide (data, a1, a2, b1, b2, vinte_a, vinte_b, inserita_da, inserita_il) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data_sfide.isoformat(), *ids, va, vb, user_id, datetime.now().isoformat()),
        )
        numeri.append(cur.lastrowid)
    conn.commit()
    return numeri, nuovi


def intestazione_sfide(numeri: list[int], nuovi: list[str], data_sfide: date, oggi: date) -> str:
    quando = "oggi" if data_sfide == oggi else data_sfide.strftime("%d/%m/%Y")
    n = len(numeri)
    intest = (f"✅ {'Registrata 1 sfida' if n == 1 else f'Registrate {n} sfide'} ({quando}): "
              + ", ".join(f"n° {x}" for x in numeri))
    if nuovi:
        intest += "\n🆕 Nuovi giocatori: " + ", ".join(nuovi) + " (se è un errore di battitura, avvisa un admin)"
    return intest


def annulla_sfida(conn, sid: int, user_id: int, admin: bool):
    """Ritorna (ok, testo, anno della sfida)."""
    row = conn.execute("SELECT inserita_da, annullata, data FROM sfide WHERE id = ?", (sid,)).fetchone()
    if not row or row[1]:
        return False, f"Sfida n° {sid} non trovata o già annullata.", None
    if row[0] != user_id and not admin:
        return False, "Puoi annullare solo le sfide inserite da te (o chiedi a un admin).", None
    conn.execute("UPDATE sfide SET annullata = 1 WHERE id = ?", (sid,))
    conn.commit()
    return True, f"🗑️ Sfida n° {sid} annullata.", int(row[2][:4])


def pagina_sfide(conn, pagina: int = 1, per_pagina: int = 20) -> dict:
    nomi = dict(conn.execute("SELECT id, nome FROM giocatori").fetchall())
    totale = conn.execute("SELECT COUNT(*) FROM sfide WHERE annullata = 0").fetchone()[0]
    pagine = max(1, -(-totale // per_pagina))
    pagina = min(max(1, pagina), pagine)
    rows = conn.execute(
        "SELECT id, data, a1, a2, b1, b2, vinte_a, vinte_b, inserita_da FROM sfide WHERE annullata = 0 "
        "ORDER BY id DESC LIMIT ? OFFSET ?", (per_pagina, (pagina - 1) * per_pagina)).fetchall()
    return {"pagina": pagina, "pagine": pagine, "totale": totale,
            "sfide": [{"id": sid, "data": d, "a": [nomi[a1], nomi[a2]], "b": [nomi[b1], nomi[b2]],
                       "va": va, "vb": vb, "da": da} for sid, d, a1, a2, b1, b2, va, vb, da in rows]}


def anni_con_dati(conn) -> list[int]:
    anni = {int(r[0]) for r in conn.execute("SELECT DISTINCT substr(data, 1, 4) FROM sfide WHERE annullata = 0")}
    anni.add(_oggi().year)
    return sorted(anni, reverse=True)


# ======================= ELO E CLASSIFICA =======================

def _sfide_anno(conn, anno: int):
    return conn.execute(
        "SELECT id, a1, a2, b1, b2, vinte_a, vinte_b FROM sfide "
        "WHERE annullata = 0 AND substr(data, 1, 4) = ? ORDER BY data, id",
        (str(anno),),
    ).fetchall()


def calcola_classifica(conn, anno: int):
    """Ricalcola da zero: annullare o correggere una sfida non lascia residui.
    Ritorna lista di dict ordinata per Elo."""
    elo, giocate, vinte = {}, {}, {}
    for _, a1, a2, b1, b2, va, vb in _sfide_anno(conn, anno):
        for p in (a1, a2, b1, b2):
            elo.setdefault(p, ELO_BASE)
        ra = (elo[a1] + elo[a2]) / 2
        rb = (elo[b1] + elo[b2]) / 2
        atteso_a = 1 / (1 + 10 ** ((rb - ra) / 400))
        n = va + vb
        delta = ELO_K * (va - n * atteso_a)  # = somma delle n partite
        for p in (a1, a2):
            elo[p] += delta
            giocate[p] = giocate.get(p, 0) + n
            vinte[p] = vinte.get(p, 0) + va
        for p in (b1, b2):
            elo[p] -= delta
            giocate[p] = giocate.get(p, 0) + n
            vinte[p] = vinte.get(p, 0) + vb
    nomi = dict(conn.execute("SELECT id, nome FROM giocatori").fetchall())
    righe = [
        {"nome": nomi.get(p, "?"), "elo": elo[p], "g": giocate[p], "v": vinte[p],
         "perc": 100 * vinte[p] / giocate[p] if giocate[p] else 0}
        for p in elo
    ]
    righe.sort(key=lambda r: (-r["elo"], -r["perc"], r["nome"].lower()))
    return righe


def _tabella(intestazione: str, righe_txt: list[str], piede: str = "") -> str:
    corpo = "\n".join(righe_txt)
    testo = f"{intestazione}\n<pre>{corpo}</pre>"
    if piede:
        testo += f"\n{piede}"
    return testo


def testo_classifica(conn, anno: int) -> str:
    righe = calcola_classifica(conn, anno)
    if not righe:
        return f"📊 <b>Classifica sfidine {anno}</b>\n\nNessuna sfida registrata."
    w = min(max(len(r["nome"]) for r in righe), 14)
    tab = [f"{'#':>2} {'Giocatore':<{w}} {'Elo':>4} {'G':>3} {'V':>3} {'%':>3}"]
    medaglie = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(righe, start=1):
        tab.append(f"{i:>2} {escape(r['nome'][:w]):<{w}} {r['elo']:>4.0f} {r['g']:>3} {r['v']:>3} {r['perc']:>3.0f}")
    testo = _tabella(f"📊 <b>Classifica sfidine {anno}</b>", tab,
                     "G = partite giocate · V = vinte · Elo parte da 1000")
    if righe:
        podio = " ".join(f"{medaglie[i]} {escape(r['nome'])}" for i, r in enumerate(righe[:3], start=1))
        testo = testo.replace("</b>\n", f"</b>\n{podio}\n", 1)
    return testo[:MAX_MSG]


# ======================= MEDAGLIERE =======================

def _anno_torneo(data_torneo, creato_il) -> int | None:
    for v in (data_torneo, creato_il):
        if v and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
    return None


def torneo_escluso(nome: str, flag) -> bool:
    if flag is not None:
        return bool(flag)
    return "5x" in (nome or "").lower()


def calcola_medagliere(conn, anno: int | None):
    """anno=None -> di sempre. Ritorna (righe, tornei_contati, podi_non_letti)."""
    tornei = conn.execute(
        "SELECT nome, podio_primo, podio_secondo, podio_terzo, data_torneo, creato_il, escluso_medagliere "
        "FROM tornei WHERE podio_primo IS NOT NULL"
    ).fetchall()
    med, contati, non_letti = {}, 0, []
    for nome, p1, p2, p3, dt, cr, flag in tornei:
        if torneo_escluso(nome, flag):
            continue
        if anno is not None and _anno_torneo(dt, cr) != anno:
            continue
        contati += 1
        for pos, squadra in enumerate((p1, p2, p3)):
            coppia = dividi_coppia(squadra or "")
            if not coppia:
                if squadra:
                    non_letti.append(f"{nome}: {squadra}")
                continue
            for g in coppia:
                n = nome_canonico(conn, g)
                med.setdefault(n, [0, 0, 0])[pos] += 1
    righe = sorted(med.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[1][2], kv[0].lower()))
    return righe, contati, non_letti


def testo_medagliere(conn, anno: int | None) -> str:
    righe, contati, non_letti = calcola_medagliere(conn, anno)
    titolo = f"🏅 <b>Medagliere tornei {anno if anno else '— di sempre'}</b>"
    if not righe:
        return f"{titolo}\n\nNessun podio registrato."
    w = min(max(len(n) for n, _ in righe), 14)
    tab = [f"{'#':>2} {'Giocatore':<{w}}  🥇  🥈  🥉"]
    for i, (n, (o, a, b)) in enumerate(righe, start=1):
        tab.append(f"{i:>2} {escape(n[:w]):<{w}} {o:>3} {a:>3} {b:>3}")
    piede = f"Tornei conteggiati: {contati} (esclusi i 5X)"
    if non_letti:
        piede += "\n⚠️ Podi non leggibili (non nel formato Nome1-Nome2): " + escape("; ".join(non_letti))
    return _tabella(titolo, tab, piede)[:MAX_MSG]


# ======================= INVIO NEL GRUPPO =======================

APP_SFIDE = "sfide"   # nome breve della Mini App su BotFather (/newapp): t.me/<bot>/sfide


def bottone_sfide(bot):
    """Bottone «Segna una sfida»: apre la Mini App sulla scheda Sfide con un tocco, anche dal gruppo."""
    try:
        nome = bot.username
    except Exception:
        nome = None
    if not nome:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📱 Segna una sfida", url=f"https://t.me/{nome}/{APP_SFIDE}?startapp=sfide")]])


def bottone_iscrizione(bot):
    """Bottone «Iscriviti»: apre la Mini App sulla scheda Iscrizione (stessa app
    BotFather delle sfide, cambia solo startapp). Sotto la lista squadre e la locandina."""
    try:
        nome = bot.username
    except Exception:
        nome = None
    if not nome:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📝 Iscriviti / ritirati", url=f"https://t.me/{nome}/{APP_SFIDE}?startapp=iscrizione")]])


async def _sostituisci(bot, conn, chat_id: int, chiave: str, testo: str):
    """Come la lista squadre: un solo messaggio vivo per tipo, il vecchio sparisce.
    Sotto la classifica c'è sempre il bottone per segnare una sfida."""
    extra = {}
    if chiave == "classifica_msg" and (tasti := bottone_sfide(bot)):
        extra["reply_markup"] = tasti
    nuovo = await bot.send_message(chat_id=chat_id, text=testo, parse_mode=ParseMode.HTML, **extra)
    k = f"{chiave}_{chat_id}"
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (k,)).fetchone()
    if row:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(row[0]))
        except Exception:
            pass
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        (k, str(nuovo.message_id)),
    )
    conn.commit()


def _in_gruppo(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


async def _mostra(update: Update, context, conn, chiave: str, testo: str):
    if _in_gruppo(update):
        await _sostituisci(context.bot, conn, update.effective_chat.id, chiave, testo)
        await _cancella_comando(update)
    else:
        await context.bot.send_message(update.effective_chat.id, testo, parse_mode=ParseMode.HTML)


def _oggi() -> date:
    return datetime.now(FUSO).date()


# ======================= COMANDI =======================

ESEMPIO_SFIDA = (
    "Uso: /sfida Luca-Matteo Paolo-Michele 5-2\n"
    "Più sfide: una per riga. Data facoltativa in testa: /sfida 26/10 e sotto le righe."
)


async def sfida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    testo = update.message.text or ""
    testo = re.sub(r"^/sfida(?:@\w+)?", "", testo, count=1, flags=re.I).strip()
    righe = [r.strip() for r in testo.splitlines() if r.strip()]
    if not righe:
        await _rispondi(update, context, ESEMPIO_SFIDA)
        return

    oggi = _oggi()
    data_sfide = oggi
    # data in testa: da sola sulla prima riga, o come prima parola
    prima = righe[0].split(maxsplit=1)
    d = leggi_data(prima[0], oggi)
    if d:
        if d > oggi:
            await _rispondi(update, context, "La data è nel futuro.")
            return
        data_sfide = d
        if len(prima) > 1:
            righe[0] = prima[1]
        else:
            righe = righe[1:]
    if not righe:
        await _rispondi(update, context, ESEMPIO_SFIDA)
        return

    lette, errori = [], []
    for i, riga in enumerate(righe, start=1):
        r = leggi_riga(riga)
        if isinstance(r, str):
            errori.append(f"riga {i} «{riga}»: {r}")
            continue
        a, b, va, vb = r
        nomi = [pulisci_nome(n).lower() for n in a + b]
        ids_alias = [cerca_giocatore(conn, n) for n in nomi]
        chiavi = [x[0] if x else n for x, n in zip(ids_alias, nomi)]
        if len(set(chiavi)) != 4:
            errori.append(f"riga {i} «{riga}»: lo stesso giocatore compare due volte")
            continue
        lette.append((a, b, va, vb))

    if errori:
        # tutto o niente: con un errore non salva nulla, così non si creano doppioni
        await _rispondi(update, context, "❌ Nessuna sfida salvata. Correggi e rimanda tutto:\n• "
                        + "\n• ".join(errori) + "\n\n" + ESEMPIO_SFIDA)
        return

    numeri, nuovi = salva_sfide(conn, lette, data_sfide, update.effective_user.id)
    intest = intestazione_sfide(numeri, nuovi, data_sfide, oggi)
    testo_cl = testo_classifica(conn, data_sfide.year)
    await _mostra(update, context, conn, "classifica_msg", escape(intest) + "\n\n" + testo_cl)


async def sfide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    nomi = dict(conn.execute("SELECT id, nome FROM giocatori").fetchall())
    rows = conn.execute(
        "SELECT id, data, a1, a2, b1, b2, vinte_a, vinte_b FROM sfide WHERE annullata = 0 "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not rows:
        testo = "Nessuna sfida registrata."
    else:
        testo = "🎯 <b>Ultime sfide</b> (n° · data)\n\n"
        for sid, d, a1, a2, b1, b2, va, vb in rows:
            dd = f"{d[8:10]}/{d[5:7]}"
            testo += (f"n° {sid} · {dd} — {escape(nomi[a1])}-{escape(nomi[a2])} "
                      f"<b>{va}-{vb}</b> {escape(nomi[b1])}-{escape(nomi[b2])}\n")
        testo += "\nErrore? /annullasfida N"
    await _mostra(update, context, conn, "sfide_msg", testo[:MAX_MSG])


async def annullasfida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    if not context.args or not context.args[0].lstrip("n°").isdigit():
        await _rispondi(update, context, "Uso: /annullasfida N (il numero lo trovi in /sfide)")
        return
    sid = int(context.args[0].lstrip("n°"))
    ok, testo, anno = annulla_sfida(conn, sid, update.effective_user.id, _is_admin(update.effective_user.id))
    if not ok:
        await _rispondi(update, context, testo)
        return
    await _mostra(update, context, conn, "classifica_msg", f"{testo}\n\n" + testo_classifica(conn, anno))


def _anno_da_args(args, default: int):
    if args and args[0].isdigit() and len(args[0]) == 4:
        return int(args[0])
    return default


async def classifica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    anno = _anno_da_args(context.args, _oggi().year)
    await _mostra(update, context, conn, "classifica_msg", testo_classifica(conn, anno))


async def medagliere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    if context.args and context.args[0].lower() in ("sempre", "tutti", "totale"):
        anno = None
    else:
        anno = _anno_da_args(context.args, _oggi().year)
    await _mostra(update, context, conn, "medagliere_msg", testo_medagliere(conn, anno))


async def giocatori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    rows = conn.execute("""
        SELECT g.id, g.nome,
          (SELECT COUNT(*) FROM sfide s WHERE s.annullata = 0 AND g.id IN (s.a1, s.a2, s.b1, s.b2))
        FROM giocatori g ORDER BY LOWER(g.nome)
    """).fetchall()
    if not rows:
        await _mostra(update, context, conn, "giocatori_msg", "Anagrafica vuota.")
        return
    alias = {}
    for a, gid in conn.execute("SELECT alias, giocatore_id FROM giocatori_alias"):
        alias.setdefault(gid, []).append(a)
    testo = f"👥 <b>Giocatori</b> ({len(rows)})\n\n"
    for gid, nome, n in rows:
        altri = [a for a in alias.get(gid, []) if a != nome.lower()]
        extra = f" (anche: {escape(', '.join(altri))})" if altri else ""
        testo += f"• {escape(nome)} — {n} {'sfida' if n == 1 else 'sfide'}{extra}\n"
    await _mostra(update, context, conn, "giocatori_msg", testo[:MAX_MSG])


def _due_nomi(context):
    parti = [p.strip() for p in " ".join(context.args or []).split("|")]
    return parti if len(parti) == 2 and all(parti) else None


async def rinominagiocatore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    conn = _conn()
    nomi = _due_nomi(context)
    if not nomi:
        await _rispondi(update, context, "Uso: /rinominagiocatore Vecchio | Nuovo")
        return
    vecchio, nuovo = nomi[0], pulisci_nome(nomi[1])
    g = cerca_giocatore(conn, vecchio)
    if not g:
        await _rispondi(update, context, f"Giocatore «{vecchio}» non trovato. Vedi /giocatori")
        return
    altro = cerca_giocatore(conn, nuovo)
    if altro and altro[0] != g[0]:
        await _rispondi(update, context, f"«{nuovo}» esiste già: se sono la stessa persona usa /unisci {vecchio} | {nuovo}")
        return
    conn.execute("UPDATE giocatori SET nome = ? WHERE id = ?", (nuovo, g[0]))
    # il vecchio nome resta come soprannome: i podi già scritti continuano a contare
    conn.execute("INSERT OR IGNORE INTO giocatori_alias (alias, giocatore_id) VALUES (?, ?)", (nuovo.lower(), g[0]))
    conn.commit()
    await _rispondi(update, context, f"✏️ «{g[1]}» ora si chiama «{nuovo}».")


async def unisci(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    conn = _conn()
    nomi = _due_nomi(context)
    if not nomi:
        await _rispondi(update, context, "Uso: /unisci Doppione | Giusto  (es. /unisci Marchino | Marco)")
        return
    giusto = cerca_giocatore(conn, nomi[1])
    if not giusto:
        await _rispondi(update, context, f"Giocatore «{nomi[1]}» non trovato. Vedi /giocatori")
        return
    doppione = cerca_giocatore(conn, nomi[0])
    if doppione and doppione[0] == giusto[0]:
        await _rispondi(update, context, "Sono già lo stesso giocatore.")
        return
    if doppione:
        did, gid = doppione[0], giusto[0]
        # stessa persona nella stessa sfida = dato incoerente: blocco
        conflitto = conn.execute(
            "SELECT id FROM sfide WHERE annullata = 0 AND ? IN (a1,a2,b1,b2) AND ? IN (a1,a2,b1,b2)",
            (did, gid)).fetchone()
        if conflitto:
            await _rispondi(update, context, f"Non posso: sono entrambi nella sfida n° {conflitto[0]}. Annullala o correggila prima.")
            return
        for col in ("a1", "a2", "b1", "b2"):
            conn.execute(f"UPDATE sfide SET {col} = ? WHERE {col} = ?", (gid, did))
        conn.execute("UPDATE giocatori_alias SET giocatore_id = ? WHERE giocatore_id = ?", (gid, did))
        # account Telegram del doppione: passa al giusto se questo non ne ha già uno
        conn.execute("UPDATE OR IGNORE giocatori_telegram SET giocatore_id = ? WHERE giocatore_id = ?", (gid, did))
        conn.execute("DELETE FROM giocatori_telegram WHERE giocatore_id = ?", (did,))
        conn.execute("DELETE FROM giocatori WHERE id = ?", (did,))
    else:
        # nome mai registrato (es. scritto solo in un vecchio podio): diventa soprannome
        conn.execute("INSERT OR REPLACE INTO giocatori_alias (alias, giocatore_id) VALUES (?, ?)",
                     (pulisci_nome(nomi[0]).lower(), giusto[0]))
    conn.commit()
    await _rispondi(update, context, f"🔗 «{nomi[0]}» unito a «{giusto[1]}».")


async def toglisoprannome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annulla un soprannome sbagliato (es. aggiunto dalla Mini App). Le sfide già
    salvate restano al giocatore: se erano di un altro, vanno annullate e rifatte."""
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    conn = _conn()
    alias = pulisci_nome(" ".join(context.args or [])).lower()
    if not alias:
        await _rispondi(update, context, "Uso: /toglisoprannome Soprannome")
        return
    g = cerca_giocatore(conn, alias)
    if not g:
        await _rispondi(update, context, f"Soprannome «{alias}» non trovato.")
        return
    if g[1].lower() == alias:
        await _rispondi(update, context, f"«{g[1]}» è il nome principale, non un soprannome: usa /rinominagiocatore.")
        return
    conn.execute("DELETE FROM giocatori_alias WHERE alias = ?", (alias,))
    conn.commit()
    await _rispondi(update, context, f"✂️ «{alias}» non è più un soprannome di «{g[1]}». "
                    "Le sfide già salvate restano a lui: se erano di un altro, annullale e rifalle.")


async def _imposta_esclusione(update, context, valore: int):
    if not _is_admin(update.effective_user.id):
        await _rispondi(update, context, "Comando riservato agli admin.")
        return
    conn = _conn()
    nome = " ".join(context.args or []).strip()
    if not nome:
        await _rispondi(update, context, "Uso: /escluditorneo NomeTorneo (o /includitorneo). Nomi in /storico")
        return
    row = conn.execute("SELECT id, nome FROM tornei WHERE LOWER(nome) = LOWER(?) ORDER BY id DESC LIMIT 1",
                       (nome,)).fetchone()
    if not row:
        await _rispondi(update, context, f"Torneo «{nome}» non trovato. Nomi in /storico")
        return
    conn.execute("UPDATE tornei SET escluso_medagliere = ? WHERE id = ?", (valore, row[0]))
    conn.commit()
    await _rispondi(update, context, f"{'🚫 Escluso dal' if valore else '✅ Incluso nel'} medagliere: {row[1]}")


async def escluditorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _imposta_esclusione(update, context, 1)


async def includitorneo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _imposta_esclusione(update, context, 0)


COMANDI_STATISTICHE = [
    ("sfida", "Segna una sfidina 2vs2 — /sfida Luca-Matteo Paolo-Michele 5-2 (più righe = più sfide)"),
    ("sfide", "Ultime sfidine inserite, con il loro numero"),
    ("annullasfida", "Annulla una sfidina inserita da te — /annullasfida N"),
    ("classifica", "Classifica sfidine dell'anno (Elo) — /classifica 2026 per un anno passato"),
    ("medagliere", "Medagliere tornei per giocatore — /medagliere sempre per il totale"),
    ("giocatori", "Elenco giocatori registrati"),
    ("rinominagiocatore", "[admin] Corregge il nome di un giocatore — /rinominagiocatore Vecchio | Nuovo"),
    ("unisci", "[admin] Fonde un doppione nel giocatore giusto — /unisci Marchino | Marco"),
    ("toglisoprannome", "[admin] Toglie un soprannome sbagliato — /toglisoprannome Gio"),
    ("escluditorneo", "[admin] Esclude un torneo dal medagliere — /escluditorneo NomeTorneo"),
    ("includitorneo", "[admin] Include un torneo nel medagliere (annulla l'esclusione automatica 5X)"),
]


ORARIO_CLASSIFICA_SETTIMANALE = dtime(hour=21, minute=0, tzinfo=FUSO)   # lunedì, dopo il gioco libero


async def classifica_settimanale(context):
    """Ogni lunedì alle 21 ripubblica la classifica nel gruppo (la vecchia sparisce),
    così il bottone «📱 Segna una sfida» è sempre in fondo alla chat.
    Se nell'anno non c'è ancora nessuna sfida non pubblica nulla."""
    from database import chat_gruppo_registrato
    conn = _conn()
    gruppo = chat_gruppo_registrato(conn)
    anno = _oggi().year
    if gruppo is None or not calcola_classifica(conn, anno):
        return
    await _sostituisci(context.bot, conn, gruppo, "classifica_msg",
                       "🗓️ Classifica del lunedì\n\n" + testo_classifica(conn, anno))


def registra(app, db_connect, is_admin, rispondi, cancella_comando):
    global _db_connect, _is_admin, _rispondi, _cancella_comando
    _db_connect, _is_admin, _rispondi, _cancella_comando = db_connect, is_admin, rispondi, cancella_comando
    crea_tabelle(db_connect())
    for nome, funz in (
        ("sfida", sfida), ("sfide", sfide), ("annullasfida", annullasfida),
        ("classifica", classifica), ("medagliere", medagliere), ("giocatori", giocatori),
        ("rinominagiocatore", rinominagiocatore), ("unisci", unisci), ("toglisoprannome", toglisoprannome),
        ("escluditorneo", escluditorneo), ("includitorneo", includitorneo),
    ):
        app.add_handler(CommandHandler(nome, funz))
    if app.job_queue is not None:
        # PTB >= 20: 0 = domenica, 1 = lunedì (come il sondaggio del gioco libero)
        app.job_queue.run_daily(classifica_settimanale, time=ORARIO_CLASSIFICA_SETTIMANALE,
                                days=(1,), name="classifica_settimanale_lunedi")
