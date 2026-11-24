"""Strato logico di tornei e iscrizioni: niente Telegram, solo database e testi.

Lo usano i comandi (/iscrivi, /rinomina, /storico, /podio, ...) e lo userà il
pannello admin della Mini App: una sola logica, due ingressi. Ogni funzione che
modifica qualcosa ritorna un Esito (ok + testo da mostrare + dati utili)."""
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

import comando_locandina
import statistiche
from config import LIMITE_COPPIE_MAX, LIMITE_COPPIE_MIN, PER_PAGINA_STORICO
from database import get_torneo_attivo, iscrizioni_aperte, max_coppie

MAX_CAPTION = 1024  # limite Telegram per la didascalia di una foto
TESTO_ISCRIZIONI_CHIUSE = "Le iscrizioni sono chiuse. Contatta un admin."


@dataclass
class Esito:
    ok: bool
    testo: str
    dati: dict = field(default_factory=dict)


def _errore(testo: str, **dati) -> Esito:
    return Esito(False, testo, dati)


# ======================= NOMI DELLE COPPIE =======================

SEPARATORI_COPPIA = re.compile(r"\s+(?:e|ed)\s+|\s*[-/&+,]\s*", re.IGNORECASE)


def togli_simboli(testo: str) -> str:
    """Sostituisce con uno spazio emoji, simboli e caratteri invisibili
    (giunzioni, selettori di variante, toni della pelle). Restano lettere,
    numeri, spazi, apostrofi, punti e i separatori di coppia."""
    out = []
    for ch in testo:
        cat = unicodedata.category(ch)
        if cat[0] in "LN" or ch in " '’.-/&+,":
            out.append(ch)
        elif cat[0] == "M" and out and out[-1] != " " and ch not in "︎️":
            out.append(ch)  # accenti combinati attaccati a una lettera
        else:
            out.append(" ")
    return "".join(out)


def normalizza_nome_squadra(testo: str) -> str:
    """Le squadre sono sempre coppie: porta tutto al formato 'Nome1-Nome2'.
    'antonio e eva', 'Antonio/Eva', 'Antonio Eva' -> 'Antonio-Eva'.
    Emoji e simboli valgono come spazio: 'Gianni🔥Pinna' -> 'Gianni-Pinna'.
    Se non si riesce a riconoscere con certezza due nomi, lascia com'è."""
    pulito = " ".join(togli_simboli(testo).split())
    parti = [p for p in SEPARATORI_COPPIA.split(pulito) if p]
    if len(parti) != 2:
        parole = pulito.split(" ")
        if len(parole) != 2:
            return pulito  # caso ambiguo: lo sistemi tu con /rinomina
        parti = parole

    def maiuscola(nome: str) -> str:
        return " ".join(w[:1].upper() + w[1:] for w in nome.split(" "))

    return "-".join(maiuscola(p.strip()) for p in parti)


def _registra_giocatori(conn, nome_squadra: str):
    try:
        statistiche.registra_coppia(conn, nome_squadra)  # anagrafica giocatori
    except Exception:
        pass  # l'iscrizione non deve mai fallire per l'anagrafica


# ======================= SQUADRE DEL TORNEO ATTIVO =======================

def squadre_torneo(conn, torneo_id: int) -> list[tuple[int, str]]:
    """(id, nome) delle squadre di un torneo, in ordine di iscrizione."""
    return conn.execute(
        "SELECT id, nome_squadra FROM squadre WHERE torneo_id = ? ORDER BY iscritto_il",
        (torneo_id,),
    ).fetchall()


def conta_squadre(conn, torneo_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM squadre WHERE torneo_id = ?", (torneo_id,)).fetchone()[0]


def riga_conferma(conn, iscritte: int, massimo: int) -> str:
    """Riga di conferma in fondo alla lista squadre, solo a soglia raggiunta.
    Se il massimo del torneo è sotto la soglia, vale il massimo (torneo pieno = confermato)."""
    soglia = min(comando_locandina.get_soglia(conn), massimo)
    if iscritte < soglia:
        return ""
    restano = massimo - iscritte
    if restano <= 0:
        return f"✅ Torneo confermato! Raggiunte {soglia} coppie. Torneo al completo."
    posti = "c'è ancora 1 posto" if restano == 1 else f"ci sono ancora {restano} posti"
    return (f"✅ Torneo confermato! Raggiunte {soglia} coppie. "
            f"{posti[0].upper() + posti[1:]}, iscrivetevi con /iscrivi Nome1-Nome2")


def testo_squadre(conn, torneo_id: int, nome_torneo: str) -> str:
    righe = squadre_torneo(conn, torneo_id)
    if not righe:
        return f"Nessuna squadra ancora iscritta a {nome_torneo}."

    testo = f"🏆 Squadre iscritte — {nome_torneo}\n\n"
    for i, (_, nome) in enumerate(righe, start=1):
        testo += f"{i}. {nome}\n"
    massimo = max_coppie(conn, torneo_id)
    testo += f"\nTotale: {len(righe)}/{massimo}"
    stato_torneo = riga_conferma(conn, len(righe), massimo)
    if stato_torneo:
        testo += f"\n\n{stato_torneo}"
    return testo


NOTA_OMONIMI = "Se è un'altra persona con lo stesso nome, aggiungi l'iniziale del cognome (es. Gianni G.)."


def giocatore_gia_iscritto(conn, torneo_id: int, nome_squadra: str, escludi_id: int | None = None) -> str | None:
    """Errore se uno dei due giocatori di 'Nome1-Nome2' è già in un'altra coppia del torneo.
    Nomi non nel formato a coppia: nessun controllo (li sistema l'admin con /rinomina)."""
    parti = [p.strip().lower() for p in nome_squadra.split("-")]
    if len(parti) != 2:
        return None
    if parti[0] == parti[1]:
        return "Lo stesso giocatore compare due volte."
    for (esistente,) in conn.execute("SELECT nome_squadra FROM squadre WHERE torneo_id = ? AND id IS NOT ?",
                                     (torneo_id, escludi_id)):
        gia = [p.strip().lower() for p in esistente.split("-")]
        for nome, p in zip(nome_squadra.split("-"), parti):
            if len(gia) == 2 and p in gia:
                return f"{nome.strip()} è già iscritto in {esistente}. {NOTA_OMONIMI}"
    return None


def iscrivi_coppia(conn, user_id: int, username: str | None, nome_scritto: str, admin: bool) -> Esito:
    """Iscrive una coppia al torneo attivo. Gli utenti normali: una coppia a testa;
    gli admin quante ne vogliono (spesso le iscrive Andrea per gli altri)."""
    torneo_id, _ = get_torneo_attivo(conn)
    if not iscrizioni_aperte(conn):
        return _errore(TESTO_ISCRIZIONI_CHIUSE)

    nome_squadra = normalizza_nome_squadra(nome_scritto.strip())
    if not nome_squadra:
        return _errore("Uso corretto: /iscrivi Nome1-Nome2")

    if not admin:
        esistente = conn.execute(
            "SELECT nome_squadra FROM squadre WHERE user_id = ? AND torneo_id = ?",
            (user_id, torneo_id),
        ).fetchone()
        if esistente:
            return _errore(f"Sei già iscritto come '{esistente[0]}'. Usa /ritira prima di reiscriverti.")

    duplicato = conn.execute(
        "SELECT 1 FROM squadre WHERE torneo_id = ? AND LOWER(nome_squadra) = LOWER(?)",
        (torneo_id, nome_squadra),
    ).fetchone()
    if duplicato:
        return _errore(f"Il nome '{nome_squadra}' è già stato preso in questo torneo. Scegline un altro.")

    # stesso giocatore in due coppie: vale per tutti, admin compresi (comando, pannello, app)
    if (err := giocatore_gia_iscritto(conn, torneo_id, nome_squadra)):
        return _errore(err)

    massimo = max_coppie(conn, torneo_id)
    if conta_squadre(conn, torneo_id) >= massimo:
        return _errore(
            f"Torneo al completo: raggiunto il limite di {massimo} coppie. "
            "Contatta un admin se vuoi essere messo in lista d'attesa."
        )

    conn.execute(
        "INSERT INTO squadre (torneo_id, user_id, username, nome_squadra, iscritto_il) "
        "VALUES (?, ?, ?, ?, ?)",
        (torneo_id, user_id, username, nome_squadra, datetime.now().isoformat()),
    )
    conn.commit()
    _registra_giocatori(conn, nome_squadra)
    return Esito(True, f"✅ Iscritta: {nome_squadra}", {"nome": nome_squadra})


def ritira_coppia(conn, user_id: int, nome_indicato: str | None) -> Esito:
    """Self-service: l'utente ritira una delle SUE coppie dal torneo attivo."""
    torneo_id, _ = get_torneo_attivo(conn)
    proprie = conn.execute(
        "SELECT id, nome_squadra FROM squadre WHERE user_id = ? AND torneo_id = ?",
        (user_id, torneo_id),
    ).fetchall()
    if not proprie:
        return _errore("Non risulti iscritto a nessuna squadra in questo torneo.")

    if len(proprie) == 1 and not nome_indicato:
        squadra_id, nome = proprie[0]
    elif nome_indicato:
        match = next((p for p in proprie if p[1].lower() == nome_indicato.lower()), None)
        if not match:
            elenco = ", ".join(n for _, n in proprie)
            return _errore(f"Non trovo '{nome_indicato}' tra le tue squadre. Le tue: {elenco}")
        squadra_id, nome = match
    else:
        elenco = ", ".join(n for _, n in proprie)
        return _errore(f"Hai più squadre iscritte. Specifica quale: /ritira NomeSquadra\nLe tue: {elenco}")

    conn.execute("DELETE FROM squadre WHERE id = ?", (squadra_id,))
    conn.commit()
    return Esito(True, f"↩️ Ritirata: {nome}", {"nome": nome})


def rimuovi_coppia(conn, nome_cercato: str) -> Esito:
    """Admin: rimuove QUALSIASI coppia dal torneo attivo, chiunque l'abbia iscritta."""
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    match = conn.execute(
        "SELECT id, nome_squadra FROM squadre WHERE torneo_id = ? AND LOWER(nome_squadra) = LOWER(?)",
        (torneo_id, nome_cercato),
    ).fetchone()
    if not match:
        return _errore(f"Nessuna squadra '{nome_cercato}' trovata in {nome_torneo}. Controlla /squadre.")

    squadra_id, nome_reale = match
    conn.execute("DELETE FROM squadre WHERE id = ?", (squadra_id,))
    conn.commit()
    return Esito(True, f"🗑️ Rimossa: {nome_reale}", {"nome": nome_reale})


def rinomina_coppia(conn, vecchio: str, nuovo_scritto: str) -> Esito:
    """Admin: corregge il nome di una coppia del torneo attivo. 'vecchio' è il
    numero in /squadre o il nome attuale. La coppia resta di chi l'ha iscritta
    e mantiene la sua posizione."""
    nuovo = normalizza_nome_squadra(nuovo_scritto)
    if not nuovo:
        return _errore("Il nuovo nome è vuoto.")

    torneo_id, nome_torneo = get_torneo_attivo(conn)
    righe = squadre_torneo(conn, torneo_id)
    if vecchio.isdigit():
        pos = int(vecchio)
        if not 1 <= pos <= len(righe):
            return _errore(f"Non c'è una squadra n° {pos}. Controlla /squadre.")
        squadra_id, nome_reale = righe[pos - 1]
    else:
        match = next((r for r in righe if r[1].lower() == vecchio.lower()), None)
        if not match:
            return _errore(f"Nessuna squadra '{vecchio}' in {nome_torneo}. Usa il numero da /squadre.")
        squadra_id, nome_reale = match

    if any(r[1].lower() == nuovo.lower() and r[0] != squadra_id for r in righe):
        return _errore(f"Il nome '{nuovo}' è già usato da un'altra squadra.")

    if (err := giocatore_gia_iscritto(conn, torneo_id, nuovo, escludi_id=squadra_id)):   # la coppia stessa non conta
        return _errore(err)

    conn.execute("UPDATE squadre SET nome_squadra = ? WHERE id = ?", (nuovo, squadra_id))
    conn.commit()
    _registra_giocatori(conn, nuovo)
    return Esito(True, f"✏️ {nome_reale} → {nuovo}", {"vecchio": nome_reale, "nuovo": nuovo})


def word_squadre(conn) -> tuple[bytes, str]:
    """Elenco squadre del torneo attivo in Word: (contenuto, nome file)."""
    import io
    from docx import Document
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    documento = Document()
    documento.add_heading(nome_torneo, level=1)
    for _, nome_squadra in squadre_torneo(conn, torneo_id):
        documento.add_paragraph(nome_squadra)
    buffer = io.BytesIO()
    documento.save(buffer)
    return buffer.getvalue(), f"squadre_{nome_torneo}.docx"


def svuota_torneo_attivo(conn) -> Esito:
    """Admin (/reset): cancella le iscrizioni del torneo attivo, lo storico resta."""
    torneo_id, nome_torneo = get_torneo_attivo(conn)
    conn.execute("DELETE FROM squadre WHERE torneo_id = ?", (torneo_id,))
    conn.commit()
    return Esito(True, f"Elenco squadre di '{nome_torneo}' svuotato.")


def imposta_soglia(conn, valore: str) -> Esito:
    """Admin (/soglia): coppie minime per scrivere 'Torneo confermato'."""
    attuale = comando_locandina.get_soglia(conn)
    if not valore.isdigit() or not 2 <= int(valore) <= LIMITE_COPPIE_MAX:
        return _errore(f"Scrivi un numero tra 2 e {LIMITE_COPPIE_MAX}. Es. /soglia 8")
    nuova = int(valore)
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES (?, ?) "
        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
        (comando_locandina.CHIAVE_SOGLIA, str(nuova)),
    )
    conn.commit()
    return Esito(True, f"✅ Soglia di conferma: {attuale} → {nuova} coppie.")


def valida_max_coppie(valore) -> str | None:
    """Errore da mostrare se il massimo non va bene, altrimenti None."""
    if not str(valore).isdigit() or not LIMITE_COPPIE_MIN <= int(valore) <= LIMITE_COPPIE_MAX:
        return f"Il massimo di coppie va da {LIMITE_COPPIE_MIN} a {LIMITE_COPPIE_MAX}. Es. /maxcoppie 14"
    return None


def imposta_max_coppie(conn, valore, torneo_id: int | None = None) -> Esito:
    """Coppie massime del torneo (attivo, se non indicato). Abbassarlo sotto le
    iscritte non toglie nessuno: blocca solo le nuove iscrizioni."""
    errore = valida_max_coppie(valore)
    if errore:
        return _errore(errore)
    if torneo_id is None:
        torneo_id, nome_torneo = get_torneo_attivo(conn)
    else:
        nome_torneo = conn.execute("SELECT nome FROM tornei WHERE id = ?", (torneo_id,)).fetchone()[0]
    vecchio, nuovo = max_coppie(conn, torneo_id), int(valore)
    conn.execute("UPDATE tornei SET max_coppie = ? WHERE id = ?", (nuovo, torneo_id))
    conn.commit()
    testo = f"✅ Massimo coppie di «{nome_torneo}»: {vecchio} → {nuovo}."
    iscritte = conta_squadre(conn, torneo_id)
    if iscritte > nuovo:
        testo += (f"\n⚠️ Sono già iscritte {iscritte} coppie: non tolgo nessuno, "
                  f"ma le nuove iscrizioni restano bloccate finché non si scende sotto {nuovo}.")
    elif iscritte == nuovo:
        testo += f"\nCon {iscritte} iscritte il torneo è al completo: nuove iscrizioni bloccate."
    soglia = comando_locandina.get_soglia(conn)
    if soglia > nuovo:
        testo += (f"\nℹ️ La soglia di conferma ({soglia}) è sopra il massimo: "
                  f"il torneo risulterà confermato a {nuovo} coppie, cioè quando è pieno.")
    return Esito(True, testo, {"torneo_id": torneo_id, "vecchio": vecchio, "nuovo": nuovo,
                               "iscritte": iscritte})


# ======================= TORNEI (attivo e storico) =======================

def apri_nuovo_torneo(conn, nuovo_nome: str) -> Esito:
    """Archivia il torneo attivo e ne apre uno nuovo, con iscrizioni aperte.
    Le coppie già iscritte restano legate al torneo vecchio."""
    vecchio_id, vecchio_nome = get_torneo_attivo(conn)
    if nuovo_nome.lower() == vecchio_nome.lower():
        return _errore(f"Il torneo attivo è già '{vecchio_nome}'.")

    conn.execute("UPDATE tornei SET chiuso = 1 WHERE id = ?", (vecchio_id,))
    conn.execute(
        "INSERT INTO tornei (nome, creato_il, chiuso) VALUES (?, ?, 0)",
        (nuovo_nome, datetime.now().isoformat()),
    )
    conn.execute(
        "INSERT INTO stato (chiave, valore) VALUES ('aperte', '1') "
        "ON CONFLICT(chiave) DO UPDATE SET valore = '1'"
    )
    conn.commit()
    return Esito(
        True,
        f"🏓 Nuovo torneo aperto: '{nuovo_nome}'.\n"
        f"'{vecchio_nome}' è stato archiviato ed è consultabile con /storico.",
        {"vecchio": vecchio_nome, "nuovo": nuovo_nome},
    )


def cerca_torneo(conn, cercato: str, colonne: str):
    """Torneo per numero ('#12', id fisso) o per nome (l'ultimo con quel nome).
    Ritorna (riga o None, testo da mostrare se non trovato)."""
    m = re.fullmatch(r"#\s*(\d+)", cercato)
    if m:
        row = conn.execute(f"SELECT {colonne} FROM tornei WHERE id = ?", (int(m.group(1)),)).fetchone()
        return row, f"Nessun torneo con numero #{m.group(1)}. Elenco con /storico"
    row = conn.execute(
        f"SELECT {colonne} FROM tornei WHERE LOWER(nome) = LOWER(?) ORDER BY id DESC LIMIT 1",
        (cercato,),
    ).fetchone()
    return row, f"Nessun torneo trovato con nome '{cercato}'."


def pagina_tornei(conn, pagina: int) -> dict:
    """Una pagina dello storico (dal più recente). Pagina fuori misura -> la più vicina."""
    totale = conn.execute("SELECT COUNT(*) FROM tornei").fetchone()[0]
    pagine = max(1, -(-totale // PER_PAGINA_STORICO))
    pagina = min(max(pagina, 1), pagine)
    righe = conn.execute("""
        SELECT t.id, t.nome, t.chiuso, COUNT(s.id), t.podio_primo
        FROM tornei t
        LEFT JOIN squadre s ON s.torneo_id = t.id
        GROUP BY t.id
        ORDER BY t.id DESC
        LIMIT ? OFFSET ?
    """, (PER_PAGINA_STORICO, (pagina - 1) * PER_PAGINA_STORICO)).fetchall()
    tornei = [{"id": tid, "nome": nome, "attivo": not chiuso, "squadre": n, "primo": primo}
              for tid, nome, chiuso, n, primo in righe]
    return {"pagina": pagina, "pagine": pagine, "totale": totale, "tornei": tornei}


def testo_pagina_tornei(dati: dict) -> str:
    pagina, pagine = dati["pagina"], dati["pagine"]
    testo = f"📜 Tornei — pagina {pagina}/{pagine} ({dati['totale']} in tutto)\n\n"
    for t in dati["tornei"]:
        stato_icona = " 🔵 (attivo)" if t["attivo"] else ""
        vincitori = f" — 🥇 {t['primo']}" if t["primo"] else ""
        testo += f"• #{t['id']} {t['nome']} — {t['squadre']} squadre{vincitori}{stato_icona}\n"
    if pagina < pagine:
        testo += f"\nPagina successiva: /storico {pagina + 1}"
    testo += "\nDettagli di uno specifico: /storico #N (o /storico NomeTorneo)"
    return testo


def dettaglio_torneo(conn, cercato: str) -> Esito:
    """Podio e squadre di un torneo (per #N o nome)."""
    row, non_trovato = cerca_torneo(conn, cercato, "id, nome, podio_primo, podio_secondo, podio_terzo")
    if not row:
        return _errore(non_trovato)
    tid, nome, primo, secondo, terzo = row
    squadre = [n for _, n in squadre_torneo(conn, tid)]

    testo = f"📜 #{tid} {nome}\n\n"
    if primo or secondo or terzo:
        testo += "🏆 Podio:\n"
        if primo:
            testo += f"🥇 {primo}\n"
        if secondo:
            testo += f"🥈 {secondo}\n"
        if terzo:
            testo += f"🥉 {terzo}\n"
        testo += "\n"
    testo += f"Squadre iscritte ({len(squadre)}):\n"
    for i, nome_sq in enumerate(squadre, start=1):
        testo += f"{i}. {nome_sq}\n"
    return Esito(True, testo, {"id": tid, "nome": nome, "podio": [primo, secondo, terzo],
                               "squadre": squadre})


def esamina_eliminazione(conn, cercato: str) -> Esito:
    """Primo passo di /eliminatorneo: trova il torneo e prepara il riepilogo.
    Il torneo attivo non si elimina (per non cancellare quello in corso)."""
    row, non_trovato = cerca_torneo(
        conn, cercato, "id, nome, chiuso, podio_primo, podio_secondo, podio_terzo")
    if not row:
        return _errore(non_trovato)
    torneo_id, nome, chiuso, primo, secondo, terzo = row
    if not chiuso:
        return _errore(
            f"Non puoi eliminare il torneo ATTIVO (#{torneo_id} «{nome}»). Aprine prima uno "
            "nuovo (locandina o /nometorneo) per archiviarlo, poi elimina quello archiviato."
        )
    n_squadre = conta_squadre(conn, torneo_id)
    testo = f"🗑️ Stai per eliminare #{torneo_id} «{nome}»: {n_squadre} squadre"
    if primo:
        testo += f", podio 🥇 {primo} 🥈 {secondo} 🥉 {terzo} (esce anche dal medagliere)"
    testo += f".\nPer confermare: /eliminatorneo #{torneo_id} CONFERMA"
    return Esito(True, testo, {"id": torneo_id, "nome": nome, "squadre": n_squadre})


def elimina_torneo(conn, cercato: str) -> Esito:
    """Elimina per sempre un torneo archiviato: squadre, podio e dati dell'app tornei."""
    esame = esamina_eliminazione(conn, cercato)
    if not esame.ok:
        return esame
    torneo_id, nome, n_squadre = esame.dati["id"], esame.dati["nome"], esame.dati["squadre"]
    conn.execute("DELETE FROM squadre WHERE torneo_id = ?", (torneo_id,))
    for tabella in ("app_tornei", "app_messaggi", "app_storia"):  # se le tabelle esistono
        try:
            conn.execute(f"DELETE FROM {tabella} WHERE torneo_id = ?", (torneo_id,))
        except sqlite3.OperationalError:
            pass
    conn.execute("DELETE FROM tornei WHERE id = ?", (torneo_id,))
    conn.commit()
    return Esito(
        True,
        f"🗑️ Torneo #{torneo_id} «{nome}» eliminato definitivamente dallo storico "
        f"({n_squadre} squadre rimosse con lui). Gli altri tornei mantengono il loro numero.",
        esame.dati,
    )


# ======================= RIORDINO NUMERI =======================
# I numeri #N sono gli id dei tornei: di norma fissi (vedi elimina_torneo). Questo li
# riporta a 1, 2, 3… in ordine di creazione, solo quando lo decide l'admin.
TABELLE_TORNEO = (("squadre", "torneo_id"), ("app_tornei", "torneo_id"), ("app_messaggi", "torneo_id"),
                  ("app_storia", "torneo_id"), ("locandine", "torneo_ref"))


def piano_riordino(conn) -> list[tuple[int, int]]:
    """[(vecchio, nuovo)] solo per i tornei che cambiano numero."""
    ids = [r[0] for r in conn.execute("SELECT id FROM tornei ORDER BY id")]
    return [(v, n) for n, v in enumerate(ids, 1) if v != n]


def blocco_riordino(conn, adesso: datetime | None = None) -> str | None:
    """Motivo per cui ora non si può riordinare (None = si può)."""
    from zoneinfo import ZoneInfo
    t = adesso or datetime.now(ZoneInfo("Europe/Rome"))
    if (t.weekday() == 4 and t.hour >= 20) or (t.weekday() == 5 and t.hour < 5):
        return "Non si riordina il venerdì sera (dalle 20 alle 5 del sabato): c'è il torneo."
    try:
        for (stato,) in conn.execute("SELECT stato FROM app_tornei WHERE torneo_id > 0"):
            try:
                fase = (json.loads(stato) or {}).get("fase")
            except (ValueError, AttributeError):
                fase = None
            if fase in ("gironi", "ko"):
                return "C'è un torneo in corso nell'app (gironi o fase finale): riordina a torneo finito."
    except sqlite3.OperationalError:
        pass
    try:
        if conn.execute("SELECT COUNT(*) FROM locandine WHERE stato = 'programmata'").fetchone()[0]:
            return "C'è una locandina programmata in attesa: pubblicala o annullala, poi riordina."
    except sqlite3.OperationalError:
        pass
    return None


def esamina_riordino(conn) -> Esito:
    blocco = blocco_riordino(conn)
    if blocco:
        return _errore(blocco)
    piano = piano_riordino(conn)
    if not piano:
        return _errore("I numeri sono già in ordine (1, 2, 3…): niente da riordinare.")
    testo = "🔢 Nuovi numeri dei tornei:\n" + "\n".join(f"#{v} → #{n}" for v, n in piano)
    return Esito(True, testo, {"piano": piano})


def riordina_tornei(conn) -> Esito:
    """Rinumera tutto in una sola transazione (se qualcosa va storto non cambia nulla)."""
    esame = esamina_riordino(conn)
    if not esame.ok:
        return esame
    piano = esame.dati["piano"]
    tabelle = [(t, c) for t, c in TABELLE_TORNEO
               if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    try:   # nessun commit fino alla fine: un errore annulla tutto
        for fase in (0, 1):   # prima a negativo (-1000-vecchio), poi al numero nuovo: niente scontri
            for v, n in piano:
                da, a = (v, -1000 - v) if fase == 0 else (-1000 - v, n)
                conn.execute("UPDATE tornei SET id = ? WHERE id = ?", (a, da))
                for t, c in tabelle:
                    conn.execute(f"UPDATE {t} SET {c} = ? WHERE {c} = ?", (a, da))
        massimo = conn.execute("SELECT COALESCE(MAX(id), 0) FROM tornei").fetchone()[0]
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'tornei'", (massimo,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return _errore(f"Riordino non riuscito, nulla è cambiato ({e}).")
    return Esito(True, f"🔢 Numeri riordinati: {len(piano)} tornei rinumerati, il prossimo sarà #{massimo + 1}.",
                 {"piano": piano})


# ======================= PODIO =======================

def leggi_podio(testo_args: str):
    """'[#N] Sq1 | Sq2 | Sq3 [| descrizione]' -> (num_torneo o None, [3 squadre] o None,
    descrizione). Dal 4° campo in poi è descrizione (eventuali '|' restano)."""
    num_torneo = None
    m = re.match(r"\s*#\s*(\d+)\s*", testo_args)
    if m:
        num_torneo = int(m.group(1))
        testo_args = testo_args[m.end():]
    parti = [s.strip() for s in testo_args.split("|")]
    squadre = [s for s in parti[:3] if s]
    descrizione = " | ".join(parti[3:]).strip()
    if len(parti) < 3 or len(squadre) != 3:
        return num_torneo, None, descrizione
    return num_torneo, squadre, descrizione


def prepara_podio(conn, num_torneo: int | None, squadre: list[str], descrizione: str,
                  foto_file_id: str | None, sostituzione_ammessa: bool) -> Esito:
    """Controlla e prepara il podio (senza salvarlo). Esito.dati: torneo_id,
    primo/secondo/terzo, foto, caption, gia_podio (tupla o None), nome_torneo.
    sostituzione_ammessa=False (nel gruppo): un podio già presente blocca."""
    if num_torneo is not None:
        row = conn.execute("SELECT id, nome FROM tornei WHERE id = ?", (num_torneo,)).fetchone()
        if not row:
            return _errore(f"Nessun torneo con numero #{num_torneo}. Elenco con /storico")
        torneo_id, nome_torneo = row
    else:
        torneo_id, nome_torneo = get_torneo_attivo(conn)
        # sicurezza: torneo attivo senza squadre = quasi certamente appena aperto con
        # la locandina, il podio è del torneo precedente -> mi fermo e chiedo #N
        precedente = conn.execute(
            "SELECT id, nome FROM tornei WHERE id < ? ORDER BY id DESC LIMIT 1", (torneo_id,)
        ).fetchone()
        if conta_squadre(conn, torneo_id) == 0 and precedente:
            return _errore(
                f"⚠️ Il torneo attivo #{torneo_id} «{nome_torneo}» non ha squadre iscritte: "
                f"il podio è forse del torneo precedente #{precedente[0]} «{precedente[1]}»?\n"
                f"Rimanda il comando indicando il numero: /podio #{precedente[0]} Sq1 | Sq2 | Sq3 "
                f"(oppure /podio #{torneo_id} … se è davvero quello attivo)."
            )
    vecchio = conn.execute(
        "SELECT podio_primo, podio_secondo, podio_terzo FROM tornei WHERE id = ?", (torneo_id,)
    ).fetchone()
    gia_podio = tuple(vecchio) if vecchio and vecchio[0] else None
    if gia_podio and not sostituzione_ammessa:
        return _errore(
            f"⚠️ Il torneo #{torneo_id} «{nome_torneo}» ha già un podio "
            f"(🥇 {vecchio[0]} 🥈 {vecchio[1]} 🥉 {vecchio[2]}).\n"
            "Per sostituirlo manda il comando in chat privata col bot: "
            "vedrai l'anteprima e confermi con il bottone."
        )
    primo, secondo, terzo = squadre
    caption = f"🏆 Podio — {nome_torneo}\n\n🥇 {primo}\n🥈 {secondo}\n🥉 {terzo}"
    if descrizione:
        caption += f"\n\n{descrizione}"
    if foto_file_id and len(caption) > MAX_CAPTION:
        return _errore(
            f"Descrizione troppo lunga per la didascalia della foto: "
            f"togli circa {len(caption) - MAX_CAPTION} caratteri e riprova."
        )
    dati = {"torneo_id": torneo_id, "primo": primo, "secondo": secondo, "terzo": terzo,
            "foto": foto_file_id, "caption": caption}
    avviso = ""
    if gia_podio:
        avviso = (f"⚠️ Attenzione: il torneo #{torneo_id} «{nome_torneo}» ha già un podio "
                  f"(🥇 {gia_podio[0]} 🥈 {gia_podio[1]} 🥉 {gia_podio[2]}).\n"
                  "Se pubblichi, viene sostituito (anche nel medagliere).")
    return Esito(True, avviso, {"podio": dati, "nome_torneo": nome_torneo, "gia_podio": gia_podio})


def salva_podio(conn, dati: dict):
    """Scrive il podio sul torneo (sostituisce quello vecchio) e mette i giocatori
    in anagrafica per il medagliere. dati = Esito.dati['podio'] di prepara_podio."""
    conn.execute(
        "UPDATE tornei SET podio_primo = ?, podio_secondo = ?, podio_terzo = ? WHERE id = ?",
        (dati["primo"], dati["secondo"], dati["terzo"], dati["torneo_id"]),
    )
    for sq in (dati["primo"], dati["secondo"], dati["terzo"]):
        statistiche.registra_coppia(conn, sq)
    if dati["foto"]:
        conn.execute("UPDATE tornei SET foto_file_id = ? WHERE id = ?", (dati["foto"], dati["torneo_id"]))
    conn.commit()
