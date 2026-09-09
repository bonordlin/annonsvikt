#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uppdatering.py — kollar om en nyare Annonsvikt finns och hämtar den.

Manifestet ligger på en fast adress hos GitHub som alltid pekar på den senaste
releasen, så ingen inloggning och ingen API-nyckel behövs:

    https://github.com/bonordlin/annonsvikt/releases/latest/download/version.json

Programmet laddar ner och kör en körbar fil. Därför är kontrollerna här inte
valfria: bara https, bara GitHubs värdar, och SHA-256 måste stämma mot
manifestet. Stämmer den inte körs filen aldrig.

Bara standardbiblioteket, precis som resten av programmet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MANIFEST_URL = (
    "https://github.com/bonordlin/annonsvikt/releases/latest/download/version.json"
)
RELEASESIDA = "https://github.com/bonordlin/annonsvikt/releases/latest"

# GitHub skickar vidare nedladdningen till sin lagring — alla leden måste tillåtas.
TILLATNA_VARDAR = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)

KONTROLLINTERVALL = 24 * 3600  # högst en kontroll per dygn
MAX_NEDLADDNING = 200 * 1024 * 1024  # en installerare är ~2 MB; taket är en spärr
TIDSGRANS = 15

# Sätts vid provkörning för att peka på ett lokalt manifest i stället.
MILJOVARIABEL = "ANNONSVIKT_UPPDATERING_URL"

_MONSTER_VERSION = re.compile(r"^\d+(\.\d+){0,3}$")
_MONSTER_SHA = re.compile(r"^[0-9a-f]{64}$", re.I)


class Uppdateringsfel(Exception):
    """Något gick fel som användaren behöver få veta."""


# ── Inställningar ────────────────────────────────────────────────────────────


def installningsmapp() -> str:
    """Utanför installationsmappen — annars raderas inställningarna av en uppdatering."""
    bas = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.config")
    return os.path.join(bas, "Annonsvikt")


def installningsfil() -> str:
    return os.path.join(installningsmapp(), "installningar.json")


def las_installningar() -> dict:
    try:
        with open(installningsfil(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def spara_installningar(d: dict) -> None:
    try:
        os.makedirs(installningsmapp(), exist_ok=True)
        with open(installningsfil(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # kan inte skrivas: strunta i det, uppdateringen fungerar ändå


# ── Versionsjämförelse ───────────────────────────────────────────────────────


def version_tupel(text: str) -> tuple:
    """1.10.0 är nyare än 1.9.0 — därför tal, inte text."""
    delar = []
    for bit in str(text).strip().lstrip("vV").split("."):
        try:
            delar.append(int(bit))
        except ValueError:
            delar.append(0)
    return tuple(delar)


def ar_nyare(kandidat: str, nuvarande: str) -> bool:
    return version_tupel(kandidat) > version_tupel(nuvarande)


# ── Adresskontroll ───────────────────────────────────────────────────────────


def manifestadress() -> tuple[str, bool]:
    """Returnerar (adress, lokalt_prov). Miljövariabeln finns för provkörning."""
    egen = os.environ.get(MILJOVARIABEL, "").strip()
    if egen:
        return egen, True
    return MANIFEST_URL, False


def _kontrollera_adress(url: str, lokalt_prov: bool) -> None:
    """Vägrar allt som inte är https mot GitHub — filen kommer att köras."""
    delar = urllib.parse.urlparse(url)
    if lokalt_prov:
        if delar.scheme in ("http", "https") and delar.hostname in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            return
        if delar.scheme == "file":
            return
    if delar.scheme != "https":
        raise Uppdateringsfel(f"Uppdateringar hämtas bara över https, inte {delar.scheme}.")
    värd = (delar.hostname or "").lower()
    if värd not in TILLATNA_VARDAR and not värd.endswith(".githubusercontent.com"):
        raise Uppdateringsfel(f"Adressen {värd} är inte en av GitHubs — hämtar inget därifrån.")


def _oppna(url: str, lokalt_prov: bool):
    _kontrollera_adress(url, lokalt_prov)
    begaran = urllib.request.Request(
        url, headers={"User-Agent": "Annonsvikt uppdateringskontroll", "Accept": "*/*"}
    )
    svar = urllib.request.urlopen(begaran, timeout=TIDSGRANS)
    # Efter omdirigeringar: kontrollera var vi faktiskt hamnade.
    _kontrollera_adress(svar.geturl(), lokalt_prov)
    return svar


# ── Manifestet ───────────────────────────────────────────────────────────────


def granska_manifest(rå: dict) -> dict:
    """Ett manifest vi inte förstår är ett manifest vi inte litar på."""
    if not isinstance(rå, dict):
        raise Uppdateringsfel("Manifestet har fel form.")
    version = str(rå.get("version", "")).strip().lstrip("vV")
    url = str(rå.get("url", "")).strip()
    sha = str(rå.get("sha256", "")).strip().lower()
    try:
        storlek = int(rå.get("storlek", 0))
    except (TypeError, ValueError):
        storlek = 0

    if not _MONSTER_VERSION.match(version):
        raise Uppdateringsfel(f"Manifestet saknar ett begripligt versionsnummer ({version!r}).")
    if not url:
        raise Uppdateringsfel("Manifestet saknar adress till installeraren.")
    if not _MONSTER_SHA.match(sha):
        raise Uppdateringsfel("Manifestet saknar en giltig SHA-256-summa.")
    if storlek <= 0 or storlek > MAX_NEDLADDNING:
        raise Uppdateringsfel(f"Manifestet anger en orimlig filstorlek ({storlek} byte).")

    nyheter = rå.get("nyheter") or []
    if isinstance(nyheter, str):
        nyheter = [nyheter]

    return {
        "version": version,
        "url": url,
        "sha256": sha,
        "storlek": storlek,
        "datum": str(rå.get("datum", "")),
        "nyheter": [str(n) for n in nyheter][:8],
        "releasesida": str(rå.get("releasesida") or RELEASESIDA),
    }


def hamta_manifest() -> dict:
    url, lokalt = manifestadress()
    try:
        with _oppna(url, lokalt) as svar:
            rå = json.loads(svar.read(256 * 1024).decode("utf-8"))
    except Uppdateringsfel:
        raise
    except urllib.error.HTTPError as fel:
        if fel.code == 404:
            # Vanligast innan den första releasen är publicerad.
            raise Uppdateringsfel(
                "Det finns ingen publicerad version att jämföra med ännu."
            ) from fel
        raise Uppdateringsfel(
            f"Uppdateringsservern svarade med fel {fel.code}."
        ) from fel
    except (urllib.error.URLError, OSError) as fel:
        raise Uppdateringsfel(f"Kunde inte nå uppdateringsservern: {fel}") from fel
    except (json.JSONDecodeError, UnicodeDecodeError) as fel:
        raise Uppdateringsfel("Uppdateringsservern svarade med något oläsbart.") from fel
    return granska_manifest(rå)


def finns_uppdatering(nuvarande: str, tvinga: bool = False) -> dict | None:
    """Manifestet om något nyare finns, annars None.

    Utan `tvinga` görs ingen kontroll oftare än en gång per dygn, och en version
    användaren valt att hoppa över nämns inte igen."""
    inst = las_installningar()
    if not tvinga:
        if inst.get("avstangd"):
            return None
        senast = float(inst.get("senast_kollad") or 0)
        if time.time() - senast < KONTROLLINTERVALL:
            return None

    manifest = hamta_manifest()

    inst["senast_kollad"] = time.time()
    inst["senast_sedda_version"] = manifest["version"]
    spara_installningar(inst)

    if not ar_nyare(manifest["version"], nuvarande):
        return None
    if not tvinga and inst.get("overhoppad") == manifest["version"]:
        return None
    return manifest


def hoppa_over(version: str) -> None:
    inst = las_installningar()
    inst["overhoppad"] = version
    spara_installningar(inst)


# ── Nedladdning ──────────────────────────────────────────────────────────────


def hamta_installerare(manifest: dict, framsteg=None) -> str:
    """Laddar ner installeraren och kontrollerar den. Returnerar sökvägen.

    `framsteg` anropas med (hämtade byte, totalt) om den är angiven."""
    _url, lokalt = manifestadress()
    mapp = os.path.join(tempfile.gettempdir(), "annonsvikt-uppdatering")
    os.makedirs(mapp, exist_ok=True)
    mal = os.path.join(mapp, f"AnnonsviktSetup-{manifest['version']}.exe")

    summa = hashlib.sha256()
    hamtat = 0
    try:
        with _oppna(manifest["url"], lokalt) as svar, open(mal, "wb") as f:
            while True:
                bit = svar.read(64 * 1024)
                if not bit:
                    break
                hamtat += len(bit)
                if hamtat > MAX_NEDLADDNING:
                    raise Uppdateringsfel("Nedladdningen är orimligt stor — avbryter.")
                summa.update(bit)
                f.write(bit)
                if framsteg:
                    framsteg(hamtat, manifest["storlek"])
    except Uppdateringsfel:
        _radera(mal)
        raise
    except (urllib.error.URLError, OSError) as fel:
        _radera(mal)
        raise Uppdateringsfel(f"Nedladdningen misslyckades: {fel}") from fel

    if hamtat != manifest["storlek"]:
        _radera(mal)
        raise Uppdateringsfel(
            f"Filen har fel storlek: {hamtat} byte mot {manifest['storlek']} i manifestet. "
            "Nedladdningen avbröts eller filen är utbytt — installerar inget."
        )
    if summa.hexdigest() != manifest["sha256"]:
        _radera(mal)
        raise Uppdateringsfel(
            "Checksumman stämmer inte med manifestet. Filen kan ha skadats under "
            "nedladdningen eller bytts ut. Den körs inte."
        )
    return mal


def _radera(stig: str) -> None:
    try:
        os.remove(stig)
    except OSError:
        pass


# ── Installation ─────────────────────────────────────────────────────────────


def starta_installation(exe: str) -> None:
    """Startar installeraren fristående och lämnar över.

    Anroparen måste avsluta programmet direkt efteråt: den körande appen låser
    venv\\Scripts\\pythonw.exe, och då kan installeraren inte byta ut den."""
    if not os.path.isfile(exe):
        raise Uppdateringsfel("Installationsfilen hittades inte.")
    flaggor = 0
    if sys.platform == "win32":
        flaggor = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [exe, "/SILENT", "/NORESTART", "/STARTAPP=1"],
            close_fds=True,
            creationflags=flaggor,
        )
    except OSError as fel:
        raise Uppdateringsfel(f"Installeraren kunde inte startas: {fel}") from fel


# ── Kommandorad ──────────────────────────────────────────────────────────────


def kolla_fran_kommandoraden(nuvarande: str) -> int:
    """Används av `annonsvikt --kolla-uppdatering`."""
    try:
        manifest = finns_uppdatering(nuvarande, tvinga=True)
    except Uppdateringsfel as fel:
        print(f"  {fel}")
        return 1
    if not manifest:
        print(f"  Annonsvikt {nuvarande} är den senaste versionen.")
        return 0
    print(f"  Version {manifest['version']} finns — du kör {nuvarande}.")
    if manifest["datum"]:
        print(f"  Utgiven {manifest['datum']}.")
    for rad in manifest["nyheter"]:
        print(f"    · {rad}")
    print(f"  Hämta den i programmet, eller från {manifest['releasesida']}")
    return 0
