#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skapa_manifest.py — skriver dist/version.json för den byggda installeraren.

Manifestet är det appen läser för att se om en nyare version finns. Versionen
hämtas från VERSION i annonsvikt.py — samma enda källa som styr installerarens
versionsnummer — och SHA-256 räknas på den exe-fil som just byggts.

Nyheterna i listan tas från NYHETER.md om den finns: raderna under den översta
rubriken som matchar versionen.

    python verktyg/skapa_manifest.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys

ROT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROT, "dist", "AnnonsviktSetup.exe")
MAL = os.path.join(ROT, "dist", "version.json")
KONTO = "bonordlin"
REPO = "annonsvikt"


def las_version() -> str:
    kalla = open(os.path.join(ROT, "annonsvikt.py"), encoding="utf-8").read()
    träff = re.search(r'^VERSION\s*=\s*"([^"]+)"', kalla, re.M)
    if not träff:
        sys.exit("kunde inte hitta VERSION i annonsvikt.py")
    return träff.group(1)


def las_nyheter(version: str) -> list[str]:
    """Punkterna under rubriken för den här versionen i NYHETER.md."""
    stig = os.path.join(ROT, "NYHETER.md")
    if not os.path.exists(stig):
        return []
    rader = open(stig, encoding="utf-8").read().splitlines()
    samlar, ut = False, []
    for rad in rader:
        if rad.startswith("## "):
            if samlar:
                break
            samlar = version in rad
            continue
        if samlar:
            putsad = rad.strip()
            if putsad.startswith(("-", "*")):
                ut.append(putsad.lstrip("-* ").strip())
    return ut[:8]


def main() -> None:
    if not os.path.exists(EXE):
        sys.exit(f"{EXE} saknas — kör bygg-installerare.cmd först.")

    version = las_version()
    data = open(EXE, "rb").read()

    manifest = {
        "version": version,
        "url": (
            f"https://github.com/{KONTO}/{REPO}/releases/download/"
            f"v{version}/AnnonsviktSetup.exe"
        ),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storlek": len(data),
        "datum": dt.date.today().isoformat(),
        "nyheter": las_nyheter(version),
        "releasesida": f"https://github.com/{KONTO}/{REPO}/releases/tag/v{version}",
    }

    with open(MAL, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"  version.json skriven för {version} ({len(data)} byte)")
    print(f"  sha256: {manifest['sha256']}")
    if not manifest["nyheter"]:
        print("  (inga nyheter hittades — lägg en rubrik för versionen i NYHETER.md)")


if __name__ == "__main__":
    main()
