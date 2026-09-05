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


def mat_i_webblasare(url: str, vantetid: float, huvud: bool, bredd: int, hojd: int, tyst: bool):
    """Laddar annonsen och spelar in all nätverkstrafik. Returnerar rådata."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright saknas. Installera med:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium"
        )

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

        if not tyst:
            print(f"  laddar {url} …", file=sys.stderr)
        sida.goto(mal, wait_until="load", timeout=60000)
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

    return lage, forsta_kropp, rader, sonder, konsol, skarmbild


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


def bygg_analys(kalla, lage, forsta_kropp, rader, sonder, konsol) -> Analys:
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
    return a


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
    p(f"  ANNONSVIKT · {a.kalla}")
    p("═" * W)

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
</style></head><body><div class="wrap">
__INNEHALL__
</div></body></html>"""


def html_rapport(a: Analys, rad: list[Rad]) -> str:
    e = lambda s: htmlmod.escape(str(s))
    bokstav, motivering = satt_betyg(a.totalvikt)
    farg = {"A": "var(--a)", "B": "var(--b)", "C": "var(--c)", "D": "var(--d)", "F": "var(--f)"}[bokstav]
    u = []
    u.append(f"<h1>Annonsvikt</h1>")
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
    u.append(
        "<footer>Mätt med annonsvikt.py — riktig headless Chromium, tom cache, "
        "alla nätverkssvar inspelade. Besparingar är uppskattningar baserade på "
        "typiska konverteringsvinster.</footer>"
    )
    return HTML_MALL.replace("__KALLA__", e(a.kalla)).replace("__INNEHALL__", "\n".join(u))


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════


def analysera(url: str, args) -> tuple[Analys, list[Rad], bytes | None]:
    lage, kropp, rader, sonder, konsol, bild = mat_i_webblasare(
        url, args.vantetid, args.huvud, args.bredd, args.hojd, args.tyst
    )
    a = bygg_analys(url, lage, kropp, rader, sonder, konsol)
    rad = samla_rad(a)
    return a, rad, bild


def satt_utdata_utf8() -> None:
    """Windows-konsolen kör cp1252 som standard och kvävs på ram- och stapeltecken."""
    for strom in (sys.stdout, sys.stderr):
        try:
            strom.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    satt_utdata_utf8()
    ap = argparse.ArgumentParser(
        description="Mäter vikten på en display-annons och ger råd om hur den kan bantas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exempel:\n"
        "  python annonsvikt.py https://embed.bannerboo.com/b990849fd8c4b\n"
        "  python annonsvikt.py b990849fd8c4b --html rapport.html --alla\n",
    )
    ap.add_argument("url", nargs="+", help="en eller flera annons-URL:er (eller BannerBoo-id)")
    ap.add_argument("--vantetid", type=float, default=12.0, help="sekunder att låta annonsen rulla (standard 12)")
    ap.add_argument("--alla", action="store_true", help="lista alla filer, inte bara de tyngsta")
    ap.add_argument("--html", metavar="FIL", help="skriv en HTML-rapport")
    ap.add_argument("--json", metavar="FIL", help="skriv rådata som JSON")
    ap.add_argument("--bild", metavar="FIL", help="spara en skärmbild av annonsen")
    ap.add_argument("--huvud", action="store_true", help="visa webbläsarfönstret (felsökning)")
    ap.add_argument("--bredd", type=int, default=1200, help="fönsterbredd")
    ap.add_argument("--hojd", type=int, default=800, help="fönsterhöjd")
    ap.add_argument("--tyst", action="store_true", help="inga statusrader")
    args = ap.parse_args()

    resultat = []
    for i, rå in enumerate(args.url):
        url = normalisera_url(rå)
        if not args.tyst:
            print(f"[{i + 1}/{len(args.url)}] mäter {url}", file=sys.stderr)
        a, rad, bild = analysera(url, args)
        skriv_rapport(a, rad, args.alla)
        resultat.append((a, rad, bild))

    if args.html:
        if len(resultat) == 1:
            text = html_rapport(*resultat[0][:2])
        else:
            text = "\n<hr>\n".join(html_rapport(a, r) for a, r, _ in resultat)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"HTML-rapport skriven: {args.html}", file=sys.stderr)

    if args.json:
        ut = []
        for a, rad, _ in resultat:
            d = asdict(a)
            d["typsnitt_laddade"] = sorted(a.typsnitt_laddade)
            d["animationstyper"] = sorted(a.animationstyper)
            d["totalvikt"] = a.totalvikt
            d["potentialvikt"] = a.potentialvikt
            d["betyg"] = satt_betyg(a.totalvikt)[0]
            d["rad"] = [asdict(r) for r in rad]
            ut.append(d)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(ut if len(ut) > 1 else ut[0], f, ensure_ascii=False, indent=2)
        print(f"JSON skriven: {args.json}", file=sys.stderr)

    if args.bild and resultat and resultat[0][2]:
        with open(args.bild, "wb") as f:
            f.write(resultat[0][2])
        print(f"Skärmbild sparad: {args.bild}", file=sys.stderr)

    if len(resultat) > 1:
        print("\n" + "═" * 78)
        print("  JÄMFÖRELSE")
        print("═" * 78)
        print(f"  {'Annons':<44}{'Vikt':>12}  {'Betyg':>6}  {'Möjlig':>10}")
        for a, rad, _ in sorted(resultat, key=lambda x: -x[0].totalvikt):
            print(
                f"  {a.kalla[-44:]:<44}{fmt(a.totalvikt):>12}  "
                f"{satt_betyg(a.totalvikt)[0]:>6}  {fmt(a.potentialvikt):>10}"
            )
        print("")


if __name__ == "__main__":
    main()
