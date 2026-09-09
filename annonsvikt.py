#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annonsvikt.py — mäter hur tung en display-annons är, och varför.

Laddar annonsen i en riktig headless Chromium via Playwright och spelar in all
nätverkstrafik. Redovisar vikten per del, räknar ut totalen, sätter betyg mot
IAB:s budget och ger konkreta råd om vad som bör göras för att minska tyngden.

Körs så här:
    python annonsvikt.py https://embed.bannerboo.com/b990849fd8c4b
    python annonsvikt.py b990849fd8c4b --html rapport.html --json matning.json
    python annonsvikt.py <url1> <url2>          # flera annonser + jämförelse

Kräver: pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html as htmlmod
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict

VERSION = "1.2.0"

SAKNAS_MEDDELANDE = (
    "Playwright saknas i den här Python-miljön.\n\n"
    "Kör \"Reparera Annonsvikt\" på Start-menyn, eller installera själv med:\n"
    "    pip install playwright\n"
    "    python -m playwright install chromium"
)

# ══════════════════════════════════════════════════════════════════════════════
#  BUDGETAR OCH TRÖSKLAR
# ══════════════════════════════════════════════════════════════════════════════

KB = 1024

# IAB Display Creative Guidelines / LEAN. Initial last = allt som behövs för
# första bildrutan. Subload = allt som får laddas efter att sidan är klar.
IAB_INITIAL = 150 * KB
IAB_TOTALT = 1000 * KB

# Animation: IAB rekommenderar max 15 sekunder och max 3 loopar.
IAB_ANIM_SEK = 15.0
IAB_LOOPAR = 3

BETYGSSKALA = [
    ("A", 150 * KB, "Utmärkt — inom IAB:s budget för initial laddning"),
    ("B", 250 * KB, "Bra — något över budget, men lätt att åtgärda"),
    ("C", 500 * KB, "Godkänt — märkbart tung, bör bantas"),
    ("D", 1000 * KB, "Svagt — långt över budget, kostar visningar"),
    ("F", float("inf"), "Underkänt — extremt tung för en display-annons"),
]

# Uppskattade konverteringsvinster. Konservativa värden.
FAKTOR_TTF_TILL_WOFF2 = 0.55  # woff2 är typiskt 45 % mindre än ttf
FAKTOR_SUBSET = 0.30  # ett bannersubset behåller ~30 % av ett komplett snitt
FAKTOR_JPEG_TILL_WEBP = 0.70
FAKTOR_PNG_TILL_WEBP = 0.55
FAKTOR_GIF_TILL_VIDEO = 0.25
NETTHINNA = 2.0  # bilder får vara 2× visningsytan (retina), inte mer

# Skript som är animations-/hjälpbibliotek och ofta kan ersättas i en banner.
TUNGA_BIBLIOTEK = {
    "gsap": "GSAP",
    "tweenmax": "GSAP TweenMax",
    "jquery": "jQuery",
    "createjs": "CreateJS",
    "pixi": "PixiJS",
    "three": "Three.js",
    "anime": "anime.js",
    "lottie": "Lottie",
    "velocity": "Velocity.js",
}

MONSTER_SPARNING = re.compile(
    r"(track|analytic|pixel|beacon|telemetr|collect|/tr\?|stat[sz]?\.|metric)", re.I
)

# ══════════════════════════════════════════════════════════════════════════════
#  RÅDBANK — REDIGERA HÄR
# ══════════════════════════════════════════════════════════════════════════════
#
#  Varje regel är en funktion som tar analysen (`a`) och lägger till noll eller
#  flera Rad-objekt i listan den returnerar. Lägg till egna råd genom att skriva
#  en ny funktion med @regel(prioritet) ovanför. Inget annat i filen behöver
#  röras. Högre prioritet = visas tidigare vid samma besparing.
#
#  Rad-fält:
#    rubrik      kort imperativ mening: "Byt typsnitten till woff2"
#    varfor      varför det är ett problem, i en mening
#    gor         lista med konkreta steg
#    sparar      uppskattad besparing i byte (0 om okänd)
#    allvar      "kritisk" | "hög" | "medel" | "låg"
#    valfritt    True = kräver större ingrepp, räknas inte in i huvudprognosen
#
# ══════════════════════════════════════════════════════════════════════════════

RAD_REGLER: list[tuple[int, object]] = []


def regel(prioritet: int):
    """Registrerar en rådregel. Används som dekorator."""

    def dekorator(fn):
        RAD_REGLER.append((prioritet, fn))
        return fn

    return dekorator


@dataclass
class Rad:
    rubrik: str
    varfor: str
    gor: list[str]
    sparar: int = 0
    allvar: str = "medel"
    valfritt: bool = False


@regel(100)
def rad_komprimering(a: "Analys") -> list[Rad]:
    okomprimerade = [r for r in a.resurser if r.text_utan_komprimering]
    if not okomprimerade:
        return []
    vinst = sum(r.storlek - r.gzip_storlek for r in okomprimerade if r.gzip_storlek)
    if vinst < 2 * KB:
        return []
    filer = ", ".join(r.filnamn for r in sorted(okomprimerade, key=lambda x: -x.storlek)[:4])
    return [
        Rad(
            rubrik="Slå på gzip/brotli på servern",
            varfor=(
                f"{antal(len(okomprimerade), 'textfil', 'textfiler')} "
                f"({fmt(sum(r.storlek for r in okomprimerade))}) "
                "skickas helt okomprimerade. Det är ren förlust — komprimering kostar "
                "ingenting i kvalitet."
            ),
            gor=[
                f"Aktivera gzip (helst brotli) för text/html, application/javascript, "
                f"text/css och image/svg+xml. Berör: {filer}",
                "Brotli ger ytterligare ~15 % jämfört med gzip på samma innehåll.",
                "Ligger annonsen hos en leverantör: be dem slå på komprimering, "
                "det är en serverinställning och inget du behöver bygga om.",
            ],
            sparar=vinst,
            allvar="kritisk" if vinst > 30 * KB else "hög",
        )
    ]


@regel(95)
def rad_typsnitt_format(a: "Analys") -> list[Rad]:
    ttf = [r for r in a.resurser if r.kategori == "typsnitt" and r.underformat in ("ttf", "otf")]
    if not ttf:
        return []
    vikt = sum(r.storlek for r in ttf)
    kvar = int(vikt * FAKTOR_TTF_TILL_WOFF2 * FAKTOR_SUBSET)
    return [
        Rad(
            rubrik="Byt typsnitten till woff2 och skär bort oanvända tecken",
            varfor=(
                f"{antal(len(ttf), 'typsnitt laddas', 'typsnitt laddas')} som "
                f"{'/'.join(sorted({r.underformat for r in ttf}))} ({fmt(vikt)}). Det är "
                "skrivbordsformat med alla tecken för alla språk — en banner använder "
                "oftast under 40 tecken. Tyngst: "
                + ", ".join(f"{r.filnamn} ({fmt(r.storlek)})"
                            for r in sorted(ttf, key=lambda x: -x.storlek)[:2])
                + "."
            ),
            gor=[
                "Konvertera till woff2 (~45 % mindre direkt, stöds av alla webbläsare "
                "som är relevanta idag).",
                "Subsetta till de tecken som faktiskt står i annonsen, t.ex. "
                "`pyftsubset font.ttf --text=\"Robusta IT-avtal\" --flavor=woff2`.",
                "Behåll `font-display: swap` så texten syns direkt även om snittet dröjer.",
                f"Uppskattat resultat: {fmt(vikt)} → ca {fmt(kvar)}.",
            ],
            sparar=vikt - kvar,
            allvar="kritisk" if vikt > 100 * KB else "hög",
        )
    ]


@regel(90)
def rad_for_manga_familjer(a: "Analys") -> list[Rad]:
    familjer = sorted(a.typsnitt_laddade)
    if len(familjer) < 3:
        return []
    tunga = sorted(
        [r for r in a.resurser if r.kategori == "typsnitt"], key=lambda r: -r.storlek
    )
    kandidat = sum(r.storlek for r in tunga[2:])
    return [
        Rad(
            rubrik=f"Minska antalet typsnittsfamiljer från {len(familjer)} till högst två",
            varfor=(
                f"Annonsen laddar {len(familjer)} olika familjer: {', '.join(familjer)}. "
                "Varje familj är en egen nedladdning, och på 600×300 px syns knappast "
                "skillnaden mellan dem."
            ),
            gor=[
                "Välj ett snitt för rubrik och ett för brödtext — resten ersätts.",
                "Dekorativa snitt som bara används på ett par ord kan ofta ritas som "
                "SVG-text i stället, då försvinner nedladdningen helt.",
                "Systemsnitt (Arial, Georgia, Tahoma) kostar noll byte om de duger.",
            ],
            sparar=kandidat,
            allvar="hög" if kandidat > 50 * KB else "medel",
        )
    ]


@regel(88)
def rad_deklarerade_snitt(a: "Analys") -> list[Rad]:
    if a.typsnitt_deklarerade <= len(a.typsnitt_laddade) + 3:
        return []
    doc = max(
        (r for r in a.resurser if r.kategori == "dokument"),
        key=lambda r: r.storlek,
        default=None,
    )
    extra = a.typsnitt_deklarerade - len(a.typsnitt_laddade)
    # ~180 byte per @font-face-deklaration i dokumentet
    vinst = min(int(extra * 180), int(doc.storlek * 0.4) if doc else 0)
    return [
        Rad(
            rubrik=f"Rensa {extra} oanvända @font-face-deklarationer ur dokumentet",
            varfor=(
                f"Dokumentet deklarerar {a.typsnitt_deklarerade} typsnittsskärningar men bara "
                f"{len(a.typsnitt_laddade)} används. Filerna hämtas visserligen inte, men "
                "deklarationerna ligger kvar som död vikt i HTML-koden och måste parsas."
            ),
            gor=[
                "Låt bannerverktyget bara skriva ut de skärningar som annonsen faktiskt "
                "använder — resten är mall-skräp.",
                "Är det en tredjepartsgenerator: rapportera det, det drabbar alla deras kunder.",
            ],
            sparar=max(vinst, 0),
            allvar="låg",
        )
    ]


@regel(85)
def rad_dolda_resurser(a: "Analys") -> list[Rad]:
    dolda = [r for r in a.resurser if r.dold_orsak]
    if not dolda:
        return []
    vikt = sum(r.storlek for r in dolda)
    rader = [f"{r.filnamn} ({fmt(r.storlek)}, {r.dold_orsak})" for r in dolda]
    return [
        Rad(
            rubrik=f"Ta bort {antal(len(dolda), 'resurs', 'resurser')} som laddas men aldrig syns",
            varfor=(
                "Element som är dolda med visibility:hidden eller opacity:0 laddar ändå "
                "sina bilder. Besökaren betalar för byte som aldrig visas."
            ),
            gor=[
                "Radera lagret ur annonsen i stället för att dölja det: " + "; ".join(rader),
                "Behövs lagret för en variant — bygg en separat annons, dölj det inte.",
                "Ska det visas senare i animationen: ladda det med display:none och "
                "sätt in det via JS när det behövs.",
            ],
            sparar=vikt,
            allvar="hög" if vikt > 30 * KB else "medel",
        )
    ]


@regel(80)
def rad_overdimensionerade_bilder(a: "Analys") -> list[Rad]:
    stora = [r for r in a.resurser if r.overdim_faktor and r.overdim_faktor > 1.15]
    if not stora:
        return []
    vinst = sum(r.storlek - int(r.storlek / (r.overdim_faktor**2)) for r in stora)
    rader = [
        f"{r.filnamn}: {r.nat_b}×{r.nat_h} px levereras, visas som {r.vis_b}×{r.vis_h} px "
        f"→ skala till {int(r.vis_b * NETTHINNA)}×{int(r.vis_h * NETTHINNA)} px"
        for r in stora
    ]
    return [
        Rad(
            rubrik=f"Skala ner {antal(len(stora), 'bild', 'bilder')} till rätt pixelmått",
            varfor=(
                "Bilderna levereras större än de visas. Webbläsaren skalar ner dem — "
                "de extra pixlarna kostar bandbredd utan att synas."
            ),
            gor=rader
            + [
                f"Tumregel: max {NETTHINNA:.0f}× visningsytan räcker även på retinaskärmar.",
            ],
            sparar=max(vinst, 0),
            allvar="hög" if vinst > 50 * KB else "medel",
        )
    ]


@regel(78)
def rad_bildformat(a: "Analys") -> list[Rad]:
    gamla = [
        r
        for r in a.resurser
        if r.kategori == "bild" and r.underformat in ("jpeg", "png", "gif") and not r.dold_orsak
    ]
    if not gamla:
        return []
    vinst = 0
    rader = []
    for r in sorted(gamla, key=lambda x: -x.storlek):
        f = {
            "jpeg": FAKTOR_JPEG_TILL_WEBP,
            "png": FAKTOR_PNG_TILL_WEBP,
            "gif": FAKTOR_GIF_TILL_VIDEO,
        }[r.underformat]
        v = int(r.storlek * (1 - f))
        vinst += v
        rader.append(
            f"{r.filnamn} ({r.underformat.upper()}, {fmt(r.storlek)}) → WebP ≈ "
            f"{fmt(int(r.storlek * f))}"
        )
    if vinst < 5 * KB:
        return []
    return [
        Rad(
            rubrik="Konvertera bilderna till WebP (eller AVIF)",
            varfor=(
                f"{antal(len(gamla), 'bild ligger', 'bilder ligger')} kvar i {', '.join(sorted({r.underformat.upper() for r in gamla}))}. "
                "WebP ger samma upplevda kvalitet på klart färre byte och stöds av alla "
                "webbläsare sedan 2020."
            ),
            gor=rader
            + [
                "`cwebp -q 80 in.png -o ut.webp` — jämför sedan på skärm, inte i siffror.",
                "Foton mår bra av kvalitet 75–82; ytor med platta färger kan gå lägre.",
            ],
            sparar=vinst,
            allvar="hög" if vinst > 50 * KB else "medel",
        )
    ]


@regel(75)
def rad_animationsbibliotek(a: "Analys") -> list[Rad]:
    bib = [r for r in a.resurser if r.bibliotek]
    if not bib:
        return []
    vikt = sum(r.storlek for r in bib)
    namn = ", ".join(sorted({r.bibliotek for r in bib}))
    enkla = bool(a.animationstyper) and a.animationstyper.issubset(ENKLA_ANIMATIONER)
    return [
        Rad(
            rubrik=f"Ersätt {namn} med CSS-animation",
            varfor=(
                f"{namn} väger {fmt(vikt)} och laddas för varje visning."
                + (
                    " Animationerna i annonsen är rena in/ut-toningar och förflyttningar — "
                    "sådant klarar CSS utan en enda rad JavaScript."
                    if enkla
                    else " Kontrollera om animationerna verkligen kräver ett helt bibliotek."
                )
            ),
            gor=[
                "@keyframes + transform/opacity ger samma resultat, körs på GPU och "
                "kostar 0 byte extra.",
                "Behövs tidslinjestyrning: Web Animations API finns inbyggt i webbläsaren.",
                "Måste biblioteket vara kvar: importera bara de moduler du använder och "
                "lägg filen i annonsbundeln i stället för att hämta den från ett externt CDN "
                "— då slipper du en extra DNS- och TLS-runda före första bildrutan.",
            ],
            sparar=vikt,
            allvar="hög" if vikt > 40 * KB else "medel",
            valfritt=True,
        )
    ]


@regel(70)
def rad_tredjepart(a: "Analys") -> list[Rad]:
    tp = [r for r in a.resurser if r.tredjepart and not r.bibliotek]
    if not tp:
        return []
    vikt = sum(r.storlek for r in tp)
    varder = sorted({urllib.parse.urlparse(r.url).netloc for r in tp})
    sparning = [r for r in tp if r.sparning]
    gor = [
        "Varje extern värd kostar DNS-uppslag, TCP-handskakning och TLS innan en "
        "enda byte av innehåll hämtas — ofta 100–300 ms.",
        f"Externa värdar just nu: {', '.join(varder)}.",
    ]
    if sparning:
        gor.append(
            f"Spårskript: {', '.join(r.filnamn for r in sparning)}. Kontrollera att det "
            "verkligen används och att det är förenligt med er samtyckeshantering."
        )
    return [
        Rad(
            rubrik=f"Se över {antal(len(tp), 'anrop', 'anrop')} till externa värdar",
            varfor=(
                f"Annonsen hämtar {fmt(vikt)} från {len(varder)} externa domäner utöver "
                "själva annonsservern."
            ),
            gor=gor,
            sparar=sum(r.storlek for r in sparning),
            allvar="medel",
            valfritt=True,
        )
    ]


@regel(65)
def rad_cache(a: "Analys") -> list[Rad]:
    utan = [r for r in a.resurser if not r.cachebar and r.storlek > 2 * KB]
    if not utan:
        return []
    vikt = sum(r.storlek for r in utan)
    return [
        Rad(
            rubrik="Sätt cache-headers på resurser som saknar dem",
            varfor=(
                f"{antal(len(utan), 'resurs', 'resurser')} ({fmt(vikt)}) saknar användbar Cache-Control. "
                "De hämtas om vid varje visning, även för samma besökare."
            ),
            gor=[
                "Filer med hash i namnet kan sättas till "
                "`Cache-Control: public, max-age=31536000, immutable`.",
                f"Berör bl.a.: {', '.join(r.filnamn for r in sorted(utan, key=lambda x: -x.storlek)[:4])}",
            ],
            sparar=0,
            allvar="låg",
        )
    ]


@regel(60)
def rad_animationslangd(a: "Analys") -> list[Rad]:
    if a.anim_sekunder is None:
        return []
    problem = []
    if a.anim_sekunder > IAB_ANIM_SEK:
        problem.append(
            f"animationen är {a.anim_sekunder:.1f} s, IAB rekommenderar högst "
            f"{IAB_ANIM_SEK:.0f} s"
        )
    if a.anim_loopar == 0:
        problem.append("den loopar oändligt — IAB rekommenderar högst 3 loopar")
    elif a.anim_loopar and a.anim_loopar > IAB_LOOPAR:
        problem.append(f"den loopar {a.anim_loopar} gånger, rekommendationen är {IAB_LOOPAR}")
    if not problem:
        return []
    return [
        Rad(
            rubrik="Korta animationen och stoppa den oändliga loopen",
            varfor=(
                "Det här är inte bandbredd utan CPU och batteri: " + ", ".join(problem) + "."
            ),
            gor=[
                f"Sätt annonsen att stanna efter {IAB_LOOPAR} loopar och landa på "
                "en slutbild med budskap och knapp.",
                "En animation som aldrig tar slut håller renderingstråden vaken hela "
                "tiden sidan är öppen — det märks tydligt på mobil.",
                "Budskapet bör vara läsbart redan i första bildrutan; många ser "
                "annonsen i mindre än tre sekunder.",
            ],
            sparar=0,
            allvar="medel",
        )
    ]


@regel(55)
def rad_antal_forfragningar(a: "Analys") -> list[Rad]:
    if len(a.resurser) <= 15:
        return []
    return [
        Rad(
            rubrik=f"Minska antalet förfrågningar ({len(a.resurser)} stycken)",
            varfor=(
                "Varje förfrågan har en fast kostnad i latens. På 3G eller ett svagt "
                "mobilnät väger antalet ofta tyngre än byten."
            ),
            gor=[
                "Bädda in små bilder och SVG direkt i HTML-koden som data-URI eller inline "
                "SVG — under ~2 kB lönar det sig nästan alltid.",
                "Slå ihop CSS och JS till en fil.",
                "Ligger allt i samma bundle kan hela annonsen levereras i ett svar.",
            ],
            sparar=0,
            allvar="låg",
        )
    ]


@regel(50)
def rad_over_budget(a: "Analys") -> list[Rad]:
    if a.totalvikt <= IAB_INITIAL:
        return []
    over = a.totalvikt - IAB_INITIAL
    return [
        Rad(
            rubrik=f"Totalvikten ligger {fmt(over)} över IAB:s budget",
            varfor=(
                f"Annonsen väger {fmt(a.totalvikt)}. IAB:s riktvärde för initial laddning "
                f"är {fmt(IAB_INITIAL)}, och flera annonsnätverk avvisar eller "
                "nedprioriterar kreativ som ligger långt över."
            ),
            gor=[
                "Tyngsta delarna först: "
                + ", ".join(
                    f"{r.filnamn} ({fmt(r.storlek)})"
                    for r in sorted(a.resurser, key=lambda x: -x.storlek)[:3]
                ),
                "Går annonsen inte att få under budget: dela upp i initial last "
                "(första bildrutan) och subload (resten, efter sidans onload).",
            ],
            sparar=0,
            allvar="kritisk" if a.totalvikt > 2 * IAB_TOTALT else "hög",
        )
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  DATAMODELL
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class Resurs:
    url: str
    kategori: str = "övrigt"  # dokument | skript | stilmall | bild | typsnitt | media | data
    underformat: str = ""  # png, jpeg, ttf, woff2 ...
    status: int = 0
    mime: str = ""
    storlek: int = 0  # överförda byte (komprimerat, som det gick över nätet)
    uppackat: int = 0  # byte efter uppackning
    gzip_storlek: int = 0  # hur stort det hade varit med gzip
    komprimering: str = ""  # gzip | br | deflate | ""
    cachebar: bool = False
    cache_info: str = ""
    tredjepart: bool = False
    sparning: bool = False
    bibliotek: str = ""
    frame: str = ""
    # bildspecifikt
    nat_b: int = 0
    nat_h: int = 0
    vis_b: int = 0
    vis_h: int = 0
    overdim_faktor: float = 0.0
    dold_orsak: str = ""
    # räknat
    potential: int = 0  # rimlig storlek efter åtgärd

    @property
    def filnamn(self) -> str:
        delad = urllib.parse.urlparse(self.url)
        segment = [d for d in delad.path.split("/") if d]
        namn = segment[-1] if segment else delad.netloc
        if delad.path.endswith("/"):
            namn += "/"
        if len(namn) > 32:
            namn = namn[:17] + "…" + namn[-14:]
        return namn

    @property
    def text_utan_komprimering(self) -> bool:
        return (
            self.kategori in ("dokument", "skript", "stilmall")
            or self.underformat == "svg"
        ) and not self.komprimering


@dataclass
class Analys:
    kalla: str
    tidpunkt: str = ""
    bredd: int = 0
    hojd: int = 0
    visningstyp: str = ""
    anim_sekunder: float | None = None
    anim_loopar: int | None = None
    lager: list[dict] = field(default_factory=list)
    animationstyper: set[str] = field(default_factory=set)
    resurser: list[Resurs] = field(default_factory=list)
    typsnitt_laddade: set[str] = field(default_factory=set)
    typsnitt_deklarerade: int = 0
    konsolfel: list[str] = field(default_factory=list)
    varningar: list[str] = field(default_factory=list)
    misslyckande: str = ""  # ifyllt när adressen inte gav någon annons att mäta

    @property
    def totalvikt(self) -> int:
        return sum(r.storlek for r in self.resurser)

    @property
    def totalt_uppackat(self) -> int:
        return sum(r.uppackat or r.storlek for r in self.resurser)

    @property
    def potentialvikt(self) -> int:
        return sum(r.potential for r in self.resurser)


# ══════════════════════════════════════════════════════════════════════════════
#  HJÄLPFUNKTIONER
# ══════════════════════════════════════════════════════════════════════════════


def beratta(args, text: str) -> None:
    """Löpande besked under en mätning.

    Kommandoraden skriver till stderr, fönstret sätter sin statusrad. Vilket som
    avgörs av om anroparen lagt en funktion i args.status."""
    if args is None:
        return
    aterkoppling = getattr(args, "status", None)
    if callable(aterkoppling):
        aterkoppling(text)
    elif not getattr(args, "tyst", False):
        print(f"  {text}", file=sys.stderr)


def fmt(byte: float) -> str:
    """Formaterar byte som svensk läsbar storlek."""
    b = float(byte)
    if abs(b) < 1000:
        return f"{int(b)} B"
    if abs(b) < 1000 * KB:
        return f"{b / KB:.1f} kB".replace(".", ",")
    return f"{b / KB / KB:.2f} MB".replace(".", ",")


def antal(n: int, ental: str, flertal: str) -> str:
    """1 resurs / 3 resurser — svenska räkneord ska stämma."""
    return f"{n} {ental if n == 1 else flertal}"


def procent(del_: float, helhet: float) -> str:
    if not helhet:
        return "0 %"
    return f"{100 * del_ / helhet:.1f} %".replace(".", ",")


def normalisera_url(indata: str) -> str:
    """Tar emot full URL, url utan protokoll, eller bara ett BannerBoo-id."""
    s = indata.strip()
    if re.fullmatch(r"[0-9a-f]{8,32}", s):
        return f"https://embed.bannerboo.com/{s}"
    if not s.startswith(("http://", "https://")):
        return "https://" + s
    return s


def registrerbar_domän(host: str) -> str:
    delar = host.split(".")
    return ".".join(delar[-2:]) if len(delar) >= 2 else host


def kategorisera(resurstyp: str, mime: str, url: str) -> tuple[str, str]:
    """Returnerar (kategori, underformat)."""
    m = (mime or "").split(";")[0].strip().lower()
    stig = urllib.parse.urlparse(url).path.lower()
    ext = stig.rsplit(".", 1)[-1] if "." in stig.rsplit("/", 1)[-1] else ""

    if m.startswith("image/") or resurstyp == "image":
        under = m.split("/")[-1] if "/" in m else ext
        under = {"jpg": "jpeg", "svg+xml": "svg", "x-icon": "ico"}.get(under, under)
        return "bild", under
    if m.startswith("font/") or "font" in m or resurstyp == "font" or ext in ("ttf", "otf", "woff", "woff2", "eot"):
        under = ext or m.split("/")[-1]
        under = {"sfnt": "ttf", "truetype": "ttf", "opentype": "otf"}.get(under, under)
        return "typsnitt", under
    if resurstyp == "document" or m == "text/html":
        return "dokument", "html"
    if resurstyp == "stylesheet" or m == "text/css":
        return "stilmall", "css"
    if resurstyp == "script" or "javascript" in m or "ecmascript" in m:
        return "skript", "js"
    if m.startswith(("video/", "audio/")) or resurstyp == "media":
        return "media", m.split("/")[-1]
    if resurstyp in ("xhr", "fetch"):
        return "data", ext or "json"
    return "övrigt", ext


def hitta_bibliotek(url: str) -> str:
    lag = url.lower()
    for nyckel, namn in TUNGA_BIBLIOTEK.items():
        if re.search(rf"\b{nyckel}[.\-/]", lag) or f"/{nyckel}" in lag:
            return namn
    return ""


def cache_bedomning(headers: dict) -> tuple[bool, str]:
    cc = (headers.get("cache-control") or "").lower()
    if "no-store" in cc or "no-cache" in cc:
        return False, cc or "no-store"
    m = re.search(r"max-age=(\d+)", cc)
    if m:
        sek = int(m.group(1))
        if sek >= 3600:
            dagar = sek / 86400
            return True, f"max-age {dagar:.0f} dygn" if dagar >= 1 else f"max-age {sek} s"
        return False, f"max-age {sek} s"
    if headers.get("expires") or headers.get("etag") or headers.get("last-modified"):
        return True, "villkorad (etag/last-modified)"
    return False, "saknas"


# ══════════════════════════════════════════════════════════════════════════════
#  MÄTNING I RIKTIG WEBBLÄSARE
# ══════════════════════════════════════════════════════════════════════════════

VARDPAGE = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><title>annonsvikt</title>
<style>html,body{margin:0;padding:0;background:#fff}</style></head>
<body><div id="annonsvikt-slot"></div>
<script src="__EMBED__"></script>
</body></html>"""

# Sonden körs inne i annonsens egen ram och plockar ut det som bara går att se
# när sidan är renderad: verkliga pixelmått, visningsmått, dolda lager och
# vilka typsnitt som faktiskt laddades.
SOND = r"""
async () => {
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  const urlerUr = v => {
    if (!v || v === 'none') return [];
    return Array.from(v.matchAll(/url\((["']?)([^"')]+)\1\)/g)).map(m => abs(m[2]));
  };
  const doldOrsak = el => {
    let e = el;
    while (e && e.nodeType === 1) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none') return 'display:none';
      if (cs.visibility === 'hidden') return 'visibility:hidden';
      if (parseFloat(cs.opacity) === 0) return 'opacity:0';
      e = e.parentElement;
    }
    return '';
  };

  const bilder = new Map();
  const notera = (url, el) => {
    if (!url || url.startsWith('data:')) return;
    const r = el.getBoundingClientRect();
    const b = Math.round(r.width), h = Math.round(r.height);
    const f = bilder.get(url) || { url, vis_b: 0, vis_h: 0, dold: doldOrsak(el) };
    if (b * h > f.vis_b * f.vis_h) { f.vis_b = b; f.vis_h = h; }
    if (!doldOrsak(el)) f.dold = '';
    bilder.set(url, f);
  };

  document.querySelectorAll('img').forEach(im => notera(abs(im.currentSrc || im.src), im));
  document.querySelectorAll('*').forEach(el => {
    const cs = getComputedStyle(el);
    [cs.backgroundImage, cs.maskImage, cs.webkitMaskImage, cs.borderImageSource,
     cs.content, cs.listStyleImage].forEach(v => urlerUr(v).forEach(u => notera(u, el)));
  });

  // Verkliga pixelmått — bilderna ligger i cache, så det här går direkt.
  const matt = await Promise.all(Array.from(bilder.values()).map(f => new Promise(klar => {
    const i = new Image();
    i.onload = () => klar(Object.assign(f, { nat_b: i.naturalWidth, nat_h: i.naturalHeight }));
    i.onerror = () => klar(Object.assign(f, { nat_b: 0, nat_h: 0 }));
    i.src = f.url;
  })));

  const anvanda = new Set();
  document.querySelectorAll('*').forEach(el => {
    if (!el.textContent || !el.textContent.trim()) return;
    const ff = getComputedStyle(el).fontFamily || '';
    const forsta = ff.split(',')[0].replace(/^["']|["']$/g, '').trim();
    if (forsta) anvanda.add(forsta);
  });

  const snitt = [];
  document.fonts.forEach(f => snitt.push({
    familj: f.family.replace(/^["']|["']$/g, ''),
    vikt: f.weight, stil: f.style, status: f.status
  }));

  return {
    url: location.href,
    titel: document.title,
    bilder: matt,
    snitt,
    anvanda: Array.from(anvanda),
    element: document.querySelectorAll('*').length,
    dom_byte: document.documentElement.outerHTML.length
  };
}
"""


def mat_i_webblasare(url: str, vantetid: float, huvud: bool, bredd: int, hojd: int,
                     tyst: bool, args=None):
    """Laddar annonsen och spelar in all nätverkstrafik. Returnerar rådata."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as fel:
        # Aldrig sys.exit() här: anropas det från en arbetstråd blir det ett
        # SystemExit som inte fångas av "except Exception", och tråden dör tyst.
        raise RuntimeError(SAKNAS_MEDDELANDE) from fel

    # Sniffa först: är URL:en ett inbäddningsskript eller ett färdigt dokument?
    lage, forsta_kropp = sniffa(url)

    inspelat: list[dict] = []
    konsol: list[str] = []
    poster: dict = {}

    with sync_playwright() as p:
        webblasare = p.chromium.launch(headless=not huvud)
        kontext = webblasare.new_context(
            viewport={"width": max(bredd, 800), "height": max(hojd, 600)},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            bypass_csp=True,
        )
        sida = kontext.new_page()

        def vid_svar(svar):
            try:
                poster[svar.request] = svar
            except Exception:
                pass

        def vid_klar(req):
            try:
                storlekar = req.sizes()
            except Exception:
                storlekar = {}
            inspelat.append({"req": req, "sizes": storlekar})

        def vid_fel(req):
            inspelat.append({"req": req, "sizes": {}, "fel": req.failure})

        kontext.on("response", vid_svar)
        kontext.on("requestfinished", vid_klar)
        kontext.on("requestfailed", vid_fel)
        sida.on(
            "console",
            lambda m: konsol.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None,
        )

        if lage == "skript":
            vard = "https://annonsvikt.test/vardsida.html"
            kropp = VARDPAGE.replace("__EMBED__", htmlmod.escape(url, quote=True))
            sida.route(vard, lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=kropp
            ))
            mal = vard
        else:
            mal = url

        if args is not None:
            beratta(args, f"laddar {url} …")
        elif not tyst:
            print(f"  laddar {url} …", file=sys.stderr)
        navfel = ""
        try:
            sida.goto(mal, wait_until="load", timeout=60000)
        except Exception as fel:
            navfel = las_navigeringsfel(fel)
        try:
            sida.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # Låt animationen rulla så att sent laddade resurser hinner med.
        sida.wait_for_timeout(int(vantetid * 1000))

        # Sond i varje ram utom vår egen värdsida.
        sonder = []
        for ram in sida.frames:
            if ram.url.startswith("https://annonsvikt.test/") or ram.url in ("about:blank", ""):
                continue
            try:
                sonder.append(ram.evaluate(SOND))
            except Exception as e:
                konsol.append(f"sond misslyckades i {ram.url[:60]}: {e}")

        skarmbild = None
        try:
            el = sida.query_selector("iframe") or sida.query_selector("body")
            if el:
                skarmbild = el.screenshot()
        except Exception:
            pass

        # Kroppar hämtas efter att sidan är klar; de ligger kvar i minnet.
        kroppar = {}
        for post in inspelat:
            svar = poster.get(post["req"])
            if not svar:
                continue
            try:
                kroppar[post["req"]] = svar.body()
            except Exception:
                kroppar[post["req"]] = b""

        rader = []
        for post in inspelat:
            req = post["req"]
            svar = poster.get(req)
            if req.url.startswith("https://annonsvikt.test/") or req.url.startswith("data:"):
                continue
            huvuden = {}
            status = 0
            if svar:
                try:
                    huvuden = {k.lower(): v for k, v in svar.headers.items()}
                    status = svar.status
                except Exception:
                    pass
            kropp = kroppar.get(req, b"")
            rader.append(
                {
                    "url": req.url,
                    "resurstyp": req.resource_type,
                    "status": status,
                    "huvuden": huvuden,
                    "overfort": (post.get("sizes") or {}).get("responseBodySize", 0),
                    "huvudbyte": (post.get("sizes") or {}).get("responseHeadersSize", 0),
                    "kropp": kropp,
                    "ram": (req.frame.url if req.frame else ""),
                }
            )

        webblasare.close()

    return lage, forsta_kropp, rader, sonder, konsol, skarmbild, navfel


def las_navigeringsfel(fel: Exception) -> str:
    """Chromiums felkoder säger inget för en användare — översätt dem."""
    text = str(fel)
    if "ERR_NAME_NOT_RESOLVED" in text:
        return "Adressen gick inte att slå upp. Kontrollera att den är rätt stavad."
    if "ERR_CERT" in text:
        return "Adressens säkerhetscertifikat kunde inte verifieras."
    if "ERR_CONNECTION_REFUSED" in text:
        return "Servern nekade anslutningen."
    if "ERR_CONNECTION" in text or "ERR_ADDRESS" in text or "ERR_INTERNET" in text:
        return "Det gick inte att ansluta till adressen. Kontrollera nätverket."
    if "Timeout" in text or "ERR_TIMED_OUT" in text:
        return "Adressen svarade inte i tid — servern kan vara nere eller mycket långsam."
    if "ERR_ABORTED" in text:
        return "Laddningen avbröts av servern."
    return "Adressen kunde inte laddas: " + text.splitlines()[0][:120]


def sniffa(url: str) -> tuple[str, str]:
    """Avgör om URL:en levererar ett inbäddningsskript eller ett HTML-dokument."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as svar:
            rå = svar.read()
            if svar.headers.get("Content-Encoding") == "gzip":
                rå = gzip.decompress(rå)
            text = rå.decode("utf-8", "replace")
    except Exception as e:
        print(f"  varning: kunde inte förhandsläsa {url}: {e}", file=sys.stderr)
        return "dokument", ""
    inled = text.lstrip()[:200].lower()
    if inled.startswith(("<!doctype", "<html", "<head", "<body")):
        return "dokument", text
    return "skript", text


# ══════════════════════════════════════════════════════════════════════════════
#  BANNERBOO-ADAPTER — läser annonsens egen konfiguration
# ══════════════════════════════════════════════════════════════════════════════


ENKLA_ANIMATIONER = {
    "fade", "slide", "zoom", "scale", "rotate", "flip", "pulse", "bounce",
    "blur", "wipe", "none",
}


def grundanimation(kod: str) -> str:
    """"slideleft", "pulseimpRight" osv. → grundrörelsen."""
    for grund in ENKLA_ANIMATIONER:
        if kod.startswith(grund):
            return grund
    return kod


def las_bannerboo(text: str, a: Analys) -> None:
    m = re.search(r"var c=(\{.*?\});function ", text, re.S)
    if not m:
        return
    try:
        c = json.loads(m.group(1))
    except json.JSONDecodeError:
        return
    a.bredd = int(c.get("item_width") or 0)
    a.hojd = int(c.get("item_height") or 0)
    a.visningstyp = c.get("display_type") or ""
    anim = c.get("item_animation") or {}
    if anim:
        a.anim_sekunder = float(anim.get("animtime") or 0) or None
        a.anim_loopar = anim.get("playtimes")
        for obj in (anim.get("objects") or {}).values():
            for nyckel in ("animin", "animout", "animpulse"):
                kod = ((obj.get(nyckel) or {}).get("code") or "").lower()
                if kod:
                    a.animationstyper.add(grundanimation(kod))
    for obj in ((c.get("item_settings") or {}).get("objects") or {}).values():
        a.lager.append(
            {
                "namn": obj.get("name") or "",
                "typ": obj.get("type") or "",
                "synligt": bool(obj.get("visible", 1)),
                "bredd": (obj.get("style") or {}).get("width", ""),
                "hojd": (obj.get("style") or {}).get("height", ""),
                "url": obj.get("url") or "",
            }
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ANALYS
# ══════════════════════════════════════════════════════════════════════════════


def bygg_analys(kalla, lage, forsta_kropp, rader, sonder, konsol, navfel="") -> Analys:
    a = Analys(kalla=kalla, tidpunkt=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    a.konsolfel = konsol[:20]

    if lage == "skript" and forsta_kropp:
        las_bannerboo(forsta_kropp, a)

    hem = registrerbar_domän(urllib.parse.urlparse(kalla).netloc)

    # Sonddata: pixelmått, visningsmått, dolda lager, typsnittsstatus
    bildinfo: dict[str, dict] = {}
    for s in sonder:
        for b in s.get("bilder", []):
            bildinfo[b["url"]] = b
        for f in s.get("snitt", []):
            if f.get("status") == "loaded":
                a.typsnitt_laddade.add(f["familj"])
        a.typsnitt_deklarerade += len(s.get("snitt", []))

    sedda = set()
    for rad in rader:
        url = rad["url"]
        if url in sedda:
            continue
        sedda.add(url)
        kategori, underformat = kategorisera(rad["resurstyp"], rad["huvuden"].get("content-type", ""), url)
        kropp = rad["kropp"] or b""
        overfort = rad["overfort"]
        if not overfort or overfort < 0:
            overfort = len(kropp)  # t.ex. 204/redirect där Playwright saknar mått
        kodning = (rad["huvuden"].get("content-encoding") or "").lower()
        cachebar, cache_info = cache_bedomning(rad["huvuden"])
        värd = urllib.parse.urlparse(url).netloc

        r = Resurs(
            url=url,
            kategori=kategori,
            underformat=underformat,
            status=rad["status"],
            mime=(rad["huvuden"].get("content-type") or "").split(";")[0],
            storlek=int(overfort),
            uppackat=len(kropp),
            komprimering=kodning,
            cachebar=cachebar,
            cache_info=cache_info,
            tredjepart=registrerbar_domän(värd) != hem,
            sparning=bool(MONSTER_SPARNING.search(url)),
            bibliotek=hitta_bibliotek(url),
            frame=rad["ram"],
        )

        # Hur litet hade det blivit med gzip?
        if r.text_utan_komprimering and kropp:
            try:
                r.gzip_storlek = len(gzip.compress(kropp, 6))
            except Exception:
                r.gzip_storlek = 0

        info = bildinfo.get(url)
        if info:
            r.nat_b, r.nat_h = int(info.get("nat_b") or 0), int(info.get("nat_h") or 0)
            r.vis_b, r.vis_h = int(info.get("vis_b") or 0), int(info.get("vis_h") or 0)
            r.dold_orsak = info.get("dold") or ""
            if r.nat_b and r.vis_b:
                r.overdim_faktor = round(r.nat_b / max(r.vis_b * NETTHINNA, 1), 2)

        a.resurser.append(r)

    berakna_potential(a)
    a.misslyckande = navfel or bedom_misslyckande(a, lage, forsta_kropp)
    return a


def bedom_misslyckande(a: Analys, lage: str, forsta_kropp: str) -> str:
    """Gav adressen någon annons alls? Returnerar en förklaring om inte.

    BannerBoo svarar med HTTP 200 även för ett id som inte finns — felet ligger i
    ett JSON-svar i kroppen. Utan den här kontrollen blir resultatet en rapport på
    noll byte utan antydan om varför."""
    text = (forsta_kropp or "").strip()
    if text.startswith("{"):
        try:
            svar = json.loads(text)
        except json.JSONDecodeError:
            svar = None
        if isinstance(svar, dict) and svar.get("success") is False:
            # Serverns egen text ("Method not allowed") är missvisande här och
            # säger inget om den verkliga orsaken, så den återges inte.
            return "Adressen finns hos BannerBoo men pekar inte på någon annons."

    if not a.resurser:
        return "Ingenting kunde laddas från adressen."

    # En riktig BannerBoo-annons ger alltid en konfiguration med lager och mått.
    if lage == "skript" and not a.lager and a.totalvikt < 5 * KB:
        return (
            f"Adressen svarade, men där fanns ingen annons att mäta — bara "
            f"{fmt(a.totalvikt)} över {antal(len(a.resurser), 'förfrågan', 'förfrågningar')}."
        )
    return ""


def berakna_potential(a: Analys) -> None:
    """Rimlig storlek per resurs efter åtgärd — utan att räkna samma vinst två gånger."""
    for r in a.resurser:
        if r.dold_orsak:
            r.potential = 0
            continue
        if r.kategori == "typsnitt":
            if r.underformat in ("ttf", "otf"):
                r.potential = int(r.storlek * FAKTOR_TTF_TILL_WOFF2 * FAKTOR_SUBSET)
            elif r.underformat in ("woff", "woff2"):
                r.potential = int(r.storlek * FAKTOR_SUBSET)
            else:
                r.potential = r.storlek
            continue
        if r.kategori == "bild":
            skala = 1.0
            if r.overdim_faktor and r.overdim_faktor > 1.15:
                skala = 1 / (r.overdim_faktor**2)
            formatfaktor = {
                "jpeg": FAKTOR_JPEG_TILL_WEBP,
                "png": FAKTOR_PNG_TILL_WEBP,
                "gif": FAKTOR_GIF_TILL_VIDEO,
            }.get(r.underformat, 1.0)
            if r.underformat == "svg" and not r.komprimering and r.gzip_storlek:
                r.potential = r.gzip_storlek
            else:
                r.potential = int(r.storlek * skala * formatfaktor)
            continue
        if r.text_utan_komprimering and r.gzip_storlek:
            r.potential = r.gzip_storlek
            continue
        r.potential = r.storlek


def satt_betyg(vikt: int) -> tuple[str, str]:
    for bokstav, gräns, text in BETYGSSKALA:
        if vikt <= gräns:
            return bokstav, text
    return "F", BETYGSSKALA[-1][2]


def samla_rad(a: Analys) -> list[Rad]:
    rad: list[Rad] = []
    for prio, fn in sorted(RAD_REGLER, key=lambda x: -x[0]):
        try:
            rad.extend(fn(a) or [])
        except Exception as e:
            a.varningar.append(f"rådregeln {fn.__name__} kraschade: {e}")
    rad.sort(key=lambda r: (-r.sparar, r.valfritt))
    return rad


# ══════════════════════════════════════════════════════════════════════════════
#  RAPPORT I TERMINALEN
# ══════════════════════════════════════════════════════════════════════════════

KATEGORINAMN = {
    "dokument": "Dokument (HTML)",
    "skript": "Skript (JS)",
    "stilmall": "Stilmallar (CSS)",
    "bild": "Bilder",
    "typsnitt": "Typsnitt",
    "media": "Video/ljud",
    "data": "Data/API",
    "övrigt": "Övrigt",
}
KATEGORIORDNING = ["typsnitt", "bild", "skript", "dokument", "stilmall", "media", "data", "övrigt"]


def stapel(andel: float, bredd: int = 24) -> str:
    fylld = int(round(andel * bredd))
    return "█" * fylld + "·" * (bredd - fylld)


def skriv_rapport(a: Analys, rad: list[Rad], visa_alla: bool) -> None:
    W = 78
    p = print
    p("")
    p("═" * W)
    p(f"  ANNONSVIKT {VERSION} · {a.kalla}")
    p("═" * W)

    if a.misslyckande:
        p("")
        p("  MÄTNINGEN GAV INGEN ANNONS")
        p("  " + "─" * (W - 4))
        p(f"  {a.misslyckande}")
        p("")
        p("  Så här anges en annons:")
        p("      annonsvikt bb6a6b2536dcc")
        p("      annonsvikt https://embed.bannerboo.com/bb6a6b2536dcc")
        p("")
        p("  Ska en hel sida genomsökas efter annonser anges sidans adress i stället:")
        p("      annonsvikt upphandling24.se")
        p("")
        p("  Ett vanligt misstag är att adressen råkat bli hopklistrad, till exempel")
        p("  embed.bannerboo.com/embed.bannerboo.com/… — kontrollera att den ser rätt ut.")
        p("")
        return

    delar = []
    if a.bredd and a.hojd:
        delar.append(f"{a.bredd} × {a.hojd} px")
    if a.visningstyp:
        delar.append(a.visningstyp)
    if a.anim_sekunder:
        loop = "oändlig loop" if a.anim_loopar == 0 else f"{a.anim_loopar} loopar"
        delar.append(f"{str(round(a.anim_sekunder, 1)).replace('.', ',')} s, {loop}")
    if delar:
        p(f"  Format     : {'  ·  '.join(delar)}")
    if a.lager:
        typer: dict[str, int] = {}
        for l in a.lager:
            typer[l["typ"]] = typer.get(l["typ"], 0) + 1
        dolda = sum(1 for l in a.lager if not l["synligt"])
        txt = ", ".join(f"{n} {t}" for t, n in sorted(typer.items(), key=lambda x: -x[1]))
        p(f"  Lager      : {len(a.lager)} ({txt})" + (f", varav {dolda} dolt" if dolda else ""))
    p(f"  Mätt       : {a.tidpunkt} · headless Chromium · tom cache")
    p(f"  Förfrågningar: {len(a.resurser)}")

    # ── Vikt per kategori ────────────────────────────────────────────────────
    p("")
    p("  VIKT PER DEL")
    p("  " + "─" * (W - 4))
    grupper: dict[str, list[Resurs]] = {}
    for r in a.resurser:
        grupper.setdefault(r.kategori, []).append(r)
    total = a.totalvikt or 1
    for kat in KATEGORIORDNING:
        rs = grupper.get(kat)
        if not rs:
            continue
        vikt = sum(x.storlek for x in rs)
        formater = sorted({x.underformat for x in rs if x.underformat})
        etikett = KATEGORINAMN[kat]
        if formater and kat in ("bild", "typsnitt"):
            etikett += f" ({', '.join(formater)})"
        p(
            f"  {etikett:<28} {stapel(vikt / total)} {fmt(vikt):>10}  "
            f"{procent(vikt, total):>7}  {len(rs):>2} st"
        )
    p("  " + "─" * (W - 4))
    p(f"  {'TOTALT':<28} {' ' * 24} {fmt(a.totalvikt):>10}  {'100 %':>7}  {len(a.resurser):>2} st")
    if a.totalt_uppackat > a.totalvikt:
        p(f"  {'(uppackat i minnet)':<28} {' ' * 24} {fmt(a.totalt_uppackat):>10}")

    # ── Betyg ────────────────────────────────────────────────────────────────
    bokstav, motivering = satt_betyg(a.totalvikt)
    p("")
    p("  " + "─" * (W - 4))
    p(f"  BETYG: {bokstav}   {motivering}")
    kvot = a.totalvikt / IAB_INITIAL
    p(
        f"  IAB-budget initial laddning: {fmt(IAB_INITIAL)} — annonsen ligger på "
        f"{str(round(kvot, 1)).replace('.', ',')}× budgeten"
    )
    p("  " + "─" * (W - 4))

    # ── Tyngsta enskilda filer ───────────────────────────────────────────────
    p("")
    p("  TYNGSTA FILER")
    p("  " + "─" * (W - 4))
    p(f"  {'Fil':<34}{'Typ':<10}{'Vikt':>10}  {'Andel':>7}  Anmärkning")
    sorterade = sorted(a.resurser, key=lambda r: -r.storlek)
    visade = sorterade if visa_alla else sorterade[:12]
    for r in visade:
        anm = []
        if r.dold_orsak:
            anm.append(f"DOLD ({r.dold_orsak})")
        if r.overdim_faktor and r.overdim_faktor > 1.15:
            anm.append(f"{r.nat_b}×{r.nat_h}→{r.vis_b}×{r.vis_h} px")
        if r.text_utan_komprimering:
            anm.append("okomprimerad")
        if r.bibliotek:
            anm.append(r.bibliotek)
        if r.sparning:
            anm.append("spårning")
        if r.tredjepart and not r.bibliotek:
            anm.append("extern värd")
        if r.status >= 400:
            anm.append(f"HTTP {r.status}")
        p(
            f"  {r.filnamn:<34}{(r.underformat or r.kategori):<10}{fmt(r.storlek):>10}  "
            f"{procent(r.storlek, total):>7}  {', '.join(anm)}"
        )
    if not visa_alla and len(sorterade) > 12:
        rest = sorterade[12:]
        p(f"  {'… ' + str(len(rest)) + ' övriga filer':<34}{'':<10}{fmt(sum(r.storlek for r in rest)):>10}")
        p("  (kör med --alla för hela listan)")

    # ── Råd ──────────────────────────────────────────────────────────────────
    p("")
    p("═" * W)
    p("  RÅD — SORTERADE EFTER EFFEKT")
    p("═" * W)
    märke = {"kritisk": "!!!", "hög": "!! ", "medel": "!  ", "låg": "   "}
    for i, r in enumerate(rad, 1):
        vinst = f"−{fmt(r.sparar)}" if r.sparar else "—"
        extra = "  (större ingrepp)" if r.valfritt else ""
        p("")
        p(f"  {i}. {märke.get(r.allvar, '   ')} {r.rubrik}   [{vinst}]{extra}")
        p(f"      {r.varfor}")
        for steg in r.gor:
            p(f"      · {steg}")

    # ── Prognos ──────────────────────────────────────────────────────────────
    p("")
    p("═" * W)
    mal = a.potentialvikt
    valfri_vinst = sum(r.sparar for r in rad if r.valfritt)
    ny_bokstav, _ = satt_betyg(mal)
    p("  OM RÅDEN GENOMFÖRS")
    p("  " + "─" * (W - 4))
    p(f"  Nu:              {fmt(a.totalvikt):>10}   betyg {satt_betyg(a.totalvikt)[0]}")
    p(
        f"  Efter åtgärd:    {fmt(mal):>10}   betyg {ny_bokstav}   "
        f"(−{procent(a.totalvikt - mal, a.totalvikt)})"
    )
    if valfri_vinst:
        med_valfritt = max(mal - valfri_vinst, 0)
        p(
            f"  Med de större ingreppen: {fmt(med_valfritt):>10}   "
            f"betyg {satt_betyg(med_valfritt)[0]}"
        )
    p("═" * W)
    if a.konsolfel:
        p("")
        p("  KONSOLMEDDELANDEN UNDER MÄTNINGEN")
        for k in a.konsolfel[:5]:
            p(f"    {k[:110]}")
    if a.varningar:
        p("")
        for v in a.varningar:
            p(f"  varning: {v}")
    p("")


# ══════════════════════════════════════════════════════════════════════════════
#  HTML-RAPPORT
# ══════════════════════════════════════════════════════════════════════════════

HTML_MALL = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Annonsvikt — __KALLA__</title>
<style>
  :root{--bg:#fbfaf8;--kort:#fff;--text:#1c1a17;--svag:#6b6560;--linje:#e6e1da;
        --a:#1f7a4d;--b:#5c8a2f;--c:#c08a1e;--d:#c85a2b;--f:#b8342a;--accent:#2b5f8f}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:940px;margin:0 auto;padding:32px 20px 80px}
  h1{font-size:26px;margin:0 0 4px} h2{font-size:17px;margin:34px 0 12px;
     text-transform:uppercase;letter-spacing:.08em;color:var(--svag)}
  .meta{color:var(--svag);font-size:14px;margin-bottom:24px}
  .kort{background:var(--kort);border:1px solid var(--linje);border-radius:10px;padding:20px;margin-bottom:16px}
  .betyg{display:flex;align-items:center;gap:20px}
  .bok{font-size:60px;font-weight:700;line-height:1;width:88px;height:88px;border-radius:12px;
       display:flex;align-items:center;justify-content:center;color:#fff}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;font-weight:600;color:var(--svag);border-bottom:1px solid var(--linje);padding:8px 6px}
  td{padding:8px 6px;border-bottom:1px solid var(--linje);vertical-align:top}
  td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .bar{height:8px;background:var(--linje);border-radius:4px;overflow:hidden;min-width:90px}
  .bar i{display:block;height:100%;background:var(--accent)}
  .rad{border-left:3px solid var(--linje);padding:2px 0 2px 16px;margin:18px 0}
  .rad.kritisk{border-color:var(--f)} .rad.hög{border-color:var(--d)}
  .rad.medel{border-color:var(--c)} .rad.låg{border-color:var(--linje)}
  .rad h3{margin:0 0 4px;font-size:16px}
  .vinst{font-variant-numeric:tabular-nums;background:#eef3ee;color:var(--a);
         border-radius:5px;padding:1px 7px;font-size:13px;font-weight:600;margin-left:8px}
  .rad ul{margin:8px 0 0;padding-left:18px;color:#3b3733} .rad li{margin:3px 0}
  .varfor{color:var(--svag)}
  .flagga{font-size:12px;background:#f2ede6;border-radius:4px;padding:1px 6px;margin-right:4px;white-space:nowrap}
  .dold{background:#fbe6e2;color:#9c2f24}
  .prognos{display:flex;gap:28px;flex-wrap:wrap}
  .prognos div{flex:1;min-width:150px}
  .stor{font-size:24px;font-weight:700}
  footer{color:var(--svag);font-size:13px;margin-top:40px;border-top:1px solid var(--linje);padding-top:14px}
  .annonsrubrik{font-size:20px;text-transform:none;letter-spacing:0;color:var(--text);
     margin-top:52px;padding-top:22px;border-top:2px solid var(--linje)}
  .sidkort{display:flex;gap:32px;flex-wrap:wrap;align-items:flex-start}
  .sidkort>div{min-width:165px}
  .btg{display:inline-block;width:23px;height:23px;border-radius:5px;color:#fff;
       text-align:center;font-weight:700;font-size:13px;line-height:23px}
  .delad{color:var(--a);font-weight:600}
</style></head><body><div class="wrap">
__INNEHALL__
</div></body></html>"""


def html_innehall(a: Analys, rad: list[Rad], rubrik: str | None = None) -> list[str]:
    """Rapportens kropp. Bruten ur html_rapport så att sidrapporten kan bädda in en
    sektion per annons utan att upprepa hela dokumentet."""
    e = lambda s: htmlmod.escape(str(s))
    bokstav, motivering = satt_betyg(a.totalvikt)
    farg = {"A": "var(--a)", "B": "var(--b)", "C": "var(--c)", "D": "var(--d)", "F": "var(--f)"}[bokstav]
    u = []
    u.append("<h1>Annonsvikt</h1>" if rubrik is None else f"<h2 class='annonsrubrik'>{e(rubrik)}</h2>")
    if a.misslyckande:
        u.append(
            f'<div class="meta"><a href="{e(a.kalla)}">{e(a.kalla)}</a></div>'
            "<div class='kort'><b>Mätningen gav ingen annons.</b><br>"
            + e(a.misslyckande)
            + "<br><br>Så här anges en annons: <code>bb6a6b2536dcc</code> eller "
            "<code>https://embed.bannerboo.com/bb6a6b2536dcc</code>.<br>"
            "Ska en hel sida genomsökas anges sidans adress i stället.</div>"
        )
        return u

    rubrikdelar = []
    if a.bredd:
        rubrikdelar.append(f"{a.bredd} × {a.hojd} px")
    if a.visningstyp:
        rubrikdelar.append(a.visningstyp)
    if a.anim_sekunder:
        loop = "oändlig loop" if a.anim_loopar == 0 else f"{a.anim_loopar} loopar"
        rubrikdelar.append(f"{a.anim_sekunder:.1f} s · {loop}".replace(".", ","))
    u.append(
        f'<div class="meta"><a href="{e(a.kalla)}">{e(a.kalla)}</a><br>'
        f'{e(" · ".join(rubrikdelar))}<br>Mätt {e(a.tidpunkt)} i headless Chromium med tom cache · '
        f"{len(a.resurser)} förfrågningar</div>"
    )

    u.append('<div class="kort betyg">')
    u.append(f'<div class="bok" style="background:{farg}">{bokstav}</div>')
    kvot = f"{a.totalvikt / IAB_INITIAL:.1f}".replace(".", ",")
    u.append(
        f"<div><div class='stor'>{e(fmt(a.totalvikt))}</div><div>{e(motivering)}</div>"
        f"<div class='varfor'>IAB:s budget för initial laddning är {e(fmt(IAB_INITIAL))} — "
        f"annonsen ligger på {kvot}× det.</div></div>"
    )
    u.append("</div>")

    # kategorier
    u.append("<h2>Vikt per del</h2><div class='kort'><table>")
    u.append("<tr><th>Del</th><th></th><th class='n'>Vikt</th><th class='n'>Andel</th><th class='n'>Filer</th></tr>")
    grupper: dict[str, list[Resurs]] = {}
    for r in a.resurser:
        grupper.setdefault(r.kategori, []).append(r)
    total = a.totalvikt or 1
    for kat in KATEGORIORDNING:
        rs = grupper.get(kat)
        if not rs:
            continue
        vikt = sum(x.storlek for x in rs)
        formater = sorted({x.underformat for x in rs if x.underformat})
        namn = KATEGORINAMN[kat] + (f" ({', '.join(formater)})" if kat in ("bild", "typsnitt") and formater else "")
        u.append(
            f"<tr><td>{e(namn)}</td><td><div class='bar'><i style='width:{100 * vikt / total:.1f}%'></i></div></td>"
            f"<td class='n'>{e(fmt(vikt))}</td><td class='n'>{e(procent(vikt, total))}</td>"
            f"<td class='n'>{len(rs)}</td></tr>"
        )
    u.append(
        f"<tr><td><b>Totalt</b></td><td></td><td class='n'><b>{e(fmt(a.totalvikt))}</b></td>"
        f"<td class='n'>100 %</td><td class='n'><b>{len(a.resurser)}</b></td></tr>"
    )
    u.append("</table></div>")

    # filer
    u.append("<h2>Alla filer</h2><div class='kort'><table>")
    u.append("<tr><th>Fil</th><th>Typ</th><th class='n'>Vikt</th><th>Anmärkning</th></tr>")
    for r in sorted(a.resurser, key=lambda r: -r.storlek):
        flaggor = []
        if r.dold_orsak:
            flaggor.append(f"<span class='flagga dold'>dold: {e(r.dold_orsak)}</span>")
        if r.overdim_faktor and r.overdim_faktor > 1.15:
            flaggor.append(
                f"<span class='flagga'>{r.nat_b}×{r.nat_h} → {r.vis_b}×{r.vis_h} px</span>"
            )
        if r.text_utan_komprimering:
            flaggor.append("<span class='flagga'>okomprimerad</span>")
        if r.bibliotek:
            flaggor.append(f"<span class='flagga'>{e(r.bibliotek)}</span>")
        if r.sparning:
            flaggor.append("<span class='flagga'>spårning</span>")
        if r.tredjepart and not r.bibliotek:
            flaggor.append("<span class='flagga'>extern värd</span>")
        if not r.cachebar:
            flaggor.append(f"<span class='flagga'>cache: {e(r.cache_info)}</span>")
        u.append(
            f"<tr><td><span title='{e(r.url)}'>{e(r.filnamn)}</span></td>"
            f"<td>{e(r.underformat or r.kategori)}</td><td class='n'>{e(fmt(r.storlek))}</td>"
            f"<td>{''.join(flaggor)}</td></tr>"
        )
    u.append("</table></div>")

    # råd
    u.append("<h2>Råd</h2>")
    for i, r in enumerate(rad, 1):
        vinst = f"<span class='vinst'>−{e(fmt(r.sparar))}</span>" if r.sparar else ""
        extra = " <span class='flagga'>större ingrepp</span>" if r.valfritt else ""
        steg = "".join(f"<li>{e(s)}</li>" for s in r.gor)
        u.append(
            f"<div class='rad {r.allvar}'><h3>{i}. {e(r.rubrik)}{vinst}{extra}</h3>"
            f"<div class='varfor'>{e(r.varfor)}</div><ul>{steg}</ul></div>"
        )

    mal = a.potentialvikt
    valfri = sum(r.sparar for r in rad if r.valfritt)
    u.append("<h2>Om råden genomförs</h2><div class='kort prognos'>")
    u.append(f"<div><div class='varfor'>Idag</div><div class='stor'>{e(fmt(a.totalvikt))}</div><div>betyg {satt_betyg(a.totalvikt)[0]}</div></div>")
    u.append(f"<div><div class='varfor'>Efter åtgärd</div><div class='stor'>{e(fmt(mal))}</div><div>betyg {satt_betyg(mal)[0]} · −{e(procent(a.totalvikt - mal, a.totalvikt))}</div></div>")
    if valfri:
        mv = max(mal - valfri, 0)
        u.append(f"<div><div class='varfor'>Med större ingrepp</div><div class='stor'>{e(fmt(mv))}</div><div>betyg {satt_betyg(mv)[0]}</div></div>")
    u.append("</div>")
    return u


FOTNOT = (
    f"<footer>Mätt med Annonsvikt {VERSION} — riktig headless Chromium, tom cache, "
    "alla nätverkssvar inspelade. Besparingar är uppskattningar baserade på "
    "typiska konverteringsvinster.</footer>"
)


def html_rapport(a: Analys, rad: list[Rad]) -> str:
    u = html_innehall(a, rad) + [FOTNOT]
    return HTML_MALL.replace("__KALLA__", htmlmod.escape(a.kalla)).replace(
        "__INNEHALL__", "\n".join(u)
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ANALYS AV EN ENSKILD ANNONS
# ══════════════════════════════════════════════════════════════════════════════


def analysera(url: str, args) -> tuple[Analys, list[Rad], bytes | None]:
    lage, kropp, rader, sonder, konsol, bild, navfel = mat_i_webblasare(
        url, args.vantetid, args.huvud, args.bredd, args.hojd, args.tyst, args
    )
    a = bygg_analys(url, lage, kropp, rader, sonder, konsol, navfel)
    rad = samla_rad(a)
    return a, rad, bild


def analys_till_dict(a: Analys, rad: list[Rad]) -> dict:
    """Mätningen som JSON-vänlig struktur."""
    d = asdict(a)
    d["typsnitt_laddade"] = sorted(a.typsnitt_laddade)
    d["animationstyper"] = sorted(a.animationstyper)
    d["totalvikt"] = a.totalvikt
    d["potentialvikt"] = a.potentialvikt
    d["betyg"] = satt_betyg(a.totalvikt)[0]
    d["rad"] = [asdict(r) for r in rad]
    return d


# ══════════════════════════════════════════════════════════════════════════════
#  SIDSKANNING — hitta alla BannerBoo-annonser på en sida och mät dem
# ══════════════════════════════════════════════════════════════════════════════
#
#  Annonserna injiceras av JavaScript (Advanced Ads, postscribe) och roterar mellan
#  sidladdningar. Därför laddas sidan i en riktig webbläsare flera varv i samma
#  session — rotationen styrs av en kaka och avancerar bara om sessionen behålls.
#  Varje funnen annons mäts sedan isolerat med analysera() och tom cache, så att
#  siffrorna blir jämförbara med en enskild mätning.
#
# ══════════════════════════════════════════════════════════════════════════════

# Ett BannerBoo-id är hexadecimalt. Kravet gör att /assets/render.min.js inte matchar.
MONSTER_LADDARE = re.compile(r"embed\.bannerboo\.com/([0-9a-f]{8,20})(?:[?#]|$)", re.I)
MONSTER_IFRAME = re.compile(r"embed\.bannerboo\.com/embed/[^/]+/[^/]+/([0-9a-f]{8,20})/", re.I)

# Delad hjälpfil som alla responsiva annonser på en sida hämtar en gång.
RENDERAREN = "assets/render.min.js"


@dataclass
class Annonsfynd:
    """En annons som hittats på sidan, innan den mätts."""

    id: str
    laddar_url: str = ""
    iframe_url: str = ""
    bredd: int = 0
    hojd: int = 0
    topp_px: int = 0
    ovanfor_veck: bool = False
    plats: str = ""
    responsive: bool = False
    varv_sedd: set = field(default_factory=set)

    @property
    def matning_url(self) -> str:
        """URL:en som ska mätas — helst den exakta som sidan använde."""
        if self.laddar_url:
            return self.laddar_url
        fraga = "?responsive=1" if self.responsive else ""
        return f"https://embed.bannerboo.com/{self.id}{fraga}"

    @property
    def format(self) -> str:
        return f"{self.bredd}×{self.hojd}" if self.bredd else "—"


@dataclass
class Sidanalys:
    url: str
    tidpunkt: str = ""
    varv: int = 1
    stabil: bool = False  # samma annonser i två varv i rad → platsen roterar inte
    andra_iframes: list = field(default_factory=list)  # värdar för iframes som inte är BannerBoo
    poster: list = field(default_factory=list)  # (Annonsfynd, Analys, list[Rad])
    bilder: dict = field(default_factory=dict)  # annons-id → skärmbild som PNG-byte
    sidrad: list = field(default_factory=list)
    samtyckesknapp: str = ""
    banderoll_sedd: bool = False
    sidhojd: int = 0
    varningar: list = field(default_factory=list)

    @property
    def summa_var_for_sig(self) -> int:
        """Vad annonserna väger om var och en mäts för sig — dagens siffra."""
        return sum(a.totalvikt for _, a, _ in self.poster)

    def delning(self):
        """Union över resurs-URL:er. Varje fil räknas full vikt första gången den ses.

        Returnerar (unik vikt, lista över delade filer sorterad efter besparing)."""
        sedd: dict[str, list] = {}
        unik = 0
        for _, analys, _ in self.poster:
            for r in analys.resurser:
                if r.url in sedd:
                    sedd[r.url][1] += 1
                else:
                    sedd[r.url] = [r.storlek, 1]
                    unik += r.storlek
        delade = [
            (url, storlek, antal) for url, (storlek, antal) in sedd.items() if antal > 1
        ]
        delade.sort(key=lambda x: -(x[1] * (x[2] - 1)))
        return unik, delade

    @property
    def delad_vikt(self) -> int:
        return self.delning()[0]

    @property
    def vinst_av_delning(self) -> int:
        return self.summa_var_for_sig - self.delad_vikt


# ── Samtyckesbanderoll ────────────────────────────────────────────────────────

SAMTYCKE_JS = r"""
() => {
  const synlig = el => el && el.offsetParent !== null &&
        el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
  const beskriv = el => el.tagName.toLowerCase() +
        (el.id ? '#' + el.id : '') +
        (el.className ? '.' + String(el.className).trim().split(/\s+/).join('.') : '') +
        ' — "' + (el.textContent || el.value || '').trim().slice(0, 40) + '"';

  // Kända "tillåt allt"-knappar först — de är entydiga.
  const specifika = ['.cc-allowall', '.cc-btn.cc-allow', '#cc-approve-button-thissite',
                     '[class*="allowall"]', '[class*="allow-all"]', '[class*="accept-all"]',
                     '[id*="accept-all"]', '[id*="acceptAll"]', '[class*="acceptAll"]'];
  for (const s of specifika) {
    let träff = null;
    try { träff = [...document.querySelectorAll(s)].find(synlig); } catch (e) {}
    if (träff) {
      träff.setAttribute('data-annonsvikt-samtycke', '1');
      return { hittad: true, beskrivning: beskriv(träff), banderoll: true };
    }
  }

  // Annars: knapp med rätt text, men bara inuti en samtyckesbehållare — annars
  // riskerar vi att klicka på ett "OK" någon helt annanstans på sidan.
  const behallare = [...document.querySelectorAll(
      '[class*="cc-"],[id*="cookie"],[class*="cookie"],[class*="consent"],' +
      '[id*="consent"],[class*="cmp"],[class*="gdpr"],[id*="gdpr"]')].filter(synlig);
  const monster = /tillåt alla|acceptera alla|godkänn alla|jag samtycker|godkänn|acceptera|allow all|accept all|tillåt/i;
  for (const b of behallare) {
    const knapp = [...b.querySelectorAll('a,button,input[type=button],input[type=submit],[role=button]')]
      .filter(synlig)
      .find(e => monster.test((e.textContent || e.value || '')));
    if (knapp) {
      knapp.setAttribute('data-annonsvikt-samtycke', '1');
      return { hittad: true, beskrivning: beskriv(knapp), banderoll: true };
    }
  }
  return { hittad: false, banderoll: behallare.length > 0 };
}
"""


def godkann_samtycke(sida) -> tuple[str, bool]:
    """Klickar i "tillåt allt" så att annonserna inte hålls tillbaka.

    Returnerar (beskrivning av knappen, om en banderoll alls syntes)."""
    try:
        svar = sida.evaluate(SAMTYCKE_JS)
    except Exception:
        return "", False
    if not svar or not svar.get("hittad"):
        return "", bool(svar and svar.get("banderoll"))

    beskrivning = svar.get("beskrivning", "")
    try:
        sida.click("[data-annonsvikt-samtycke]", timeout=5000)
    except Exception:
        # Banderollen kan ligga under ett overlay — klicka via DOM i stället.
        try:
            sida.evaluate("document.querySelector('[data-annonsvikt-samtycke]').click()")
            beskrivning += "  (JS-klick)"
        except Exception:
            return "", True
    sida.wait_for_timeout(1200)  # låt annonsskripten som samtycket låser upp starta
    return beskrivning, True


# ── Sond som läser annonsplatserna ur den renderade sidan ────────────────────

SIDSOND = r"""
() => {
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  const hexId = /^[0-9a-f]{8,20}$/i;   // BannerBoos egen wrapper, inte en annonsplats
  const plats = el => {
    let e = el.parentElement;
    while (e && e !== document.body) {
      if (e.id && !hexId.test(e.id)) return '#' + e.id;
      e = e.parentElement;
    }
    return '';
  };
  const ut = [];
  document.querySelectorAll('iframe[src*="bannerboo"]').forEach(f => {
    const r = f.getBoundingClientRect();
    ut.push({ typ: 'iframe', url: abs(f.getAttribute('src') || f.src),
              b: Math.round(r.width), h: Math.round(r.height),
              topp: Math.round(r.top + window.scrollY), plats: plats(f) });
  });
  document.querySelectorAll('script[src*="bannerboo"]').forEach(s => {
    ut.push({ typ: 'skript', url: abs(s.getAttribute('src') || s.src),
              b: 0, h: 0, topp: 0, plats: plats(s) });
  });
  const andra = [...document.querySelectorAll('iframe')]
    .filter(f => !/bannerboo/i.test(f.getAttribute('src') || ''))
    .map(f => { try { return new URL(f.src, location.href).hostname; } catch (e) { return ''; } })
    .filter(Boolean);
  return { element: ut, veck: window.innerHeight, sidhojd: document.body.scrollHeight,
           andra_iframes: [...new Set(andra)] };
}
"""


def annons_id(url: str) -> str:
    """Plockar ut BannerBoo-id ur en laddar- eller iframe-URL."""
    if RENDERAREN in url:
        return ""
    for monster in (MONSTER_IFRAME, MONSTER_LADDARE):
        m = monster.search(url)
        if m:
            return m.group(1).lower()
    return ""


def rulla_igenom(sida, steg: int = 900, max_steg: int = 40) -> None:
    """Scrollar igenom sidan så att lazy-laddade annonser hinner triggas."""
    try:
        hojd = int(sida.evaluate("document.body.scrollHeight") or 0)
    except Exception:
        return
    y, n = 0, 0
    while y < hojd and n < max_steg:
        y += steg
        n += 1
        try:
            sida.evaluate(f"window.scrollTo(0, {y})")
        except Exception:
            break
        sida.wait_for_timeout(140)
    try:
        sida.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    sida.wait_for_timeout(400)


def samla_fynd(fynd: dict, traffar: list, dom: dict, varv: int) -> None:
    """Slår ihop nätverksträffar och DOM-sond till Annonsfynd."""
    for url in traffar:
        aid = annons_id(url)
        if not aid:
            continue
        f = fynd.setdefault(aid, Annonsfynd(id=aid))
        f.varv_sedd.add(varv)
        if MONSTER_IFRAME.search(url):
            f.iframe_url = f.iframe_url or url
        else:
            f.laddar_url = f.laddar_url or url
        if "responsive=1" in url:
            f.responsive = True

    for el in dom.get("element", []):
        url = el.get("url", "")
        aid = annons_id(url)
        if not aid:
            continue
        f = fynd.setdefault(aid, Annonsfynd(id=aid))
        f.varv_sedd.add(varv)
        if "responsive=1" in url:
            f.responsive = True
        if el.get("typ") == "skript":
            f.laddar_url = url  # exakt URL med query, den vi helst mäter
            if not f.plats:
                f.plats = el.get("plats", "")
            continue
        f.iframe_url = url
        if el.get("b"):
            f.bredd, f.hojd = int(el["b"]), int(el["h"])
        if el.get("topp"):
            f.topp_px = int(el["topp"])
            f.ovanfor_veck = f.topp_px < int(dom.get("veck") or 0)
        if el.get("plats"):
            f.plats = el["plats"]


def skanna_sida(url: str, args):
    """Laddar sidan `args.varv` gånger i samma session och samlar alla BannerBoo-annonser.

    Sessionen delas mellan varven med flit: annonsrotationen styrs av en kaka, så en
    ny kontext per varv skulle ge samma annons om och om igen. Varje annons mäts
    däremot isolerat efteråt, med tom cache."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as fel:
        # Aldrig sys.exit() här: anropas det från en arbetstråd blir det ett
        # SystemExit som inte fångas av "except Exception", och tråden dör tyst.
        raise RuntimeError(SAKNAS_MEDDELANDE) from fel

    fynd: dict[str, Annonsfynd] = {}
    samtyckesknapp, banderoll, sidhojd = "", False, 0
    varningar: list[str] = []
    forra_uppsattningen: set | None = None
    korda_varv, stabil = 0, False
    andra_iframes: set = set()
    vill_samtycka = not getattr(args, "utan_samtycke", False)

    traffar: list[str] = []

    with sync_playwright() as p:
        webblasare = p.chromium.launch(headless=not args.huvud)
        # EN kontext för hela skanningen. Annonsrotationen i Advanced Ads styrs av en
        # kaka — med ny kontext per varv nollställs räknaren och samma annons kommer
        # tillbaka varje gång. En besökare som laddar om sidan får nästa annons, och
        # det är beteendet vi vill efterlikna.
        kontext = webblasare.new_context(
            viewport={"width": args.bredd, "height": args.hojd},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        kontext.on("request", lambda begaran: traffar.append(begaran.url))
        sida = kontext.new_page()

        for varv in range(1, max(1, args.varv) + 1):
            beratta(args, f"varv {varv} av {args.varv}: laddar sidan …")
            traffar.clear()
            try:
                sida.goto(url, wait_until="load", timeout=60000)
            except Exception as fel:
                varningar.append(f"varv {varv}: sidan kunde inte laddas ({fel})")
                continue

            if vill_samtycka:
                # Efter första varvet ligger samtycket i en kaka och banderollen
                # visas inte igen — då returnerar den här tomt, vilket är rätt.
                beratta(args, f"varv {varv} av {args.varv}: söker samtyckesbanderoll …")
                knapp, sedd = godkann_samtycke(sida)
                banderoll = banderoll or sedd
                if knapp and not samtyckesknapp:
                    samtyckesknapp = knapp

            beratta(args, f"varv {varv} av {args.varv}: scrollar igenom sidan …")
            rulla_igenom(sida)
            beratta(args, f"varv {varv} av {args.varv}: väntar in annonserna …")
            try:
                sida.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            sida.wait_for_timeout(int(min(args.vantetid, 6) * 1000))

            dom = {}
            try:
                dom = sida.evaluate(SIDSOND)
            except Exception as fel:
                varningar.append(f"varv {varv}: sidsonden misslyckades ({fel})")
            sidhojd = max(sidhojd, int(dom.get("sidhojd") or 0))
            andra_iframes.update(dom.get("andra_iframes") or [])
            samla_fynd(fynd, traffar, dom, varv)
            korda_varv = varv
            beratta(
                args,
                f"varv {varv} av {args.varv}: "
                + antal(len(fynd), "annons hittad hittills", "annonser hittade hittills"),
            )

            # Ger två varv i rad exakt samma annonser roterar platsen inte, och
            # fler varv tillför ingenting utom väntetid.
            nu = {f.id for f in fynd.values() if varv in f.varv_sedd}
            if nu and nu == forra_uppsattningen:
                stabil = True
                beratta(args, "samma annonser två varv i rad — annonsvalet är stabilt")
                break
            forra_uppsattningen = nu

        kontext.close()
        webblasare.close()

    return (list(fynd.values()), samtyckesknapp, banderoll, sidhojd, varningar,
            korda_varv, stabil, sorted(andra_iframes))


def analysera_sida(url: str, args) -> Sidanalys:
    """Skannar sidan efter annonser och mäter var och en isolerat."""
    (fynd, knapp, banderoll, sidhojd, varningar,
     korda_varv, stabil, andra_iframes) = skanna_sida(url, args)
    fynd.sort(key=lambda f: (f.topp_px or 10**9, f.id))

    s = Sidanalys(
        url=url,
        tidpunkt=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        varv=korda_varv or max(1, args.varv),
        stabil=stabil,
        andra_iframes=andra_iframes,
        samtyckesknapp=knapp,
        banderoll_sedd=banderoll,
        sidhojd=sidhojd,
        varningar=varningar,
    )
    if not fynd:
        beratta(args, "inga BannerBoo-annonser hittades på sidan")
    for i, f in enumerate(fynd, 1):
        beratta(args, f"mäter annons {i} av {len(fynd)}: {f.id} …")
        try:
            analys, rad, bild = analysera(f.matning_url, args)
        except Exception as fel:
            s.varningar.append(f"annonsen {f.id} kunde inte mätas: {fel}")
            continue
        s.poster.append((f, analys, rad))
        if bild:
            s.bilder[f.id] = bild

    s.sidrad = samla_sidrad(s)
    return s


def ar_annonslank(url: str) -> bool:
    """Är det här en annons hos BannerBoo, eller en sida att skanna?"""
    return urllib.parse.urlparse(url).netloc.lower().endswith("bannerboo.com")


# ══════════════════════════════════════════════════════════════════════════════
#  SIDNIVÅRÅD — REDIGERA HÄR
# ══════════════════════════════════════════════════════════════════════════════
#
#  Samma mönster som RAD_REGLER, men reglerna får hela Sidanalysen och handlar om
#  samspelet mellan annonserna. Lägg till egna med @sidregel(prioritet).
#
# ══════════════════════════════════════════════════════════════════════════════

SIDRAD_REGLER: list[tuple[int, object]] = []


def sidregel(prioritet: int):
    def dekorator(fn):
        SIDRAD_REGLER.append((prioritet, fn))
        return fn

    return dekorator


@sidregel(100)
def sidrad_sidbudget(s: Sidanalys) -> list[Rad]:
    n = len(s.poster)
    if not n:
        return []
    budget = n * IAB_INITIAL
    unik = s.delad_vikt
    if unik <= budget:
        return []
    varsta = max(s.poster, key=lambda p: p[1].totalvikt)
    return [
        Rad(
            rubrik=f"Sidans annonser väger {fmt(unik)} — {fmt(unik - budget)} över budget",
            varfor=(
                f"{antal(n, 'annons', 'annonser')} på sidan ger en budget på {fmt(budget)} "
                f"({n} × {fmt(IAB_INITIAL)}). Besökaren betalar {fmt(unik)} bara för annonserna, "
                "utöver sidans eget innehåll."
            ),
            gor=[
                f"Börja med den tyngsta: {varsta[0].id} väger {fmt(varsta[1].totalvikt)} "
                f"(betyg {satt_betyg(varsta[1].totalvikt)[0]}).",
                "Råden per annons längre ned visar exakt var vikten sitter.",
                "Sätt ett tak i annonsvillkoren — annonsörer levererar det de får leverera.",
            ],
            sparar=0,
            allvar="kritisk" if unik > 2 * budget else "hög",
        )
    ]


@sidregel(90)
def sidrad_lat_ladda(s: Sidanalys) -> list[Rad]:
    under = [(f, a) for f, a, _ in s.poster if f.topp_px and not f.ovanfor_veck]
    if not under:
        return []
    vikt = sum(a.totalvikt for _, a in under)
    return [
        Rad(
            rubrik=f"Skjut upp {antal(len(under), 'annons', 'annonser')} som ligger under vecket",
            varfor=(
                f"{fmt(vikt)} laddas direkt fast annonserna sitter längre ned på sidan och "
                "många besökare aldrig scrollar dit. Vikten konkurrerar med sidans eget "
                "innehåll om bandbredden i det ögonblick det spelar mest roll."
            ),
            gor=[
                "Sätt `loading=\"lazy\"` på annonsens iframe — det räcker långt och kostar "
                "ingen utveckling.",
                "Vill du ha mer kontroll: låt en IntersectionObserver skjuta in annonskoden "
                "när platsen närmar sig visningsytan.",
            ]
            + [
                f"Berör: {f.id} på {f.topp_px} px ned ({fmt(a.totalvikt)})"
                for f, a in under
            ],
            sparar=vikt,
            allvar="hög" if vikt > 300 * KB else "medel",
        )
    ]


@sidregel(85)
def sidrad_tung_ovanfor_veck(s: Sidanalys) -> list[Rad]:
    tunga = [(f, a) for f, a, _ in s.poster if f.ovanfor_veck and a.totalvikt > IAB_INITIAL]
    if not tunga:
        return []
    return [
        Rad(
            rubrik=f"{antal(len(tunga), 'annons', 'annonser')} ovanför vecket är tyngre än budget",
            varfor=(
                "Annonser i första skärmbilden laddas samtidigt som sidans huvudinnehåll och "
                "drar ut på tiden till största innehållselementet ritas (LCP). Det är det "
                "måttet Google väger in i sökresultaten."
            ),
            gor=[
                f"{f.id} ({f.format} px) väger {fmt(a.totalvikt)} — budget är {fmt(IAB_INITIAL)}."
                for f, a in tunga
            ]
            + [
                "Ska en tung annons ligga högst upp bör den åtminstone vara statisk bild "
                "i första bildrutan, med animation och typsnitt efterladdade.",
            ],
            sparar=0,
            allvar="hög",
        )
    ]


@sidregel(80)
def sidrad_typsnittsberg(s: Sidanalys) -> list[Rad]:
    if not s.poster:
        return []
    unika: dict[str, int] = {}
    familjer: set = set()
    for _, analys, _ in s.poster:
        familjer |= analys.typsnitt_laddade
        for r in analys.resurser:
            if r.kategori == "typsnitt":
                unika.setdefault(r.url, r.storlek)
    vikt = sum(unika.values())
    if vikt < 150 * KB:
        return []
    return [
        Rad(
            rubrik=f"Typsnitten är sidans tyngsta annonspost: {fmt(vikt)}",
            varfor=(
                f"Annonserna hämtar tillsammans {antal(len(unika), 'typsnittsfil', 'typsnittsfiler')} "
                f"i {antal(len(familjer), 'familj', 'familjer')}: {', '.join(sorted(familjer))}. "
                "Varje familj är en egen nedladdning som ingen besökare lägger märke till."
            ),
            gor=[
                "Kom överens med annonsörerna om ett par tillåtna snitt — då delar annonserna "
                "nedladdning och sidan betalar för dem en gång.",
                "Kräv woff2 med subsetting i annonsvillkoren; det är den enskilt största "
                "besparingen och kostar inget i utseende.",
                "Ligger annonserna hos samma leverantör kan snitten cachas gemensamt om de "
                "hämtas från samma URL:er.",
            ],
            sparar=0,
            allvar="hög" if vikt > 400 * KB else "medel",
        )
    ]


@sidregel(70)
def sidrad_ingen_delning(s: Sidanalys) -> list[Rad]:
    if len(s.poster) < 2 or s.vinst_av_delning > 20 * KB:
        return []
    return [
        Rad(
            rubrik="Annonserna delar nästan inga resurser",
            varfor=(
                f"Sidans {len(s.poster)} annonser återanvänder bara {fmt(s.vinst_av_delning)} "
                "mellan sig. Varje annons drar med sig sina egna kopior av bibliotek och "
                "typsnitt, trots att de ligger på samma sida."
            ),
            gor=[
                "Låt annonserna hämta gemensamma delar från samma URL:er — då räcker en "
                "nedladdning för hela sidan.",
                "Det gäller särskilt animationsbiblioteket och typsnitten.",
            ],
            sparar=0,
            allvar="medel",
        )
    ]


def samla_sidrad(s: Sidanalys) -> list[Rad]:
    rad: list[Rad] = []
    for _prio, fn in sorted(SIDRAD_REGLER, key=lambda x: -x[0]):
        try:
            rad.extend(fn(s) or [])
        except Exception as fel:
            s.varningar.append(f"sidregeln {fn.__name__} kraschade: {fel}")
    rad.sort(key=lambda r: (-r.sparar, r.valfritt))
    return rad


# ══════════════════════════════════════════════════════════════════════════════
#  SIDRAPPORT I TERMINALEN
# ══════════════════════════════════════════════════════════════════════════════


def skriv_sidrapport(s: Sidanalys, visa_alla: bool) -> None:
    W = 78
    p = print
    p("")
    p("═" * W)
    p(f"  SIDANALYS · {s.url}")
    p("═" * W)
    p(f"  Mätt       : {s.tidpunkt} · {antal(s.varv, 'varv', 'varv')}")
    if s.poster:
        p(
            "  Rotation   : "
            + (
                "stabil — samma annonser i varje varv"
                if s.stabil
                else f"platsen visade olika annonser mellan varven"
            )
        )
    if s.samtyckesknapp:
        p(f"  Samtycke   : klickade {s.samtyckesknapp}")
    elif s.banderoll_sedd:
        p("  Samtycke   : banderoll upptäcktes men ingen knapp för att tillåta allt hittades")
    if s.sidhojd:
        p(f"  Sidhöjd    : {s.sidhojd} px")
    p(f"  Annonser   : {antal(len(s.poster), 'BannerBoo-annons', 'unika BannerBoo-annonser')}")

    if not s.poster:
        p("")
        p("  Inga BannerBoo-annonser hittades på sidan.")
        p("  Verktyget mäter bara annonser som ligger hos BannerBoo. En annonsplats kan")
        p("  just nu innehålla något annat — en bildannons från den egna servern, till")
        p("  exempel — och då finns det inget här att väga.")
        if s.andra_iframes:
            p(f"  Andra iframes på sidan: {', '.join(s.andra_iframes[:5])}")
        if s.banderoll_sedd and not s.samtyckesknapp:
            p("  En samtyckesbanderoll syntes — annonserna kan vara spärrade bakom den.")
        p("  Menade du att mäta URL:en som en annons i sig? Kör med --annons.")
        for v in s.varningar:
            p(f"  varning: {v}")
        p("")
        return

    # ── Annonserna ───────────────────────────────────────────────────────────
    p("")
    p("  ANNONSER PÅ SIDAN")
    p("  " + "─" * (W - 4))
    p(f"  {'Id':<16}{'Visas som':<11}{'Annonsplats':<21}{'Vikt':>10} {'Betyg':>6} {'Varv':>5}")
    for fynd, analys, _ in s.poster:
        plats = (fynd.plats or "—")[:20]
        varv = f"{len(fynd.varv_sedd)}/{s.varv}"
        p(
            f"  {fynd.id:<16}{fynd.format:<11}{plats:<21}"
            f"{fmt(analys.totalvikt):>10} {satt_betyg(analys.totalvikt)[0]:>6} {varv:>5}"
        )
        lage = (
            "ovanför vecket"
            if fynd.ovanfor_veck
            else f"{fynd.topp_px} px ned på sidan"
        )
        p(f"  {'':<16}{lage}")
    p("  " + "─" * (W - 4))
    p(f"  {'Summa var för sig':<48}{fmt(s.summa_var_for_sig):>10}")
    p(f"  {'Faktisk kostnad för besökaren':<48}{fmt(s.delad_vikt):>10}")
    p(f"  {'Vinst av delade resurser':<48}{fmt(s.vinst_av_delning):>10}")

    # ── Delade filer ─────────────────────────────────────────────────────────
    _unik, delade = s.delning()
    if delade:
        p("")
        p("  DELADE FILER — hämtas en gång, används av flera annonser")
        p("  " + "─" * (W - 4))
        for url, storlek, antal_annonser in delade[: (99 if visa_alla else 8)]:
            namn = [d for d in urllib.parse.urlparse(url).path.split("/") if d]
            namn = (namn[-1] if namn else url)[:40]
            p(f"  {namn:<42}{fmt(storlek):>10}   {antal_annonser} annonser")

    # ── Sidnivåråd ───────────────────────────────────────────────────────────
    if s.sidrad:
        p("")
        p("═" * W)
        p("  RÅD FÖR SIDAN SOM HELHET")
        p("═" * W)
        marke = {"kritisk": "!!!", "hög": "!! ", "medel": "!  ", "låg": "   "}
        for i, r in enumerate(s.sidrad, 1):
            vinst = f"−{fmt(r.sparar)}" if r.sparar else "—"
            p("")
            p(f"  {i}. {marke.get(r.allvar, '   ')} {r.rubrik}   [{vinst}]")
            p(f"      {r.varfor}")
            for steg in r.gor:
                p(f"      · {steg}")

    for v in s.varningar:
        p(f"\n  varning: {v}")

    # ── Varje annons för sig ─────────────────────────────────────────────────
    for fynd, analys, rad in s.poster:
        p("")
        p("")
        p("█" * W)
        p(f"  ANNONS {fynd.id}   ·   {fynd.format} px   ·   {fynd.plats or 'okänd plats'}")
        p("█" * W)
        skriv_rapport(analys, rad, visa_alla)


# ══════════════════════════════════════════════════════════════════════════════
#  SIDRAPPORT SOM HTML
# ══════════════════════════════════════════════════════════════════════════════


def html_sidrapport(s: Sidanalys) -> str:
    e = lambda x: htmlmod.escape(str(x))
    farger = {"A": "var(--a)", "B": "var(--b)", "C": "var(--c)", "D": "var(--d)", "F": "var(--f)"}
    u = ["<h1>Annonsvikt — hela sidan</h1>"]

    rader = [antal(s.varv, "varv", "varv")]
    if s.poster:
        rader.append(
            "stabilt annonsval" if s.stabil else "olika annonser mellan varven"
        )
    if s.samtyckesknapp:
        rader.append(f"samtycke: klickade {s.samtyckesknapp}")
    elif s.banderoll_sedd:
        rader.append("samtyckesbanderoll upptäckt, ingen tillåt-allt-knapp hittad")
    u.append(
        f'<div class="meta"><a href="{e(s.url)}">{e(s.url)}</a><br>'
        f"Mätt {e(s.tidpunkt)} · {e(' · '.join(rader))}</div>"
    )

    if not s.poster:
        rader_tom = [
            "Verktyget mäter bara annonser som ligger hos BannerBoo. En annonsplats kan "
            "just nu innehålla något annat — en bildannons från den egna servern, till "
            "exempel — och då finns det inget här att väga."
        ]
        if s.andra_iframes:
            rader_tom.append("Andra iframes på sidan: " + ", ".join(s.andra_iframes[:5]) + ".")
        if s.banderoll_sedd and not s.samtyckesknapp:
            rader_tom.append(
                "En samtyckesbanderoll syntes — annonserna kan vara spärrade bakom den."
            )
        rader_tom.append(
            "Menade du att mäta URL:en som en annons i sig? Kör med <code>--annons</code>."
        )
        u.append(
            "<div class='kort'><b>Inga BannerBoo-annonser hittades på sidan.</b><br>"
            + "<br>".join(rader_tom)
            + "</div>"
        )
        return HTML_MALL.replace("__KALLA__", e(s.url)).replace("__INNEHALL__", "\n".join(u))

    u.append("<div class='kort sidkort'>")
    u.append(
        f"<div><div class='varfor'>Annonser på sidan</div>"
        f"<div class='stor'>{len(s.poster)}</div></div>"
    )
    u.append(
        f"<div><div class='varfor'>Summa var för sig</div>"
        f"<div class='stor'>{e(fmt(s.summa_var_for_sig))}</div></div>"
    )
    u.append(
        f"<div><div class='varfor'>Faktisk kostnad för besökaren</div>"
        f"<div class='stor'>{e(fmt(s.delad_vikt))}</div>"
        f"<div class='delad'>−{e(fmt(s.vinst_av_delning))} genom delade resurser</div></div>"
    )
    u.append("</div>")

    u.append("<h2>Annonserna</h2><div class='kort'><table>")
    u.append(
        "<tr><th>Id</th><th>Visas som</th><th>Annonsplats</th><th>Läge</th>"
        "<th class='n'>Vikt</th><th class='n'>Betyg</th><th class='n'>Varv</th></tr>"
    )
    for fynd, analys, _ in s.poster:
        bok = satt_betyg(analys.totalvikt)[0]
        lage = "ovanför vecket" if fynd.ovanfor_veck else f"{fynd.topp_px} px ned"
        u.append(
            f"<tr><td><a href='#annons-{e(fynd.id)}'>{e(fynd.id)}</a></td>"
            f"<td>{e(fynd.format)}</td><td>{e(fynd.plats or '—')}</td><td>{e(lage)}</td>"
            f"<td class='n'>{e(fmt(analys.totalvikt))}</td>"
            f"<td class='n'><span class='btg' style='background:{farger[bok]}'>{bok}</span></td>"
            f"<td class='n'>{len(fynd.varv_sedd)}/{s.varv}</td></tr>"
        )
    u.append("</table></div>")

    _unik, delade = s.delning()
    if delade:
        u.append("<h2>Delade filer</h2><div class='kort'><table>")
        u.append("<tr><th>Fil</th><th class='n'>Vikt</th><th class='n'>Annonser</th></tr>")
        for url, storlek, antal_annonser in delade[:20]:
            namn = [d for d in urllib.parse.urlparse(url).path.split("/") if d]
            u.append(
                f"<tr><td><span title='{e(url)}'>{e(namn[-1] if namn else url)}</span></td>"
                f"<td class='n'>{e(fmt(storlek))}</td>"
                f"<td class='n'>{antal_annonser}</td></tr>"
            )
        u.append("</table></div>")

    if s.sidrad:
        u.append("<h2>Råd för sidan som helhet</h2>")
        for i, r in enumerate(s.sidrad, 1):
            vinst = f"<span class='vinst'>−{e(fmt(r.sparar))}</span>" if r.sparar else ""
            steg = "".join(f"<li>{e(x)}</li>" for x in r.gor)
            u.append(
                f"<div class='rad {r.allvar}'><h3>{i}. {e(r.rubrik)}{vinst}</h3>"
                f"<div class='varfor'>{e(r.varfor)}</div><ul>{steg}</ul></div>"
            )

    for fynd, analys, rad in s.poster:
        u.append(f"<div id='annons-{e(fynd.id)}'></div>")
        u.extend(
            html_innehall(
                analys,
                rad,
                rubrik=f"Annons {fynd.id} · {fynd.format} px · {fynd.plats or 'okänd plats'}",
            )
        )

    u.append(FOTNOT)
    return HTML_MALL.replace("__KALLA__", e(s.url)).replace("__INNEHALL__", "\n".join(u))


def sida_till_dict(s: Sidanalys) -> dict:
    """Sidanalysen som JSON-vänlig struktur."""
    _unik, delade = s.delning()
    return {
        "sida": s.url,
        "tidpunkt": s.tidpunkt,
        "varv": s.varv,
        "samtyckesknapp": s.samtyckesknapp,
        "banderoll_sedd": s.banderoll_sedd,
        "sidhojd": s.sidhojd,
        "andra_iframes": s.andra_iframes,
        "antal_annonser": len(s.poster),
        "summa_var_for_sig": s.summa_var_for_sig,
        "delad_vikt": s.delad_vikt,
        "vinst_av_delning": s.vinst_av_delning,
        "delade_filer": [
            {"url": url, "storlek": storlek, "annonser": n} for url, storlek, n in delade
        ],
        "sidrad": [asdict(r) for r in s.sidrad],
        "varningar": s.varningar,
        "annonser": [
            {
                "fynd": {**asdict(f), "varv_sedd": sorted(f.varv_sedd)},
                "matning": analys_till_dict(analys, rad),
            }
            for f, analys, rad in s.poster
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════


def satt_utdata_utf8() -> None:
    """Windows-konsolen kör cp1252 som standard och kvävs på ram- och stapeltecken."""
    for strom in (sys.stdout, sys.stderr):
        try:
            strom.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    satt_utdata_utf8()
    try:
        _main()
    except RuntimeError as fel:
        print(f"\n  {fel}\n", file=sys.stderr)
        sys.exit(2)


def _main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Mäter vikten på display-annonser och ger råd om hur de kan bantas. "
            "Peka på en enskild annons, eller på en sida — då hittas alla "
            "BannerBoo-annonser på sidan och mäts var för sig."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exempel:\n"
        "  python annonsvikt.py b990849fd8c4b\n"
        "  python annonsvikt.py upphandling24.se --varv 3 --html sida.html\n"
        "  python annonsvikt.py https://upphandling24.se/debatt/ --utan-samtycke\n",
    )
    ap.add_argument("url", nargs="*", help="annons-URL, BannerBoo-id, eller en sida att skanna")
    ap.add_argument("--vantetid", type=float, default=12.0, help="sekunder att låta annonsen rulla (standard 12)")
    ap.add_argument("--alla", action="store_true", help="lista alla filer, inte bara de tyngsta")
    ap.add_argument("--html", metavar="FIL", help="skriv en HTML-rapport")
    ap.add_argument("--json", metavar="FIL", help="skriv rådata som JSON")
    ap.add_argument("--bild", metavar="FIL", help="spara en skärmbild av annonsen")
    ap.add_argument("--huvud", action="store_true", help="visa webbläsarfönstret (felsökning)")
    ap.add_argument("--bredd", type=int, default=1200, help="fönsterbredd")
    ap.add_argument("--hojd", type=int, default=800, help="fönsterhöjd")
    ap.add_argument("--tyst", action="store_true", help="inga statusrader")
    ap.add_argument("--version", action="version", version=f"Annonsvikt {VERSION}")
    ap.add_argument(
        "--kolla-uppdatering", dest="kolla_uppdatering", action="store_true",
        help="ser efter om en nyare version av Annonsvikt finns",
    )
    ap.add_argument(
        "--varv", type=int, default=3,
        help="antal omladdningar av sidan för att fånga roterande annonser (standard 3)",
    )
    ap.add_argument("--sida", action="store_true", help="tvinga sidskanning")
    ap.add_argument("--annons", action="store_true", help="tvinga mätning av URL:en som en annons")
    ap.add_argument(
        "--utan-samtycke", dest="utan_samtycke", action="store_true",
        help="klicka inte i samtyckesbanderollen (visar vad en besökare som inte godkänner får)",
    )
    args = ap.parse_args()

    if args.kolla_uppdatering:
        import uppdatering

        sys.exit(uppdatering.kolla_fran_kommandoraden(VERSION))

    if not args.url:
        ap.error("ange minst en annons eller sida att mäta")

    resultat = []  # enskilda annonser
    sidor = []  # sidanalyser
    for i, rå in enumerate(args.url):
        url = normalisera_url(rå)
        # Läget avgörs av värden, om inget annat sägs: bannerboo.com är en annons,
        # allt annat är en sida att skanna.
        som_annons = args.annons or (not args.sida and ar_annonslank(url))
        if not args.tyst:
            vad = "mäter annons" if som_annons else "skannar sida"
            print(f"[{i + 1}/{len(args.url)}] {vad} {url}", file=sys.stderr)
        if som_annons:
            a, rad, bild = analysera(url, args)
            skriv_rapport(a, rad, args.alla)
            resultat.append((a, rad, bild))
        else:
            sidanalys = analysera_sida(url, args)
            skriv_sidrapport(sidanalys, args.alla)
            sidor.append(sidanalys)
            resultat.extend((a, rad, None) for _f, a, rad in sidanalys.poster)

    if args.html:
        if sidor:
            text = "\n<hr>\n".join(html_sidrapport(sa) for sa in sidor)
        elif len(resultat) == 1:
            text = html_rapport(*resultat[0][:2])
        else:
            text = "\n<hr>\n".join(html_rapport(a, r) for a, r, _ in resultat)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"HTML-rapport skriven: {args.html}", file=sys.stderr)

    if args.json:
        if sidor:
            ut = [sida_till_dict(sa) for sa in sidor]
        else:
            ut = [analys_till_dict(a, rad) for a, rad, _ in resultat]
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(ut if len(ut) > 1 else (ut[0] if ut else {}), f, ensure_ascii=False, indent=2)
        print(f"JSON skriven: {args.json}", file=sys.stderr)

    if args.bild and resultat and resultat[0][2]:
        with open(args.bild, "wb") as f:
            f.write(resultat[0][2])
        print(f"Skärmbild sparad: {args.bild}", file=sys.stderr)

    if len(resultat) > 1:
        print("\n" + "═" * 78)
        print("  JÄMFÖRELSE — ALLA MÄTTA ANNONSER")
        print("═" * 78)
        print(f"  {'Annons':<44}{'Vikt':>12}  {'Betyg':>6}  {'Möjlig':>10}")
        for a, rad, _ in sorted(
            (r for r in resultat if not r[0].misslyckande), key=lambda x: -x[0].totalvikt
        ):
            print(
                f"  {a.kalla[-44:]:<44}{fmt(a.totalvikt):>12}  "
                f"{satt_betyg(a.totalvikt)[0]:>6}  {fmt(a.potentialvikt):>10}"
            )
        print("")


if __name__ == "__main__":
    main()
