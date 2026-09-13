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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict

VERSION = "1.5.0"

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

# En bild flaggas när minst så här stor andel av pixlarna aldrig behövs.
BILD_TROSKEL = 0.10
# Så här mycket måste klippas bort för att det ska kallas beskärning.
BESKURET_TROSKEL = 0.02

# Vad ni själva får ut av att komprimera inför uppladdning. WebP räknas inte in —
# det är okänt om BannerBoo tar emot formatet.
FAKTOR_JPEG_KOMPRIMERING = 0.80
FAKTOR_PNG_KOMPRIMERING = 0.60

# Ett par ord konverterade till konturer och sparade som SVG.
SVG_ORD_BYTE = 1500

# Animationen fångas som bildrutor. I en animerad annons är ingen ruta exakt lik
# den förra — en pulserande knapp räcker — så dubbletter går inte att slå ihop.
# Takten hålls därför nere: drygt sex rutor per sekund räcker för toningar och
# förflyttningar, och tio sekunder blir ungefär 4 MB i stället för 9.
FANG_INTERVALL_MS = 150
FANG_MAX_RUTOR = 90
FANG_MAX_S = 15.0

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
#    ansvar      "ni" = görs i BannerBoo, "sajten" = görs i WordPress,
#                "bannerboo" = styrs av BannerBoo och går inte att ändra i annonsen
#    valfritt    kvar för bakåtkompatibilitet, används inte längre
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
    ansvar: str = "ni"



ANSVARSORDNING = ("ni", "sajten", "bannerboo")
ANSVARSRUBRIKER = {
    "ni": "Det här gör ni i BannerBoo",
    "sajten": "Det här gör ni på sajten",
    "bannerboo": "Det här styrs av BannerBoo — ta upp det med dem",
}


def gruppera_rad(rad: list) -> list:
    """Råden i den ordning man agerar på dem: först det man kan göra själv."""
    grupper = []
    for nyckel in ANSVARSORDNING:
        lista = [r for r in rad if getattr(r, "ansvar", "ni") == nyckel]
        if lista:
            grupper.append((ANSVARSRUBRIKER[nyckel], lista))
    ovriga = [r for r in rad if getattr(r, "ansvar", "ni") not in ANSVARSRUBRIKER]
    if ovriga:
        grupper.append(("Övrigt", ovriga))
    return grupper


def _ansvarsnyckel(r) -> tuple:
    ansvar = getattr(r, "ansvar", "ni")
    ordning = ANSVARSORDNING.index(ansvar) if ansvar in ANSVARSORDNING else len(ANSVARSORDNING)
    return (ordning, -r.sparar)


@regel(100)
def rad_bildmatt(a: "Analys") -> list[Rad]:
    """Bilder som är större än det som faktiskt syns — beskurna eller för högupplösta."""
    bilder = [r for r in a.resurser if r.har_bildatgard and not r.dold_orsak]
    if not bilder:
        return []
    vinst = sum(r.storlek - int(r.storlek * r.pixelandel) for r in bilder)
    beskurna = [r for r in bilder if r.beskuren_andel >= BESKURET_TROSKEL]
    namn = antal(len(bilder), "bild", "bilder")
    if beskurna and len(beskurna) == len(bilder):
        rubrik = f"Beskär {namn} till det som syns i annonsen"
    elif beskurna:
        rubrik = f"Beskär och skala ner {namn}"
    else:
        rubrik = f"Skala ner {namn} till rätt pixelmått"

    rader = []
    for r in sorted(bilder, key=lambda x: -x.storlek):
        if r.beskuren_andel >= BESKURET_TROSKEL:
            rader.append(
                f"{r.filnamn}: {r.nat_b}×{r.nat_h} px, men rutan på {r.vis_b}×{r.vis_h} px "
                f"visar bara {r.synlig_b}×{r.synlig_h} px av den — "
                f"{procent(r.beskuren_andel, 1)} klipps bort. "
                f"Exportera som {r.mal_b}×{r.mal_h} px."
            )
        else:
            rader.append(
                f"{r.filnamn}: {r.nat_b}×{r.nat_h} px visas som {r.vis_b}×{r.vis_h} px. "
                f"Exportera som {r.mal_b}×{r.mal_h} px."
            )

    varfor = (
        "BannerBoo lägger bilden så att den fyller rutan och klipper bort det som sticker "
        "utanför. De bortklippta pixlarna laddas ändå — de syns bara aldrig."
        if beskurna
        else "Bilderna är större än de visas. Webbläsaren skalar ner dem, men de extra "
        "pixlarna laddas ändå."
    )
    return [
        Rad(
            rubrik=rubrik,
            varfor=varfor,
            gor=rader
            + [
                "Beskär och exportera i ert bildprogram innan ni laddar upp bilden i BannerBoo.",
                f"Måtten utgår från {NETTHINNA:.0f}× visningsytan, vilket räcker även på "
                "skärmar med hög upplösning.",
            ],
            sparar=max(vinst, 0),
            allvar="hög" if vinst > 50 * KB else "medel",
            ansvar="ni",
        )
    ]


@regel(98)
def rad_dolda_resurser(a: "Analys") -> list[Rad]:
    dolda = [r for r in a.resurser if r.dold_orsak]
    if not dolda:
        return []
    vikt = sum(r.storlek for r in dolda)
    return [
        Rad(
            rubrik=f"Radera {antal(len(dolda), 'dolt lager', 'dolda lager')} i BannerBoo",
            varfor=(
                "Lager som är gömda i BannerBoo laddar ändå sina bilder. Besökaren betalar "
                f"för {fmt(vikt)} som aldrig syns."
            ),
            gor=[
                "Radera lagret i stället för att dölja det: "
                + "; ".join(f"{r.filnamn} ({fmt(r.storlek)})" for r in dolda),
                "Behöver ni lagret i en annan variant av annonsen: gör en kopia av "
                "annonsen för den varianten, i stället för att dölja lagret.",
            ],
            sparar=vikt,
            allvar="hög" if vikt > 30 * KB else "medel",
            ansvar="ni",
        )
    ]


@regel(95)
def rad_typsnitt_i_annonsen(a: "Analys") -> list[Rad]:
    """Fler än två typsnitt. Orden i de övriga görs bättre som SVG."""
    grupper = typsnitt_per_familj(a)
    if len(grupper) < 3:
        return []
    hus = husets_typsnitt(a)
    extra = [(fam, g) for fam, g in grupper.items() if fam not in hus]
    vinst = sum(max(0, g["vikt"] - SVG_ORD_BYTE) for _fam, g in extra)
    typsnittsvikt = sum(g["vikt"] for g in grupper.values())

    gor = []
    for fam, g in sorted(grupper.items(), key=lambda x: -x[1]["vikt"]):
        anvands = (
            f" till \"{g['text']}\" — {antal(g['unika'], 'tecken', 'tecken')}"
            if g["text"]
            else ""
        )
        roll = "behåll" if fam in hus else "gör orden som SVG"
        gor.append(f"{fam}, {fmt(g['vikt'])}{anvands}: {roll}")
    gor += [
        "Ord i ett typsnitt ni inte behåller: skriv dem i ert designverktyg, konvertera "
        "texten till konturer och ladda upp som SVG i BannerBoo. Då väger de ett par kB "
        "i stället för ett helt typsnitt.",
        "Bestäm två husteckensnitt för alla era annonser. Då delar annonserna nedladdning "
        "när flera ligger på samma sida.",
    ]
    woff = sorted(fam for fam, g in grupper.items() if g["format"] & {"woff", "woff2"})
    ttf = sorted(fam for fam, g in grupper.items() if g["format"] & {"ttf", "otf"})
    if woff and ttf:
        gor.append(
            f"Välj helst snitt som BannerBoo levererar som woff — i den här annonsen "
            f"{', '.join(woff)}. De är klart lättare än de som kommer som ttf."
        )

    andel = typsnittsvikt / (a.totalvikt or 1)
    return [
        Rad(
            rubrik=f"Använd högst två typsnitt — annonsen laddar {len(grupper)}",
            varfor=(
                "Varje typsnitt laddas i sin helhet, med hundratals tecken, även när "
                "annonsen bara använder några få."
                + (
                    f" Här står typsnitten för {procent(andel, 1)} av annonsens vikt."
                    if andel >= 0.3
                    else ""
                )
            ),
            gor=gor,
            sparar=vinst,
            allvar="kritisk" if vinst > 150 * KB else ("hög" if vinst > 50 * KB else "medel"),
            ansvar="ni",
        )
    ]


@regel(92)
def rad_bildkomprimering(a: "Analys") -> list[Rad]:
    bilder = [
        r
        for r in a.resurser
        if r.kategori == "bild" and r.underformat in ("jpeg", "png") and not r.dold_orsak
    ]
    if not bilder:
        return []
    rader, vinst = [], 0
    for r in sorted(bilder, key=lambda x: -x.storlek):
        faktor = FAKTOR_JPEG_KOMPRIMERING if r.underformat == "jpeg" else FAKTOR_PNG_KOMPRIMERING
        efter_matt = int(r.storlek * r.pixelandel)
        efter = int(efter_matt * faktor)
        # Bara det komprimeringen ger. Beskärningen räknas i sitt eget råd.
        vinst += efter_matt - efter
        tillagg = " efter beskärning och komprimering" if r.har_bildatgard else ""
        rader.append(
            f"{r.filnamn} ({r.underformat.upper()}, {fmt(r.storlek)}) → ungefär {fmt(efter)}{tillagg}"
        )
    if vinst < 3 * KB:
        return []
    return [
        Rad(
            rubrik="Komprimera bilderna innan ni laddar upp dem",
            varfor=(
                ("Bilden" if len(bilder) == 1 else f"De {len(bilder)} bilderna")
                + " kan bli betydligt mindre utan någon synlig skillnad."
            ),
            gor=rader
            + [
                "Kör bilderna genom Squoosh (squoosh.app) eller TinyPNG innan ni laddar "
                "upp dem i BannerBoo.",
                "Foton klarar sig bra med JPEG-kvalitet 75–82. Bilder med stora enfärgade "
                "ytor blir ofta minst som PNG med färre färger.",
                "Tar BannerBoo emot WebP blir bilderna ännu mindre.",
            ],
            sparar=vinst,
            allvar="hög" if vinst > 50 * KB else "medel",
            ansvar="ni",
        )
    ]


@regel(80)
def rad_animationslangd(a: "Analys") -> list[Rad]:
    if a.anim_sekunder is None:
        return []
    problem = []
    if a.anim_sekunder > IAB_ANIM_SEK:
        problem.append(
            f"den är {a.anim_sekunder:.1f} s lång, IAB rekommenderar högst "
            f"{IAB_ANIM_SEK:.0f} s".replace(".", ",", 1)
        )
    if a.anim_loopar == 0:
        problem.append("den loopar i all oändlighet")
    elif a.anim_loopar and a.anim_loopar > IAB_LOOPAR:
        problem.append(f"den spelas {a.anim_loopar} gånger")
    if not problem:
        return []
    return [
        Rad(
            rubrik=f"Låt animationen stanna efter {IAB_LOOPAR} varv",
            varfor=(
                "Det här handlar inte om nedladdning utan om processor och batteri: "
                + " och ".join(problem)
                + "."
            ),
            gor=[
                f"Ställ in antalet uppspelningar i BannerBoos animationsinställningar. "
                f"{IAB_LOOPAR} räcker, och IAB rekommenderar inte fler.",
                "Låt annonsen landa på en slutbild med budskap och knapp.",
                "En animation som aldrig tar slut håller webbläsaren sysselsatt så länge "
                "sidan är öppen — det märks på mobilens batteri.",
                "Budskapet bör gå att läsa redan i första bildrutan; många ser annonsen i "
                "mindre än tre sekunder.",
            ],
            sparar=0,
            allvar="medel",
            ansvar="ni",
        )
    ]


@regel(75)
def rad_antal_forfragningar(a: "Analys") -> list[Rad]:
    if len(a.resurser) <= 15:
        return []
    return [
        Rad(
            rubrik=f"Annonsen gör {len(a.resurser)} förfrågningar",
            varfor=(
                "Varje förfrågan har en fast kostnad i väntetid. På ett svagt mobilnät "
                "väger antalet ofta tyngre än storleken."
            ),
            gor=[
                "Varje uppladdad bild och varje typsnitt är en egen förfrågan. Färre lager "
                "och färre typsnitt minskar antalet.",
                "Enkla former och ikoner kan göras med BannerBoos egna SVG-element i stället "
                "för som uppladdade bilder — de ligger då direkt i annonsen.",
            ],
            sparar=0,
            allvar="låg",
            ansvar="ni",
        )
    ]


@regel(70)
def rad_over_budget(a: "Analys") -> list[Rad]:
    if a.totalvikt <= IAB_INITIAL:
        return []
    over = a.totalvikt - IAB_INITIAL
    return [
        Rad(
            rubrik=f"Annonsen väger {fmt(a.totalvikt)} — {fmt(over)} över riktvärdet",
            varfor=(
                f"IAB:s riktvärde för en annons är {fmt(IAB_INITIAL)}. Flera annonsnätverk "
                "nedprioriterar tyngre annonser, och på mobilen tar de längre tid att visa."
            ),
            gor=[
                "Tyngsta delarna: "
                + ", ".join(
                    f"{r.filnamn} ({fmt(r.storlek)})"
                    for r in sorted(a.resurser, key=lambda x: -x.storlek)[:3]
                ),
                f"Sätt {fmt(IAB_INITIAL)} som mål för era annonser, och mät varje ny annons "
                "innan den publiceras.",
            ],
            sparar=0,
            allvar="kritisk" if a.totalvikt > 2 * IAB_TOTALT else "hög",
            ansvar="ni",
        )
    ]


# ── Det här styrs av BannerBoo och går inte att ändra i annonsen ─────────────


@regel(65)
def rad_typsnitt_format(a: "Analys") -> list[Rad]:
    ttf = [r for r in a.resurser if r.kategori == "typsnitt" and r.underformat in ("ttf", "otf")]
    if not ttf:
        return []
    vikt = sum(r.storlek for r in ttf)
    kvar = int(vikt * FAKTOR_TTF_TILL_WOFF2 * FAKTOR_SUBSET)
    return [
        Rad(
            rubrik="BannerBoo levererar typsnitten som ttf",
            varfor=(
                f"{antal(len(ttf), 'typsnitt', 'typsnitt')} ({fmt(vikt)}) laddas i "
                "skrivbordsformatet ttf, med alla tecken för alla språk. Woff2 är ungefär "
                "45 % mindre, och ett typsnitt beskuret till de tecken som används ännu mindre."
            ),
            gor=[
                "Be BannerBoo leverera typsnitten som woff2.",
                "Be dem också beskära typsnitten till de tecken som faktiskt används i "
                "annonsen (subsetting).",
                "Det gör alla era annonser lättare på en gång, utan att någon av dem behöver "
                "göras om.",
            ],
            sparar=vikt - kvar,
            allvar="medel",
            ansvar="bannerboo",
        )
    ]


@regel(60)
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
            rubrik="BannerBoo skickar filer okomprimerade",
            varfor=(
                f"{antal(len(okomprimerade), 'textfil', 'textfiler')} "
                f"({fmt(sum(r.storlek for r in okomprimerade))}) skickas utan komprimering "
                "från BannerBoos server."
            ),
            gor=[
                "Be BannerBoo slå på gzip eller brotli för HTML, JavaScript, CSS och SVG — "
                "det är en serverinställning hos dem.",
                f"Berör: {filer}",
            ],
            sparar=vinst,
            allvar="medel",
            ansvar="bannerboo",
        )
    ]


@regel(55)
def rad_deklarerade_snitt(a: "Analys") -> list[Rad]:
    if a.typsnitt_deklarerade <= len(a.typsnitt_laddade) + 3:
        return []
    extra = a.typsnitt_deklarerade - len(a.typsnitt_laddade)
    return [
        Rad(
            rubrik=f"BannerBoos mall deklarerar {extra} typsnitt som aldrig används",
            varfor=(
                f"Annonsens kod beskriver {a.typsnitt_deklarerade} typsnittsvarianter men "
                f"bara {len(a.typsnitt_laddade)} används. Filerna hämtas inte, men "
                "beskrivningarna ligger kvar i koden."
            ),
            gor=[
                "Det ligger i BannerBoos mall och går inte att ändra i annonsen.",
                "Värt att nämna för BannerBoo, eftersom det gäller alla annonser som byggs där.",
            ],
            sparar=0,
            allvar="låg",
            ansvar="bannerboo",
        )
    ]


@regel(50)
def rad_animationsbibliotek(a: "Analys") -> list[Rad]:
    bib = [r for r in a.resurser if r.bibliotek]
    if not bib:
        return []
    vikt = sum(r.storlek for r in bib)
    namn = ", ".join(sorted({r.bibliotek for r in bib}))
    return [
        Rad(
            rubrik=f"BannerBoo laddar {namn} i varje annons",
            varfor=(
                f"{namn} ({fmt(vikt)}) hämtas från ett externt CDN för varje annons, "
                "oavsett hur enkel animationen är."
            ),
            gor=[
                "Det styrs av BannerBoo och går inte att välja bort i annonsen.",
                "Enklare animationer gör inte filen mindre — den laddas ändå.",
            ],
            sparar=0,
            allvar="låg",
            ansvar="bannerboo",
        )
    ]


@regel(45)
def rad_tredjepart(a: "Analys") -> list[Rad]:
    tp = [r for r in a.resurser if r.tredjepart and not r.bibliotek]
    sparning = [r for r in a.resurser if r.sparning and r.storlek > 0]
    if not tp and not sparning:
        return []
    varder = sorted({urllib.parse.urlparse(r.url).netloc for r in tp})
    gor = []
    if varder:
        gor.append(f"Externa värdar: {', '.join(varder)}.")
    if sparning:
        gor.append(
            f"BannerBoos spårskript: {', '.join(r.filnamn for r in sparning)}. "
            "Kontrollera att det stämmer med samtyckeshanteringen på er sajt."
        )
    gor.append("Det styrs av BannerBoo och går inte att ändra i annonsen.")
    return [
        Rad(
            rubrik="Annonsen hämtar filer från andra värdar än BannerBoo",
            varfor=(
                "Varje extra värd kostar uppslag och anslutning innan något alls hämtas — "
                "ofta 100–300 ms."
            ),
            gor=gor,
            sparar=0,
            allvar="låg",
            ansvar="bannerboo",
        )
    ]


@regel(40)
def rad_cache(a: "Analys") -> list[Rad]:
    utan = [r for r in a.resurser if not r.cachebar and r.storlek > 2 * KB]
    if not utan:
        return []
    return [
        Rad(
            rubrik="BannerBoo låter inte alla filer cachas",
            varfor=(
                f"{antal(len(utan), 'fil', 'filer')} "
                f"({fmt(sum(r.storlek for r in utan))}) hämtas om vid varje visning, även "
                "för en besökare som redan sett annonsen."
            ),
            gor=[
                "Be BannerBoo sätta cache-inställningar på filerna.",
                f"Berör bland annat: "
                f"{', '.join(r.filnamn for r in sorted(utan, key=lambda x: -x.storlek)[:4])}",
            ],
            sparar=0,
            allvar="låg",
            ansvar="bannerboo",
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
    overdim_faktor: float = 0.0  # kvar för JSON-läsare; se mal_b/mal_h
    passning: str = ""  # background-size eller object-fit: cover, contain, 100% 100% …
    synlig_b: int = 0  # den del av bilden som faktiskt syns, i bildens egna pixlar
    synlig_h: int = 0
    mal_b: int = 0  # rekommenderat exportmått
    mal_h: int = 0
    beskuren_andel: float = 0.0  # andel av bildens pixlar som klipps bort av rutan
    dold_orsak: str = ""
    miniatyr: str = ""  # PNG som base64 — bilden, eller ett textprov för typsnitt
    # typsnittsspecifikt
    typsnitt_familj: str = ""
    typsnitt_text: str = ""  # texten annonsen sätter i typsnittet
    unika_tecken: int = 0
    # räknat
    potential: int = 0  # om både ni och BannerBoo åtgärdar det som går
    potential_egen: int = 0  # om ni åtgärdar det ni själva kan

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
    def pixelandel(self) -> float:
        """Andel av bildens pixlar som behövs. 1.0 om inget är känt."""
        if not (self.nat_b and self.nat_h and self.mal_b and self.mal_h):
            return 1.0
        return min(1.0, (self.mal_b * self.mal_h) / (self.nat_b * self.nat_h))

    @property
    def har_bildatgard(self) -> bool:
        return self.kategori == "bild" and (1.0 - self.pixelandel) >= BILD_TROSKEL

    @property
    def bildatgard(self) -> str:
        """Kort anmärkning om vad som bör göras med bilden, eller tom sträng."""
        if not self.har_bildatgard:
            return ""
        beskar = self.beskuren_andel >= BESKURET_TROSKEL
        skala = self.synlig_b > self.mal_b or self.synlig_h > self.mal_h
        verb = (
            "beskär och skala till" if beskar and skala
            else "beskär till" if beskar
            else "skala till"
        )
        return f"{self.nat_b}×{self.nat_h} → {verb} {self.mal_b}×{self.mal_h} px"

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
    # Animationen som bildrutor: [(png, varaktighet i ms), …]. Hör till fönstret,
    # följer aldrig med till JSON.
    bildrutor: list = field(default_factory=list, repr=False)
    fangad_tid_s: float = 0.0

    @property
    def totalvikt(self) -> int:
        return sum(r.storlek for r in self.resurser)

    @property
    def totalt_uppackat(self) -> int:
        return sum(r.uppackat or r.storlek for r in self.resurser)

    @property
    def potentialvikt(self) -> int:
        return sum(r.potential for r in self.resurser)

    @property
    def potential_egen(self) -> int:
        return sum(r.potential_egen for r in self.resurser)


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


# src= eller href= i en inbäddningskod, med eller utan citattecken.
MONSTER_SRC = re.compile(
    r"""(?:src|href)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""", re.I
)
MONSTER_TAGG = re.compile(r"<[^>]*>")


def _adress_ur_kod(text: str) -> str:
    """Plockar adressen ur en inbäddningskod. Returnerar texten oförändrad annars."""
    if "<" not in text:
        return text
    kandidater = [g for m in MONSTER_SRC.finditer(text) for g in m.groups() if g]
    if kandidater:
        # Flera taggar kan förekomma; BannerBoos egen går före.
        bannerboo = [k for k in kandidater if "bannerboo" in k.lower()]
        return (bannerboo or kandidater)[0].strip()
    # Ingen src alls — kanske bara taggar runt en adress. Skala bort dem.
    kvar = MONSTER_TAGG.sub(" ", text).strip()
    return kvar or text


def normalisera_url(indata: str) -> str:
    """Tar emot full URL, adress utan protokoll, ett BannerBoo-id — eller hela
    inbäddningskoden som BannerBoo ger en att klistra in på sajten."""
    s = " ".join(str(indata).split())  # radbrytningar i en inklistrad kod
    s = s.strip().strip("\"'")  # citattecken som följt med vid kopieringen

    s = _adress_ur_kod(s)
    s = s.strip().strip("\"'")

    if s.startswith("//"):  # protokollrelativ, som i inbäddningskoden
        s = "https:" + s
    if re.fullmatch(r"[0-9a-f]{8,32}", s):
        return f"https://embed.bannerboo.com/{s}"
    if not s.startswith(("http://", "https://")):
        s = "https://" + s

    # En iframe-adress pekar på själva kreativen. Laddarens adress ger samma
    # annons men mäter det en riktig sida faktiskt hämtar, inklusive laddaren.
    traff = MONSTER_IFRAME.search(s)
    if traff:
        fraga = "?responsive=1" if "responsive=1" in s else ""
        return f"https://embed.bannerboo.com/{traff.group(1).lower()}{fraga}"
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
  // passning avgör hur mycket av bilden som syns: cover klipper, contain gör det inte.
  const notera = (url, el, passning) => {
    if (!url || url.startsWith('data:')) return;
    const r = el.getBoundingClientRect();
    const b = Math.round(r.width), h = Math.round(r.height);
    const f = bilder.get(url) || { url, vis_b: 0, vis_h: 0, dold: doldOrsak(el), passning };
    if (b * h > f.vis_b * f.vis_h) { f.vis_b = b; f.vis_h = h; f.passning = passning; }
    if (!doldOrsak(el)) f.dold = '';
    bilder.set(url, f);
  };
  const forstaLagret = v => String(v || '').split(',')[0].trim();

  document.querySelectorAll('img').forEach(im =>
    notera(abs(im.currentSrc || im.src), im, getComputedStyle(im).objectFit || 'fill'));
  document.querySelectorAll('*').forEach(el => {
    const cs = getComputedStyle(el);
    urlerUr(cs.backgroundImage).forEach(u => notera(u, el, forstaLagret(cs.backgroundSize)));
    urlerUr(cs.maskImage || cs.webkitMaskImage).forEach(u =>
      notera(u, el, forstaLagret(cs.maskSize || cs.webkitMaskSize || 'auto')));
    [cs.borderImageSource, cs.content, cs.listStyleImage].forEach(v =>
      urlerUr(v).forEach(u => notera(u, el, 'auto')));
  });

  // En visningsbar kopia: Tk klarar bara PNG, annonser innehåller jpeg och svg.
  // Duken smittas av bilder från annan domän — då kastar toDataURL och vi avstår.
  const MINIMAX = 640;
  const avbild = (bild) => {
    try {
      const skala = Math.min(1, MINIMAX / Math.max(bild.naturalWidth, bild.naturalHeight));
      const duk = document.createElement('canvas');
      duk.width = Math.max(1, Math.round(bild.naturalWidth * skala));
      duk.height = Math.max(1, Math.round(bild.naturalHeight * skala));
      duk.getContext('2d').drawImage(bild, 0, 0, duk.width, duk.height);
      return duk.toDataURL('image/png');
    } catch (e) {
      return '';
    }
  };

  // Verkliga pixelmått — bilderna ligger i cache, så det här går direkt.
  const matt = await Promise.all(Array.from(bilder.values()).map(f => new Promise(klar => {
    const i = new Image();
    i.onload = () => klar(Object.assign(f, {
      nat_b: i.naturalWidth, nat_h: i.naturalHeight, mini: avbild(i),
    }));
    i.onerror = () => klar(Object.assign(f, { nat_b: 0, nat_h: 0, mini: '' }));
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

  // ── Typsnitten: vilken fil hör till vilket snitt, och vilken text står i det ──
  const rensaFamilj = v => String(v || '').replace(/^["']|["']$/g, '').trim();
  const regler = [];
  for (const blad of document.styleSheets) {
    let lista;
    try { lista = blad.cssRules; } catch (e) { continue; }  // blad från annan domän
    for (const regel of lista) {
      if (!(regel instanceof CSSFontFaceRule)) continue;
      const src = regel.style.getPropertyValue('src') || '';
      regler.push({
        familj: rensaFamilj(regel.style.getPropertyValue('font-family')),
        vikt: (regel.style.getPropertyValue('font-weight') || '400').trim(),
        stil: (regel.style.getPropertyValue('font-style') || 'normal').trim(),
        urler: Array.from(src.matchAll(/url\((["']?)([^"')]+)\1\)/g)).map(m => abs(m[2])),
      });
    }
  }

  // Bara synlig text — stilblad, skript och sidtitel står också i dokumentet.
  const textPerFamilj = {};
  const inteText = new Set(['SCRIPT', 'STYLE', 'TITLE', 'NOSCRIPT', 'TEMPLATE']);
  for (const el of document.querySelectorAll('body *')) {
    if (inteText.has(el.tagName)) continue;
    const egen = [...el.childNodes].filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').trim();
    if (!egen) continue;
    const fam = rensaFamilj(getComputedStyle(el).fontFamily.split(',')[0]);
    textPerFamilj[fam] = ((textPerFamilj[fam] || '') + ' ' + egen).trim();
  }

  // Bara snitt som redan laddats ritas. Att be om ett oladdat snitt skulle hämta
  // en fil till — och den skulle hamna i mätningen som om annonsen laddat den.
  const PROVRAD = 'Aa Bb Cc Åå Ää Öö 0123456789';
  const typsnittsprov = [];
  const laddade = [];
  document.fonts.forEach(f => { if (f.status === 'loaded') laddade.push(f); });
  for (const f of laddade) {
    const fam = rensaFamilj(f.family);
    const kandidater = regler.filter(r => r.familj === fam && r.stil === f.style);
    if (!kandidater.length) continue;
    const onskad = parseInt(f.weight) || 400;
    kandidater.sort((x, y) =>
      Math.abs((parseInt(x.vikt) || 400) - onskad) - Math.abs((parseInt(y.vikt) || 400) - onskad));
    const text = (textPerFamilj[fam] || '').replace(/\s+/g, ' ').trim();
    const tecken = new Set(text.replace(/\s/g, '')).size;
    let png = '';
    try {
      const snittet = `${f.style} ${f.weight} `;
      const rad1 = text.slice(0, 42) || fam;
      const duk = document.createElement('canvas');
      let c = duk.getContext('2d');
      c.font = `${snittet}46px "${fam}"`;
      const b1 = c.measureText(rad1).width;
      c.font = `${snittet}26px "${fam}"`;
      const b2 = c.measureText(PROVRAD).width;
      duk.width = Math.min(960, Math.ceil(Math.max(b1, b2)) + 36);
      duk.height = 120;
      c = duk.getContext('2d');  // storleksbytet nollställer duken
      c.fillStyle = '#1c1a17';
      c.font = `${snittet}46px "${fam}"`;
      c.fillText(rad1, 18, 56);
      c.fillStyle = '#6b6560';
      c.font = `${snittet}26px "${fam}"`;
      c.fillText(PROVRAD, 18, 100);
      png = duk.toDataURL('image/png');
    } catch (e) {}
    typsnittsprov.push({ urler: kandidater[0].urler, familj: fam, vikt: f.weight,
                         stil: f.style, text: text.slice(0, 80), unika: tecken, png });
  }

  return {
    url: location.href,
    titel: document.title,
    bilder: matt,
    snitt,
    typsnittsprov,
    anvanda: Array.from(anvanda),
    element: document.querySelectorAll('*').length,
    dom_byte: document.documentElement.outerHTML.length
  };
}
"""


def animationstid(text: str) -> float | None:
    """Animationens längd ur BannerBoos konfiguration, om den finns."""
    traff = re.search(r'"animtime"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', text or "")
    try:
        varde = float(traff.group(1)) if traff else 0.0
    except ValueError:
        varde = 0.0
    return varde if varde > 0 else None


def fanga_animation(sida, element, langd_s: float) -> list:
    """Fotograferar annonsen medan den spelar.

    Returnerar [(png, varaktighet i ms), …]. Varaktigheten kommer från när rutorna
    faktiskt togs, så att uppspelningen går i annonsens egen takt även när en
    skärmdump tar olika lång tid."""
    if element is None or langd_s < 1.0:
        return []
    rutor, tider = [], []
    start = time.monotonic()
    while time.monotonic() - start < langd_s and len(rutor) < FANG_MAX_RUTOR:
        t0 = time.monotonic()
        try:
            rutor.append(element.screenshot(animations="allow"))
        except Exception:
            break
        tider.append(t0 - start)
        kvar = FANG_INTERVALL_MS / 1000 - (time.monotonic() - t0)
        if kvar > 0:
            sida.wait_for_timeout(int(kvar * 1000))
    if len(rutor) < 2:
        return []
    slut = time.monotonic() - start
    # Rutor som är exakt lika den förra slås ihop till en längre. En annons som
    # står still en stund blir då färre rutor, och en som aldrig rör sig ingen
    # animation alls — då räcker skärmbilden.
    sammanslagna = []
    for i, png in enumerate(rutor):
        ms = max(20, int(((tider[i + 1] if i + 1 < len(tider) else slut) - tider[i]) * 1000))
        if sammanslagna and sammanslagna[-1][0] == png:
            sammanslagna[-1] = (png, sammanslagna[-1][1] + ms)
        else:
            sammanslagna.append((png, ms))
    return sammanslagna if len(sammanslagna) > 1 else []


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

        # Låt animationen rulla så att sent laddade resurser hinner med — och
        # fånga den under tiden, så att mätningen inte blir en sekund längre.
        # Är animationens längd känd fångas precis en cykel: då går loopen i
        # förhandsvyn ihop utan skarv, var i cykeln fångsten än började.
        vantan_s = float(vantetid)
        fangtid = min(vantan_s, animationstid(forsta_kropp) or vantan_s, FANG_MAX_S)
        bildrutor = []
        start_vantan = time.monotonic()
        if not navfel:
            try:
                annonsyta = sida.query_selector("iframe") or sida.query_selector("body")
                if annonsyta:
                    if args is not None:
                        beratta(args, "fångar animationen …")
                    bildrutor = fanga_animation(sida, annonsyta, fangtid)
            except Exception:
                bildrutor = []
        kvar_ms = int((vantan_s - (time.monotonic() - start_vantan)) * 1000)
        if kvar_ms > 0:
            sida.wait_for_timeout(kvar_ms)

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

    return lage, forsta_kropp, rader, sonder, konsol, skarmbild, navfel, bildrutor


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
    prov_per_url: dict[str, dict] = {}
    for s in sonder:
        for b in s.get("bilder", []):
            bildinfo[b["url"]] = b
        for prov in s.get("typsnittsprov", []):
            for u in prov.get("urler", []):
                prov_per_url.setdefault(u, prov)
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
            avbild = info.get("mini") or ""
            if avbild.startswith("data:image/png;base64,"):
                r.miniatyr = avbild.split(",", 1)[1]
            r.nat_b, r.nat_h = int(info.get("nat_b") or 0), int(info.get("nat_h") or 0)
            r.vis_b, r.vis_h = int(info.get("vis_b") or 0), int(info.get("vis_h") or 0)
            r.dold_orsak = info.get("dold") or ""
            r.passning = str(info.get("passning") or "")
            if r.nat_b and r.nat_h and r.vis_b and r.vis_h:
                r.synlig_b, r.synlig_h, r.mal_b, r.mal_h = bildens_mal(
                    r.nat_b, r.nat_h, r.vis_b, r.vis_h, r.passning
                )
                r.beskuren_andel = 1.0 - (r.synlig_b * r.synlig_h) / (r.nat_b * r.nat_h)
                if r.mal_b and r.mal_h:
                    r.overdim_faktor = round(
                        ((r.nat_b * r.nat_h) / (r.mal_b * r.mal_h)) ** 0.5, 2
                    )

        prov = prov_per_url.get(url) if kategori == "typsnitt" else None
        if prov:
            r.typsnitt_familj = str(prov.get("familj") or "")
            r.typsnitt_text = str(prov.get("text") or "")
            r.unika_tecken = int(prov.get("unika") or 0)
            png = str(prov.get("png") or "")
            if png.startswith("data:image/png;base64,"):
                r.miniatyr = png.split(",", 1)[1]

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


def bildens_mal(nat_b: int, nat_h: int, vis_b: int, vis_h: int, passning: str):
    """Hur mycket av bilden syns, och vilket mått borde den exporteras i?

    Returnerar (synlig bredd, synlig höjd, mål-bredd, mål-höjd), allt i bildens
    egna pixlar. BannerBoo lägger bilder med background-size: cover, som skalar
    bilden tills den fyller rutan och klipper bort resten.
    """
    p = (passning or "").strip().lower()
    if p == "cover":
        skala = max(vis_b / nat_b, vis_h / nat_h)
        synlig_b, synlig_h = min(nat_b, vis_b / skala), min(nat_h, vis_h / skala)
        visad_b, visad_h = vis_b, vis_h
    elif p in ("contain", "scale-down"):
        skala = min(vis_b / nat_b, vis_h / nat_h)
        if p == "scale-down":
            skala = min(skala, 1.0)
        synlig_b, synlig_h = nat_b, nat_h
        visad_b, visad_h = nat_b * skala, nat_h * skala
    elif p in ("auto", "auto auto", "none", "initial"):
        # Naturlig storlek: det som sticker utanför rutan klipps, inget skalas.
        synlig_b, synlig_h = min(nat_b, vis_b), min(nat_h, vis_h)
        visad_b, visad_h = synlig_b / NETTHINNA, synlig_h / NETTHINNA
    else:
        # fill, 100% 100% och fasta mått: hela bilden sträcks över rutan.
        synlig_b, synlig_h = nat_b, nat_h
        visad_b, visad_h = vis_b, vis_h
    mal_b = min(synlig_b, visad_b * NETTHINNA)
    mal_h = min(synlig_h, visad_h * NETTHINNA)
    return round(synlig_b), round(synlig_h), max(1, round(mal_b)), max(1, round(mal_h))


def typsnitt_per_familj(a: Analys) -> dict:
    """familj → filer, vikt, format och den text annonsen sätter i den."""
    grupper: dict[str, dict] = {}
    for r in a.resurser:
        if r.kategori != "typsnitt":
            continue
        fam = r.typsnitt_familj or r.filnamn
        g = grupper.setdefault(
            fam, {"filer": [], "vikt": 0, "text": "", "unika": 0, "format": set()}
        )
        g["filer"].append(r)
        g["vikt"] += r.storlek
        g["format"].add(r.underformat)
        if r.unika_tecken > g["unika"]:
            g["unika"], g["text"] = r.unika_tecken, r.typsnitt_text
    return grupper


LANG_TEXT_TECKEN = 20  # fler olika tecken än så är löptext, inte ett par ord


def husets_typsnitt(a: Analys, antal_att_behalla: int = 2) -> set:
    """De typsnitt som är värda att behålla som riktiga typsnitt.

    Typsnitt med längre text behålls först — löptext ska inte bli en bild. Bär
    alla typsnitt bara korta ord avgör vikten i stället: de tunga sparar mest på
    att göras som SVG, så det är de lätta som behålls.
    """
    grupper = typsnitt_per_familj(a)

    def nyckel(post):
        g = post[1]
        kort = g["unika"] <= LANG_TEXT_TECKEN
        return (kort, g["vikt"] if kort else -g["unika"])

    rangordnade = sorted(grupper.items(), key=nyckel)
    return {fam for fam, _g in rangordnade[:antal_att_behalla]}


def berakna_potential(a: Analys) -> None:
    """Rimlig storlek per fil efter åtgärd, i två nivåer och utan dubbelräkning.

    potential_egen — det ni kan göra själva i BannerBoo: radera dolda lager,
                     beskära, skala och komprimera bilder, göra ord i extra
                     typsnitt som SVG.
    potential      — om BannerBoo dessutom komprimerar sina filer och levererar
                     typsnitten som woff2 med bara de tecken som används.
    """
    hus = husets_typsnitt(a)
    for r in a.resurser:
        if r.dold_orsak:
            r.potential_egen = r.potential = 0
            continue

        if r.kategori == "bild":
            faktor = {
                "jpeg": FAKTOR_JPEG_KOMPRIMERING,
                "png": FAKTOR_PNG_KOMPRIMERING,
            }.get(r.underformat, 1.0)
            egen = int(r.storlek * r.pixelandel * faktor)
            full = egen
            if r.underformat == "svg" and r.text_utan_komprimering and r.gzip_storlek:
                full = min(egen, r.gzip_storlek)
            r.potential_egen, r.potential = egen, full
            continue

        if r.kategori == "typsnitt":
            extra = bool(r.typsnitt_familj) and len(hus) >= 2 and r.typsnitt_familj not in hus
            egen = min(r.storlek, SVG_ORD_BYTE) if extra else r.storlek
            if r.underformat in ("ttf", "otf"):
                bannerboo = int(r.storlek * FAKTOR_TTF_TILL_WOFF2 * FAKTOR_SUBSET)
            elif r.underformat in ("woff", "woff2"):
                bannerboo = int(r.storlek * FAKTOR_SUBSET)
            else:
                bannerboo = r.storlek
            r.potential_egen, r.potential = egen, min(egen, bannerboo)
            continue

        if r.text_utan_komprimering and r.gzip_storlek:
            r.potential_egen, r.potential = r.storlek, r.gzip_storlek
            continue

        r.potential_egen = r.potential = r.storlek


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
    for r in rad:
        r.gor = [steg for steg in r.gor if steg]
    rad.sort(key=_ansvarsnyckel)
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
        if r.bildatgard:
            anm.append(r.bildatgard)
        if r.kategori == "typsnitt" and r.unika_tecken:
            anm.append(f"{r.unika_tecken} tecken används")
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
    p("  RÅD")
    p("═" * W)
    märke = {"kritisk": "!!!", "hög": "!! ", "medel": "!  ", "låg": "   "}
    nummer = 0
    for rubrik, lista in gruppera_rad(rad):
        p("")
        p(f"  ── {rubrik.upper()} ".ljust(W - 2, "─"))
        for r in lista:
            nummer += 1
            vinst = f"−{fmt(r.sparar)}" if r.sparar else "—"
            p("")
            p(f"  {nummer}. {märke.get(r.allvar, '   ')} {r.rubrik}   [{vinst}]")
            p(f"      {r.varfor}")
            for steg in r.gor:
                p(f"      · {steg}")

    # ── Prognos ──────────────────────────────────────────────────────────────
    p("")
    p("═" * W)
    egen, full = a.potential_egen, a.potentialvikt
    p("  OM RÅDEN GENOMFÖRS")
    p("  " + "─" * (W - 4))
    p(f"  {'Nu':<34}{fmt(a.totalvikt):>10}   betyg {satt_betyg(a.totalvikt)[0]}")
    p(
        f"  {'Det ni kan göra i BannerBoo':<34}{fmt(egen):>10}   betyg {satt_betyg(egen)[0]}   "
        f"(−{procent(a.totalvikt - egen, a.totalvikt)})"
    )
    if full < egen:
        p(
            f"  {'Om BannerBoo också gör sin del':<34}{fmt(full):>10}   "
            f"betyg {satt_betyg(full)[0]}   (−{procent(a.totalvikt - full, a.totalvikt)})"
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
  h3.ansvar{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--svag);
     margin:26px 0 4px;border-bottom:1px solid var(--linje);padding-bottom:6px}
  .tumnagel{max-width:54px;max-height:38px;border:1px solid var(--linje);border-radius:3px;
       vertical-align:middle;
       /* Rutmönster: vita och genomskinliga motiv syns inte mot vitt. */
       background-color:#fff;
       background-image:linear-gradient(45deg,#e4ded6 25%,transparent 25%),
         linear-gradient(-45deg,#e4ded6 25%,transparent 25%),
         linear-gradient(45deg,transparent 75%,#e4ded6 75%),
         linear-gradient(-45deg,transparent 75%,#e4ded6 75%);
       background-size:8px 8px;
       background-position:0 0,0 4px,4px -4px,-4px 0}
</style></head><body><div class="wrap">
__INNEHALL__
</div></body></html>"""


def html_rad(rad: list, rubrik: str, e) -> str:
    """Råden grupperade efter vem som kan göra något åt dem."""
    if not rad:
        return ""
    delar = [f"<h2>{e(rubrik)}</h2>"]
    nummer = 0
    for grupprubrik, lista in gruppera_rad(rad):
        delar.append(f"<h3 class='ansvar'>{e(grupprubrik)}</h3>")
        for r in lista:
            nummer += 1
            vinst = f"<span class='vinst'>−{e(fmt(r.sparar))}</span>" if r.sparar else ""
            steg = "".join(f"<li>{e(x)}</li>" for x in r.gor)
            delar.append(
                f"<div class='rad {r.allvar}'><h3>{nummer}. {e(r.rubrik)}{vinst}</h3>"
                f"<div class='varfor'>{e(r.varfor)}</div><ul>{steg}</ul></div>"
            )
    return "\n".join(delar)


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
    u.append("<tr><th></th><th>Fil</th><th>Typ</th><th class='n'>Vikt</th><th>Anmärkning</th></tr>")
    for r in sorted(a.resurser, key=lambda r: -r.storlek):
        flaggor = []
        if r.dold_orsak:
            flaggor.append(f"<span class='flagga dold'>dold: {e(r.dold_orsak)}</span>")
        if r.bildatgard:
            flaggor.append(f"<span class='flagga'>{e(r.bildatgard)}</span>")
        if r.kategori == "typsnitt" and r.unika_tecken:
            flaggor.append(f"<span class='flagga'>{r.unika_tecken} tecken används</span>")
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
        # Avbilder på över ~300 kB base64 utelämnas: rapporten ska gå att skicka.
        tumnagel = (
            f"<img class='tumnagel' src='data:image/png;base64,{r.miniatyr}' alt=''>"
            if r.miniatyr and len(r.miniatyr) < 300000
            else ""
        )
        u.append(
            f"<tr><td>{tumnagel}</td><td><span title='{e(r.url)}'>{e(r.filnamn)}</span></td>"
            f"<td>{e(r.underformat or r.kategori)}</td><td class='n'>{e(fmt(r.storlek))}</td>"
            f"<td>{''.join(flaggor)}</td></tr>"
        )
    u.append("</table></div>")

    # råd
    u.append(html_rad(rad, "Råd", e))

    egen, full = a.potential_egen, a.potentialvikt
    u.append("<h2>Om råden genomförs</h2><div class='kort prognos'>")
    u.append(f"<div><div class='varfor'>Idag</div><div class='stor'>{e(fmt(a.totalvikt))}</div><div>betyg {satt_betyg(a.totalvikt)[0]}</div></div>")
    u.append(f"<div><div class='varfor'>Det ni kan göra i BannerBoo</div><div class='stor'>{e(fmt(egen))}</div><div>betyg {satt_betyg(egen)[0]} · −{e(procent(a.totalvikt - egen, a.totalvikt))}</div></div>")
    if full < egen:
        u.append(f"<div><div class='varfor'>Om BannerBoo också gör sin del</div><div class='stor'>{e(fmt(full))}</div><div>betyg {satt_betyg(full)[0]} · −{e(procent(a.totalvikt - full, a.totalvikt))}</div></div>")
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
    lage, kropp, rader, sonder, konsol, bild, navfel, rutor = mat_i_webblasare(
        url, args.vantetid, args.huvud, args.bredd, args.hojd, args.tyst, args
    )
    a = bygg_analys(url, lage, kropp, rader, sonder, konsol, navfel)
    a.bildrutor = rutor
    a.fangad_tid_s = round(sum(ms for _png, ms in rutor) / 1000, 1)
    rad = samla_rad(a)
    return a, rad, bild


def analys_till_dict(a: Analys, rad: list[Rad]) -> dict:
    """Mätningen som JSON-vänlig struktur."""
    # asdict kopierar allt djupt. Rutorna är flera MB och ska inte med i JSON,
    # så de lyfts ur innan kopieringen i stället för att slängas efteråt.
    rutor, a.bildrutor = a.bildrutor, []
    try:
        d = asdict(a)
    finally:
        a.bildrutor = rutor
    d.pop("bildrutor", None)
    d["typsnitt_laddade"] = sorted(a.typsnitt_laddade)
    d["animationstyper"] = sorted(a.animationstyper)
    d["totalvikt"] = a.totalvikt
    d["potentialvikt"] = a.potentialvikt
    d["potential_egen"] = a.potential_egen
    d["betyg"] = satt_betyg(a.totalvikt)[0]
    d["rad"] = [asdict(r) for r in rad]
    # Avbilderna är till för fönstret. I JSON skulle de svälla filen utan nytta.
    for post in d.get("resurser", []):
        post.pop("miniatyr", None)
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
    # Skild från topp_px: en annons högst upp har toppen på 0, och 0 är en
    # position — inte ett tecken på att positionen saknas.
    position_kand: bool = False
    plats: str = ""
    responsive: bool = False
    varv_sedd: set = field(default_factory=set)

    @property
    def lage(self) -> str:
        if not self.position_kand:
            return "position okänd"
        return "ovanför vecket" if self.ovanfor_veck else f"{self.topp_px} px ned på sidan"

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
        # Positionen går att lita på när ramen faktiskt har en storlek. Toppen
        # kan då mycket väl vara 0 — det är annonsen högst upp på sidan.
        if el.get("b") or el.get("h"):
            f.position_kand = True
            f.topp_px = int(el.get("topp") or 0)
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
    fynd.sort(key=lambda f: (f.topp_px if f.position_kand else 10**9, f.id))

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


#  Två sorters sidråd. De som handlar om annonsens plats på sidan gäller även när
#  sidan bara har en annons. De som jämför annonser med varandra ges bara när det
#  finns flera — med en enda annons säger dess egna råd redan samma sak, och
#  "börja med den tyngsta" blir meningslöst.


@sidregel(100)
def sidrad_sidbudget(s: Sidanalys) -> list[Rad]:
    n = len(s.poster)
    if n < 2:
        return []
    budget = n * IAB_INITIAL
    unik = s.delad_vikt
    if unik <= budget:
        return []
    varsta = max(s.poster, key=lambda p: p[1].totalvikt)
    return [
        Rad(
            rubrik=f"Sidans {n} annonser väger {fmt(unik)} — {fmt(unik - budget)} över riktvärdet",
            varfor=(
                f"Med {n} annonser blir riktvärdet {fmt(budget)} ({n} × {fmt(IAB_INITIAL)}). "
                f"Besökaren hämtar {fmt(unik)} bara för annonserna, utöver sidans eget innehåll."
            ),
            gor=[
                f"Börja med den tyngsta: {varsta[0].id} väger {fmt(varsta[1].totalvikt)} "
                f"(betyg {satt_betyg(varsta[1].totalvikt)[0]}).",
                "Råden för varje annons längre ned visar exakt var vikten sitter.",
                f"Sätt {fmt(IAB_INITIAL)} som mål per annons och mät varje ny annons innan "
                "den publiceras.",
            ],
            sparar=0,
            allvar="kritisk" if unik > 2 * budget else "hög",
            ansvar="ni",
        )
    ]


@sidregel(90)
def sidrad_lat_ladda(s: Sidanalys) -> list[Rad]:
    under = [(f, a) for f, a, _ in s.poster if f.position_kand and not f.ovanfor_veck]
    if not under:
        return []
    vikt = sum(a.totalvikt for _, a in under)
    en = len(under) == 1
    return [
        Rad(
            rubrik=(
                "Ladda annonsen först när besökaren närmar sig den"
                if en
                else f"Ladda de {len(under)} annonserna under vecket först när de behövs"
            ),
            varfor=(
                f"{'Annonsen' if en else 'Annonserna'} ligger under vecket men "
                f"{'laddas' if en else 'laddas alla'} direkt — {fmt(vikt)} som konkurrerar med "
                "sidans eget innehåll om bandbredden, trots att många besökare aldrig "
                "scrollar dit."
            ),
            gor=[
                "Slå på lazy load för annonsplatsen i Advanced Ads. Pro-tillägget har "
                "inställningen per placering, och då hämtas annonsen först när den närmar "
                "sig skärmen.",
            ]
            + [
                f"Berör {f.id} i {f.plats or 'okänd annonsplats'}, {f.topp_px} px ned på sidan "
                f"({fmt(a.totalvikt)})"
                for f, a in under
            ],
            sparar=vikt,
            allvar="hög" if vikt > 300 * KB else "medel",
            ansvar="sajten",
        )
    ]


@sidregel(85)
def sidrad_tung_ovanfor_veck(s: Sidanalys) -> list[Rad]:
    tunga = [(f, a) for f, a, _ in s.poster if f.ovanfor_veck and a.totalvikt > IAB_INITIAL]
    if not tunga:
        return []
    en = len(tunga) == 1
    return [
        Rad(
            rubrik=(
                "Annonsen överst på sidan är tyngre än riktvärdet"
                if en
                else f"{len(tunga)} annonser överst på sidan är tyngre än riktvärdet"
            ),
            varfor=(
                "Annonser i första skärmbilden laddas samtidigt som sidans huvudinnehåll och "
                "fördröjer när sidan ser färdig ut. Det är ett mått Google väger in i "
                "sökresultaten."
            ),
            gor=[
                f"{f.id} ({f.format} px) väger {fmt(a.totalvikt)} — riktvärdet är {fmt(IAB_INITIAL)}."
                for f, a in tunga
            ]
            + [
                "Gör annonsen lättare enligt råden längre ned, eller flytta annonsplatsen "
                "längre ned på sidan i Advanced Ads.",
            ],
            sparar=0,
            allvar="hög",
            ansvar="ni",
        )
    ]


@sidregel(80)
def sidrad_typsnittsberg(s: Sidanalys) -> list[Rad]:
    if len(s.poster) < 2:
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
            rubrik=f"Annonserna på sidan laddar {antal(len(familjer), 'typsnitt', 'typsnitt')} för {fmt(vikt)}",
            varfor=(
                f"Tillsammans hämtar annonserna {antal(len(unika), 'typsnittsfil', 'typsnittsfiler')} "
                f"i {antal(len(familjer), 'familj', 'familjer')}: {', '.join(sorted(familjer))}."
            ),
            gor=[
                "Bestäm två husteckensnitt för alla era annonser. Samma typsnitt hämtas från "
                "samma adress hos BannerBoo, och laddas då bara en gång för hela sidan.",
                "Råden för varje annons längre ned visar vilka ord som kan göras som SVG i stället.",
            ],
            sparar=0,
            allvar="hög" if vikt > 400 * KB else "medel",
            ansvar="ni",
        )
    ]


@sidregel(70)
def sidrad_ingen_delning(s: Sidanalys) -> list[Rad]:
    if len(s.poster) < 2 or s.vinst_av_delning > 20 * KB:
        return []
    return [
        Rad(
            rubrik="Annonserna på sidan delar nästan inga filer",
            varfor=(
                f"Sidans {len(s.poster)} annonser återanvänder bara {fmt(s.vinst_av_delning)} "
                "mellan sig. Varje annons drar med sig sina egna typsnitt."
            ),
            gor=[
                "Använd samma typsnitt i era annonser. Samma typsnitt från BannerBoo hämtas "
                "från samma adress och laddas då en gång för hela sidan.",
            ],
            sparar=0,
            allvar="medel",
            ansvar="ni",
        )
    ]


def samla_sidrad(s: Sidanalys) -> list[Rad]:
    rad: list[Rad] = []
    for _prio, fn in sorted(SIDRAD_REGLER, key=lambda x: -x[0]):
        try:
            rad.extend(fn(s) or [])
        except Exception as fel:
            s.varningar.append(f"sidregeln {fn.__name__} kraschade: {fel}")
    for r in rad:
        r.gor = [steg for steg in r.gor if steg]
    rad.sort(key=_ansvarsnyckel)
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
        p(f"  {'':<16}{fynd.lage}")
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
        nummer = 0
        for rubrik, lista in gruppera_rad(s.sidrad):
            p("")
            p(f"  ── {rubrik.upper()} ".ljust(W - 2, "─"))
            for r in lista:
                nummer += 1
                vinst = f"−{fmt(r.sparar)}" if r.sparar else "—"
                p("")
                p(f"  {nummer}. {marke.get(r.allvar, '   ')} {r.rubrik}   [{vinst}]")
                p(f"      {r.varfor}")
                for steg in r.gor:
                    p(f"      · {steg}")
    if len(s.poster) == 1:
        p("")
        p("  Råden för själva annonsen står under annonsen nedan.")

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
        lage = fynd.lage
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
        u.append(html_rad(s.sidrad, "Råd för sidan som helhet", e))
    if len(s.poster) == 1:
        u.append("<p class='varfor'>Råden för själva annonsen står under annonsen nedan.</p>")

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
        print(f"  {'Annons':<44}{'Vikt':>12}  {'Betyg':>6}  {'Själva':>10}")
        for a, rad, _ in sorted(
            (r for r in resultat if not r[0].misslyckande), key=lambda x: -x[0].totalvikt
        ):
            print(
                f"  {a.kalla[-44:]:<44}{fmt(a.totalvikt):>12}  "
                f"{satt_betyg(a.totalvikt)[0]:>6}  {fmt(a.potential_egen):>10}"
            )
        print("")


if __name__ == "__main__":
    main()
