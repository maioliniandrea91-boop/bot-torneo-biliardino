"""PDF stampabile del torneo (A4 orizzontale): gironi e tabellone doppio KO.

Piano B se cade la rete e foglio da appendere: i risultati già giocati sono
scritti, quelli da giocare hanno le caselle vuote da compilare a penna.
Pagine: Gironi (classifiche + partite) · Vincenti + Finale · Perdenti.
Le lettere (A, B, C…) dicono dove scende chi perde nel tabellone vincenti,
come nell'app.
"""
from io import BytesIO

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen.canvas import Canvas

import motore_torneo as M

TEAL, MAGENTA, GRIGIO, NERO = HexColor("#22bdb7"), HexColor("#d4008f"), HexColor("#8a8f98"), HexColor("#111111")
PAG_W, PAG_H = landscape(A4)
MARG = 28
LETTERE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NOMI_GIRONI = "ABCDEFGHIJ"
CW, BH, GAP, STEP = 170, 40, 34, 52        # unità del tabellone (poi scalate sulla pagina)


def _idx(pid: str) -> int:
    return int(pid.split("-")[1]) - 1


def _punti(p) -> str:
    """Vittorie dell'incontro, o il punteggio se partita secca."""
    if not p.get("giochi"):
        return ""
    if len(p["giochi"]) == 1:
        return "{}-{}".format(*p["giochi"][0])
    va = sum(a > b for a, b in p["giochi"])
    return f"{va}-{len(p['giochi']) - va}"


def _testa(c: Canvas, titolo: str, sotto: str) -> None:
    c.setFillColor(TEAL)
    c.rect(0, PAG_H - 46, PAG_W, 46, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(MARG, PAG_H - 30, titolo)
    c.setFont("Helvetica", 10)
    c.drawRightString(PAG_W - MARG, PAG_H - 29, sotto)
    c.setFillColor(NERO)


def _taglia(c, testo, font, size, larg) -> str:
    while testo and c.stringWidth(testo, font, size) > larg:
        testo = testo[:-2] + "…" if len(testo) > 2 else ""
    return testo


# ---------------------------------------------------------------- gironi

def _pagina_gironi(c: Canvas, t: dict, nome: str) -> None:
    _testa(c, nome, "Gironi")
    n = len(t["gironi"])
    colonne = min(n, 3)
    lw = (PAG_W - 2 * MARG - (colonne - 1) * 16) / colonne
    for gi in range(n):
        x = MARG + (gi % colonne) * (lw + 16)
        y = PAG_H - 70 - (gi // colonne) * 270
        c.setFillColor(MAGENTA)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x, y, f"Girone {NOMI_GIRONI[gi]}")
        c.setFillColor(NERO)
        y -= 16
        c.setFont("Helvetica-Bold", 8)
        for testo, dx in (("Coppia", 14), ("G", lw - 66), ("Pt", lw - 44), ("DG", lw - 20)):
            c.drawString(x + dx, y, testo)
        c.setFont("Helvetica", 9)
        for r in M.classifica_girone(t, gi):
            y -= 12
            c.drawString(x, y, str(r["pos"]))
            c.drawString(x + 14, y, _taglia(c, r["coppia"], "Helvetica", 9, lw - 84))
            c.drawString(x + lw - 66, y, str(r["g"]))
            c.drawString(x + lw - 44, y, str(r["pt"]))
            c.drawString(x + lw - 20, y, f"{r['dg']:+d}")
        y -= 18
        partite = sorted((p for p in t["partite"].values() if p["tab"] == "G" and p["girone"] == gi),
                         key=lambda p: p["ordine"])
        c.setFont("Helvetica", 8)
        for p in partite:
            c.drawString(x, y, _taglia(c, f"{p['a']} – {p['b']}", "Helvetica", 8, lw - 44))
            if p["stato"] == "finita":
                c.setFont("Helvetica-Bold", 8)
                c.drawRightString(x + lw, y, _punti(p) + (" (tav.)" if p.get("tavolino") else ""))
                c.setFont("Helvetica", 8)
            else:
                c.setStrokeColor(GRIGIO)
                c.rect(x + lw - 36, y - 2, 36, 10, stroke=1, fill=0)
            y -= 11


# ---------------------------------------------------------------- tabellone

def _posizioni(P: dict) -> dict:
    """Stessa disposizione del tabellone a farfalla dell'app."""
    W = [p for p in P.values() if p["tab"] == "W"]
    L = [p for p in P.values() if p["tab"] == "L"]

    def ordine(p):
        return (p["turno"], _idx(p["id"]))

    def fonti_linea(q):
        out = []
        for f in (q.get("fonte_a"), q.get("fonte_b")):
            if not f or f[1] not in P:
                continue
            s = P[f[1]]
            if f[0] == "v" and (s["tab"] == q["tab"] or (q["tab"] == "F" and s["tab"] == "W")):
                out.append(f)
            elif f[0] == "p" and s["tab"] == "W" and s["turno"] == 1:
                out.append(f)
        return out

    y = {p["id"]: _idx(p["id"]) * STEP + STEP / 2 for p in W if p["turno"] == 1}
    for q in sorted([p for p in W if p["turno"] > 1], key=ordine) + sorted(L, key=ordine):
        src = [y[f[1]] for f in fonti_linea(q) if f[1] in y]
        y[q["id"]] = sum(src) / len(src) if src else STEP / 2
    if "F" in P:
        y["F"] = y[P["F"]["fonte_a"][1]]
    return y, fonti_linea


def _pagina_lato(c: Canvas, t: dict, nome: str, lato: str, P: dict, y: dict, fonti_linea, lettera, etichetta):
    colonne = sorted({p["turno"] for p in P.values() if p["tab"] == lato})
    k = max(colonne)
    pos_x = {(lato, r): i * (CW + GAP) for i, r in enumerate(colonne)}
    if lato == "W" and "F" in P:
        pos_x["F"] = len(colonne) * (CW + GAP)
    visibili = [p for p in P.values() if p["tab"] == lato or (lato == "W" and p["tab"] == "F")]
    ys = [y[p["id"]] for p in visibili]
    y0, y1 = min(ys) - BH / 2, max(ys) + BH / 2
    larg = max(pos_x.values()) + CW
    disp_w, disp_h = PAG_W - 2 * MARG, PAG_H - 46 - 50 - MARG
    sc = min(1.0, disp_w / larg, disp_h / (y1 - y0))
    ox = MARG + (disp_w - larg * sc) / 2
    oy = PAG_H - 46 - 44                                    # sotto le intestazioni di colonna

    def X(p):
        return pos_x["F"] if p["tab"] == "F" else pos_x[(lato, p["turno"])]

    def T(px, py):                                         # unità -> punti pagina
        return ox + px * sc, oy - (py - y0) * sc

    titolo = "Tabellone vincenti" if lato == "W" else "Tabellone perdenti"
    _testa(c, nome, titolo)
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(MAGENTA)
    for r in colonne:
        nomec = ("Finale vincenti" if r == k else f"Vincenti {r}") if lato == "W" else \
                ("Finale perdenti" if r == k else f"Perdenti {r}")
        c.drawString(T(pos_x[(lato, r)], 0)[0], PAG_H - 46 - 22, nomec)
    if "F" in pos_x:
        c.drawString(T(pos_x["F"], 0)[0], PAG_H - 46 - 22, "FINALE")
    c.setFillColor(NERO)

    # linee
    c.setStrokeColor(GRIGIO)
    c.setLineWidth(.8)
    for q in visibili:
        for f in fonti_linea(q):
            s = P[f[1]]
            if s not in visibili:
                continue
            x1, x2 = X(s) + CW, X(q)
            xm = (x1 + x2) / 2
            pts = [(x1, y[s["id"]]), (xm, y[s["id"]]), (xm, y[q["id"]]), (x2, y[q["id"]])]
            for (a, b), (cc, d) in zip(pts, pts[1:]):
                c.line(*T(a, b), *T(cc, d))

    # riquadri
    fs = max(5.5, 9 * sc)
    for p in visibili:
        bx, by = T(X(p), y[p["id"]] - BH / 2)
        w, h = CW * sc, BH * sc
        c.setStrokeColor(MAGENTA if p["tab"] == "F" else NERO)
        c.setLineWidth(1.2 if p["tab"] == "F" else .8)
        c.rect(bx, by - h, w, h, stroke=1, fill=0)
        c.line(bx, by - h / 2, bx + w, by - h / 2)
        # etichetta sopra il riquadro
        c.setFont("Helvetica", fs * .75)
        c.setFillColor(GRIGIO)
        regole = p.get("regole") or {}
        bo = regole.get("al_meglio_di") or M._al_meglio_di(t, p)
        info = etichetta(p, t) + (f" · {bo // 2 + 1} su {bo}" if bo > 1 else "")
        if p["id"] in lettera:
            info = f"[{lettera[p['id']]}] " + info
        c.drawString(bx + 1, by + 2, _taglia(c, info, "Helvetica", fs * .75, w))
        c.setFillColor(NERO)
        for i, side in enumerate(("a", "b")):
            ty = by - h / 2 * i - h / 2 + (h / 2 - fs) / 2 + 1
            nomec = p[side]
            f = p.get("fonte_" + side)
            pref = f"{lettera[f[1]]} · " if f and f[0] == "p" and f[1] in lettera else ""
            if nomec is None:
                if f and f[0] == "p":
                    s = P.get(f[1])
                    nomec = "Perdente" + (f" {s['a']} / {s['b']}" if not pref and s and s["a"] else "")
                elif f and P.get(f[1]) and P[f[1]]["tab"] == "L" and p["tab"] == "F":
                    nomec = "Vincente perdenti"
                else:
                    nomec = ""
                c.setFillColor(GRIGIO)
            vinc = p.get("vincitore") == p[side] and p[side] is not None
            font = "Helvetica-Bold" if vinc else "Helvetica"
            c.setFont(font, fs)
            c.drawString(bx + 3, ty, _taglia(c, pref + nomec, font, fs, w - 30 * sc - 6))
            c.setFillColor(NERO)
        # punteggio: scritto se giocata, altrimenti caselle vuote da riempire
        if p["stato"] == "finita" and M.BYE not in (p["a"], p["b"]):
            va = sum(a > b for a, b in p["giochi"])
            vb = len(p["giochi"]) - va
            c.setFont("Helvetica-Bold", fs)
            for i, v in enumerate((va, vb) if len(p["giochi"]) > 1 else p["giochi"][0]):
                c.drawRightString(bx + w - 3, by - h / 2 * i - h / 2 + (h / 2 - fs) / 2 + 1, str(v))
        elif M.BYE not in (p["a"], p["b"]):
            c.setStrokeColor(GRIGIO)
            c.line(bx + w - 30 * sc, by, bx + w - 30 * sc, by - h)
    if lato == "W":
        c.setFont("Helvetica", 8)
        c.setFillColor(GRIGIO)
        c.drawString(MARG, 18, "Le lettere [A], [B]… indicano dove scende chi perde: "
                               "stessa lettera nel tabellone perdenti.")
        if t["config"].get("vantaggio_finale"):
            c.drawRightString(PAG_W - MARG, 18, f"Finale: la vincente dei perdenti parte con "
                                                f"{t['config']['vantaggio_finale']} gol di vantaggio")


def _podio(c: Canvas, t: dict) -> None:
    if not t.get("podio"):
        return
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(MAGENTA)
    testo = "   ".join(f"{m} {n}" for m, n in zip(("1°", "2°", "3°"), t["podio"]) if n)
    c.drawString(MARG, 30, "Podio:  " + testo)
    c.setFillColor(NERO)


def crea_pdf(t: dict, nome: str, etichetta) -> bytes:
    buf = BytesIO()
    c = Canvas(buf, pagesize=(PAG_W, PAG_H))
    c.setTitle(nome)
    if t.get("gironi"):
        _pagina_gironi(c, t, nome)
        c.showPage()
    P = {p["id"]: p for p in t["partite"].values() if p["tab"] != "G"}
    if P:
        W = [p for p in P.values() if p["tab"] == "W"]
        lettera = {p["id"]: LETTERE[i] if i < len(LETTERE) else "?" for i, p in enumerate(
            sorted((p for p in W if p["turno"] >= 2), key=lambda p: (p["turno"], _idx(p["id"]))))}
        y, fonti_linea = _posizioni(P)
        _pagina_lato(c, t, nome, "W", P, y, fonti_linea, lettera, etichetta)
        _podio(c, t)
        c.showPage()
        if any(p["tab"] == "L" for p in P.values()):
            _pagina_lato(c, t, nome, "L", P, y, fonti_linea, lettera, etichetta)
            c.showPage()
    c.save()
    return buf.getvalue()
