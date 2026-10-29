"""
Motore tornei biliardino: gironi + doppio KO (regolamento v1, 28/10/2026).

Solo logica, nessuna dipendenza da Telegram o dal database: lo stato del
torneo è un dizionario serializzabile in JSON (da salvare in SQLite).

Regole principali
- Gironi: tutti contro tutti, partita singola a N gol.
  Vittoria 3-0 punti; vittoria con un gol di scarto (golden gol) 2-1.
  Classifica: punti -> differenza gol -> scontro diretto -> sorteggio.
- Passano tutti. Tabellone a 8/16/32 con BYE alle migliori della
  classifica unica (posizione nel girone -> media punti -> media diff. gol).
- Doppio KO: al secondo KO si è fuori. Formato (1 partita o 2 su 3)
  impostabile turno per turno, separato per vincenti e perdenti.
- Finale: vincitore vincenti vs vincitore perdenti, al meglio di N,
  il finalista dei vincenti parte con X gol di vantaggio in ogni partita.
- 3° posto: eliminato nella finale dei perdenti.
- Ogni modifica di impostazioni vale solo per partite non iniziate.
- Presenze: una partita può essere messa in stand-by; un giocatore può
  essere segnato come non arrivato (la sua coppia non viene chiamata).
  In entrambi i casi la chiamata scala alle partite successive.
"""

from __future__ import annotations

import math
import random

BYE = "BYE"

# ---------------------------------------------------------------- utilità


def _potenza2(n: int) -> int:
    return max(2, 2 ** math.ceil(math.log2(n)))


def ordine_teste(dim: int) -> list[int]:
    """Ordine classico delle teste di serie (1-based) in un tabellone."""
    ordine = [1]
    while len(ordine) < dim:
        m = len(ordine) * 2 + 1
        ordine = [x for s in ordine for x in (s, m - s)]
    return ordine


# ---------------------------------------------------------------- creazione


def nuovo_torneo(coppie: list[str], gol: int = 8, biliardini: int = 3,
                 minuti_partita: int = 7, seme: int | None = None) -> dict:
    if len(set(coppie)) != len(coppie):
        raise ValueError("Nomi coppia duplicati")
    if len(coppie) < 4:
        raise ValueError("Servono almeno 4 coppie")
    return {
        "coppie": list(coppie),
        "config": {
            "gol": gol,
            "biliardini": biliardini,
            "minuti_partita": minuti_partita,
            # formato per turno: chiave "W1", "L3", "F" -> 1 o 3
            "formato": {"F": 3},
            "formato_default": {"W": 3, "L": 1},
            "vantaggio_finale": 0,
        },
        "seme": seme if seme is not None else random.randrange(10**9),
        "fase": "iscrizioni",      # iscrizioni -> gironi -> ko -> finito
        "gironi": [],
        "partite": {},             # id -> partita
        "ritirate": [],
        "sospese": [],             # id partite in stand-by
        "assenti": {},             # coppia -> giocatori non ancora arrivati
        "podio": None,
    }


def crea_gironi(t: dict, n_gironi: int, metodo: str = "teste",
                forza: dict[str, float] | None = None,
                manuale: list[list[str]] | None = None) -> None:
    """metodo: 'teste' (serpentina per forza), 'sorteggio', 'manuale'."""
    if t["fase"] != "iscrizioni":
        raise ValueError("Gironi già creati")
    coppie = t["coppie"]
    if not 1 <= n_gironi <= len(coppie) // 2:
        raise ValueError("Numero gironi non valido (almeno 2 coppie per girone)")
    rng = random.Random(t["seme"])

    if metodo == "manuale":
        if not manuale or sorted(c for g in manuale for c in g) != sorted(coppie):
            raise ValueError("Composizione manuale incompleta o con coppie sbagliate")
        if any(len(g) < 2 for g in manuale):
            raise ValueError("Ogni girone deve avere almeno 2 coppie")
        gironi = [list(g) for g in manuale]
    else:
        if metodo == "teste":
            forza = forza or {}
            ordinate = sorted(coppie, key=lambda c: (-forza.get(c, 1000), rng.random()))
        elif metodo == "sorteggio":
            ordinate = coppie[:]
            rng.shuffle(ordinate)
        else:
            raise ValueError("Metodo sconosciuto")
        gironi = [[] for _ in range(n_gironi)]
        for i, c in enumerate(ordinate):       # serpentina: 1..g, g..1
            giro, pos = divmod(i, n_gironi)
            gironi[pos if giro % 2 == 0 else n_gironi - 1 - pos].append(c)

    t["gironi"] = gironi
    # calendario tutti contro tutti (metodo del cerchio), gironi intrecciati
    turni_per_girone = []
    for gi, g in enumerate(gironi):
        lista = g + ([None] if len(g) % 2 else [])
        n = len(lista)
        turni = []
        for r in range(n - 1):
            turno = [(lista[i], lista[n - 1 - i]) for i in range(n // 2)]
            turni.append([(a, b) for a, b in turno if a and b])
            lista = [lista[0]] + [lista[-1]] + lista[1:-1]
        turni_per_girone.append(turni)
    ordine = 0
    for r in range(max(len(x) for x in turni_per_girone)):
        for gi, turni in enumerate(turni_per_girone):
            if r < len(turni):
                for a, b in turni[r]:
                    pid = f"G{gi + 1}-{ordine + 1}"
                    t["partite"][pid] = _partita(pid, "G", r + 1, a, b, ordine, girone=gi)
                    ordine += 1
    t["fase"] = "gironi"


def _partita(pid, tab, turno, a, b, ordine, girone=None, fonte_a=None, fonte_b=None):
    return {
        "id": pid, "tab": tab, "turno": turno, "girone": girone,
        "a": a, "b": b, "fonte_a": fonte_a, "fonte_b": fonte_b,
        "ordine": ordine, "stato": "attesa",   # attesa | in_corso | finita
        "biliardino": None, "regole": None,    # regole fissate all'avvio
        "giochi": [], "vincitore": None, "perdente": None, "tavolino": False,
    }


# ---------------------------------------------------------------- impostazioni


def imposta(t: dict, **kw) -> None:
    """gol, biliardini, minuti_partita, vantaggio_finale. Valgono per le
    partite non ancora avviate (le regole di una partita si fissano all'avvio)."""
    for k, v in kw.items():
        if k not in ("gol", "biliardini", "minuti_partita", "vantaggio_finale"):
            raise ValueError(f"Impostazione sconosciuta: {k}")
        if k == "vantaggio_finale" and not 0 <= v < t["config"]["gol"]:
            raise ValueError("Vantaggio non valido")
        t["config"][k] = v


def imposta_formato(t: dict, turno: str, al_meglio_di: int) -> None:
    """turno: 'W2', 'L1', 'F'... al_meglio_di: 1 o 3. Rifiuta se il turno
    ha già partite avviate."""
    if al_meglio_di not in (1, 3):
        raise ValueError("Formato: 1 (partita singola) o 3 (2 su 3)")
    for p in t["partite"].values():
        if _chiave_turno(p) == turno and p["stato"] != "attesa":
            raise ValueError(f"Il turno {turno} ha già partite iniziate")
    t["config"]["formato"][turno] = al_meglio_di


def _chiave_turno(p):
    return "F" if p["tab"] == "F" else f'{p["tab"]}{p["turno"]}'


def _al_meglio_di(t, p):
    if p["tab"] == "G":
        return 1
    k = _chiave_turno(p)
    return t["config"]["formato"].get(k, t["config"]["formato_default"].get(p["tab"], 3))


# ---------------------------------------------------------------- risultati


def punti_girone(ga: int, gb: int, gol: int) -> tuple[int, int]:
    if max(ga, gb) != gol or min(ga, gb) >= gol:
        raise ValueError(f"Risultato non valido: chi vince deve arrivare a {gol}")
    vince_a = ga > gb
    scarto_uno = abs(ga - gb) == 1
    pv, pp = (2, 1) if scarto_uno else (3, 0)
    return (pv, pp) if vince_a else (pp, pv)


def avvia(t: dict, pid: str, biliardino: int | None = None) -> None:
    p = t["partite"][pid]
    if p["stato"] != "attesa" or not _pronta(p):
        raise ValueError("Partita non avviabile")
    c = t["config"]
    vant = c["vantaggio_finale"] if p["tab"] == "F" else 0
    p["regole"] = {"gol": c["gol"], "al_meglio_di": _al_meglio_di(t, p), "vantaggio_a": vant}
    p["stato"], p["biliardino"] = "in_corso", biliardino
    if pid in t.get("sospese", []):            # avvio esplicito = fine stand-by
        t["sospese"].remove(pid)


def registra(t: dict, pid: str, giochi: list[tuple[int, int]]) -> None:
    """Punteggi finali reali di ogni partita (in finale già col vantaggio).
    Usabile anche dall'admin per correggere: ricalcola ciò che dipende."""
    p = t["partite"][pid]
    if p["tab"] == "G" and t["fase"] != "gironi":
        # il tabellone è già stato costruito su quella classifica
        raise ValueError("Tabellone già creato: i risultati dei gironi non si possono più cambiare")
    if p["stato"] == "attesa":
        avvia(t, pid)
    r = p["regole"]
    gol, bo = r["gol"], r["al_meglio_di"]
    serve = bo // 2 + 1
    va = vb = 0
    for i, (ga, gb) in enumerate(giochi):
        if va == serve or vb == serve:
            raise ValueError("Partite in eccesso: l'incontro era già deciso")
        if max(ga, gb) != gol or min(ga, gb) >= gol:
            raise ValueError(f"Partita {i + 1}: chi vince deve arrivare a {gol}")
        if p["tab"] == "F" and ga < r["vantaggio_a"]:
            raise ValueError("In finale il punteggio include il vantaggio")
        va += ga > gb
        vb += gb > ga
    if serve not in (va, vb):
        raise ValueError(f"Incontro non concluso: servono {serve} vittorie")
    if p["stato"] == "finita" and p["tab"] != "G":
        _verifica_correzione_ko(t, p, p["a"] if va > vb else p["b"])
    p["giochi"] = [list(g) for g in giochi]
    p["vincitore"], p["perdente"] = (p["a"], p["b"]) if va > vb else (p["b"], p["a"])
    p["stato"], p["tavolino"] = "finita", False
    if pid in t.get("sospese", []):
        t["sospese"].remove(pid)
    _avanza(t)


def _verifica_correzione_ko(t, p, nuovo_vincitore):
    if nuovo_vincitore == p["vincitore"]:
        return
    for q in t["partite"].values():
        if q["stato"] != "attesa" and (q["fonte_a"], q["fonte_b"]) != (None, None) and \
                p["id"] in (q["fonte_a"][1], q["fonte_b"][1]):
            raise ValueError("Impossibile invertire il vincitore: le partite successive sono già iniziate")
    for q in t["partite"].values():               # libera le partite a valle
        for lato in ("a", "b"):
            f = q[f"fonte_{lato}"]
            if f and f[1] == p["id"]:
                q[lato] = None


def ritira(t: dict, coppia: str) -> None:
    """Partite restanti perse a tavolino N-0."""
    if coppia in t["ritirate"]:
        return
    t["ritirate"].append(coppia)
    _avanza(t)


def _tavolino(t, p):
    gol = (p["regole"] or {}).get("gol", t["config"]["gol"])
    rit_a, rit_b = p["a"] in t["ritirate"], p["b"] in t["ritirate"]
    vince_a = rit_b and not rit_a
    if rit_a and rit_b:        # entrambe ritirate: passa 'a' (irrilevante)
        vince_a = True
    p["giochi"] = [[gol, 0]] if vince_a else [[0, gol]]
    p["vincitore"], p["perdente"] = (p["a"], p["b"]) if vince_a else (p["b"], p["a"])
    p["stato"], p["tavolino"] = "finita", True


# ---------------------------------------------------------------- classifiche


def classifica_girone(t: dict, gi: int) -> list[dict]:
    coppie = t["gironi"][gi]
    s = {c: {"coppia": c, "g": 0, "pt": 0, "gf": 0, "gs": 0} for c in coppie}
    finite = [p for p in t["partite"].values()
              if p["tab"] == "G" and p["girone"] == gi and p["stato"] == "finita"]
    for p in finite:
        ga, gb = p["giochi"][0]
        gol = max(ga, gb)
        pa, pb = punti_girone(ga, gb, gol) if not p["tavolino"] else \
            ((3, 0) if ga > gb else (0, 3))
        for c, pt, f, sub in ((p["a"], pa, ga, gb), (p["b"], pb, gb, ga)):
            s[c]["g"] += 1
            s[c]["pt"] += pt
            s[c]["gf"] += f
            s[c]["gs"] += sub
    rng = random.Random(t["seme"] + gi)
    sorteggio = {c: rng.random() for c in coppie}

    def scontri(gruppo):
        pts = {c: 0 for c in gruppo}
        for p in finite:
            if p["a"] in pts and p["b"] in pts:
                pts[p["vincitore"]] += 1
        return pts

    righe = sorted(s.values(), key=lambda r: (-r["pt"], -(r["gf"] - r["gs"])))
    # spareggio scontro diretto tra coppie a pari punti e differenza
    risultato, i = [], 0
    while i < len(righe):
        j = i
        chiave = lambda r: (r["pt"], r["gf"] - r["gs"])
        while j < len(righe) and chiave(righe[j]) == chiave(righe[i]):
            j += 1
        gruppo = righe[i:j]
        if len(gruppo) > 1:
            sd = scontri([r["coppia"] for r in gruppo])
            gruppo.sort(key=lambda r: (-sd[r["coppia"]], sorteggio[r["coppia"]]))
        risultato += gruppo
        i = j
    for pos, r in enumerate(risultato, 1):
        r["pos"], r["dg"] = pos, r["gf"] - r["gs"]
    return risultato


def classifica_unica(t: dict) -> list[dict]:
    righe = []
    for gi in range(len(t["gironi"])):
        for r in classifica_girone(t, gi):
            g = max(r["g"], 1)
            righe.append({**r, "girone": gi, "mpt": r["pt"] / g, "mdg": r["dg"] / g})
    rng = random.Random(t["seme"] + 99)
    sort = {r["coppia"]: rng.random() for r in righe}
    righe.sort(key=lambda r: (r["pos"], -r["mpt"], -r["mdg"], sort[r["coppia"]]))
    return righe


# ---------------------------------------------------------------- tabellone KO


def crea_tabellone(t: dict) -> None:
    if t["fase"] != "gironi":
        raise ValueError("Tabellone già creato o gironi non creati")
    if any(p["stato"] != "finita" for p in t["partite"].values() if p["tab"] == "G"):
        raise ValueError("Ci sono partite dei gironi non concluse")
    cu = classifica_unica(t)
    teste = [r["coppia"] for r in cu]
    girone_di = {r["coppia"]: r["girone"] for r in cu}
    dim = _potenza2(len(teste))
    slot = [teste[s - 1] if s <= len(teste) else BYE for s in ordine_teste(dim)]
    coppie_w1 = [[slot[i], slot[i + 1]] for i in range(0, dim, 2)]
    _evita_stesso_girone(coppie_w1, girone_di)

    P = t["partite"]
    ordine = 10_000
    k = int(math.log2(dim))
    # vincenti
    w = {}
    for r in range(1, k + 1):
        for m in range(dim >> r):
            pid = f"W{r}-{m + 1}"
            if r == 1:
                a, b = coppie_w1[m]
                P[pid] = _partita(pid, "W", 1, a, b, ordine)
            else:
                P[pid] = _partita(pid, "W", r, None, None, ordine,
                                  fonte_a=("v", w[(r - 1, 2 * m)]), fonte_b=("v", w[(r - 1, 2 * m + 1)]))
            w[(r, m)] = pid
            ordine += 1
    # perdenti: 2(k-1) turni
    l_prec = []
    for j in range(1, 2 * (k - 1) + 1):
        ids = []
        if j == 1:
            perdenti = [w[(1, m)] for m in range(dim >> 1)]
            for m in range(len(perdenti) // 2):
                pid = f"L1-{m + 1}"
                P[pid] = _partita(pid, "L", 1, None, None, ordine,
                                  fonte_a=("p", perdenti[2 * m]), fonte_b=("p", perdenti[2 * m + 1]))
                ids.append(pid)
                ordine += 1
        elif j % 2 == 0:       # entrano i perdenti del turno vincenti j/2+1
            rw = j // 2 + 1
            perdenti = [w[(rw, m)] for m in range(dim >> rw)]
            perdenti = _ordina_ingresso(perdenti, j // 2, dim)
            for m, (prec, perd) in enumerate(zip(l_prec, perdenti)):
                pid = f"L{j}-{m + 1}"
                P[pid] = _partita(pid, "L", j, None, None, ordine,
                                  fonte_a=("v", prec), fonte_b=("p", perd))
                ids.append(pid)
                ordine += 1
        else:
            for m in range(len(l_prec) // 2):
                pid = f"L{j}-{m + 1}"
                P[pid] = _partita(pid, "L", j, None, None, ordine,
                                  fonte_a=("v", l_prec[2 * m]), fonte_b=("v", l_prec[2 * m + 1]))
                ids.append(pid)
                ordine += 1
        l_prec = ids
    P["F"] = _partita("F", "F", 1, None, None, ordine,
                      fonte_a=("v", w[(k, 0)]), fonte_b=("v", l_prec[0]))
    t["fase"] = "ko"
    _avanza(t)


def annulla_tabellone(t: dict) -> None:
    """Torna ai gironi conclusi: via tabellone, risultati KO e podio. I gironi
    restano con i loro risultati (di nuovo correggibili); impostazioni,
    formati dei turni e ritiri restano."""
    if t["fase"] not in ("ko", "finito"):
        raise ValueError("Non c'è un tabellone da annullare")
    t["partite"] = {k: p for k, p in t["partite"].items() if p["tab"] == "G"}
    t["sospese"] = [pid for pid in t.get("sospese", []) if pid in t["partite"]]
    t["podio"] = None
    t["fase"] = "gironi"


# schema di ingresso dei perdenti nei turni L pari, per dimensione tabellone:
# 0 = invariato, 1 = invertito, 2 = metà scambiate, 3 = invertito + metà scambiate
SCHEMI_INGRESSO = {}


def _ordina_ingresso(perdenti, r, dim):
    schema = SCHEMI_INGRESSO.get(dim, {}).get(r, 1 if r % 2 == 1 else 0)
    if schema in (1, 3):
        perdenti = perdenti[::-1]
    if schema in (2, 3) and len(perdenti) > 1:
        meta = len(perdenti) // 2
        perdenti = perdenti[meta:] + perdenti[:meta]
    return perdenti


def _evita_stesso_girone(coppie, girone_di):
    def conflitto(c):
        a, b = c
        return BYE not in c and girone_di[a] == girone_di[b]
    for i, c in enumerate(coppie):
        if not conflitto(c):
            continue
        # scambia l'avversario (lato b) con il più vicino che risolve senza crearne
        for d in sorted(range(len(coppie)), key=lambda j: abs(j - i)):
            if d == i:
                continue
            x = coppie[d]
            if BYE in (c[1], x[1]):        # i BYE restano alle teste di serie
                continue
            c[1], x[1] = x[1], c[1]
            if not conflitto(c) and not conflitto(x):
                break
            c[1], x[1] = x[1], c[1]


def _gia_incontrate(t):
    return {frozenset((p["a"], p["b"])) for p in t["partite"].values()
            if p["tab"] in "WL" and p["stato"] == "finita" and BYE not in (p["a"], p["b"])}


def _riduci_rivincita(t, p):
    """Turni perdenti con ingresso dai vincenti: se la partita è una rivincita
    del tabellone, scambia la coppia scesa dai vincenti con quella di un'altra
    partita dello stesso turno non ancora iniziata, se così nessuna delle due
    è più una rivincita. Restituisce True se ha scambiato."""
    if p["tab"] != "L" or not p["fonte_b"] or p["fonte_b"][0] != "p":
        return False
    viste = _gia_incontrate(t)
    if frozenset((p["a"], p["b"])) not in viste or BYE in (p["a"], p["b"]):
        return False
    for q in sorted(t["partite"].values(), key=lambda x: x["ordine"]):
        if q is p or q["tab"] != "L" or q["turno"] != p["turno"] or q["stato"] != "attesa" \
                or q["b"] is None or q["b"] == BYE:
            continue
        nuova_p = frozenset((p["a"], q["b"]))
        nuova_q = frozenset((q["a"], p["b"])) if q["a"] is not None else None
        if nuova_p in viste or (nuova_q is not None and nuova_q in viste):
            continue
        p["b"], q["b"] = q["b"], p["b"]
        p["fonte_b"], q["fonte_b"] = q["fonte_b"], p["fonte_b"]
        return True
    return False


def _pronta(p):
    return p["a"] is not None and p["b"] is not None


def _avanza(t):
    """Risolve fonti, BYE e ritiri finché lo stato è stabile."""
    P = t["partite"]
    cambiato = True
    while cambiato:
        cambiato = False
        for p in sorted(P.values(), key=lambda x: x["ordine"]):
            for lato in ("a", "b"):
                f = p[f"fonte_{lato}"]
                if f and p[lato] is None:
                    src = P[f[1]]
                    if src["stato"] == "finita":
                        p[lato] = src["vincitore"] if f[0] == "v" else src["perdente"]
                        cambiato = True
            if p["stato"] == "attesa" and _pronta(p) and _riduci_rivincita(t, p):
                cambiato = True
            if p["stato"] == "attesa" and _pronta(p):
                if BYE in (p["a"], p["b"]):
                    p["vincitore"] = p["b"] if p["a"] == BYE else p["a"]
                    p["perdente"] = BYE
                    p["stato"] = "finita"
                    cambiato = True
                elif p["a"] in t["ritirate"] or p["b"] in t["ritirate"]:
                    _tavolino(t, p)
                    cambiato = True
    if t["fase"] == "ko" and P["F"]["stato"] == "finita":
        perdenti = [p for p in P.values() if p["tab"] == "L"]
        ultima_l = max(perdenti, key=lambda p: p["turno"])
        t["podio"] = [P["F"]["vincitore"], P["F"]["perdente"], ultima_l["perdente"]]
        t["fase"] = "finito"


# ---------------------------------------------------------------- presenze


def _rimetti_in_attesa(p) -> int | None:
    """Partita chiamata ma non giocata: libera il biliardino. Ritorna quale."""
    if p["stato"] != "in_corso":
        return None
    b = p["biliardino"]
    p["stato"], p["biliardino"], p["regole"] = "attesa", None, None
    return b


def sospendi(t: dict, pid: str) -> int | None:
    """Stand-by: la partita non viene chiamata finché non si riattiva.
    Se era in campo libera il biliardino (ritorna il numero)."""
    p = t["partite"][pid]
    if p["stato"] == "finita":
        raise ValueError("Partita già conclusa")
    sosp = t.setdefault("sospese", [])
    if pid not in sosp:
        sosp.append(pid)
    return _rimetti_in_attesa(p)


def riattiva(t: dict, pid: str) -> None:
    if pid in t.get("sospese", []):
        t["sospese"].remove(pid)


def segna_assente(t: dict, coppia: str, giocatore: str, assente: bool = True) -> int | None:
    """Giocatore non ancora arrivato: la coppia non viene chiamata. Se era in
    campo, la partita torna in attesa e il biliardino si libera."""
    if coppia not in t["coppie"]:
        raise ValueError("Coppia sconosciuta")
    ass = t.setdefault("assenti", {})
    lista = ass.setdefault(coppia, [])
    if assente and giocatore not in lista:
        lista.append(giocatore)
    if not assente and giocatore in lista:
        lista.remove(giocatore)
    if not lista:
        ass.pop(coppia)
        return None
    for p in t["partite"].values():
        if p["stato"] == "in_corso" and coppia in (p["a"], p["b"]):
            return _rimetti_in_attesa(p)
    return None


def coppie_bloccate(t: dict) -> set:
    return {c for c, g in t.get("assenti", {}).items() if g}


# ---------------------------------------------------------------- chiamata partite


def partite_pronte(t: dict) -> list[dict]:
    """Partite giocabili subito, in ordine di priorità, senza coppie impegnate,
    escluse quelle in stand-by e quelle con un giocatore non arrivato."""
    impegnate = {c for p in t["partite"].values() if p["stato"] == "in_corso" for c in (p["a"], p["b"])}
    pronte, usate = [], set(impegnate) | coppie_bloccate(t)
    sospese = set(t.get("sospese", []))
    for p in sorted(t["partite"].values(), key=lambda x: x["ordine"]):
        if p["id"] in sospese:
            continue
        if p["stato"] == "attesa" and _pronta(p) and not {p["a"], p["b"]} & usate:
            pronte.append(p)
            usate |= {p["a"], p["b"]}
    return pronte


def chiama_partite(t: dict) -> list[dict]:
    """Assegna i biliardini liberi alle partite pronte e le avvia."""
    occupati = {p["biliardino"] for p in t["partite"].values() if p["stato"] == "in_corso"}
    liberi = [b for b in range(1, t["config"]["biliardini"] + 1) if b not in occupati]
    chiamate = []
    for p, b in zip(partite_pronte(t), liberi):
        avvia(t, p["id"], b)
        chiamate.append(p)
    return chiamate
