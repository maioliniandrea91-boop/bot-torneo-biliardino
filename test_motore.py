"""Simulazione tornei completi: python3 test_motore.py"""
import random
import heapq
import motore_torneo as M


def gioco_casuale(rng, gol, forza_a, forza_b, vant=0):
    a, b = vant, 0
    while a < gol and b < gol:
        if rng.random() < forza_a / (forza_a + forza_b):
            a += 1
        else:
            b += 1
    return a, b


def simula(n, n_gironi, biliardini=3, seme=1, formato=None, vantaggio=2, ritiro=None):
    rng = random.Random(seme)
    coppie = [f"C{i + 1:02d}" for i in range(n)]
    forza = {c: rng.uniform(800, 1200) for c in coppie}
    t = M.nuovo_torneo(coppie, biliardini=biliardini, seme=seme)
    M.crea_gironi(t, n_gironi, "teste", forza)
    M.imposta(t, vantaggio_finale=vantaggio)
    for k, v in (formato or {}).items():
        M.imposta_formato(t, k, v)

    ora, fine_biliardino = 0, []          # (minuto fine, pid)
    durata = t["config"]["minuti_partita"]
    while t["fase"] != "finito":
        if t["fase"] == "gironi" and not fine_biliardino and \
                all(p["stato"] == "finita" for p in t["partite"].values() if p["tab"] == "G"):
            if ritiro:
                M.ritira(t, ritiro)
            M.crea_tabellone(t)
            continue
        for p in M.chiama_partite(t):
            r = p["regole"]
            giochi, va, vb, serve = [], 0, 0, r["al_meglio_di"] // 2 + 1
            while va < serve and vb < serve:
                g = gioco_casuale(rng, r["gol"], forza[p["a"]], forza[p["b"]], r["vantaggio_a"])
                giochi.append(g)
                va += g[0] > g[1]
                vb += g[1] > g[0]
            heapq.heappush(fine_biliardino, (ora + durata * len(giochi), p["id"], giochi))
        if not fine_biliardino:
            if t["fase"] == "ko":
                raise RuntimeError("Stallo: nessuna partita giocabile")
            continue
        ora, pid, giochi = heapq.heappop(fine_biliardino)
        M.registra(t, pid, giochi)
    return t, ora


def controlli(t):
    P = t["partite"]
    ko = [p for p in P.values() if p["tab"] in "WLF" and M.BYE not in (p["a"], p["b"])]
    sconfitte = {c: 0 for c in t["coppie"]}
    for p in ko:
        sconfitte[p["perdente"]] += 1
    campione, secondo = t["podio"][0], t["podio"][1]
    for c, s in sconfitte.items():
        if c == campione:
            assert s <= 1, (c, s)
        elif c == secondo:
            assert s in (1, 2), (c, s)
        elif c not in t["ritirate"]:
            assert s == 2, (c, s)
    # stessa sfida del girone ripetuta al primo turno vincenti?
    gir = {c: gi for gi, g in enumerate(t["gironi"]) for c in g}
    w1_stesso = sum(1 for p in P.values() if p["tab"] == "W" and p["turno"] == 1
                    and M.BYE not in (p["a"], p["b"]) and gir[p["a"]] == gir[p["b"]])
    # rivincite nel KO (stessa coppia di coppie già incontrata nel KO)
    viste, rivincite = set(), 0
    for p in sorted(ko, key=lambda x: x["ordine"]):
        k = frozenset((p["a"], p["b"]))
        rivincite += k in viste
        viste.add(k)
    return w1_stesso, rivincite


if __name__ == "__main__":
    print(f"{'coppie':>6} {'gironi':>6} {'bil':>3} {'W1 stesso gir':>13} {'rivincite KO':>12} {'durata':>8}  podio")
    for n, g in [(8, 2), (10, 2), (10, 3), (12, 3), (13, 3), (14, 2), (14, 3), (18, 3)]:
        for bil in (3, 2):
            tot_w1 = tot_riv = tot_min = 0
            for s in range(1, 51):
                t, minuti = simula(n, g, bil, seme=s)
                w1, riv = controlli(t)
                tot_w1, tot_riv, tot_min = tot_w1 + w1, tot_riv + riv, tot_min + minuti
            print(f"{n:>6} {g:>6} {bil:>3} {tot_w1 / 50:>13.2f} {tot_riv / 50:>12.2f} "
                  f"{tot_min / 50 / 60:>6.1f} h  {t['podio']}")
    # perdenti a partita singola nei primi turni
    t, minuti = simula(12, 3, 3, seme=7, formato={"L1": 1, "L2": 1, "L3": 3, "L4": 3})
    controlli(t)
    print(f"12 coppie, L1-L2 singola: {minuti / 60:.1f} h")
    t, minuti = simula(12, 3, 3, seme=7, formato={"L1": 1, "L2": 1, "L3": 1, "L4": 1, "L5": 1, "L6": 1})
    print(f"12 coppie, perdenti tutti singola: {minuti / 60:.1f} h")
    # ritiro
    t, minuti = simula(12, 3, 3, seme=3, ritiro="C05")
    controlli(t)
    print("ritiro C05 ok, podio", t["podio"])
    # validazioni
    print("punti 8-7:", M.punti_girone(8, 7, 8), " 8-3:", M.punti_girone(8, 3, 8), " 10-9:", M.punti_girone(10, 9, 10))
    for bad in [(7, 5), (9, 8), (8, 8)]:
        try:
            M.punti_girone(*bad, 8)
            print("ERRORE: accettato", bad)
        except ValueError:
            pass
    print("tutti i controlli superati")
