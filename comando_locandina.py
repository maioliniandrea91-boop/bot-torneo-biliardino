"""
Comando /locandina — genera la locandina del torneo, te la mostra in privato
e la pubblica nel gruppo subito o all'orario che scegli tu.

FLUSSO (solo admin, in chat privata col bot):
  /locandina 16/10 volo                 -> data + formula (obbligatori)
  /locandina 16/10 volo tradizionale    -> più formule insieme (anche tutte e 3)
  /locandina 16/10 5x volo              -> "MODALITÀ 5X" sopra la formula (5x mai da solo)
  /locandina 16/10 rollerball 22:00 25€ -> ora e quota opzionali
  /locandina 16/10 tradizionale premi COPPE -> "premi ..." sempre in fondo
  /locandina 16/10 volo max14           -> coppie massime di quel torneo (4-32,
                                           senza: 18); poi si cambia con /maxcoppie
Il bot risponde con l'anteprima e quattro bottoni:
  📢 Pubblica ora   ⏰ Programma   ✏️ Aggiungi testo   ❌ Annulla
Con "Programma" ti chiede quando: es. 14/10 18:00, oppure domani 18:00.
Con "Aggiungi testo" scrivi qualcosa che va SOTTO la didascalia standard.
"Nuovo torneo: SÌ/NO" decide se alla pubblicazione si chiude il torneo attivo
e si apre quello della locandina (nome tipo "16/10 Rollerball"). Su NO la
locandina esce e basta: utile per un promemoria a iscrizioni già aperte.

"Ripeti ogni 48h: SÌ/NO" (predefinito SÌ): dopo la prima uscita il bot
ripubblica la locandina circa ogni 48 ore, cancellando il post precedente e
aggiungendo "Iscritte X/max — restano Y posti". Si ferma da solo quando:
il torneo è completo, hai fatto /chiudi, si apre un altro torneo, oppure
il prossimo giro cadrebbe dopo l'inizio del torneo.

Altri comandi:
  /programmate            -> locandine in attesa e ripubblicazioni attive
  /ripubblica [N] [48h]   -> rimette subito la locandina (ultima pubblicata o n° N)
                             con "Iscritte X/max" e la lista sotto, togliendo il
                             post precedente; con 48h attiva il giro ogni 48 ore
  /annullalocandina N     -> annulla la pubblicazione programmata n° N
                             o ferma le sue ripubblicazioni
  /didascalia testo       -> cambia il testo aggiunto sotto la didascalia
                             dell'ultima locandina pubblicata, modificando
                             il post già nel gruppo (vale anche per le
                             ripubblicazioni successive)
  /didascalia N           -> idem sulla locandina n° N (il testo lo chiede)
  /didascalia nessuno     -> toglie il testo aggiunto

Le programmazioni sono salvate nel database: se il bot si riavvia
(es. nuovo deploy su Railway) vengono ripristinate da sole.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import CHIAVE_CHAT_GIOCO_LIBERO, LIMITE_COPPIE_MAX, LIMITE_COPPIE_MIN
from database import db_connect as _db_predefinito
from database import max_coppie
from locandina import FORMULE, genera_locandina, testo_data

FUSO = ZoneInfo("Europe/Rome")
ORA_DEFAULT = "21:30"
QUOTA_DEFAULT = "20€"
PREMI_DEFAULT = "BV"
# Se il bot era spento all'orario previsto, pubblica comunque al riavvio
# solo se il ritardo è sotto questa soglia; oltre, avvisa e non pubblica.
RITARDO_MASSIMO = timedelta(hours=6)
# Ripubblicazione: poco meno di 48h, perché Telegram permette al bot di
# cancellare un suo messaggio solo entro 48 ore dall'invio.
INTERVALLO_REPOST = timedelta(hours=47, minutes=50)

# Vengono impostati da registra() con le funzioni del file principale
# (senza registra(), es. pannello della Mini App nei test: valori di config)
_db_connect = None
_is_admin = None
_chiave_gruppo = CHIAVE_CHAT_GIOCO_LIBERO
_admin_ids = set()
_max_squadre = 18
_ripubblica_lista = None


def _crea_tabella(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locandine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT,
            didascalia TEXT NOT NULL,
            stato TEXT NOT NULL,          -- bozza | programmata | pubblicata | annullata | scaduta
            pubblica_il TEXT,             -- ISO con fuso orario
            creato_il TEXT NOT NULL
        )
    """)
    colonne = [r[1] for r in conn.execute("PRAGMA table_info(locandine)")]
    for nome, tipo in (("extra", "TEXT"),            # testo aggiunto a mano
                       ("nome_torneo", "TEXT"),      # torneo da aprire alla pubblicazione
                       ("data_torneo", "TEXT"),
                       ("nuovo_torneo", "INTEGER NOT NULL DEFAULT 1"),   # 1 = apre il torneo
                       ("ora_torneo", "TEXT"),
                       ("ripeti", "INTEGER NOT NULL DEFAULT 1"),         # 1 = ripubblica ogni 48h
                       ("msg_gruppo", "INTEGER"),                        # ultimo post nel gruppo
                       ("torneo_ref", "INTEGER"),                        # torneo a cui si riferisce
                       ("prossimo_repost", "TEXT"),
                       ("max_coppie", "INTEGER")):                       # NULL = predefinito
        if nome not in colonne:
            conn.execute(f"ALTER TABLE locandine ADD COLUMN {nome} {tipo}")
    # data di gioco salvata anche nei tornei, per sapere se un torneo è già stato giocato
    if "data_torneo" not in [r[1] for r in conn.execute("PRAGMA table_info(tornei)")]:
        conn.execute("ALTER TABLE tornei ADD COLUMN data_torneo TEXT")
    conn.commit()


def _conn():
    conn = (_db_connect or _db_predefinito)()
    _crea_tabella(conn)
    return conn


# ---------- lettura dati dal comando ----------

def _prossima_data(giorno: int, mese: int, anno: int | None) -> date:
    oggi = datetime.now(FUSO).date()
    if anno is not None:
        if anno < 100:
            anno += 2000
        return date(anno, mese, giorno)
    d = date(oggi.year, mese, giorno)
    if d < oggi:
        d = date(oggi.year + 1, mese, giorno)
    return d


def leggi_argomenti(args: list[str]):
    """Ritorna (dati, errore). dati = dict con data, formula, ora, quota, premi."""
    if not args:
        return None, None
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", args[0])
    if not m:
        return None, f"Non capisco la data '{args[0]}'. Scrivila così: 16/10"
    try:
        d = _prossima_data(int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    except ValueError:
        return None, f"La data '{args[0]}' non esiste."

    ora, quota, premi = ORA_DEFAULT, QUOTA_DEFAULT, PREMI_DEFAULT
    formule = []  # una o più, nell'ordine in cui le scrivi
    cinque_x = False  # "5x": riga "MODALITÀ 5X" sopra le formule
    resto = args[1:]
    if any(t.lower() == "premi" for t in resto):
        i = [t.lower() for t in resto].index("premi")
        premi = " ".join(resto[i + 1:]).strip().upper() or PREMI_DEFAULT
        resto = resto[:i]

    # "volo+tradizionale" o "volo,tradizionale" valgono come parole separate
    resto = [p for t in resto for p in re.split(r"[+,]", t) if p]
    # "max 14" = "max14"
    for i in range(len(resto) - 1, 0, -1):
        if resto[i - 1].lower() in ("max", "max=", "max:") and resto[i].isdigit():
            resto[i - 1:i + 1] = ["max" + resto[i]]
    massimo = None  # coppie massime: None = predefinito

    for t in resto:
        tl = t.lower()
        if re.fullmatch(r"\d{1,2}[:.]\d{2}", tl):
            h, mi = re.split(r"[:.]", tl)
            if int(h) > 23 or int(mi) > 59:
                return None, f"Orario '{t}' non valido."
            ora = f"{int(h):02d}:{mi}"
        elif re.fullmatch(r"\d+(?:[.,]\d+)?(?:€|euro)", tl):
            quota = re.sub(r"(?:€|euro)$", "", tl) + "€"
        elif tl in FORMULE or tl in ("3", "tocchi"):
            chiave = "rollerball" if tl in ("3", "tocchi", "3tocchi") else tl
            if chiave not in formule:
                formule.append(chiave)
        elif tl == "5x":
            cinque_x = True
        elif re.fullmatch(r"max[=:]?\d+", tl):
            massimo = int(re.sub(r"\D", "", tl))
            if not LIMITE_COPPIE_MIN <= massimo <= LIMITE_COPPIE_MAX:
                return None, f"Massimo coppie '{t}' non valido: da {LIMITE_COPPIE_MIN} a {LIMITE_COPPIE_MAX}."
        elif tl in ("e", "&"):
            continue
        else:
            return None, f"Non capisco '{t}'."

    if not formule:
        return None, "Manca la formula: scrivi rollerball, volo o tradizionale (anche più di una, anche con 5x)."
    return {"data": d, "formula": formule, "ora": ora, "quota": quota, "premi": premi,
            "cinque_x": cinque_x, "max_coppie": massimo}, None


def leggi_quando(testo: str):
    """'14/10 18:00', 'oggi 18:00', 'domani 18:00' -> datetime con fuso."""
    t = testo.strip().lower()
    adesso = datetime.now(FUSO)
    m = re.fullmatch(r"(oggi|domani|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?)\s+(?:ore\s+)?(\d{1,2})[:.](\d{2})", t)
    if not m:
        return None
    giorno, h, mi = m.groups()
    if giorno == "oggi":
        d = adesso.date()
    elif giorno == "domani":
        d = adesso.date() + timedelta(days=1)
    else:
        p = re.split(r"[/.-]", giorno)
        try:
            d = _prossima_data(int(p[0]), int(p[1]), int(p[2]) if len(p) > 2 else None)
        except ValueError:
            return None
    try:
        return datetime(d.year, d.month, d.day, int(h), int(mi), tzinfo=FUSO)
    except ValueError:
        return None


def _didascalia(dati) -> str:
    return (
        f"🏆 Torneo di biliardino — {testo_data(dati['data'], dati['ora']).capitalize()}\n"
        "Iscrivi la tua coppia con /iscrivi NomeSquadra"
    )


def _nome_torneo(conn, dati) -> str:
    """'16/10 Rollerball'; se il nome esiste già nello storico aggiunge l'anno."""
    d = dati["data"]
    formula = "-".join(f.capitalize() for f in dati["formula"])
    if dati.get("cinque_x"):
        formula = "5X " + formula
    nome = f"{d.day:02d}/{d.month:02d} {formula}"
    if conn.execute("SELECT 1 FROM tornei WHERE LOWER(nome) = LOWER(?)", (nome,)).fetchone():
        nome = f"{d.day:02d}/{d.month:02d}/{d.year % 100:02d} {formula}"
    return nome


def _piano_torneo(conn, lid: int):
    """Cosa succederà ai tornei alla pubblicazione.
    Ritorna (si_cambia, testo_per_te)."""
    row = conn.execute(
        "SELECT nome_torneo, data_torneo, nuovo_torneo FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    nome_nuovo, data_nuovo, flag = row
    att = conn.execute(
        "SELECT id, nome, data_torneo FROM tornei WHERE chiuso = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not flag:
        nome_att = att[1] if att else "nessuno"
        return False, f"Torneo: nessun cambio, resta attivo '{nome_att}'."
    if not nome_nuovo:
        return False, "Torneo: nessun cambio."
    if att and att[1].lower() == nome_nuovo.lower():
        return False, f"Torneo: '{nome_nuovo}' è già quello attivo, nessun cambio."
    if att and att[2] and date.fromisoformat(att[2]) >= datetime.now(FUSO).date():
        # sicurezza: il torneo attivo non è ancora stato giocato
        return False, (
            f"⚠️ Torneo: '{att[1]}' non è ancora stato giocato, quindi NON lo chiudo. "
            "La locandina esce lo stesso; il cambio torneo fallo a mano."
        )
    n = conn.execute("SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (att[0],)).fetchone()[0] if att else 0
    vecchio = f"archivio '{att[1]}' ({n} {'squadra' if n == 1 else 'squadre'}) e " if att else ""
    return True, f"Torneo: alla pubblicazione {vecchio}apro '{nome_nuovo}' con iscrizioni aperte."


def _cambia_torneo(conn, lid: int) -> str | None:
    """Esegue il cambio torneo se previsto. Ritorna il testo per te, o None."""
    si, testo = _piano_torneo(conn, lid)
    if not si:
        return testo if testo.startswith("⚠️") else None
    nome_nuovo, data_nuovo = conn.execute(
        "SELECT nome_torneo, data_torneo FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    vecchio = conn.execute(
        "SELECT nome FROM tornei WHERE chiuso = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.execute("UPDATE tornei SET chiuso = 1 WHERE chiuso = 0")
    conn.execute(
        "INSERT INTO tornei (nome, creato_il, chiuso, data_torneo) VALUES (?, ?, 0, ?)",
        (nome_nuovo, datetime.now().isoformat(), data_nuovo),
    )
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES ('aperte', '1') "
        "ON CONFLICT(chiave) DO UPDATE SET valore = '1'"
    )
    conn.commit()
    return (f"🏆 Archiviato '{vecchio[0]}', aperto '{nome_nuovo}' con iscrizioni aperte."
            if vecchio else f"🏆 Aperto '{nome_nuovo}' con iscrizioni aperte.")


def _gruppo(conn):
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (_chiave_gruppo,)).fetchone()
    return int(row[0]) if row else None


# ---------- operazioni (una sola logica: comandi e pannello della Mini App) ----------

def crea_bozza(conn, dati, nuovo_torneo: bool = True, ripeti: bool = True, extra: str | None = None) -> int:
    """Salva la bozza della locandina (l'immagine la aggiunge chi la manda in chat)."""
    cur = conn.execute(
        "INSERT INTO locandine (didascalia, stato, creato_il, nome_torneo, data_torneo, nuovo_torneo, ora_torneo, "
        "max_coppie, ripeti, extra) VALUES (?, 'bozza', ?, ?, ?, ?, ?, ?, ?, ?)",
        (_didascalia(dati), datetime.now(FUSO).isoformat(), _nome_torneo(conn, dati), dati["data"].isoformat(),
         int(nuovo_torneo), dati["ora"], dati["max_coppie"], int(ripeti), extra or None),
    )
    conn.commit()
    return cur.lastrowid


async def genera_png(dati) -> bytes:
    return await asyncio.to_thread(
        genera_locandina, dati["data"], dati["formula"], dati["ora"], dati["quota"], dati["premi"],
        dati["cinque_x"],
    )


def modificabile(conn, lid: int) -> bool:
    row = conn.execute("SELECT stato FROM locandine WHERE id = ?", (lid,)).fetchone()
    return bool(row) and row[0] not in ("pubblicata", "annullata", "scaduta")


def imposta_testo(lid: int, testo: str):
    """Testo aggiunto sotto la didascalia di una bozza o programmata ('nessuno' lo toglie).
    Ritorna (esito, testo completo): esito = 'ok' | 'chiusa' | 'lungo'."""
    conn = _conn()
    if not modificabile(conn, lid):
        return "chiusa", ""
    didascalia = conn.execute("SELECT didascalia FROM locandine WHERE id = ?", (lid,)).fetchone()[0]
    testo = (testo or "").strip()
    extra = None if not testo or testo.lower() == "nessuno" else testo
    completo = _testo_completo(didascalia, extra)
    if len(_caption_anteprima(lid, completo)) > LIMITE_DIDASCALIA:
        return "lungo", completo
    conn.execute("UPDATE locandine SET extra = ? WHERE id = ?", (extra, lid))
    conn.commit()
    return "ok", completo


def programma(job_queue, lid: int, quando: datetime):
    """Programma l'uscita nel gruppo. Ritorna (ok, testo per l'admin)."""
    if quando <= datetime.now(FUSO):
        return False, "Quell'orario è già passato. Scrivine uno futuro."
    conn = _conn()
    if not modificabile(conn, lid):
        return False, "Questa locandina non è più modificabile."
    conn.execute(
        "UPDATE locandine SET stato = 'programmata', pubblica_il = ? WHERE id = ?",
        (quando.isoformat(), lid),
    )
    conn.commit()
    _programma_job(job_queue, lid, quando)
    gruppo_ok = _gruppo(conn) is not None
    return True, (
        f"⏰ Locandina n° {lid} programmata per {quando.strftime('%d/%m alle %H:%M')}."
        + ("" if gruppo_ok else "\n⚠️ Nessun gruppo registrato: manda /registragruppo nel gruppo, o non partirà.")
    )


def annulla(job_queue, lid: int, anche_bozza: bool = False) -> str:
    """Annulla una locandina programmata (o la bozza, dal pannello) oppure ferma
    le ripubblicazioni di quella già uscita (/annullalocandina N)."""
    conn = _conn()
    stati = "('programmata', 'bozza')" if anche_bozza else "('programmata')"
    n = conn.execute(
        f"UPDATE locandine SET stato = 'annullata' WHERE id = ? AND stato IN {stati}", (lid,)
    ).rowcount
    conn.commit()
    if job_queue is not None:
        for j in job_queue.get_jobs_by_name(f"locandina_{lid}"):
            j.schedule_removal()
    if n:
        return f"Locandina n° {lid} annullata."
    attiva = conn.execute(
        "SELECT 1 FROM locandine WHERE id = ? AND stato = 'pubblicata' AND ripeti = 1", (lid,)
    ).fetchone()
    if attiva:
        _ferma_repost(conn, job_queue, lid)
        return f"🔁 Ripubblicazioni della locandina n° {lid} fermate. L'ultimo post resta nel gruppo."
    return f"Nessuna locandina programmata o in ripubblicazione con n° {lid}."


async def ripubblica_locandina(bot, job_queue, numero: int | None = None, con_48h: bool = False) -> str:
    """Rimette subito nel gruppo una locandina già uscita (senza numero: l'ultima),
    con 'Iscritte X/max' e la lista squadre sotto; toglie il post precedente."""
    conn = _conn()
    if numero is not None:
        row = conn.execute(
            "SELECT id FROM locandine WHERE id = ? AND stato = 'pubblicata'", (numero,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM locandine WHERE stato = 'pubblicata' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return ("Nessuna locandina pubblicata da ripubblicare."
                + (f" (n° {numero} non trovata o non pubblicata)" if numero is not None else ""))
    lid = row[0]
    file_id, didascalia, extra, torneo_ref, msg_vecchio = conn.execute(
        "SELECT file_id, didascalia, extra, torneo_ref, msg_gruppo FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    gruppo = _gruppo(conn)
    if gruppo is None:
        return "Nessun gruppo registrato: manda /registragruppo dentro il gruppo del circolo."
    att = _torneo_attivo(conn)
    if torneo_ref is None and att:
        torneo_ref = att[0]
    avviso = ""
    if not att or att[0] != torneo_ref:
        avviso = "\n⚠️ Questa locandina non è del torneo attivo: la ripubblico senza riga iscritte."
    iscritte = await _iscritte(conn, torneo_ref) if not avviso else 0
    testo = _testo_completo(didascalia, extra)
    if not avviso:
        testo += f"\n\n{_riga_posti(iscritte, conn, torneo_ref)}"
    try:
        nuovo = await bot.send_photo(chat_id=gruppo, photo=file_id, caption=testo)
    except Exception as e:
        return f"❌ Locandina n° {lid} NON ripubblicata: errore Telegram ({e})."
    conn.execute("UPDATE locandine SET msg_gruppo = ?, torneo_ref = ? WHERE id = ?",
                 (nuovo.message_id, torneo_ref, lid))
    conn.commit()
    await _cancella_post_vecchi(bot, conn, gruppo, nuovo.message_id, msg_vecchio)
    if not avviso and _ripubblica_lista:
        try:
            await _ripubblica_lista(bot, conn, gruppo)
        except Exception:
            pass
    esito = f"✅ Locandina n° {lid} ripubblicata nel gruppo (post precedente tolto)." + avviso
    ripeti = conn.execute("SELECT ripeti FROM locandine WHERE id = ?", (lid,)).fetchone()[0]
    if con_48h or ripeti:  # il giro riparte da adesso, così resta nella finestra delle 48h
        conn.execute("UPDATE locandine SET ripeti = 1 WHERE id = ?", (lid,))
        conn.commit()
        if job_queue is not None:
            _avvia_repost(conn, job_queue, lid)
            prossimo = conn.execute("SELECT prossimo_repost FROM locandine WHERE id = ?", (lid,)).fetchone()[0]
            esito += (f"\n🔁 Prossima ripubblicazione: {datetime.fromisoformat(prossimo).strftime('%d/%m %H:%M')}."
                      if prossimo else "\n🔁 Nessuna ripubblicazione: il torneo arriva prima di 48 ore.")
    return esito


def elenco(conn) -> dict:
    """Per il pannello: programmate, ripubblicazioni attive, ultima pubblicata."""
    def _ora(iso):
        return datetime.fromisoformat(iso).astimezone(FUSO).strftime("%d/%m %H:%M") if iso else None
    programmate = [{"id": i, "quando": _ora(q), "nome": n, "extra": e or ""} for i, q, n, e in conn.execute(
        "SELECT id, pubblica_il, nome_torneo, extra FROM locandine WHERE stato = 'programmata' ORDER BY pubblica_il")]
    ultima = conn.execute(
        "SELECT id, nome_torneo, extra, ripeti, prossimo_repost, torneo_ref FROM locandine "
        "WHERE stato = 'pubblicata' ORDER BY id DESC LIMIT 1").fetchone()
    if ultima:
        att = _torneo_attivo(conn)
        ultima = {"id": ultima[0], "nome": ultima[1], "extra": ultima[2] or "",
                  "ripeti": bool(ultima[3] and ultima[4]), "prossimo": _ora(ultima[4]),
                  "del_torneo_attivo": bool(att and ultima[5] == att[0])}
    return {"programmate": programmate, "ultima": ultima}


# ---------- comandi ----------

async def locandina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usa /locandina in chat privata col bot.")
        return

    dati, errore = leggi_argomenti(context.args)
    if dati is None:
        await update.message.reply_text(
            (errore + "\n\n" if errore else "")
            + "Uso: /locandina 16/10 volo\n"
            "Opzionali: ora (22:00), quota (25€), premi in fondo (premi COPPE)\n"
            "Formule: rollerball, volo, tradizionale — anche più di una: /locandina 16/10 volo tradizionale\n"
            "Modalità 5X: aggiungi 5x insieme alla formula: /locandina 16/10 5x volo\n"
            f"Coppie massime: max14 (da {LIMITE_COPPIE_MIN} a {LIMITE_COPPIE_MAX})\n"
            f"Se non li scrivi: ore {ORA_DEFAULT}, {QUOTA_DEFAULT} a coppia, premi in {PREMI_DEFAULT}, "
            "massimo 18 coppie."
        )
        return

    png = await asyncio.to_thread(
        genera_locandina, dati["data"], dati["formula"], dati["ora"], dati["quota"], dati["premi"],
        dati["cinque_x"],
    )
    conn = _conn()
    didascalia = _didascalia(dati)
    lid = crea_bozza(conn, dati)

    msg = await update.message.reply_photo(
        photo=png,
        caption=_caption_anteprima(lid, didascalia),
        reply_markup=_tastiera(lid),
    )
    conn.execute("UPDATE locandine SET file_id = ? WHERE id = ?", (msg.photo[-1].file_id, lid))
    conn.commit()


LIMITE_DIDASCALIA = 1024  # limite Telegram per la didascalia di una foto


def _tastiera(lid: int):
    flag, ripeti = _conn().execute(
        "SELECT nuovo_torneo, ripeti FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    interruttore = "🏆 Nuovo torneo: SÌ (tocca per NO)" if flag else "🏆 Nuovo torneo: NO (tocca per SÌ)"
    tasto_ripeti = "🔁 Ripeti ogni 48h: SÌ (tocca per NO)" if ripeti else "🔁 Ripeti ogni 48h: NO (tocca per SÌ)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Pubblica ora", callback_data=f"loc:ora:{lid}"),
         InlineKeyboardButton("⏰ Programma", callback_data=f"loc:prog:{lid}")],
        [InlineKeyboardButton("✏️ Aggiungi testo", callback_data=f"loc:testo:{lid}"),
         InlineKeyboardButton("❌ Annulla", callback_data=f"loc:no:{lid}")],
        [InlineKeyboardButton(interruttore, callback_data=f"loc:torneo:{lid}")],
        [InlineKeyboardButton(tasto_ripeti, callback_data=f"loc:ripeti:{lid}")],
    ])


def _testo_completo(didascalia: str, extra: str | None) -> str:
    return f"{didascalia}\n\n{extra}" if extra else didascalia


def _inizio_torneo(data_iso: str | None, ora: str | None) -> datetime | None:
    if not data_iso:
        return None
    h, m = (ora or ORA_DEFAULT).split(":")
    d = date.fromisoformat(data_iso)
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=FUSO)


def _caption_anteprima(lid: int, testo: str) -> str:
    conn = _conn()
    _, piano = _piano_torneo(conn, lid)
    ripeti, data_iso, ora = conn.execute(
        "SELECT ripeti, data_torneo, ora_torneo FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    inizio = _inizio_torneo(data_iso, ora)
    if ripeti and inizio:
        piano_ripeti = (f"Ripubblicazione: ogni ~48h fino a {inizio.strftime('%d/%m %H:%M')} "
                        "(stop se completo o iscrizioni chiuse).")
    else:
        piano_ripeti = "Ripubblicazione: no."
    return (f"Anteprima n° {lid}\n\nDidascalia nel gruppo:\n{testo}\n\n{piano}\n"
            f"{_piano_max(conn, lid)}\n{piano_ripeti}")


def _piano_max(conn, lid: int) -> str:
    """Riga dell'anteprima sulle coppie massime."""
    massimo, flag = conn.execute(
        "SELECT max_coppie, nuovo_torneo FROM locandine WHERE id = ?", (lid,)).fetchone()
    if massimo:
        return f"Massimo coppie: {massimo}."
    if flag:
        return f"Massimo coppie: {max_coppie(conn, None)} (predefinito; per cambiarlo: max14 nel comando)."
    att = _torneo_attivo(conn)
    return f"Massimo coppie: resta {max_coppie(conn, att[0] if att else None)}."


def _applica_max(conn, lid: int, esito_torneo: str | None) -> str:
    """Alla pubblicazione: mette il massimo della locandina sul suo torneo.
    Vale per il torneo appena aperto o, con 'Nuovo torneo: NO', per quello attivo.
    Ritorna una riga per l'admin ("" se la locandina non ha un massimo)."""
    massimo, flag, nome = conn.execute(
        "SELECT max_coppie, nuovo_torneo, nome_torneo FROM locandine WHERE id = ?", (lid,)).fetchone()
    if not massimo:
        return ""
    att = _torneo_attivo(conn)
    aperto_ora = bool(esito_torneo) and esito_torneo.startswith("🏆")
    if att and (aperto_ora or not flag or (nome and att[1].lower() == nome.lower())):
        conn.execute("UPDATE tornei SET max_coppie = ? WHERE id = ?", (massimo, att[0]))
        conn.commit()
        return f"👥 Massimo coppie di '{att[1]}': {massimo}."
    return (f"⚠️ Massimo {massimo} coppie NON applicato: il torneo della locandina non è stato aperto. "
            "Quando lo apri, usa /maxcoppie.")


def _torneo_attivo(conn):
    return conn.execute(
        "SELECT id, nome FROM tornei WHERE chiuso = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _iscrizioni_aperte(conn) -> bool:
    row = conn.execute("SELECT valore FROM stato WHERE chiave = 'aperte'").fetchone()
    return row is None or row[0] == "1"


def _ferma_repost(conn, job_queue, lid: int):
    conn.execute("UPDATE locandine SET ripeti = 0, prossimo_repost = NULL WHERE id = ?", (lid,))
    conn.commit()
    if job_queue is not None:
        for j in job_queue.get_jobs_by_name(f"repost_{lid}"):
            j.schedule_removal()


def _programma_repost(conn, job_queue, lid: int, quando: datetime):
    conn.execute("UPDATE locandine SET prossimo_repost = ? WHERE id = ?", (quando.isoformat(), lid))
    conn.commit()
    for j in job_queue.get_jobs_by_name(f"repost_{lid}"):
        j.schedule_removal()
    job_queue.run_once(_job_repost, when=quando, data=lid, name=f"repost_{lid}")


def _avvia_repost(conn, job_queue, lid: int):
    """Dopo la prima uscita: un solo giro di ripubblicazione attivo alla volta."""
    altri = conn.execute(
        "SELECT id FROM locandine WHERE ripeti = 1 AND stato = 'pubblicata' AND id != ?", (lid,)
    ).fetchall()
    for (altro,) in altri:
        _ferma_repost(conn, job_queue, altro)
    data_iso, ora = conn.execute(
        "SELECT data_torneo, ora_torneo FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    inizio = _inizio_torneo(data_iso, ora)
    prossimo = datetime.now(FUSO) + INTERVALLO_REPOST
    if inizio is None or prossimo >= inizio:
        _ferma_repost(conn, None, lid)  # il torneo arriva prima del prossimo giro
        return
    _programma_repost(conn, job_queue, lid, prossimo)


CHIAVE_SOGLIA = "soglia_conferma"
SOGLIA_DEFAULT = 8  # coppie minime per confermare il torneo (modificabile con /soglia)


def get_soglia(conn) -> int:
    row = conn.execute("SELECT valore FROM stato WHERE chiave = ?", (CHIAVE_SOGLIA,)).fetchone()
    return int(row[0]) if row else SOGLIA_DEFAULT


def _riga_posti(iscritte: int, conn=None, torneo_id=None) -> str:
    massimo = max_coppie(conn, torneo_id) if conn is not None else _max_squadre
    restano = max(0, massimo - iscritte)
    posti = "resta 1 posto" if restano == 1 else f"restano {restano} posti"
    riga = f"📋 Iscritte {iscritte}/{massimo} — {posti}"
    if conn is not None and iscritte >= min(get_soglia(conn), massimo):
        riga += "\n✅ Torneo confermato"
    return riga


async def _cancella_post_vecchi(bot, conn, gruppo: int, tranne_msg: int, vecchio: int | None = None):
    """Toglie dal gruppo i post delle locandine precedenti (anche se sotto ci sono
    già iscrizioni o altri messaggi). Telegram fa cancellare al bot solo i suoi
    messaggi con meno di 48 ore: quelli più vecchi restano e vanno tolti a mano."""
    for lid_v, msg in conn.execute(
        "SELECT id, msg_gruppo FROM locandine WHERE msg_gruppo IS NOT NULL AND msg_gruppo != ?",
        (tranne_msg,),
    ).fetchall():
        try:
            await bot.delete_message(chat_id=gruppo, message_id=msg)
        except Exception:
            pass  # già cancellato a mano o più vecchio di 48h
        conn.execute("UPDATE locandine SET msg_gruppo = NULL WHERE id = ?", (lid_v,))
    conn.commit()
    if vecchio and vecchio != tranne_msg:  # post precedente della stessa locandina
        try:
            await bot.delete_message(chat_id=gruppo, message_id=vecchio)
        except Exception:
            pass


async def _iscritte(conn, torneo_ref) -> int:
    if torneo_ref is None:
        return 0
    return conn.execute("SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (torneo_ref,)).fetchone()[0]


async def _avvisa_admin(bot, testo: str):
    for admin in _admin_ids:
        try:
            await bot.send_message(admin, testo)
        except Exception:
            pass


async def _job_repost(context: ContextTypes.DEFAULT_TYPE):
    lid = context.job.data
    bot, jq = context.bot, context.job_queue
    conn = _conn()
    row = conn.execute(
        "SELECT file_id, didascalia, extra, stato, ripeti, msg_gruppo, torneo_ref, data_torneo, ora_torneo "
        "FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    if not row:
        return
    file_id, didascalia, extra, stato, ripeti, msg_vecchio, torneo_ref, data_iso, ora = row
    if stato != "pubblicata" or not ripeti:
        return

    att = _torneo_attivo(conn)
    iscritte = conn.execute(
        "SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (torneo_ref,)
    ).fetchone()[0]
    inizio = _inizio_torneo(data_iso, ora)
    adesso = datetime.now(FUSO)
    motivo = None
    if not att or att[0] != torneo_ref:
        motivo = "è stato aperto un altro torneo"
    elif not _iscrizioni_aperte(conn):
        motivo = "le iscrizioni sono chiuse"
    elif iscritte >= max_coppie(conn, torneo_ref):
        motivo = f"il torneo è completo ({iscritte}/{max_coppie(conn, torneo_ref)})"
    elif inizio is None or adesso >= inizio:
        motivo = "il torneo è già iniziato"
    if motivo:
        _ferma_repost(conn, jq, lid)
        await _avvisa_admin(bot, f"🔁 Ripubblicazioni della locandina n° {lid} fermate: {motivo}.")
        return

    gruppo = _gruppo(conn)
    if gruppo is None:
        _ferma_repost(conn, jq, lid)
        await _avvisa_admin(bot, "🔁 Ripubblicazione fermata: nessun gruppo registrato (/registragruppo).")
        return
    didascalia = f"{_testo_completo(didascalia, extra)}\n\n{_riga_posti(iscritte, conn, torneo_ref)}"
    try:
        nuovo = await bot.send_photo(chat_id=gruppo, photo=file_id, caption=didascalia)
    except Exception as e:
        # riprovo tra un'ora, senza toccare il post precedente
        _programma_repost(conn, jq, lid, adesso + timedelta(hours=1))
        await _avvisa_admin(bot, f"⚠️ Ripubblicazione n° {lid} non riuscita ({e}). Riprovo tra un'ora.")
        return
    conn.execute("UPDATE locandine SET msg_gruppo = ? WHERE id = ?", (nuovo.message_id, lid))
    conn.commit()
    await _cancella_post_vecchi(bot, conn, gruppo, nuovo.message_id, msg_vecchio)
    if _ripubblica_lista:  # lista squadre subito sotto la locandina, la vecchia sparisce
        try:
            await _ripubblica_lista(bot, conn, gruppo)
        except Exception:
            pass
    prossimo = adesso + INTERVALLO_REPOST
    if prossimo < inizio:
        _programma_repost(conn, jq, lid, prossimo)
    else:
        conn.execute("UPDATE locandine SET prossimo_repost = NULL WHERE id = ?", (lid,))
        conn.commit()


async def _pubblica(bot, lid: int, job_queue=None) -> str:
    conn = _conn()
    row = conn.execute(
        "SELECT file_id, didascalia, stato, extra FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    if not row:
        return f"Locandina n° {lid} non trovata."
    file_id, didascalia, stato, extra = row
    didascalia = _testo_completo(didascalia, extra)
    if stato in ("pubblicata", "annullata"):
        return f"La locandina n° {lid} è già {stato}."
    gruppo = _gruppo(conn)
    if gruppo is None:
        return "Nessun gruppo registrato: manda /registragruppo dentro il gruppo del circolo."
    # prima il cambio torneo, così chi vede la locandina si iscrive già a quello nuovo
    esito_torneo = _cambia_torneo(conn, lid)
    esito_max = _applica_max(conn, lid, esito_torneo)
    att = _torneo_attivo(conn)
    iscritte = await _iscritte(conn, att[0] if att else None)
    if iscritte:  # locandina a iscrizioni già aperte: mostra subito i posti
        didascalia = f"{didascalia}\n\n{_riga_posti(iscritte, conn, att[0])}"
    try:
        msg = await bot.send_photo(chat_id=gruppo, photo=file_id, caption=didascalia)
    except Exception as e:
        return (f"❌ Locandina n° {lid} NON pubblicata: errore Telegram ({e})."
                + (f"\n{esito_torneo}" if esito_torneo else "") + (f"\n{esito_max}" if esito_max else ""))
    conn.execute(
        "UPDATE locandine SET stato = 'pubblicata', msg_gruppo = ?, torneo_ref = ? WHERE id = ?",
        (msg.message_id, att[0] if att else None, lid),
    )
    conn.commit()
    await _cancella_post_vecchi(bot, conn, gruppo, msg.message_id)
    if iscritte and _ripubblica_lista:
        try:
            await _ripubblica_lista(bot, conn, gruppo)
        except Exception:
            pass
    esito = (f"✅ Locandina n° {lid} pubblicata nel gruppo." + (f"\n{esito_torneo}" if esito_torneo else "")
             + (f"\n{esito_max}" if esito_max else ""))
    ripeti = conn.execute("SELECT ripeti FROM locandine WHERE id = ?", (lid,)).fetchone()[0]
    if ripeti and job_queue is not None:
        _avvia_repost(conn, job_queue, lid)
        prossimo = conn.execute("SELECT prossimo_repost FROM locandine WHERE id = ?", (lid,)).fetchone()[0]
        if prossimo:
            esito += f"\n🔁 Prossima ripubblicazione: {datetime.fromisoformat(prossimo).strftime('%d/%m %H:%M')}."
        else:
            esito += "\n🔁 Nessuna ripubblicazione: il torneo arriva prima di 48 ore."
    return esito


async def _job_pubblica(context: ContextTypes.DEFAULT_TYPE):
    lid = context.job.data
    esito = await _pubblica(context.bot, lid, context.job_queue)
    await _avvisa_admin(context.bot, esito)


def _programma_job(job_queue, lid: int, quando: datetime):
    for j in job_queue.get_jobs_by_name(f"locandina_{lid}"):
        j.schedule_removal()
    job_queue.run_once(_job_pubblica, when=quando, data=lid, name=f"locandina_{lid}")


async def bottoni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("Riservato agli admin.", show_alert=True)
        return
    await q.answer()
    _, azione, lid = q.data.split(":")
    lid = int(lid)

    # Avviso se manca il testo aggiunto: "oraok"/"progok" = confermato senza testo
    if azione in ("ora", "prog"):
        row = _conn().execute("SELECT extra, stato FROM locandine WHERE id = ?", (lid,)).fetchone()
        if row and not row[0] and row[1] not in ("pubblicata", "annullata"):
            await q.message.reply_text(
                f"⚠️ La locandina n° {lid} non ha testo aggiunto: uscirà solo con la didascalia standard.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Aggiungi testo", callback_data=f"loc:testo:{lid}")],
                    [InlineKeyboardButton(
                        "📢 Pubblica senza testo" if azione == "ora" else "⏰ Programma senza testo",
                        callback_data=f"loc:{azione}ok:{lid}")],
                ]),
            )
            return
    if azione in ("oraok", "progok"):
        await q.edit_message_reply_markup(None)  # toglie i bottoni dell'avviso
        azione = azione[:-2]
        if azione == "ora":
            esito = await _pubblica(context.bot, lid, context.job_queue)
            await q.message.reply_text(esito)
            return

    if azione == "ora":
        esito = await _pubblica(context.bot, lid, context.job_queue)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(esito)
    elif azione == "prog":
        context.user_data["locandina_attesa"] = ("prog", lid)
        await q.message.reply_text(
            "Quando la pubblico? Scrivi ad esempio:\n14/10 18:00\noggi 20:30\ndomani 18:00\n"
            "(oppure 'annulla')"
        )
    elif azione == "testo":
        context.user_data["locandina_attesa"] = ("testo", lid)
        await q.message.reply_text(
            "Scrivi il testo da aggiungere sotto la didascalia (anche su più righe).\n"
            "Se lo riscrivi, sostituisce quello aggiunto prima.\n"
            "'nessuno' toglie il testo aggiunto, 'annulla' lascia tutto com'è."
        )
    elif azione == "torneo":
        conn = _conn()
        row = conn.execute(
            "SELECT didascalia, extra, stato FROM locandine WHERE id = ?", (lid,)
        ).fetchone()
        if not row or row[2] in ("pubblicata", "annullata"):
            await q.message.reply_text("Questa locandina non è più modificabile.")
            return
        conn.execute("UPDATE locandine SET nuovo_torneo = 1 - nuovo_torneo WHERE id = ?", (lid,))
        conn.commit()
        await q.edit_message_caption(
            caption=_caption_anteprima(lid, _testo_completo(row[0], row[1])),
            reply_markup=_tastiera(lid),
        )
    elif azione == "ripeti":
        conn = _conn()
        row = conn.execute(
            "SELECT didascalia, extra, stato FROM locandine WHERE id = ?", (lid,)
        ).fetchone()
        if not row or row[2] in ("pubblicata", "annullata"):
            await q.message.reply_text("Questa locandina non è più modificabile.")
            return
        conn.execute("UPDATE locandine SET ripeti = 1 - ripeti WHERE id = ?", (lid,))
        conn.commit()
        await q.edit_message_caption(
            caption=_caption_anteprima(lid, _testo_completo(row[0], row[1])),
            reply_markup=_tastiera(lid),
        )
    elif azione == "no":
        conn = _conn()
        conn.execute("UPDATE locandine SET stato = 'annullata' WHERE id = ?", (lid,))
        conn.commit()
        for j in context.job_queue.get_jobs_by_name(f"locandina_{lid}"):
            j.schedule_removal()
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Locandina n° {lid} annullata.")


async def risposta_privata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Riceve l'orario (dopo Programma) o il testo da aggiungere (dopo
    Aggiungi testo). Se il bot non sta aspettando nulla, non fa niente."""
    attesa = context.user_data.get("locandina_attesa")
    if attesa is None or not _is_admin(update.effective_user.id):
        return
    modo, lid = attesa
    testo = (update.message.text or "").strip()
    if testo.lower() == "annulla":
        context.user_data.pop("locandina_attesa", None)
        await update.message.reply_text("Ok, nessuna modifica. L'anteprima resta lì se ti serve.")
        return
    if modo == "testo":
        await _salva_testo(update, context, lid, testo)
    elif modo == "didascalia":
        context.user_data.pop("locandina_attesa", None)
        await update.message.reply_text(await _modifica_pubblicata(context.bot, lid, testo))
    else:
        await _salva_orario(update, context, lid, testo)


async def _salva_testo(update, context, lid: int, testo: str):
    esito, _ = imposta_testo(lid, testo)
    if esito == "chiusa":
        context.user_data.pop("locandina_attesa", None)
        await update.message.reply_text("Questa locandina non è più modificabile.")
        return
    if esito == "lungo":
        await update.message.reply_text(
            f"Troppo lungo: Telegram accetta al massimo circa {LIMITE_DIDASCALIA} caratteri di didascalia. Accorcialo."
        )
        return
    context.user_data.pop("locandina_attesa", None)
    file_id, didascalia, extra, stato = _conn().execute(
        "SELECT file_id, didascalia, extra, stato FROM locandine WHERE id = ?", (lid,)).fetchone()
    nota = ""
    if stato == "programmata":
        nota = "\n(è già programmata: uscirà con questo testo, non serve riprogrammarla)"
    await update.message.reply_photo(
        photo=file_id,
        caption=_caption_anteprima(lid, _testo_completo(didascalia, extra)) + nota,
        reply_markup=_tastiera(lid),
    )


async def _salva_orario(update, context, lid: int, testo: str):
    quando = leggi_quando(testo)
    if quando is None:
        await update.message.reply_text("Non ho capito. Scrivi ad esempio 14/10 18:00 (oppure 'annulla').")
        return
    ok, risposta = programma(context.job_queue, lid, quando)
    if ok:
        context.user_data.pop("locandina_attesa", None)
    await update.message.reply_text(risposta)


async def programmate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    conn = _conn()
    righe = conn.execute(
        "SELECT id, pubblica_il, didascalia FROM locandine WHERE stato = 'programmata' ORDER BY pubblica_il"
    ).fetchall()
    ripubb = conn.execute(
        "SELECT id, prossimo_repost, nome_torneo FROM locandine "
        "WHERE stato = 'pubblicata' AND ripeti = 1 AND prossimo_repost IS NOT NULL"
    ).fetchall()
    if not righe and not ripubb:
        await update.message.reply_text("Nessuna locandina programmata e nessuna ripubblicazione attiva.")
        return
    testo = ""
    if righe:
        testo += "⏰ Locandine programmate:\n"
        for lid, quando, did in righe:
            q = datetime.fromisoformat(quando).astimezone(FUSO)
            testo += f"n° {lid} — {q.strftime('%d/%m %H:%M')} — {did.splitlines()[0]}\n"
    if ripubb:
        testo += ("\n" if testo else "") + "🔁 Ripubblicazioni attive:\n"
        for lid, quando, nome in ripubb:
            q = datetime.fromisoformat(quando).astimezone(FUSO)
            testo += f"n° {lid} — {nome or ''} — prossima {q.strftime('%d/%m %H:%M')}\n"
    testo += "\nPer annullare o fermare: /annullalocandina N"
    await update.message.reply_text(testo)


async def annullalocandina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /annullalocandina N (il numero lo vedi con /programmate)")
        return
    await update.message.reply_text(annulla(context.job_queue, int(context.args[0])))


async def ripubblica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ripubblica [N] [48h] — rimette subito nel gruppo una locandina già uscita,
    con 'Iscritte X/max' e la lista squadre sotto; toglie il post precedente.
    Senza N prende l'ultima pubblicata. Con 48h attiva (o riparte da ora) il giro
    di ripubblicazione ogni ~48 ore fino all'inizio del torneo."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if update.effective_chat.type != "private":
        try:
            await update.message.delete()
        except Exception:
            pass
    args = [a.lower() for a in (context.args or [])]
    con_48h = "48h" in args or "48" in args
    numeri = [a for a in args if a.isdigit() and a != "48"]
    risposta = (update.message.reply_text if update.effective_chat.type == "private"
                else (lambda t: _avvisa_admin(context.bot, t)))
    await risposta(await ripubblica_locandina(
        context.bot, context.job_queue, int(numeri[0]) if numeri else None, con_48h))


async def _modifica_pubblicata(bot, lid: int, testo: str) -> str:
    """Aggiorna il testo aggiunto di una locandina già pubblicata e modifica
    la didascalia del post nel gruppo. Le ripubblicazioni useranno il nuovo testo."""
    conn = _conn()
    row = conn.execute(
        "SELECT didascalia, msg_gruppo, torneo_ref, stato FROM locandine WHERE id = ?", (lid,)
    ).fetchone()
    if not row or row[3] != "pubblicata":
        return f"La locandina n° {lid} non è pubblicata."
    didascalia, msg_gruppo, torneo_ref, _ = row
    extra = None if testo.lower() == "nessuno" else testo
    completo = _testo_completo(didascalia, extra)
    # riga "Iscritte X/max" solo se il torneo della locandina è ancora quello attivo
    att = _torneo_attivo(conn)
    if torneo_ref and att and att[0] == torneo_ref:
        iscritte = conn.execute(
            "SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (torneo_ref,)
        ).fetchone()[0]
        completo += f"\n\n{_riga_posti(iscritte, conn, torneo_ref)}"
    if len(completo) > LIMITE_DIDASCALIA:
        return f"Troppo lungo: massimo {LIMITE_DIDASCALIA} caratteri di didascalia. Accorcialo."
    gruppo = _gruppo(conn)
    if gruppo is None or not msg_gruppo:
        return "Non trovo il post nel gruppo (nessun gruppo registrato o messaggio sconosciuto)."
    try:
        await bot.edit_message_caption(chat_id=gruppo, message_id=msg_gruppo, caption=completo)
    except Exception as e:
        if "not modified" in str(e).lower():
            return "Il testo è già quello: nessuna modifica."
        return f"❌ Didascalia NON modificata: errore Telegram ({e}). Il post è stato cancellato a mano?"
    conn.execute("UPDATE locandine SET extra = ? WHERE id = ?", (extra, lid))
    conn.commit()
    return f"✅ Didascalia della locandina n° {lid} aggiornata nel gruppo:\n\n{completo}"


async def didascalia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/didascalia testo | /didascalia N | /didascalia nessuno"""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Usalo in chat privata con il bot.")
        return
    # testo dopo il comando, con gli a capo
    testo = (update.message.text or "").split(maxsplit=1)
    testo = testo[1].strip() if len(testo) > 1 else ""
    conn = _conn()
    if testo.isdigit():
        lid, testo = int(testo), ""
    else:
        row = conn.execute(
            "SELECT id FROM locandine WHERE stato = 'pubblicata' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            await update.message.reply_text("Nessuna locandina pubblicata.")
            return
        lid = row[0]
    if not testo:
        context.user_data["locandina_attesa"] = ("didascalia", lid)
        await update.message.reply_text(
            f"Scrivi il testo da mettere sotto la didascalia della locandina n° {lid} "
            "(anche su più righe). Sostituisce quello aggiunto prima.\n"
            "'nessuno' lo toglie, 'annulla' lascia tutto com'è."
        )
        return
    await update.message.reply_text(await _modifica_pubblicata(context.bot, lid, testo))


async def ripristina_programmate(application):
    """All'avvio rimette in coda le pubblicazioni salvate nel database."""
    conn = _conn()
    adesso = datetime.now(FUSO)
    righe = conn.execute(
        "SELECT id, pubblica_il FROM locandine WHERE stato = 'programmata'"
    ).fetchall()
    for lid, quando_iso in righe:
        quando = datetime.fromisoformat(quando_iso)
        if quando > adesso:
            _programma_job(application.job_queue, lid, quando)
        elif adesso - quando <= RITARDO_MASSIMO:
            _programma_job(application.job_queue, lid, adesso + timedelta(seconds=10))
        else:
            conn.execute("UPDATE locandine SET stato = 'scaduta' WHERE id = ?", (lid,))
            conn.commit()
            for admin in _admin_ids:
                try:
                    await application.bot.send_message(
                        admin,
                        f"⚠️ La locandina n° {lid} doveva uscire il {quando.astimezone(FUSO).strftime('%d/%m %H:%M')} "
                        "ma il bot era spento. Non l'ho pubblicata: rifalla con /locandina se serve.",
                    )
                except Exception:
                    pass

    # ripubblicazioni: se il bot era spento all'ora prevista, riparte tra poco
    # (il controllo di validità lo fa il giro stesso)
    for lid, quando_iso in conn.execute(
        "SELECT id, prossimo_repost FROM locandine "
        "WHERE stato = 'pubblicata' AND ripeti = 1 AND prossimo_repost IS NOT NULL"
    ).fetchall():
        quando = max(datetime.fromisoformat(quando_iso), adesso + timedelta(seconds=30))
        _programma_repost(conn, application.job_queue, lid, quando)


def registra(app, db_connect, is_admin, chiave_gruppo, admin_ids, max_squadre=18,
             ripubblica_lista=None):
    """Collega il comando al bot. Va chiamata da main() del file principale."""
    global _db_connect, _is_admin, _chiave_gruppo, _admin_ids, _max_squadre, _ripubblica_lista
    _ripubblica_lista = ripubblica_lista
    _db_connect, _is_admin, _chiave_gruppo, _admin_ids = db_connect, is_admin, chiave_gruppo, set(admin_ids)
    _max_squadre = max_squadre
    app.add_handler(CommandHandler("locandina", locandina))
    app.add_handler(CommandHandler("programmate", programmate))
    app.add_handler(CommandHandler("ripubblica", ripubblica))
    app.add_handler(CommandHandler("annullalocandina", annullalocandina))
    app.add_handler(CommandHandler("didascalia", didascalia))
    app.add_handler(CallbackQueryHandler(bottoni, pattern=r"^loc:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, risposta_privata),
        group=2,
    )


COMANDI_LOCANDINA = [
    ("locandina", "[admin] Crea la locandina — /locandina 16/10 volo [22:00] [25€] [premi COPPE]"),
    ("programmate", "[admin] Elenco locandine programmate"),
    ("ripubblica", "[admin] Ripubblica subito la locandina con iscritte e lista — /ripubblica [N] [48h]"),
    ("didascalia", "[admin] Cambia il testo sotto l'ultima locandina pubblicata — /didascalia testo"),
    ("annullalocandina", "[admin] Annulla una locandina programmata o ferma le ripubblicazioni — /annullalocandina N"),
]
