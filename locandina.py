"""
Generatore della locandina del torneo (formato Telegram/Instagram 4:5, 1080x1350).

Usa solo Pillow: niente browser, quindi gira anche su Railway.
Grafica, mascotte, biliardino e font stanno nella cartella assets/.

Uso da codice:
    from locandina import genera_locandina
    png_bytes = genera_locandina(data, formula, ora="21:30", quota="20€", premi="BV")
"""

import io
import os
from datetime import date

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CARTELLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FONT_TITOLI = os.path.join(CARTELLA, "BebasNeue-Regular.ttf")
FONT_CORSIVO = os.path.join(CARTELLA, "Yellowtail-Regular.ttf")
IMG_MASCOTTE = os.path.join(CARTELLA, "mascotte.png")
IMG_BILIARDINO = os.path.join(CARTELLA, "biliardino.png")

W, H = 1080, 1350
SFONDO = (34, 189, 183)
MAGENTA = (242, 28, 196)
ROSA_NEON = (255, 43, 214)
ROSA_SCURO = (212, 0, 143)
LIME = (184, 245, 63)
BIANCO = (255, 255, 255)
NERO = (17, 17, 17)

LUOGO = "CIRCOLO LOVECRAFT, ANCONA"
CONTATTO_NOME = "MAIO"
CONTATTO_TEL = "3759128546"

GIORNI = ["LUNEDÌ", "MARTEDÌ", "MERCOLEDÌ", "GIOVEDÌ", "VENERDÌ", "SABATO", "DOMENICA"]
MESI = ["GENNAIO", "FEBBRAIO", "MARZO", "APRILE", "MAGGIO", "GIUGNO", "LUGLIO",
        "AGOSTO", "SETTEMBRE", "OTTOBRE", "NOVEMBRE", "DICEMBRE"]

# Formule note: chiave scritta nel comando -> righe mostrate nel box grande
FORMULE = {
    "rollerball": ["3 TOCCHI", "ROLLERBALL"],
    "3tocchi": ["3 TOCCHI", "ROLLERBALL"],
    "volo": ["VOLO"],
    "tradizionale": ["TRADIZIONALE"],
}


def _font(path, size):
    return ImageFont.truetype(path, size)


def _larghezza(testo, font, spaziatura=0):
    if not spaziatura:
        return font.getlength(testo)
    return sum(font.getlength(c) for c in testo) + spaziatura * len(testo)


def _scrivi(draw, xy, testo, font, fill, spaziatura=0):
    """Scrive dal punto (x, baseline). Con spaziatura disegna lettera per lettera."""
    x, y = xy
    if not spaziatura:
        draw.text((x, y), testo, font=font, fill=fill, anchor="ls")
        return
    for c in testo:
        draw.text((x, y), c, font=font, fill=fill, anchor="ls")
        x += font.getlength(c) + spaziatura


def _baseline(font, top, interlinea):
    """Baseline di una riga come la calcola il browser (line-height CSS)."""
    asc, desc = font.getmetrics()
    altezza_riga = font.size * interlinea
    return top + (altezza_riga - (asc + desc)) / 2 + asc


def _testo_neon(base, testi, aloni, colore=BIANCO, clip=None):
    """testi: lista di (x, baseline, testo, font, spaziatura).
    aloni: lista di (raggio_px, colore) come i text-shadow CSS."""
    maschera = Image.new("L", base.size, 0)
    d = ImageDraw.Draw(maschera)
    for x, y, t, f, sp in testi:
        _scrivi(d, (x, y), t, f, 255, sp)
    strato = Image.new("RGBA", base.size, (0, 0, 0, 0))
    for raggio, col in reversed(aloni):
        alfa = maschera.filter(ImageFilter.GaussianBlur(raggio / 2))
        # i text-shadow del browser sono più densi di un blur puro: rinforzo
        alfa = alfa.point(lambda v: min(255, int(v * 1.6)))
        ombra = Image.new("RGBA", base.size, col + (0,))
        ombra.putalpha(alfa)
        strato.alpha_composite(ombra)
    piena = Image.new("RGBA", base.size, colore + (0,))
    piena.putalpha(maschera)
    strato.alpha_composite(piena)
    if clip is not None:
        a = strato.getchannel("A")
        strato.putalpha(Image.composite(a, Image.new("L", base.size, 0), clip))
    base.alpha_composite(strato)


def _poligono(x0, y0, x1, y1, percentuali):
    w, h = x1 - x0, y1 - y0
    return [(x0 + w * px / 100, y0 + h * py / 100) for px, py in percentuali]


BORDO_A = [(2, 8), (9, 0), (30, 6), (55, 1), (78, 7), (97, 2), (100, 30), (98, 62), (100, 92),
           (82, 100), (60, 94), (35, 100), (12, 95), (0, 100), (2, 66), (0, 35)]
BORDO_B = [(0, 6), (14, 1), (40, 7), (63, 0), (86, 6), (100, 0), (98, 34), (100, 70), (97, 100),
           (74, 94), (50, 100), (26, 95), (4, 100), (1, 70), (3, 38)]
BORDO_C = [(1, 4), (20, 0), (44, 5), (70, 0), (92, 4), (100, 0), (98, 28), (100, 58), (98, 90),
           (100, 100), (76, 96), (52, 100), (28, 95), (6, 100), (0, 76), (2, 44), (0, 18)]


def _box(base, rect, bordo, righe, font, interlinea, pad_top=None, pad_bottom=None):
    """Box verde effetto pennello con testo bianco neon centrato."""
    x0, y0, x1, y1 = rect
    clip = Image.new("L", base.size, 0)
    ImageDraw.Draw(clip).polygon(_poligono(x0, y0, x1, y1, bordo), fill=255)
    verde = Image.new("RGBA", base.size, LIME + (255,))
    verde.putalpha(clip)
    base.alpha_composite(verde)

    altezza_riga = font.size * interlinea
    if pad_top is not None:
        top = y0 + pad_top
    else:
        top = y0 + ((y1 - y0) - altezza_riga * len(righe)) / 2
    testi = []
    for i, r in enumerate(righe):
        bl = _baseline(font, top + i * altezza_riga, interlinea)
        x = (x0 + x1) / 2 - _larghezza(r, font) / 2
        testi.append((x, bl, r, font, 0))
    raggi = (4, 12, 24) if font.size < 90 else (4, 14, 30)
    _testo_neon(base, testi, [(raggi[0], ROSA_SCURO), (raggi[1], ROSA_NEON), (raggi[2], ROSA_NEON)], clip=clip)


def testo_data(d: date, ora: str) -> str:
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]} ORE {ora}"


def genera_locandina(d: date, formula, ora="21:30", quota="20€", premi="BV") -> bytes:
    """formula: chiave di FORMULE oppure lista di righe già pronte."""
    # formula: una chiave ("volo") o una lista di chiavi (["volo", "tradizionale"])
    chiavi = [formula] if isinstance(formula, str) else list(formula)
    if len(chiavi) == 1:
        righe_formula = FORMULE.get(chiavi[0], [chiavi[0].upper()])
    else:
        # più formule: le righe di ciascuna, una sotto l'altra
        righe_formula = [r for k in chiavi for r in FORMULE.get(k, [k.upper()])]

    img = Image.new("RGBA", (W, H), SFONDO + (255,))
    draw = ImageDraw.Draw(img)

    # TORNEO (magenta pieno)
    f_torneo = _font(FONT_TITOLI, 290)
    _scrivi(draw, (60, _baseline(f_torneo, 50, 0.9)), "TORNEO", f_torneo, MAGENTA, spaziatura=3)

    # Biliardino (corsivo neon, ruotato)
    f_corsivo = _font(FONT_CORSIVO, 190)
    margine = 80
    lw = int(_larghezza("Biliardino", f_corsivo)) + 2 * margine
    lh = 190 + 2 * margine
    strato = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    _testo_neon(strato, [(margine, _baseline(f_corsivo, margine, 1.0), "Biliardino", f_corsivo, 0)],
                [(5, BIANCO), (16, (255, 58, 216)), (36, (255, 58, 216)), (60, (255, 58, 216))])
    strato = strato.rotate(7, resample=Image.BICUBIC, expand=True)
    cx, cy = 92 + (lw - 2 * margine) / 2, 140 + 95
    img.alpha_composite(strato, (int(cx - strato.width / 2), int(cy - strato.height / 2)))

    # Mascotte
    m = Image.open(IMG_MASCOTTE).convert("RGBA")
    scala = min(240 / m.width, 352 / m.height)
    m = m.resize((round(m.width * scala), round(m.height * scala)), Image.LANCZOS)
    img.alpha_composite(m, (W - 44 - 240 + (240 - m.width) // 2, 60 + (352 - m.height) // 2))

    # Data e luogo
    aloni = [(4, ROSA_SCURO), (16, ROSA_NEON), (32, ROSA_NEON)]
    f_data = _font(FONT_TITOLI, 86)
    f_luogo = _font(FONT_TITOLI, 72)
    riga_data = testo_data(d, ora)
    # se una data lunga non ci sta, riduco il corpo
    while _larghezza(riga_data, f_data, 1) > W - 120 and f_data.size > 60:
        f_data = _font(FONT_TITOLI, f_data.size - 2)
    testi = [
        ((W - _larghezza(riga_data, f_data, 1)) / 2, _baseline(f_data, 450, 1.0), riga_data, f_data, 1),
        ((W - _larghezza(LUOGO, f_luogo, 1)) / 2, _baseline(f_luogo, 450 + 86 + 18, 1.0), LUOGO, f_luogo, 1),
    ]
    _testo_neon(img, testi, aloni)

    # Box
    f_box = _font(FONT_TITOLI, 72)
    _box(img, (70, 672, 522, 798), BORDO_A, [f"{quota} COPPIA".upper()], f_box, 1.0, pad_top=32)
    _box(img, (70, 824, 522, 950), BORDO_B, [f"PREMI IN {premi}".upper()], f_box, 1.0, pad_top=32)
    # corpo massimo che sta nel box sia in altezza sia in larghezza
    n = len(righe_formula)
    corpo = min(124 if n == 1 else 108, int((278 - 40) / (n * 0.95)))
    f_formula = _font(FONT_TITOLI, corpo)
    while max(_larghezza(r, f_formula) for r in righe_formula) > 452 - 40 and f_formula.size > 40:
        f_formula = _font(FONT_TITOLI, f_formula.size - 2)
    _box(img, (558, 672, 1010, 950), BORDO_C, righe_formula, f_formula, 0.95)

    # Info e prenotazioni
    f_info = _font(FONT_TITOLI, 52)
    f_tel = _font(FONT_TITOLI, 64)
    w1 = _larghezza("INFO E PRENOTAZIONI", f_info, 2)
    w2 = _larghezza(CONTATTO_NOME, f_info, 2)
    w3 = _larghezza(CONTATTO_TEL, f_tel, 1)
    totale = w1 + 64 + w2 + 18 + w3
    x = (W - totale) / 2
    bl = _baseline(f_tel, 968, 1.0)
    _scrivi(draw, (x, bl), "INFO E PRENOTAZIONI", f_info, NERO, 2)
    x_nome = x + w1 + 64
    _testo_neon(img, [(x_nome, bl, CONTATTO_NOME, f_info, 2)], [(8, (11, 11, 11))])
    draw = ImageDraw.Draw(img)
    _scrivi(draw, (x_nome + w2 + 18, bl), CONTATTO_TEL, f_tel, NERO, 1)

    # Biliardino in basso
    b = Image.open(IMG_BILIARDINO).convert("RGBA")
    b = b.resize((W, round(b.height * W / b.width)), Image.LANCZOS)
    if b.height > 310:
        b = b.crop((0, 0, W, 310))
    img.alpha_composite(b, (0, H - b.height))

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


if __name__ == "__main__":
    # prova veloce: python locandina.py
    with open("prova_locandina.png", "wb") as fh:
        fh.write(genera_locandina(date(2026, 10, 16), "rollerball"))
    print("Creato prova_locandina.png")
