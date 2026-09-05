#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skapa_ikon.py — ritar programikonen och skriver installer/annonsvikt.ico.

Ingen bildbehandlingsmodul behövs: PNG skrivs för hand med zlib, och ICO-formatet
tillåter PNG som nyttolast sedan Vista. Motivet är samma stapeldiagram som
rapporten visar — tre staplar i fallande längd på Upphandling24:s lila.

    python verktyg/skapa_ikon.py
"""

from __future__ import annotations

import os
import struct
import zlib

LILA = (127, 59, 143)  # #7f3b8f — samma lila som i bannern
VIT = (255, 255, 255)
STORLEKAR = (16, 24, 32, 48, 64, 128, 256)


DELPROV = 4  # 4×4 delprov per bildpunkt ger släta kanter även i 16 px


def rita(storlek: int) -> bytes:
    """Ritar ikonen som RGBA-byte med kantutjämning genom överprovtagning."""
    s = float(storlek)
    radie = s * 0.20
    # Staplarnas geometri i andelar av kanten, så motivet skalar likadant överallt.
    vanster = s * 0.21
    hojd = s * 0.115
    mellanrum = s * 0.075
    langder = (0.58, 0.42, 0.26)  # fallande, som vikt per del i rapporten
    forsta_topp = (s - (3 * hojd + 2 * mellanrum)) / 2
    staplar = [
        (vanster, forsta_topp + n * (hojd + mellanrum), vanster + s * langd, forsta_topp + n * (hojd + mellanrum) + hojd)
        for n, langd in enumerate(langder)
    ]

    def i_platta(x: float, y: float) -> bool:
        """Innanför den rundade kvadraten? Närmaste hörnmittpunkt avgör."""
        cx = min(max(x, radie), s - radie)
        cy = min(max(y, radie), s - radie)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radie * radie

    def i_stapel(x: float, y: float) -> bool:
        return any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in staplar)

    px = bytearray(storlek * storlek * 4)
    steg = 1.0 / DELPROV
    prov = DELPROV * DELPROV
    for py in range(storlek):
        for pxx in range(storlek):
            innanfor = 0
            vit = 0
            for dy in range(DELPROV):
                y = py + (dy + 0.5) * steg
                for dx in range(DELPROV):
                    x = pxx + (dx + 0.5) * steg
                    if i_platta(x, y):
                        innanfor += 1
                        if i_stapel(x, y):
                            vit += 1
            if not innanfor:
                continue
            # Vitandelen blandas mot lila, och hela punkten mot genomskinligt.
            andel_vit = vit / innanfor
            farg = tuple(
                round(LILA[k] + (VIT[k] - LILA[k]) * andel_vit) for k in range(3)
            )
            i = (py * storlek + pxx) * 4
            px[i], px[i + 1], px[i + 2] = farg
            px[i + 3] = round(255 * innanfor / prov)
    return bytes(px)


def till_png(storlek: int, rgba: bytes) -> bytes:
    """Minsta möjliga giltiga PNG: signatur, IHDR, IDAT, IEND."""

    def block(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    rader = b"".join(
        b"\x00" + rgba[y * storlek * 4 : (y + 1) * storlek * 4] for y in range(storlek)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + block(b"IHDR", struct.pack(">IIBBBBB", storlek, storlek, 8, 6, 0, 0, 0))
        + block(b"IDAT", zlib.compress(rader, 9))
        + block(b"IEND", b"")
    )


def till_ico(bilder: list[tuple[int, bytes]]) -> bytes:
    """Packar flera PNG-bilder till en ICO-fil."""
    huvud = struct.pack("<HHH", 0, 1, len(bilder))
    poster, data, förskjutning = b"", b"", 6 + 16 * len(bilder)
    for storlek, png in bilder:
        poster += struct.pack(
            "<BBBBHHII",
            0 if storlek >= 256 else storlek,  # 0 betyder 256 i ICO-huvudet
            0 if storlek >= 256 else storlek,
            0, 0, 1, 32, len(png), förskjutning,
        )
        data += png
        förskjutning += len(png)
    return huvud + poster + data


def main() -> None:
    rot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mal = os.path.join(rot, "installer", "annonsvikt.ico")
    os.makedirs(os.path.dirname(mal), exist_ok=True)
    bilder = [(s, till_png(s, rita(s))) for s in STORLEKAR]
    with open(mal, "wb") as f:
        f.write(till_ico(bilder))
    print(f"skrev {mal} ({os.path.getsize(mal)} byte, {len(bilder)} storlekar)")


if __name__ == "__main__":
    main()
