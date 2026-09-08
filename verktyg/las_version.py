#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skriver ut programmets versionsnummer.

Versionen står på ett enda ställe — VERSION i annonsvikt.py. Bygget läser den
härifrån och skickar den vidare till Inno Setup, så att guiden och posten i
Appar och funktioner alltid stämmer med koden.

    python verktyg/las_version.py
"""

import os
import re
import sys

rot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
kalla = open(os.path.join(rot, "annonsvikt.py"), encoding="utf-8").read()
träff = re.search(r'^VERSION\s*=\s*"([^"]+)"', kalla, re.M)
if not träff:
    print("kunde inte hitta VERSION i annonsvikt.py", file=sys.stderr)
    sys.exit(1)
print(träff.group(1))
