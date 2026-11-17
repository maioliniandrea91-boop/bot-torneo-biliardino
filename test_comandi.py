"""Test dei comandi più usati, con un Telegram finto (nessuna rete).
python3 test_comandi.py                      -> esegue lo scenario e i controlli
python3 test_comandi.py --trascrizione F.json -> salva anche tutto quello che il bot
                                                ha scritto e il database finale
                                                (serve a confrontare due versioni)

Lo scenario passa dal vero main() del bot: stessi comandi, stessi gestori,
stesso database SQLite (in una cartella temporanea)."""
import asyncio
import itertools
import json
import os
import re
import sqlite3
import sys
import tempfile

CARTELLA = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "123:prova"
os.environ["ADMIN_IDS"] = "1"
os.environ["DB_PATH"] = os.path.join(CARTELLA, "comandi.db")

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler  # noqa: E402

import bot_iscrizioni_torneo as BOT  # noqa: E402

ADMIN, UTENTE, UTENTE2 = 1, 200, 300
GRUPPO = -100
_ids = itertools.count(1000)


# ======================= TELEGRAM FINTO =======================

def tastiera(markup):
    if not markup or not getattr(markup, "inline_keyboard", None):
        return None
    return [[(b.text, b.callback_data) for b in riga] for riga in markup.inline_keyboard]


def pulisci(v):
    """Toglie dalle trascrizioni ciò che cambia a ogni esecuzione."""
    if isinstance(v, str):
        return re.sub(r"pod:(ok|no):\d+", r"pod:\1:<codice>", v)
    if isinstance(v, (list, tuple)):
        return [pulisci(x) for x in v]
    return v


class Foto:
    def __init__(self, file_id):
        self.file_id = file_id


class Chat:
    def __init__(self, cid):
        self.id = cid
        self.type = "private" if cid > 0 else "supergroup"


class Utente:
    def __init__(self, uid):
        self.id = uid
        self.username = f"utente{uid}"
        self.first_name = f"Nome{uid}"


class Registro:
    def __init__(self):
        self.voci = []

    def scrivi(self, chi, metodo, chat, **kw):
        voce = {"chi": chi, "metodo": metodo, "chat": chat}
        for k, v in kw.items():
            if k == "reply_markup":
                v = tastiera(v)
            elif k in ("photo", "document") and not isinstance(v, str):
                v = "<file>"
            elif hasattr(v, "to_dict"):
                v = str(v)
            voce[k] = pulisci(v)
        self.voci.append(voce)


REG = Registro()


class Messaggio:
    def __init__(self, chat_id, text=None, caption=None, photo=None, reply_to=None, utente=None):
        self.message_id = next(_ids)
        self.chat_id = chat_id
        self.chat = Chat(chat_id)
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.reply_to_message = reply_to
        self.from_user = utente
        self.document = None
        self.forward_origin = None

    async def reply_text(self, text, **kw):
        REG.scrivi("risposta", "reply_text", self.chat_id, text=text, **kw)
        return Messaggio(self.chat_id, text=text)

    async def reply_photo(self, photo, caption=None, **kw):
        REG.scrivi("risposta", "reply_photo", self.chat_id, caption=caption, **kw)
        return Messaggio(self.chat_id, caption=caption, photo=[Foto(f"foto{next(_ids)}")])

    async def reply_document(self, document, **kw):
        REG.scrivi("risposta", "reply_document", self.chat_id, **kw)
        return Messaggio(self.chat_id)

    async def delete(self):
        REG.scrivi("risposta", "delete", self.chat_id, messaggio="comando")
        return True


class BotFinto:
    username = "BotProva"
    id = 999

    def __getattr__(self, nome):
        async def metodo(*args, **kw):
            chat = kw.pop("chat_id", args[0] if args else None)
            if nome == "send_message" and len(args) > 1:
                kw["text"] = args[1]
            REG.scrivi("bot", nome, chat, **{k: v for k, v in kw.items() if k != "message_id"},
                       **({"messaggio": "id"} if "message_id" in kw else {}))
            if nome in ("send_photo",):
                return Messaggio(chat, caption=kw.get("caption"), photo=[Foto(f"foto{next(_ids)}")])
            if nome.startswith("send_") or nome.startswith("edit_"):
                return Messaggio(chat, text=kw.get("text"), caption=kw.get("caption"))
            return True
        return metodo


class Job:
    def __init__(self, callback, data, name):
        self.callback, self.data, self.name = callback, data, name

    def schedule_removal(self):
        pass


class CodaFinta:
    def __init__(self):
        self.lavori = []

    def run_once(self, callback, when=None, data=None, name=None, **kw):
        j = Job(callback, data, name)
        self.lavori.append(j)
        return j

    def run_daily(self, callback, time=None, days=None, name=None, **kw):
        j = Job(callback, None, name)
        self.lavori.append(j)
        return j

    def run_repeating(self, callback, interval=None, first=None, name=None, **kw):
        return Job(callback, None, name)

    def get_jobs_by_name(self, name):
        return [j for j in self.lavori if j.name == name]


class Contesto:
    def __init__(self, bot, coda, user_data, args=None):
        self.bot, self.job_queue, self.user_data, self.args = bot, coda, user_data, args
        self.bot_data, self.chat_data = {}, {}


class Query:
    def __init__(self, uid, data, messaggio):
        self.from_user = Utente(uid)
        self.data = data
        self.message = messaggio

    async def answer(self, *a, **kw):
        return True

    async def edit_message_text(self, text=None, **kw):
        REG.scrivi("risposta", "edit_message_text", self.message.chat_id, text=text, **kw)

    async def edit_message_caption(self, caption=None, **kw):
        REG.scrivi("risposta", "edit_message_caption", self.message.chat_id, caption=caption, **kw)

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        REG.scrivi("risposta", "edit_message_reply_markup", self.message.chat_id,
                   reply_markup=reply_markup)


class Update_:
    def __init__(self, uid, chat_id, messaggio=None, query=None):
        self.effective_user = Utente(uid)
        self.effective_chat = Chat(chat_id)
        self.message = messaggio
        self.effective_message = messaggio or (query.message if query else None)
        self.callback_query = query


class AppFinta:
    """Raccoglie i gestori registrati da main() invece di avviare il bot."""

    def __init__(self):
        self.gestori = []
        self.bot = BotFinto()
        self.job_queue = CodaFinta()
        self.bot_data = {}

    def add_handler(self, h, group=0):
        self.gestori.append((group, h))

    def add_error_handler(self, h):
        pass

    def run_polling(self, *a, **kw):
        pass


class Costruttore:
    def __getattr__(self, nome):
        return lambda *a, **kw: self

    def build(self):
        return APP


APP = AppFinta()
USER_DATA = {}


class _Application:
    @staticmethod
    def builder():
        return Costruttore()


def avvia():
    BOT.Application = _Application
    BOT.main()


def _gestore_comando(nome):
    for _, h in APP.gestori:
        if isinstance(h, CommandHandler) and nome in h.commands:
            return h.callback
    raise KeyError(f"comando /{nome} non registrato")


def _gestore_bottone(data):
    for _, h in APP.gestori:
        if isinstance(h, CallbackQueryHandler) and h.pattern and h.pattern.match(data):
            return h.callback
    raise KeyError(f"nessun gestore per il bottone {data}")


def _contesto(uid, args=None):
    return Contesto(APP.bot, APP.job_queue, USER_DATA.setdefault(uid, {}), args)


async def cmd(testo, uid=ADMIN, chat=None, foto=None, risposta_a=None):
    """Manda un comando come farebbe Telegram. chat=None -> chat privata dell'utente."""
    chat = uid if chat is None else chat
    REG.voci.append({"chi": "utente", "uid": uid, "chat": chat, "testo": testo,
                     **({"foto": True} if foto else {})})
    nome = testo.split()[0][1:].split("@")[0]
    if foto:   # comando nella didascalia di una foto: arriva da MessageHandler, args = None
        msg = Messaggio(chat, caption=testo, photo=[Foto(foto)], utente=Utente(uid))
        args = None
    else:
        msg = Messaggio(chat, text=testo, reply_to=risposta_a, utente=Utente(uid))
        args = testo.split()[1:]
    await _gestore_comando(nome)(Update_(uid, chat, messaggio=msg), _contesto(uid, args))


async def bottone(data, uid=ADMIN, chat=None, messaggio=None):
    chat = uid if chat is None else chat
    REG.voci.append({"chi": "utente", "uid": uid, "chat": chat, "bottone": pulisci(data)})
    q = Query(uid, data, messaggio or Messaggio(chat, text="anteprima"))
    await _gestore_bottone(data)(Update_(uid, chat, query=q), _contesto(uid))


async def testo_privato(testo, uid=ADMIN):
    """Testo libero in privato (risposte a 'Aggiungi testo', 'Programma'...)."""
    REG.voci.append({"chi": "utente", "uid": uid, "chat": uid, "testo": testo})
    for gruppo, h in sorted(APP.gestori, key=lambda x: x[0]):
        if isinstance(h, MessageHandler) and gruppo == 2:
            msg = Messaggio(uid, text=testo, utente=Utente(uid))
            await h.callback(Update_(uid, uid, messaggio=msg), _contesto(uid))


def ultimo_codice_podio(uid=ADMIN):
    return USER_DATA[uid]["podio_attesa"]["codice"]


def ultimi_testi(n=1):
    """Testi/didascalie degli ultimi n messaggi del bot."""
    out = [v.get("text") or v.get("caption") for v in REG.voci
           if v["chi"] in ("bot", "risposta") and (v.get("text") or v.get("caption"))]
    return out[-n:]


def dump_db():
    conn = sqlite3.connect(os.environ["DB_PATH"])
    out = {}
    for (tab,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        cur = conn.execute(f"SELECT * FROM {tab} ORDER BY 1")
        nomi = [d[0] for d in cur.description]
        tieni = [i for i, n in enumerate(nomi) if not n.endswith("_il") and n != "prossimo_repost"]
        out[tab] = [[nomi[i] for i in tieni]] + [[r[i] for i in tieni] for r in cur.fetchall()]
    return out


# ======================= SCENARIO =======================

def controlla(cond, cosa):
    if not cond:
        raise AssertionError(cosa + "\nUltimi messaggi: " + json.dumps(ultimi_testi(3), ensure_ascii=False))


async def scenario():
    avvia()
    # gruppo registrato (serve a podio, locandina, sondaggio)
    await cmd("/registragruppo", chat=GRUPPO)
    await cmd("/torneo", chat=GRUPPO)
    controlla("Torneo attuale: #1 Torneo" in ultimi_testi()[0], "/torneo iniziale")

    # ---- iscrizioni ----
    await cmd("/iscrivi antonio e eva", uid=UTENTE, chat=GRUPPO)
    controlla("✅ Iscritta: Antonio-Eva" in ultimi_testi()[0], "normalizzazione nome coppia")
    await cmd("/iscrivi Altro Nome", uid=UTENTE, chat=GRUPPO)
    controlla("Sei già iscritto come 'Antonio-Eva'" in ultimi_testi()[0], "una coppia per utente")
    await cmd("/iscrivi Gianni🔥Pinna", uid=UTENTE2, chat=GRUPPO)
    controlla("Gianni-Pinna" in ultimi_testi()[0], "emoji come separatore")
    await cmd("/iscrivi ANTONIO-eva", uid=ADMIN, chat=GRUPPO)
    controlla("è già stato preso" in ultimi_testi()[0], "nome duplicato")
    for i, c in enumerate(["Luca-Matteo", "Paolo-Michele", "Ada/Ugo", "Rino Sara", "Dino+Gino"]):
        await cmd(f"/iscrivi {c}", uid=ADMIN, chat=GRUPPO if i % 2 else ADMIN)
    await cmd("/iscrivi Lia", uid=ADMIN, chat=GRUPPO)          # nome ambiguo: resta com'è
    await cmd("/squadre", uid=UTENTE)
    controlla("Totale: 8/18" in ultimi_testi()[0], "/squadre conta 8")
    controlla("✅ Torneo confermato! Raggiunte 8 coppie" in ultimi_testi()[0], "conferma a soglia")
    await cmd("/iscrivi", uid=UTENTE2, chat=GRUPPO)
    controlla("Uso corretto" in ultimi_testi()[0], "/iscrivi senza nome")

    # ---- rinomina / rimuovi / ritira ----
    await cmd("/rinomina 8 | lia e teo", chat=GRUPPO)
    controlla("✏️ Lia → Lia-Teo" in ultimi_testi()[0], "/rinomina per numero")
    await cmd("/rinomina Ada-Ugo | Ada-Ugolino")
    controlla("✏️ Ada-Ugo → Ada-Ugolino" in ultimi_testi()[0], "/rinomina per nome")
    await cmd("/rinomina 99 | X-Y")
    controlla("Non c'è una squadra n° 99" in ultimi_testi()[0], "/rinomina numero fuori lista")
    await cmd("/rinomina 2 | Antonio-Eva")
    controlla("già usato da un'altra squadra" in ultimi_testi()[0], "/rinomina su nome esistente")
    await cmd("/rinomina 1 | Antonio-Eva", uid=UTENTE)
    controlla("riservato agli admin" in ultimi_testi()[0], "/rinomina solo admin")
    await cmd("/rimuovi dino-gino", chat=GRUPPO)
    controlla("🗑️ Rimossa: Dino-Gino" in ultimi_testi()[0], "/rimuovi")
    await cmd("/ritira", uid=UTENTE2, chat=GRUPPO)
    controlla("↩️ Ritirata: Gianni-Pinna" in ultimi_testi()[0], "/ritira")
    await cmd("/ritira", uid=ADMIN)
    controlla("Hai più squadre iscritte" in ultimi_testi()[0], "/ritira admin con più coppie")
    await cmd("/ritira Nessuno-Qui", uid=ADMIN)
    controlla("Non trovo 'Nessuno-Qui'" in ultimi_testi()[0], "/ritira nome non suo")
    await cmd("/rimuovi Lia-Teo", uid=UTENTE, chat=GRUPPO)
    await cmd("/rimuovi Nessuno-Qui")
    await cmd("/ritira Rino-Sara", uid=ADMIN)
    await cmd("/soglia")
    await cmd("/soglia 5")
    controlla("8 → 5" in ultimi_testi()[0], "/soglia")
    await cmd("/chiudi")
    await cmd("/iscrivi Pio-Pia", uid=UTENTE2, chat=GRUPPO)
    controlla("iscrizioni sono chiuse" in ultimi_testi()[0], "/chiudi blocca")
    await cmd("/apri")
    await cmd("/iscrivi Pio-Pia", uid=UTENTE2, chat=GRUPPO)
    await cmd("/esporta")
    await cmd("/squadre", chat=GRUPPO)

    # ---- sfide ----
    await cmd("/sfida Luca-Matteo Paolo-Michele 5-2", uid=UTENTE, chat=GRUPPO)
    controlla("Registrata 1 sfida" in ultimi_testi()[0], "/sfida")
    await cmd("/sfida Luca-Luca Paolo-Michele 5-2", uid=UTENTE)
    controlla("Nessuna sfida salvata" in ultimi_testi()[0], "/sfida giocatore doppio")
    await cmd("/sfida 01/01\nAntonio-Eva Lia-Teo 3-3\nLuca-Paolo Matteo-Michele 4-1", uid=UTENTE)
    controlla("Registrate 2 sfide" in ultimi_testi()[0], "/sfida più righe con data")
    await cmd("/classifica", uid=UTENTE)
    await cmd("/rinominagiocatore Teo | Teodoro")
    await cmd("/giocatori")

    # ---- podio e storico ----
    await cmd("/podio Luca-Matteo | Paolo-Michele", chat=GRUPPO)
    controlla("Uso corretto: /podio" in ultimi_testi()[0], "/podio incompleto")
    await cmd("/podio Luca-Matteo | Paolo-Michele | Antonio-Eva | Gran serata", chat=GRUPPO,
              foto="fotoPodio1")
    controlla(ultimi_testi()[0].startswith("🏆 Podio — Torneo"), "/podio con foto nel gruppo")
    await cmd("/podio Antonio-Eva | Luca-Matteo | Paolo-Michele", chat=GRUPPO)
    controlla("ha già un podio" in ultimi_testi()[0], "/podio già presente nel gruppo")
    await cmd("/podio Antonio-Eva | Luca-Matteo | Paolo-Michele")
    controlla("viene sostituito" in ultimi_testi(2)[0], "/podio sostituzione in privato: avviso")
    vecchio = ultimo_codice_podio()
    await cmd("/podio #1 Antonio-Eva | Luca-Matteo | Pio-Pia")
    await bottone(f"pod:ok:{vecchio}")
    controlla("Anteprima non più valida" in ultimi_testi()[0], "/podio bottone vecchio")
    await cmd("/podio #1 Pio-Pia | Antonio-Eva | Luca-Matteo")
    await bottone(f"pod:no:{ultimo_codice_podio()}")
    controlla("Podio annullato" in ultimi_testi()[0], "/podio annulla")
    await cmd("/podio #1 Antonio-Eva | Luca-Matteo | Pio-Pia | " + "x" * 1100, foto="fotoLunga")
    controlla("Descrizione troppo lunga" in ultimi_testi()[0], "/podio didascalia troppo lunga")
    await cmd("/podio #1 Antonio-Eva | Luca-Matteo | Pio-Pia",
              risposta_a=Messaggio(ADMIN, photo=[Foto("fotoRisposta")]))
    await bottone(f"pod:ok:{ultimo_codice_podio()}", messaggio=Messaggio(ADMIN, caption="anteprima",
                                                                       photo=[Foto("x")]))
    await cmd("/podio #1 Antonio-Eva | Luca-Matteo | Pio-Pia")
    await bottone(f"pod:ok:{ultimo_codice_podio()}")
    controlla("✅ Podio pubblicato nel gruppo." in ultimi_testi()[0], "/podio pubblicato da privato")
    await cmd("/medagliere", uid=UTENTE)
    await cmd("/storico", uid=UTENTE)
    controlla("#1 Torneo — 6 squadre — 🥇 Antonio-Eva 🔵 (attivo)" in ultimi_testi()[0], "/storico elenco")

    # ---- locandina: apre il torneo nuovo e archivia il vecchio ----
    await cmd("/locandina 20/12 volo", chat=GRUPPO)
    controlla("in chat privata" in ultimi_testi()[0], "/locandina solo in privato")
    await cmd("/locandina 99/99 volo")
    await cmd("/locandina 20/12 5x volo 22:00")
    lid = int(re.search(r"n° (\d+)", ultimi_testi()[0]).group(1))
    await bottone(f"loc:ripeti:{lid}")
    await bottone(f"loc:ora:{lid}")
    controlla("non ha testo aggiunto" in ultimi_testi()[0], "/locandina avviso senza testo")
    await bottone(f"loc:testo:{lid}")
    await testo_privato("Iscrizioni aperte!")
    await bottone(f"loc:ora:{lid}")
    controlla(any("pubblicata nel gruppo" in (t or "") for t in ultimi_testi(3)), "/locandina pubblicata")
    await cmd("/torneo")
    controlla("#2 " in ultimi_testi()[0], "nuovo torneo aperto dalla locandina")

    # ---- podio a torneo nuovo vuoto, storico, eliminatorneo ----
    await cmd("/podio Antonio-Eva | Luca-Matteo | Pio-Pia", chat=GRUPPO)
    controlla("non ha squadre iscritte" in ultimi_testi()[0], "/podio blocco torneo attivo vuoto")
    await cmd("/iscrivi Ugo-Ivo", uid=UTENTE, chat=GRUPPO)
    await cmd("/ripubblica")      # locandina + lista squadre di nuovo sotto
    controlla(any("1. Ugo-Ivo" in (t or "") for t in ultimi_testi(3)), "/ripubblica rimette la lista")
    await cmd("/storico", uid=UTENTE)
    await cmd("/storico #1", uid=UTENTE)
    controlla("🥇 Antonio-Eva" in ultimi_testi()[0] and "Squadre iscritte (6)" in ultimi_testi()[0],
              "/storico #N")
    await cmd("/storico #99", uid=UTENTE)
    await cmd("/storico 2", uid=UTENTE)
    await cmd("/nometorneo Prova")
    await cmd("/nometorneo Prova Due CONFERMA")
    controlla("Nuovo torneo aperto: 'Prova Due'" in ultimi_testi()[0], "/nometorneo")
    await cmd("/storico Prova Due", uid=UTENTE)
    await cmd("/nometorneo prova due CONFERMA")
    controlla("è già 'Prova Due'" in ultimi_testi()[0], "/nometorneo stesso nome")
    await cmd("/eliminatorneo")
    await cmd("/eliminatorneo 20/12 5X Volo")
    controlla("Stai per eliminare #2" in ultimi_testi()[0], "/eliminatorneo per nome")
    await cmd("/eliminatorneo Inesistente CONFERMA")
    await cmd("/ripubblica")
    for i in range(18):
        await cmd(f"/iscrivi Gioc{i}-Altro{i}")
    await cmd("/iscrivi Ultimo-Arrivato")
    controlla("Torneo al completo: raggiunto il limite di 18" in ultimi_testi()[0], "limite 18 coppie")
    await cmd("/squadre", chat=GRUPPO)
    controlla("Torneo al completo." in ultimi_testi()[0], "riga conferma a torneo pieno")
    await cmd("/ripubblica")
    await cmd("/eliminatorneo #3")
    controlla("torneo ATTIVO" in ultimi_testi()[0], "/eliminatorneo blocca l'attivo")
    await cmd("/eliminatorneo #1")
    controlla("Stai per eliminare #1" in ultimi_testi()[0], "/eliminatorneo riepilogo")
    await cmd("/eliminatorneo #1 CONFERMA")
    controlla("eliminato definitivamente" in ultimi_testi()[0], "/eliminatorneo conferma")
    await cmd("/eliminatorneo #1")
    controlla("Nessun torneo con numero #1" in ultimi_testi()[0], "/eliminatorneo già eliminato")
    await cmd("/medagliere", uid=UTENTE)
    await cmd("/reset")
    await cmd("/reset CONFERMA")

    # ---- sondaggio, help ----
    await cmd("/vedisondaggio")
    await cmd("/modificasondaggio Vieni? | Sì | No | Forse")
    await cmd("/sondaggio")
    await cmd("/anteprimasondaggio", chat=GRUPPO)
    for j in APP.job_queue.lavori:
        if j.name == "sondaggio_gioco_libero_lunedi":
            await j.callback(Contesto(APP.bot, APP.job_queue, {}))
    await cmd("/start")
    await cmd("/start app", uid=UTENTE)
    await cmd("/help", uid=UTENTE)
    await cmd("/help")
    for g, h in APP.gestori:   # menu admin del gruppo (gestore su tutti i messaggi)
        if g == 1 and isinstance(h, MessageHandler):
            await h.callback(Update_(ADMIN, GRUPPO, messaggio=Messaggio(GRUPPO, text="ciao")),
                             _contesto(ADMIN))


def main():
    asyncio.run(scenario())
    db = dump_db()
    tornei = db["tornei"]
    controlla(len(tornei) == 3 and [r[0] for r in tornei[1:]] == [2, 3], "tornei finali: #2 e #3")
    if "--trascrizione" in sys.argv:
        with open(sys.argv[sys.argv.index("--trascrizione") + 1], "w", encoding="utf-8") as f:
            json.dump({"messaggi": REG.voci, "database": db}, f, ensure_ascii=False, indent=1, default=str)
    print(f"test_comandi OK ({len(REG.voci)} passaggi)")


if __name__ == "__main__":
    main()
