#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annonsvikt_gui.py — grafiskt gränssnitt till annonsvikt.

Klistra in en annonslänk, tryck Mät, och se vikten per del, betyget och råden.
Mätmotorn är exakt densamma som i kommandoradsversionen.

Körs så här:
    python annonsvikt_gui.py
    python annonsvikt_gui.py b990849fd8c4b      # förifyllt fält

Kräver bara det som annonsvikt.py redan kräver — tkinter ingår i Python.
"""

from __future__ import annotations

import base64
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

import annonsvikt as av
import uppdatering as upp

# ── Utseende ──────────────────────────────────────────────────────────────────

BG = "#fbfaf8"
KORT = "#ffffff"
LINJE = "#e6e1da"
TEXT = "#1c1a17"
SVAG = "#6b6560"
ACCENT = "#2b5f8f"

BETYGSFARG = {
    "A": "#1f7a4d",
    "B": "#5c8a2f",
    "C": "#c08a1e",
    "D": "#c85a2b",
    "F": "#b8342a",
}

ALLVARSFARG = {
    "kritisk": "#b8342a",
    "hög": "#c85a2b",
    "medel": "#c08a1e",
    "låg": SVAG,
}

# Bakom förhandsgranskade bilder: vita och genomskinliga motiv syns inte mot vitt.
BILDBAKGRUND = "#e9e4dc"

# Notisen om ny version ska synas — den är hela poängen med att kolla.
UPPD_BG = "#1f7a4d"
UPPD_TEXT = "#ffffff"
UPPD_SVAG = "#cfe6d8"

BRODTEXT = ("Segoe UI", 10)
RUBRIK = ("Segoe UI", 10, "bold")
TABELL = ("Consolas", 10)


APP_ID = "Upphandling24.Annonsvikt"


def gor_dpi_medveten() -> None:
    """Utan det här skalar Windows upp fönstret och det växer ur skärmen."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-DPI
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def satt_app_id() -> None:
    """Ger programmet en egen plats i aktivitetsfältet i stället för pythonw.exe:s."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def ikonsokvag() -> str:
    """Ikonen ligger bredvid programfilen efter installation."""
    for mapp in (os.path.dirname(os.path.abspath(__file__)),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer")):
        stig = os.path.join(mapp, "annonsvikt.ico")
        if os.path.exists(stig):
            return stig
    return ""


# Mellanlisten mellan tabell och förhandsvy.
GREPP = "#a89f93"
GREPP_BG = "#f1ede7"
GREPP_AKTIV_BG = "#eaf1f7"
LISTLINJE = "#d9d2c8"

# En avkodad bildruta tar fyra byte per bildpunkt. Ryms inte alla rutor under
# taket avkodas de i stället när de ska visas.
MINNESTAK_RUTOR = 64 * 1024 * 1024

# Animationen tar några MB per annons och sparas bara för de senaste.
BEHALL_ANIMATIONER = 6


def png_matt(data: bytes) -> tuple[int, int]:
    """Bildens mått ur PNG-huvudet, utan att avkoda bilden."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def valj_skalning(bredd: int, hojd: int, maxbredd: int, maxhojd: int,
                  forstoring: int = 1) -> tuple[int, int]:
    """(zoom, subsample) som får bilden att rymmas.

    Tk skalar bara i heltalssteg. Zoom och subsample går inte att kombinera till
    ett bråk: Tk hoppar då först över bildpunkter och förstorar sedan det som
    blev kvar, vilket blir grövre än bara subsample."""
    maxbredd, maxhojd = max(1, maxbredd), max(1, maxhojd)
    if bredd <= 0 or hojd <= 0:
        return 1, 1
    if bredd > maxbredd or hojd > maxhojd:
        return 1, max(-(-bredd // maxbredd), -(-hojd // maxhojd))
    return max(1, min(forstoring, maxbredd // bredd, maxhojd // hojd)), 1


class Bildspelare:
    """Visar annonsen i en etikett: en stillbild, eller animationen i sin egen takt.

    Rutorna avkodas i den storlek ytan har och i små omgångar, så att fönstret
    aldrig hackar. En ruta visas så länge den faktiskt stod kvar när den fångades."""

    def __init__(self, yta: tk.Label, minnestak: int = MINNESTAK_RUTOR):
        self.yta = yta
        self.rot = yta._root()
        self.minnestak = minnestak
        self.rutor: list = []  # [(png, ms), …]
        self.index = 0
        self.pausad = False
        self.skalning = (1, 1)  # (zoom, subsample)
        self._forstoring = 1
        self._cache: dict = {}
        self._ordning: list = []
        self._kapacitet = 1
        self._visad = None  # rutan som visas nu, så att Tk inte slänger den
        self._tick_jobb = None
        self._forbered_jobb = None
        self._forbered_i = 0

    @property
    def animerad(self) -> bool:
        return len(self.rutor) > 1

    @property
    def matt(self) -> tuple[int, int]:
        return png_matt(self.rutor[0][0]) if self.rutor else (0, 0)

    @property
    def aktuell_png(self) -> bytes | None:
        return self.rutor[self.index][0] if self.rutor else None

    def visa(self, rutor: list, maxbredd: int, maxhojd: int,
             forstoring: int = 1, start: int = 0) -> bool:
        """Byter innehåll. Returnerar False om bilden inte gick att visa."""
        self.stoppa()
        self.rutor = [r for r in rutor if r and r[0]]
        if not self.rutor:
            return False
        self.index = start % len(self.rutor)
        self.pausad = False
        self._forstoring = forstoring
        self._satt_skalning(maxbredd, maxhojd)
        if not self._visa_ruta(self.index):
            self.stoppa()
            return False
        self._starta_forberedelse()
        if self.animerad:
            self._tick_jobb = self.rot.after(self.rutor[self.index][1], self._tick)
        return True

    def anpassa(self, maxbredd: int, maxhojd: int, forstoring: int | None = None) -> None:
        """Samma innehåll i en ny storlek. Avkodar bara om skalningen ändras."""
        if not self.rutor:
            return
        if forstoring is not None:
            self._forstoring = forstoring
        tidigare = self.skalning
        self._satt_skalning(maxbredd, maxhojd)
        if self.skalning == tidigare:
            return
        self._cache.clear()
        self._ordning.clear()
        self._visa_ruta(self.index)
        self._starta_forberedelse()

    def vaxla_paus(self) -> bool:
        """Pausar eller spelar vidare. Returnerar True när uppspelningen står still."""
        if not self.animerad:
            return False
        self.pausad = not self.pausad
        if self.pausad:
            self._avbryt("_tick_jobb")
        elif self._tick_jobb is None:
            self._tick_jobb = self.rot.after(self.rutor[self.index][1], self._tick)
        return self.pausad

    def stoppa(self) -> None:
        self._avbryt("_tick_jobb")
        self._avbryt("_forbered_jobb")
        self._cache.clear()
        self._ordning.clear()
        self.rutor = []
        try:
            self.yta.configure(image="")
        except tk.TclError:
            pass
        self._visad = None

    def _avbryt(self, namn: str) -> None:
        jobb = getattr(self, namn)
        if jobb is not None:
            try:
                self.rot.after_cancel(jobb)
            except tk.TclError:
                pass
            setattr(self, namn, None)

    def _satt_skalning(self, maxbredd: int, maxhojd: int) -> None:
        bredd, hojd = self.matt
        self.skalning = valj_skalning(bredd, hojd, maxbredd, maxhojd, self._forstoring)
        zoom, sub = self.skalning
        per_ruta = max(1, (bredd * zoom // sub) * (hojd * zoom // sub) * 4)
        self._kapacitet = max(1, self.minnestak // per_ruta)

    def _bild(self, i: int):
        bild = self._cache.get(i)
        if bild is not None:
            return bild
        try:
            hel = tk.PhotoImage(master=self.yta, data=base64.b64encode(self.rutor[i][0]))
        except (tk.TclError, IndexError):
            return None
        zoom, sub = self.skalning
        bild = hel.subsample(sub) if sub > 1 else (hel.zoom(zoom) if zoom > 1 else hel)
        self._cache[i] = bild
        self._ordning.append(i)
        while len(self._ordning) > self._kapacitet:
            self._cache.pop(self._ordning.pop(0), None)
        return bild

    def _visa_ruta(self, i: int) -> bool:
        bild = self._bild(i)
        if bild is None:
            return False
        try:
            self.yta.configure(image=bild, text="")
        except tk.TclError:
            return False
        self._visad = bild
        return True

    def _tick(self) -> None:
        self._tick_jobb = None
        if not self.animerad or self.pausad:
            return
        try:
            synlig = self.yta.winfo_viewable()
        except tk.TclError:
            self.stoppa()
            return
        if not synlig:
            # Annan flik eller minimerat fönster: vänta utan att rita.
            self._tick_jobb = self.rot.after(250, self._tick)
            return
        start = time.perf_counter()
        self.index = (self.index + 1) % len(self.rutor)
        self._visa_ruta(self.index)
        atgang = int((time.perf_counter() - start) * 1000)
        self._tick_jobb = self.rot.after(max(10, self.rutor[self.index][1] - atgang), self._tick)

    def _starta_forberedelse(self) -> None:
        self._avbryt("_forbered_jobb")
        if self.animerad and self._kapacitet >= len(self.rutor):
            self._forbered_i = 0
            self._forbered_jobb = self.rot.after(1, self._forbered)

    def _forbered(self) -> None:
        """Avkodar rutorna i förväg, några i taget mellan fönstrets övriga arbete."""
        self._forbered_jobb = None
        start = time.perf_counter()
        while self._forbered_i < len(self.rutor):
            if self._bild(self._forbered_i) is None:
                return
            self._forbered_i += 1
            if time.perf_counter() - start > 0.02:
                break
        if self._forbered_i < len(self.rutor):
            self._forbered_jobb = self.rot.after(1, self._forbered)


class Delning(tk.Frame):
    """Två ytor sida vid sida med en mellanlist emellan som går att dra i.

    Tk:s PanedWindow ritar mellanlisten som en slät yta i bakgrundens färg, och
    den gick inte att hitta. Den här har ett synligt grepp som färgas när musen
    är över. Läget sparas till nästa start, och dubbelklick återställer det.

    Utan standardandel får vänster sida den bredd innehållet ber om, tills
    listen dras."""

    def __init__(self, foralder, nyckel: str, standardandel: float | None,
                 min_vanster: int = 200, min_hoger: int = 220):
        super().__init__(foralder, bg=BG, bd=0, highlightthickness=0)
        skala = max(1.0, self.winfo_fpixels("1i") / 96)
        self.listbredd = round(14 * skala)
        self.skala = skala
        self.nyckel = nyckel
        self.standardandel = standardandel
        self.min_vanster = round(min_vanster * skala)
        self.min_hoger = round(min_hoger * skala)
        self.andel = self._sparad_andel()
        self._drag = None
        self._over = False

        self.vanster = ttk.Frame(self)
        self.hoger = ttk.Frame(self)
        self.list = tk.Canvas(
            self, width=self.listbredd, bg=BG, bd=0, highlightthickness=0,
            cursor="sb_h_double_arrow",
        )
        self.list.bind("<Configure>", lambda _e: self._rita_list())
        self.list.bind("<Enter>", lambda _e: self._markera(True))
        self.list.bind("<Leave>", lambda _e: self._markera(False))
        self.list.bind("<ButtonPress-1>", self._borja_dra)
        self.list.bind("<B1-Motion>", self._dra)
        self.list.bind("<ButtonRelease-1>", self._slapp)
        self.list.bind("<Double-Button-1>", self._aterstall)
        self.bind("<Configure>", lambda _e: self._placera())

    def _sparad_andel(self) -> float | None:
        try:
            return min(0.9, max(0.1, float(upp.las_installningar().get(self.nyckel))))
        except (TypeError, ValueError):
            return self.standardandel

    def _spara(self) -> None:
        installningar = upp.las_installningar()
        if self.andel is None:
            installningar.pop(self.nyckel, None)
        else:
            installningar[self.nyckel] = round(self.andel, 4)
        upp.spara_installningar(installningar)

    def folj_innehall(self) -> None:
        """Nytt innehåll till vänster: ge det sin bredd, om listen inte dragits."""
        if self.andel is not None:
            return
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        self._placera()

    def _begrans(self, x: int, yta: int) -> int:
        if yta >= self.min_vanster + self.min_hoger:
            return max(self.min_vanster, min(x, yta - self.min_hoger))
        # För smalt för båda minimimåtten: dela i deras proportion.
        return round(yta * self.min_vanster / (self.min_vanster + self.min_hoger))

    def _placera(self) -> None:
        bredd = self.winfo_width()
        if bredd < 2:
            return
        yta = max(2, bredd - self.listbredd)
        if self.andel is None:
            onskad = self.vanster.winfo_reqwidth()
            x = onskad if onskad > 60 else yta // 2
        else:
            x = round(yta * self.andel)
        x = max(1, self._begrans(x, yta))
        self.vanster.place(x=0, y=0, width=x, relheight=1)
        self.list.place(x=x, y=0, width=self.listbredd, relheight=1)
        self.hoger.place(x=x + self.listbredd, y=0, width=max(1, yta - x), relheight=1)

    def _rita_list(self) -> None:
        d = self.list
        d.delete("all")
        bredd, hojd = d.winfo_width(), d.winfo_height()
        aktiv = self._over or self._drag is not None
        farg = ACCENT if aktiv else GREPP
        mitt = bredd // 2
        d.create_line(mitt, 0, mitt, hojd, fill=ACCENT if aktiv else LISTLINJE, width=1)
        # Greppet mitt på listen: en ruta med tre prickar, som på en dragbar list.
        halv_b = max(3, round(4 * self.skala))
        halv_h = round(26 * self.skala)
        y = hojd // 2
        d.create_rectangle(
            mitt - halv_b, y - halv_h, mitt + halv_b, y + halv_h,
            fill=GREPP_AKTIV_BG if aktiv else GREPP_BG, outline=farg,
        )
        prick = max(1, round(1.5 * self.skala))
        steg = round(9 * self.skala)
        for dy in (-steg, 0, steg):
            d.create_oval(mitt - prick, y + dy - prick, mitt + prick, y + dy + prick,
                          fill=farg, outline=farg)

    def _markera(self, over: bool) -> None:
        self._over = over
        self._rita_list()

    def _borja_dra(self, händelse) -> None:
        self._drag = (händelse.x_root, self.vanster.winfo_width())
        self._rita_list()

    def _dra(self, händelse) -> None:
        if self._drag is None:
            return
        start_x, start_bredd = self._drag
        yta = max(2, self.winfo_width() - self.listbredd)
        x = self._begrans(start_bredd + händelse.x_root - start_x, yta)
        self.andel = x / yta
        self._placera()

    def _slapp(self, händelse) -> None:
        if self._drag is None:
            return
        self._drag = None
        # Musen kan ha lämnat listen under dragningen.
        self._over = (0 <= händelse.x < self.list.winfo_width()
                      and 0 <= händelse.y < self.list.winfo_height())
        self._rita_list()
        self._spara()

    def _aterstall(self, _händelse=None) -> None:
        self.andel = self.standardandel
        self._placera()
        self._spara()


class Annonsviktsfonster(tk.Tk):
    def __init__(self, forifylld: str = ""):
        super().__init__()
        self.title(f"Annonsvikt {av.VERSION}")
        self.configure(bg=BG)
        ikon = ikonsokvag()
        if ikon:
            try:
                self.iconbitmap(default=ikon)
            except tk.TclError:
                pass
        # Måtten är tänkta vid 100 % skalning. På en skärm med 175 % blev fönstret
        # annars hälften så stort som avsett, och förhandsvyn fick ingen höjd.
        self.skala = max(1.0, self.winfo_fpixels("1i") / 96)
        bredd = min(round(1240 * self.skala), self.winfo_screenwidth() - 60)
        hojd = min(round(830 * self.skala), self.winfo_screenheight() - 80)
        x = max(0, (self.winfo_screenwidth() - bredd) // 2)
        y = max(0, (self.winfo_screenheight() - hojd) // 2 - 20)
        self.geometry(f"{bredd}x{hojd}+{x}+{y}")
        self.minsize(min(round(860 * self.skala), bredd), min(round(620 * self.skala), hojd))
        # Annonsen visas lika stor som i webbläsaren: en CSS-pixel blir lika många
        # skärmpunkter som skalningen säger, i hela steg.
        self._annonszoom = max(1, round(self.skala))

        self.ko: queue.Queue = queue.Queue()
        self.matningar: list[tuple] = []  # (analys, råd, skärmbild)
        self.aktiv: tuple | None = None
        self.korr = False
        self._trad = None  # arbetstråden, så att en tyst död går att upptäcka
        self._uppdatering = None  # manifestet när en nyare version finns
        self._hamtar = False
        self._annonsrutor = []  # det förhandsvyn visar: [(png, ms), …]
        self._annonsanalys = None
        self._annonsvy_jobb = None  # omskalning när ytan slutat ändra storlek
        self._bildinfo_bredd = 0
        self._forhandsbild = None  # förhandsgranskningen i Filer-fliken
        self._forhandskalla = None  # (base64, filnamn) för den större vyn
        self._forhandshel = None  # oskalad förhandsbild, ritas om när rutan ändrar storlek
        self._forhandsmatt = (0, 0)
        self._resurs_per_rad = {}  # rad i fillistan → Resurs

        self._stil()
        self._bygg_uppdateringsrad()
        self._bygg_topp(forifylld)
        self._bygg_betygskort()
        self._bygg_flikar()
        self._bygg_botten()
        self._satt_knapplage()

        self.bind("<Return>", lambda _e: self.starta_matning())
        self.after(100, self._tom_ko)
        # Kollar i bakgrunden strax efter start — fönstret ska aldrig vänta på nätet.
        self.after(2000, lambda: self._starta_uppdateringskontroll(tvinga=False))

    # ── Uppbyggnad ────────────────────────────────────────────────────────────

    def _stil(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=TEXT, font=BRODTEXT)
        s.configure("TFrame", background=BG)
        s.configure("Kort.TFrame", background=KORT, relief="solid", borderwidth=1)
        # Ytor inuti kortet: utan egen ram, som annars ritades rakt genom texten.
        s.configure("KortYta.TFrame", background=KORT)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Kort.TLabel", background=KORT)
        s.configure("Svag.TLabel", foreground=SVAG)
        s.configure("SvagKort.TLabel", background=KORT, foreground=SVAG)
        s.configure("Rubrik.TLabel", font=("Segoe UI", 11, "bold"))
        s.configure("Treeview", font=TABELL, rowheight=round(23 * self.skala),
                    fieldbackground=KORT, background=KORT)
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(16, 7), font=BRODTEXT)
        s.configure("TButton", padding=(12, 5))
        s.configure("Kor.TButton", padding=(18, 6), font=RUBRIK)

    def _bygg_uppdateringsrad(self) -> None:
        """Byggs dold. Visas först när en nyare version faktiskt hittats."""
        self.uppdateringsrad = tk.Frame(self, bg=UPPD_BG)

        inre = tk.Frame(self.uppdateringsrad, bg=UPPD_BG)
        inre.pack(fill="x", padx=16, pady=10)

        # Knapparna packas före texten. Pack fördelar utrymme i packordning, och
        # det som packas sist får bara det som blir över — i ett smalt fönster
        # ingenting alls.
        knappar = tk.Frame(inre, bg=UPPD_BG)
        knappar.pack(side="right", padx=(12, 0))

        text = tk.Frame(inre, bg=UPPD_BG)
        text.pack(side="left", fill="both", expand=True)
        text.bind("<Configure>", self._anpassa_notistext)
        self.uppd_rubrik = tk.Label(
            text, text="", bg=UPPD_BG, fg=UPPD_TEXT,
            font=("Segoe UI", 12, "bold"), anchor="w", justify="left",
        )
        self.uppd_rubrik.pack(anchor="w")
        self.uppd_nyheter = tk.Label(
            text, text="", bg=UPPD_BG, fg=UPPD_SVAG,
            font=("Segoe UI", 9), anchor="w", justify="left",
        )
        self.uppd_nyheter.pack(anchor="w", fill="x")

        def knapp(txt, kommando, fet=False):
            return tk.Button(
                knappar, text=txt, command=kommando, relief="flat", bd=0,
                bg="#ffffff" if fet else UPPD_BG,
                fg=UPPD_BG if fet else UPPD_TEXT,
                activebackground="#eaf5ef" if fet else UPPD_BG,
                activeforeground=UPPD_BG if fet else UPPD_TEXT,
                font=("Segoe UI", 10, "bold") if fet else ("Segoe UI", 9),
                padx=14 if fet else 8, pady=5, cursor="hand2",
                highlightthickness=0,
            )

        self.uppd_knapp = knapp("Uppdatera nu", self._installera_uppdatering, fet=True)
        self.uppd_knapp.pack(side="left", padx=(0, 8))
        knapp("Vad är nytt", self._oppna_releasesida).pack(side="left")
        knapp("Senare", self._dolj_uppdatering).pack(side="left")

    def _anpassa_notistext(self, händelse) -> None:
        """Radbryt efter tilldelad bredd i stället för att kräva egen."""
        bredd = max(220, händelse.width - 8)
        for etikett in (self.uppd_rubrik, self.uppd_nyheter):
            # Bara vid faktisk ändring: annars kan Configure trigga sig själv.
            if etikett.cget("wraplength") != bredd:
                etikett.configure(wraplength=bredd)

    def _visa_uppdatering(self, manifest: dict) -> None:
        self._uppdatering = manifest
        self.uppd_rubrik.configure(
            text=f"Version {manifest['version']} finns — du kör {av.VERSION}"
        )
        nyheter = "  ·  ".join(manifest.get("nyheter") or [])
        if not nyheter:
            nyheter = "Klicka på Uppdatera nu så hämtas och installeras den."
        self.uppd_nyheter.configure(text=nyheter[:220])
        self.uppdateringsrad.pack(fill="x", before=self._toppram)

    def _dolj_uppdatering(self) -> None:
        self.uppdateringsrad.pack_forget()

    def _oppna_releasesida(self) -> None:
        if self._uppdatering:
            webbrowser.open(self._uppdatering.get("releasesida") or upp.RELEASESIDA)

    # ── Kontroll och hämtning, alltid utanför huvudtråden ────────────────────

    def _starta_uppdateringskontroll(self, tvinga: bool = False) -> None:
        def arbete():
            try:
                manifest = upp.finns_uppdatering(av.VERSION, tvinga=tvinga)
                if manifest:
                    self.ko.put(("uppdatering", manifest))
                elif tvinga:
                    self.ko.put(("uppdateringsbesked",
                                 f"Annonsvikt {av.VERSION} är den senaste versionen."))
            except upp.Uppdateringsfel as fel:
                # Vid automatisk kontroll ska ett nedärvt nät inte störa någon.
                if tvinga:
                    self.ko.put(("uppdateringsfel", str(fel)))
            except BaseException:
                pass

        threading.Thread(target=arbete, daemon=True).start()

    def _sok_uppdatering_manuellt(self) -> None:
        self.status.configure(text="Söker efter uppdateringar …")
        self._starta_uppdateringskontroll(tvinga=True)

    def _installera_uppdatering(self) -> None:
        if not self._uppdatering or self._hamtar:
            return
        if self.korr and not messagebox.askokcancel(
            "Annonsvikt",
            "En mätning pågår. Uppdateringen avbryter den och startar om programmet.\n\n"
            "Vill du fortsätta?",
        ):
            return

        manifest = self._uppdatering
        self._hamtar = True
        self.uppd_knapp.configure(text="Hämtar…", state="disabled")

        def arbete():
            try:
                def framsteg(hamtat, totalt):
                    if totalt:
                        self.ko.put(("status",
                                     f"Hämtar version {manifest['version']} … "
                                     f"{100 * hamtat // totalt} %"))
                exe = upp.hamta_installerare(manifest, framsteg)
                self.ko.put(("uppdatering_hamtad", exe))
            except upp.Uppdateringsfel as fel:
                self.ko.put(("uppdateringsfel", str(fel)))
            except BaseException as fel:
                self.ko.put(("uppdateringsfel", f"{type(fel).__name__}: {fel}"))

        threading.Thread(target=arbete, daemon=True).start()

    def _kor_installationen(self, exe: str) -> None:
        self._hamtar = False
        self.uppd_knapp.configure(text="Uppdatera nu", state="normal")
        manifest = self._uppdatering or {}
        if not messagebox.askokcancel(
            "Annonsvikt",
            f"Version {manifest.get('version', '')} är hämtad och kontrollerad.\n\n"
            "Annonsvikt stängs nu och installationen körs. Programmet startar "
            "sedan om av sig självt.",
        ):
            return
        try:
            upp.starta_installation(exe)
        except upp.Uppdateringsfel as fel:
            messagebox.showerror("Annonsvikt", str(fel))
            return
        # Måste stänga: den körande appen låser filerna installeraren ska byta ut.
        self.destroy()

    def _bygg_topp(self, forifylld: str) -> None:
        ram = ttk.Frame(self, padding=(16, 14, 16, 8))
        ram.pack(fill="x")
        self._toppram = ram  # notisen packas ovanför den här

        ttk.Label(
            ram,
            text="Annonslänk, inbäddningskod, BannerBoo-id eller en sida att skanna",
            style="Svag.TLabel",
        ).grid(
            row=0, column=0, sticky="w", columnspan=2
        )

        self.falt = ttk.Entry(ram, font=("Segoe UI", 11), foreground=SVAG)
        self.falt.grid(row=1, column=0, sticky="ew", pady=(3, 0), ipady=4)
        # Platshållaren måste försvinna av sig själv. Stod adressen kvar som
        # vanlig text kunde en inklistring hamna efter den och ge dubbel domän.
        self._platshallare_syns = False
        self.falt.bind("<FocusIn>", self._tom_platshallare)
        self.falt.bind("<FocusOut>", self._visa_platshallare)
        if forifylld:
            self.falt.configure(foreground=TEXT)
            self.falt.insert(0, forifylld)
        else:
            self._visa_platshallare()
        ram.columnconfigure(0, weight=1)

        self.knapp_mat = ttk.Button(
            ram, text="Mät", style="Kor.TButton", command=self.starta_matning
        )
        self.knapp_mat.grid(row=1, column=1, padx=(10, 0), pady=(3, 0), sticky="e")

        val = ttk.Frame(ram)
        val.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(val, text="Låt annonsen rulla i", style="Svag.TLabel").pack(side="left")
        self.vantetid = tk.StringVar(value="12")
        tk.Spinbox(
            val, from_=1, to=60, width=4, textvariable=self.vantetid,
            font=BRODTEXT, relief="solid", borderwidth=1, highlightthickness=0,
        ).pack(side="left", padx=6)
        ttk.Label(val, text="sekunder", style="Svag.TLabel").pack(side="left")

        ttk.Label(val, text="Varv vid sidskanning", style="Svag.TLabel").pack(
            side="left", padx=(20, 0)
        )
        self.varv = tk.StringVar(value="3")
        tk.Spinbox(
            val, from_=1, to=10, width=3, textvariable=self.varv,
            font=BRODTEXT, relief="solid", borderwidth=1, highlightthickness=0,
        ).pack(side="left", padx=6)

        self.visa_webblasare = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            val, text="Visa webbläsaren", variable=self.visa_webblasare
        ).pack(side="left", padx=(18, 0))

        ttk.Label(val, text="Resultat:", style="Svag.TLabel").pack(side="left", padx=(20, 6))
        self.historik = ttk.Combobox(val, state="readonly", width=32, font=("Segoe UI", 9))
        self.historik.pack(side="left")
        self.historik.bind("<<ComboboxSelected>>", self._byt_matning)

        self.status = ttk.Label(ram, text="Klistra in en länk och tryck Mät.", style="Svag.TLabel")
        self.status.grid(row=3, column=0, sticky="w", pady=(10, 0))

        self.progress = ttk.Progressbar(ram, mode="indeterminate", length=190)
        self.progress.grid(row=3, column=1, sticky="e", pady=(10, 0))

    PLATSHALLARE = (
        "klistra in inbäddningskoden, en länk, ett id eller en sidadress"
    )

    def _visa_platshallare(self, _händelse=None) -> None:
        if not self.falt.get().strip():
            self._platshallare_syns = True
            self.falt.configure(foreground=SVAG)
            self.falt.delete(0, "end")
            self.falt.insert(0, self.PLATSHALLARE)

    def _tom_platshallare(self, _händelse=None) -> None:
        if self._platshallare_syns:
            self._platshallare_syns = False
            self.falt.delete(0, "end")
            self.falt.configure(foreground=TEXT)

    def _bygg_betygskort(self) -> None:
        kort = ttk.Frame(self, style="Kort.TFrame", padding=16)
        kort.pack(fill="x", padx=16, pady=(6, 0))

        self.betygsruta = tk.Label(
            kort, text="–", font=("Segoe UI", 40, "bold"), fg="#ffffff", bg=LINJE,
            width=2, height=1,
        )
        self.betygsruta.grid(row=0, column=0, padx=(0, 18), sticky="nw")

        hoger = ttk.Frame(kort, style="KortYta.TFrame")
        hoger.grid(row=0, column=1, sticky="nsew")
        kort.columnconfigure(1, weight=1)

        self.etikett_vikt = ttk.Label(
            hoger, text="Ingen mätning ännu", font=("Segoe UI", 20, "bold"), style="Kort.TLabel"
        )
        self.etikett_vikt.pack(anchor="w")
        self.etikett_motiv = ttk.Label(hoger, text="", style="Kort.TLabel")
        self.etikett_motiv.pack(anchor="w")
        self.etikett_format = ttk.Label(hoger, text="", style="SvagKort.TLabel")
        self.etikett_format.pack(anchor="w", pady=(2, 0))

        self.etikett_prognos = ttk.Label(
            kort, text="", style="SvagKort.TLabel", justify="right", anchor="ne"
        )
        self.etikett_prognos.grid(row=0, column=2, sticky="ne", padx=(14, 0))

    def _bygg_flikar(self) -> None:
        self.flikar = ttk.Notebook(self)
        self.flikar.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        # Översikt: vikt per del + skärmbild
        oversikt = ttk.Frame(self.flikar, padding=12)
        self.flikar.add(oversikt, text="Översikt")

        # Tabellen och annonsen delas av en mellanlist som går att dra i. Tills
        # den dragits får tabellen den bredd den behöver och annonsen resten.
        self._delning_oversikt = Delning(
            oversikt, "delning_oversikt", None, min_vanster=200, min_hoger=280
        )
        self._delning_oversikt.pack(fill="both", expand=True)

        self.kategoriram = ttk.Frame(self._delning_oversikt.vanster, padding=(0, 0, 8, 0))
        self.kategoriram.pack(fill="both", expand=True, anchor="n")

        bildsida = ttk.Frame(self._delning_oversikt.hoger, padding=(8, 0, 0, 0))
        bildsida.pack(fill="both", expand=True)

        # Rubrik och knappar på en rad ovanför, som i Filer. Varje rad under
        # bildytan tar höjd från annonsen, och höjden tar slut före bredden.
        huvud = ttk.Frame(bildsida)
        huvud.pack(side="top", fill="x")
        # Knapparna packas före rubriken så att de syns även i en smal ruta.
        self.knapp_storre = ttk.Button(huvud, text="Förstora", command=self._forstora_annonsbilden)
        self.knapp_storre.pack(side="right")
        self.knapp_storre.state(["disabled"])
        self.knapp_paus = ttk.Button(huvud, text="Pausa", command=self._vaxla_paus)
        self.knapp_paus.pack(side="right", padx=(0, 8))
        self.knapp_paus.state(["disabled"])
        self.bildrubrik = ttk.Label(huvud, text="Så såg annonsen ut", style="Svag.TLabel")
        self.bildrubrik.pack(side="left")

        # Packas bara när det finns något att säga, som att animationen rensats.
        self.bildinfo = ttk.Label(bildsida, text="", style="Svag.TLabel", justify="left")

        # Ramen styr storleken, inte bilden i den — samma skäl som i Filer.
        self.bildbehallare = tk.Frame(
            bildsida, bg=BILDBAKGRUND, highlightthickness=1,
            highlightbackground=LINJE, width=300, height=170,
        )
        self.bildbehallare.pack(side="top", fill="both", expand=True, pady=(4, 0))
        self.bildbehallare.pack_propagate(False)
        self.bildyta = tk.Label(
            self.bildbehallare, bg=BILDBAKGRUND, fg=SVAG, cursor="hand2",
            text="annonsen visas här när den är mätt —\nanimerade annonser spelas upp\n\n"
                 "dra i mellanlisten till vänster för att\ngöra förhandsvyn större",
        )
        self.bildyta.pack(fill="both", expand=True)
        self.bildyta.bind("<Button-1>", lambda _e: self._forstora_annonsbilden())
        self.bildbehallare.bind("<Configure>", self._planera_annonsvy)
        self.spelare = Bildspelare(self.bildyta)

        # Filer
        filer = ttk.Frame(self.flikar, padding=12)
        self.flikar.add(filer, text="Filer")

        # Listan och förhandsvyn delas av en mellanlist som går att dra i.
        delning = Delning(filer, "delning_filer", 0.64, min_vanster=200, min_hoger=220)
        delning.pack(fill="both", expand=True)

        listram = ttk.Frame(delning.vanster, padding=(0, 0, 4, 0))
        listram.pack(fill="both", expand=True)
        forhand = ttk.Frame(delning.hoger, padding=(8, 0, 0, 0))
        forhand.pack(fill="both", expand=True)

        huvud = ttk.Frame(forhand)
        huvud.pack(fill="x")
        ttk.Label(huvud, text="Förhandsgranskning", style="Svag.TLabel").pack(side="left")
        self.knapp_forstora = ttk.Button(
            huvud, text="Förstora", command=self._forstora_forhandsbild
        )
        self.knapp_forstora.pack(side="right")
        self.knapp_forstora.state(["disabled"])

        # Bildrutan packas före texten. Pack delar ut utrymme i packordning, och
        # bilden ska få sin plats först — texten får det som blir över.
        # Ramen styr storleken, inte bilden i den — annars kan omritningen driva
        # sin egen storleksändring i en slinga.
        self.forhandsram = tk.Frame(
            forhand, bg=BILDBAKGRUND, highlightthickness=1,
            highlightbackground=LINJE, width=300, height=170,
        )
        self.forhandsram.pack(fill="both", expand=True, pady=(4, 0))
        self.forhandsram.pack_propagate(False)

        self.forhandsinfo = ttk.Label(forhand, text="", style="Svag.TLabel", justify="left")
        self.forhandsinfo.pack(anchor="w", fill="x", pady=(6, 0))
        self.forhandsyta = tk.Label(
            self.forhandsram, bg=BILDBAKGRUND, cursor="hand2", fg=SVAG,
            text="välj en bildfil eller ett typsnitt\n\n"
                 "dra i mellanlisten till vänster för att\ngöra förhandsvyn större",
        )
        self.forhandsyta.pack(fill="both", expand=True)
        self.forhandsyta.bind("<Button-1>", lambda _e: self._forstora_forhandsbild())
        self.forhandsram.bind("<Configure>", self._anpassa_forhandsvy)
        kolumner = ("typ", "vikt", "andel", "anm")
        self.trad_filer = ttk.Treeview(listram, columns=kolumner, show="tree headings", height=6)
        self.trad_filer.heading("#0", text="Fil")
        self.trad_filer.heading("typ", text="Typ")
        self.trad_filer.heading("vikt", text="Vikt")
        self.trad_filer.heading("andel", text="Andel")
        self.trad_filer.heading("anm", text="Anmärkning")
        import tkinter.font as tkfont

        mat = tkfont.Font(font=TABELL).measure("0")
        self.trad_filer.column("#0", width=mat * 38, anchor="w", stretch=False)
        self.trad_filer.column("typ", width=mat * 7, anchor="w", stretch=False)
        self.trad_filer.column("vikt", width=mat * 10, anchor="e", stretch=False)
        self.trad_filer.column("andel", width=mat * 8, anchor="e", stretch=False)
        self.trad_filer.column("anm", width=mat * 40, anchor="w")
        rull = ttk.Scrollbar(listram, orient="vertical", command=self.trad_filer.yview)
        self.trad_filer.configure(yscrollcommand=rull.set)
        self.trad_filer.pack(side="left", fill="both", expand=True)
        rull.pack(side="right", fill="y")
        self.trad_filer.tag_configure("varning", foreground=BETYGSFARG["F"])
        self.trad_filer.tag_configure("annonsrad", font=("Consolas", 10, "bold"))
        self.trad_filer.tag_configure("delad", foreground="#1f7a4d")
        self.trad_filer.bind("<Double-1>", self._oppna_fil)
        self.trad_filer.bind("<<TreeviewSelect>>", self._visa_forhandsgranskning)

        # Råd
        radram = ttk.Frame(self.flikar, padding=12)
        self.flikar.add(radram, text="Råd")
        self.radtext = tk.Text(
            radram, wrap="word", font=BRODTEXT, bg=KORT, fg=TEXT, relief="solid", bd=1,
            padx=16, pady=14, spacing1=2, spacing3=4, cursor="arrow",
            height=8,  # begärd höjd; fliken växer ändå med fönstret
        )
        radrull = ttk.Scrollbar(radram, orient="vertical", command=self.radtext.yview)
        self.radtext.configure(yscrollcommand=radrull.set)
        self.radtext.pack(side="left", fill="both", expand=True)
        radrull.pack(side="right", fill="y")
        self.radtext.tag_configure("rubrik", font=("Segoe UI", 11, "bold"), spacing1=14)
        self.radtext.tag_configure(
            "ansvar", font=("Segoe UI", 9, "bold"), foreground=SVAG, spacing1=20, spacing3=2
        )
        self.radtext.tag_configure(
            "annonsrubrik", font=("Segoe UI", 13, "bold"), foreground=TEXT,
            spacing1=26, spacing3=4,
        )
        self.radtext.tag_configure("varfor", foreground=SVAG, spacing3=6)
        self.radtext.tag_configure("steg", lmargin1=18, lmargin2=30)
        self.radtext.tag_configure("vinst", font=("Segoe UI", 10, "bold"), foreground="#1f7a4d")
        for namn, farg in ALLVARSFARG.items():
            self.radtext.tag_configure(f"allvar_{namn}", foreground=farg, font=RUBRIK)
        self.radtext.configure(state="disabled")

    def _bygg_botten(self) -> None:
        ram = ttk.Frame(self, padding=(16, 10, 16, 14))
        # Före flikarna i packordningen: i ett lågt fönster fick flikarna annars
        # all höjd de bad om, och knapparna här hamnade utanför fönstret.
        ram.pack(fill="x", side="bottom", before=self.flikar)
        # Höger sida packas först, av samma skäl som i notisraden.
        ttk.Label(
            ram, text=f"Annonsvikt {av.VERSION}", style="Svag.TLabel"
        ).pack(side="right", padx=(10, 0))
        ttk.Button(
            ram, text="Sök efter uppdateringar", command=self._sok_uppdatering_manuellt
        ).pack(side="right")

        self.knapp_html = ttk.Button(ram, text="Spara HTML-rapport…", command=self.spara_html)
        self.knapp_html.pack(side="left")
        self.knapp_json = ttk.Button(ram, text="Spara JSON…", command=self.spara_json)
        self.knapp_json.pack(side="left", padx=8)
        self.knapp_oppna = ttk.Button(ram, text="Öppna rapport i webbläsare", command=self.oppna_rapport)
        self.knapp_oppna.pack(side="left")

    # ── Mätning ───────────────────────────────────────────────────────────────

    def starta_matning(self) -> None:
        if self.korr:
            return
        rå = "" if self._platshallare_syns else self.falt.get().strip()
        if not rå or rå.rstrip("/").endswith("bannerboo.com"):
            messagebox.showinfo(
                "Annonsvikt",
                "Fyll i något att mäta först.\n\n"
                "En annons anges med id eller länk:\n"
                "    bb6a6b2536dcc\n"
                "    https://embed.bannerboo.com/bb6a6b2536dcc\n\n"
                "En hel sida anges med sidans adress:\n"
                "    upphandling24.se",
            )
            return
        try:
            vantetid = max(1.0, float(self.vantetid.get()))
        except ValueError:
            vantetid = 12.0

        try:
            varv = max(1, int(self.varv.get()))
        except ValueError:
            varv = 3

        url = av.normalisera_url(rå)
        som_annons = av.ar_annonslank(url)
        self.korr = True
        self._satt_knapplage()
        self.progress.start(12)
        self.status.configure(
            text=(
                f"Mäter {url} …"
                if som_annons
                else f"Skannar {url} efter annonser — det tar ungefär en minut per varv …"
            )
        )

        args = SimpleNamespace(
            vantetid=vantetid,
            huvud=self.visa_webblasare.get(),
            bredd=1200,
            hojd=800,
            tyst=True,
            varv=varv,
            utan_samtycke=False,
            # Mätmotorn ropar hit med varje steg. Tk får bara röras från
            # huvudtråden, så beskedet läggs i kön i stället för att skrivas direkt.
            status=lambda text: self.ko.put(("status", text)),
        )
        self._trad = threading.Thread(
            target=self._matarbete, args=(url, args, som_annons), daemon=True
        )
        self._trad.start()

    def _matarbete(self, url: str, args, som_annons: bool) -> None:
        """Körs i egen tråd — Playwright blockerar, gränssnittet får inte frysa."""
        try:
            if som_annons:
                analys, rad, bild = av.analysera(url, args)
                self.ko.put(("annons", ("annons", analys, rad, bild)))
            else:
                self.ko.put(("sida", ("sida", av.analysera_sida(url, args), None, None)))
        except BaseException as fel:  # även SystemExit — annars dör tråden tyst
            self.ko.put(("fel", str(fel) or f"{type(fel).__name__}"))

    def _tom_ko(self) -> None:
        try:
            while True:
                sort, nyttolast = self.ko.get_nowait()
                if sort == "status":
                    self.status.configure(text=nyttolast)
                elif sort == "uppdatering":
                    self._visa_uppdatering(nyttolast)
                elif sort == "uppdatering_hamtad":
                    self._kor_installationen(nyttolast)
                elif sort == "uppdateringsbesked":
                    self.status.configure(text=nyttolast)
                elif sort == "uppdateringsfel":
                    self._hamtar = False
                    self.uppd_knapp.configure(text="Uppdatera nu", state="normal")
                    self.status.configure(text="Uppdateringen gick inte att hämta.")
                    messagebox.showerror("Annonsvikt", nyttolast)
                elif sort == "annons":
                    self._visa_resultat([nyttolast])
                elif sort == "sida":
                    sidanalys = nyttolast[1]
                    poster = [nyttolast] + [
                        ("annons", analys, rad, sidanalys.bilder.get(fynd.id))
                        for fynd, analys, rad in sidanalys.poster
                    ]
                    self._visa_resultat(poster)
                elif sort == "fel":
                    self.korr = False
                    self.progress.stop()
                    self._satt_knapplage()
                    self.status.configure(text="Mätningen misslyckades.")
                    messagebox.showerror("Mätningen misslyckades", nyttolast)
        except queue.Empty:
            pass
        # Dör arbetstråden utan att lämna något i kön skulle fönstret annars stå
        # kvar på "Arbetar…" i all oändlighet.
        if self.korr and self._trad is not None and not self._trad.is_alive():
            self.korr = False
            self.progress.stop()
            self._satt_knapplage()
            self.status.configure(text="Mätningen avbröts oväntat.")
            messagebox.showerror(
                "Annonsvikt",
                "Mätningen avbröts utan besked.\n\n"
                "Kör \"Reparera Annonsvikt\" på Start-menyn om det upprepas.",
            )
        self.after(100, self._tom_ko)

    # ── Presentation ──────────────────────────────────────────────────────────

    def _visa_resultat(self, poster: list) -> None:
        self.korr = False
        self.progress.stop()
        forsta = len(self.matningar)
        self.matningar.extend(poster)
        self._gallra_bildrutor()
        self.historik.configure(values=[self._etikett(p) for p in self.matningar])
        self.historik.current(forsta)
        self._rita(self.matningar[forsta])
        self._satt_knapplage()

    def _gallra_bildrutor(self) -> None:
        """Animationen tar några MB per annons. Den sparas för de senaste
        annonserna; äldre mätningar visar skärmbilden i stället."""
        sedda = set()
        for post in reversed(self.matningar):
            if post[0] != "annons" or id(post[1]) in sedda:
                continue
            sedda.add(id(post[1]))
            if len(sedda) > BEHALL_ANIMATIONER:
                post[1].bildrutor = []

    @staticmethod
    def _etikett(post) -> str:
        if post[0] == "sida":
            s = post[1]
            return f"◆ SIDAN {s.url.replace('https://', '')} ({len(s.poster)} annonser)"
        return "   " + post[1].kalla.replace("https://embed.bannerboo.com/", "")

    def _byt_matning(self, _händelse=None) -> None:
        i = self.historik.current()
        if 0 <= i < len(self.matningar):
            self._rita(self.matningar[i])

    def _rita(self, post: tuple) -> None:
        self.aktiv = post
        if post[0] == "sida":
            self._rita_sida(post[1])
            return
        _sort, analys, rad, bild = post

        if getattr(analys, "misslyckande", ""):
            self._rita_misslyckande(analys)
            return

        bokstav, motivering = av.satt_betyg(analys.totalvikt)
        self.betygsruta.configure(text=bokstav, bg=BETYGSFARG[bokstav])
        self.etikett_vikt.configure(text=av.fmt(analys.totalvikt))
        self.etikett_motiv.configure(text=motivering)

        delar = []
        if analys.bredd:
            delar.append(f"{analys.bredd} × {analys.hojd} px")
        if analys.visningstyp:
            delar.append(analys.visningstyp)
        if analys.anim_sekunder:
            loop = "oändlig loop" if analys.anim_loopar == 0 else f"{analys.anim_loopar} loopar"
            delar.append(f"{analys.anim_sekunder:.1f} s · {loop}".replace(".", ","))
        delar.append(f"{len(analys.resurser)} förfrågningar")
        self.etikett_format.configure(text="  ·  ".join(delar))

        egen, full = analys.potential_egen, analys.potentialvikt
        rader = [
            "Det ni kan göra i BannerBoo",
            f"{av.fmt(egen)} · betyg {av.satt_betyg(egen)[0]} · "
            f"−{av.procent(analys.totalvikt - egen, analys.totalvikt)}",
        ]
        if full < egen:
            rader.append(f"om BannerBoo gör sin del: {av.fmt(full)} · {av.satt_betyg(full)[0]}")
        self.etikett_prognos.configure(text="\n".join(rader))

        self.status.configure(
            text=f"Klar · {analys.kalla} · mätt {analys.tidpunkt}"
        )

        self.flikar.select(0)
        self._rita_kategorier(analys)
        self._rita_filer(analys)
        self._rita_rad(rad)
        self._rita_bild(bild, analys, rubrik="Så såg annonsen ut")

    def _kategorirad(self, r: int, namn, andel, vikt, procent, antal, fet=False) -> None:
        stil = ("Segoe UI", 10, "bold") if fet else BRODTEXT
        ttk.Label(self.kategoriram, text=namn, font=stil).grid(row=r, column=0, sticky="w", pady=3)
        if andel is not None:
            duk = tk.Canvas(self.kategoriram, height=11, width=190, bg=BG, highlightthickness=0)
            duk.grid(row=r, column=1, padx=14, sticky="w")
            duk.create_rectangle(0, 2, 190, 11, fill=LINJE, outline="")
            if andel > 0:
                duk.create_rectangle(0, 2, max(2, 190 * andel), 11, fill=ACCENT, outline="")
        for kol, text in ((2, vikt), (3, procent), (4, antal)):
            ttk.Label(self.kategoriram, text=text, font=stil, anchor="e", width=11 if kol == 2 else 8).grid(
                row=r, column=kol, sticky="e", padx=(6, 0)
            )

    def _rita_misslyckande(self, analys) -> None:
        """Adressen gav ingen annons — säg det i klartext i stället för tomma flikar."""
        self.betygsruta.configure(text="?", bg=LINJE)
        self.etikett_vikt.configure(text="Ingen annons hittades")
        self.etikett_motiv.configure(text=analys.misslyckande)
        self.etikett_format.configure(text=analys.kalla)
        self.etikett_prognos.configure(text="")
        self.status.configure(text=f"Ingen annons på {analys.kalla}")

        for barn in self.kategoriram.winfo_children():
            barn.destroy()
        ttk.Label(
            self.kategoriram,
            justify="left",
            text=(
                "Så här anges en annons:\n"
                "    bb6a6b2536dcc\n"
                "    https://embed.bannerboo.com/bb6a6b2536dcc\n\n"
                "Ska en hel sida genomsökas efter annonser anges sidans adress:\n"
                "    upphandling24.se\n\n"
                "Kontrollera också att adressen inte råkat klistras in\n"
                "två gånger i rad."
            ),
        ).grid(row=0, column=0, sticky="w", pady=8)

        self.trad_filer.delete(*self.trad_filer.get_children())
        self.trad_filer.insert("", "end", text="—", values=("", "", "", "ingen annons att mäta"))
        self.radtext.configure(state="normal")
        self.radtext.delete("1.0", "end")
        self.radtext.insert("end", analys.misslyckande + "\n")
        self.radtext.configure(state="disabled")
        self._delning_oversikt.folj_innehall()
        self._rita_bild(None, rubrik="Så såg annonsen ut", tomtext="ingen annons")
        self.flikar.select(0)

    def _rita_sida(self, s) -> None:
        """Sammanfattning av en hel sida: annonserna, delade filer och sidnivåråd."""
        betyg = [av.satt_betyg(a.totalvikt)[0] for _f, a, _r in s.poster]
        varst = max(betyg, key="ABCDF".index) if betyg else "A"
        self.betygsruta.configure(text=varst if s.poster else "–",
                                  bg=BETYGSFARG.get(varst, LINJE))
        self.etikett_vikt.configure(text=av.fmt(s.delad_vikt) if s.poster else "Inga annonser")
        self.etikett_motiv.configure(
            text=(
                f"{av.antal(len(s.poster), 'BannerBoo-annons', 'BannerBoo-annonser')} på sidan"
                if s.poster
                else "Inga BannerBoo-annonser hittades på sidan"
            )
        )
        delar = [av.antal(s.varv, "varv", "varv")]
        if s.poster:
            delar.append("stabilt annonsval" if s.stabil else "olika annonser mellan varven")
        if s.samtyckesknapp:
            delar.append("samtycke klickat")
        self.etikett_format.configure(text="  ·  ".join(delar))
        self.etikett_prognos.configure(
            text=(
                "Summa var för sig\n"
                f"{av.fmt(s.summa_var_for_sig)}\n"
                f"delade resurser sparar {av.fmt(s.vinst_av_delning)}"
            )
            if len(s.poster) > 1
            else ""
        )
        self.status.configure(text=f"Klar · {s.url} · mätt {s.tidpunkt}")

        # Översikt: en rad per annons
        for barn in self.kategoriram.winfo_children():
            barn.destroy()
        self.kategoriram.columnconfigure(0, weight=1)
        total = s.delad_vikt or 1
        self._kategorirad(0, "Annons", None, "Vikt", "Andel", "Betyg", fet=True)
        for i, (fynd, analys, _rad) in enumerate(s.poster, 1):
            lage = fynd.lage
            self._kategorirad(
                i,
                f"{fynd.id}   {fynd.plats or ''}  ({lage})",
                analys.totalvikt / total,
                av.fmt(analys.totalvikt),
                av.procent(analys.totalvikt, total),
                av.satt_betyg(analys.totalvikt)[0],
            )
        if s.poster:
            rad_nr = len(s.poster) + 1
            ttk.Separator(self.kategoriram, orient="horizontal").grid(
                row=rad_nr, column=0, columnspan=5, sticky="ew", pady=6
            )
            self._kategorirad(
                rad_nr + 1, "Faktisk kostnad för besökaren", None,
                av.fmt(s.delad_vikt), "100 %", "", fet=True,
            )
        # Panelen visar den tyngsta annonsen; det är den råden handlar mest om.
        self._delning_oversikt.folj_innehall()
        tyngst = max(s.poster, key=lambda p: p[1].totalvikt, default=None)
        if tyngst:
            self._rita_bild(
                s.bilder.get(tyngst[0].id), tyngst[1],
                rubrik=("Annonsen på sidan" if len(s.poster) == 1
                        else f"Tyngsta annonsen: {tyngst[0].id}"),
            )
        else:
            self._rita_bild(None, rubrik="Så såg annonsen ut", tomtext="inga annonser på sidan")

        self._rita_sidfiler(s)

        self._rita_sidrad(s)
        self.flikar.select(0)

    def _rita_sidfiler(self, s) -> None:
        """Varje annons filer under en egen rubrikrad, delade filer markerade."""
        self.trad_filer.delete(*self.trad_filer.get_children())
        self._resurs_per_rad = {}
        self._visa_forhandsgranskning()
        _unik, delade = s.delning()
        antal_per_url = {url: antal for url, _storlek, antal in delade}
        sidtotal = s.summa_var_for_sig or 1

        if not s.poster:
            self.trad_filer.insert(
                "", "end", text="—",
                values=("", "", "", "inga annonser hittades på sidan"),
            )
            return

        for fynd, analys, _rad in s.poster:
            forald = self.trad_filer.insert(
                "", "end",
                text=f"{fynd.id}  ({fynd.format} px)",
                iid=f"annons:{fynd.id}",
                open=True,
                tags=("annonsrad",),
                values=(
                    "annons",
                    av.fmt(analys.totalvikt),
                    av.procent(analys.totalvikt, sidtotal),
                    f"{av.antal(len(analys.resurser), 'fil', 'filer')}"
                    f" · betyg {av.satt_betyg(analys.totalvikt)[0]}"
                    f" · {fynd.plats or 'okänd annonsplats'}",
                ),
            )
            total = analys.totalvikt or 1
            for r in sorted(analys.resurser, key=lambda x: -x.storlek):
                anm = self._filanmarkning(r)
                delad = antal_per_url.get(r.url)
                if delad:
                    anm.append(f"delas med {delad - 1} annan annons"
                               if delad == 2 else f"delas med {delad - 1} andra annonser")
                taggar = ("varning",) if r.dold_orsak else (("delad",) if delad else ())
                self._resurs_per_rad[f"{fynd.id}|{r.url}"] = r
                self.trad_filer.insert(
                    forald, "end",
                    text=r.filnamn,
                    iid=f"{fynd.id}|{r.url}",
                    tags=taggar,
                    values=(
                        r.underformat or r.kategori,
                        av.fmt(r.storlek),
                        av.procent(r.storlek, total),
                        ", ".join(anm),
                    ),
                )

    def _rita_kategorier(self, analys) -> None:
        for barn in self.kategoriram.winfo_children():
            barn.destroy()
        grupper: dict[str, list] = {}
        for r in analys.resurser:
            grupper.setdefault(r.kategori, []).append(r)
        total = analys.totalvikt or 1
        self.kategoriram.columnconfigure(0, weight=1)

        rad = 0
        self._kategorirad(rad, "Del", None, "Vikt", "Andel", "Filer", fet=True)
        for kat in av.KATEGORIORDNING:
            rs = grupper.get(kat)
            if not rs:
                continue
            rad += 1
            vikt = sum(x.storlek for x in rs)
            formater = sorted({x.underformat for x in rs if x.underformat})
            namn = av.KATEGORINAMN[kat]
            if formater and kat in ("bild", "typsnitt"):
                namn += f" ({', '.join(formater)})"
            self._kategorirad(
                rad, namn, vikt / total, av.fmt(vikt), av.procent(vikt, total), str(len(rs))
            )
        rad += 1
        ttk.Separator(self.kategoriram, orient="horizontal").grid(
            row=rad, column=0, columnspan=5, sticky="ew", pady=6
        )
        self._kategorirad(
            rad + 1, "Totalt", None, av.fmt(analys.totalvikt), "100 %",
            str(len(analys.resurser)), fet=True,
        )
        self._delning_oversikt.folj_innehall()

    @staticmethod
    def _filanmarkning(r) -> list[str]:
        """Samma anmärkningar som kommandoraden skriver ut för en fil."""
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
        if r.hamtning:
            anm.append(r.hamtning)
        if r.status >= 400:
            anm.append(f"HTTP {r.status}")
        return anm

    def _rita_filer(self, analys) -> None:
        self.trad_filer.delete(*self.trad_filer.get_children())
        self._resurs_per_rad = {}
        self._visa_forhandsgranskning()
        total = analys.totalvikt or 1
        for r in sorted(analys.resurser, key=lambda x: -x.storlek):
            self._resurs_per_rad[r.url] = r
            self.trad_filer.insert(
                "", "end", text=r.filnamn, iid=r.url,
                tags=("varning",) if r.dold_orsak else (),
                values=(
                    r.underformat or r.kategori,
                    av.fmt(r.storlek),
                    av.procent(r.storlek, total),
                    ", ".join(self._filanmarkning(r)),
                ),
            )

    def _skriv_radlista(self, rad: list, nummer: int = 0) -> int:
        """Skriver råden grupperade efter vem som gör något. Returnerar sista numret."""
        for grupprubrik, lista in av.gruppera_rad(rad):
            self.radtext.insert("end", grupprubrik.upper() + "\n", "ansvar")
            for r in lista:
                nummer += 1
                self.radtext.insert("end", f"{nummer}. {r.rubrik}", ("rubrik", f"allvar_{r.allvar}"))
                if r.sparar:
                    self.radtext.insert("end", f"   −{av.fmt(r.sparar)}", "vinst")
                self.radtext.insert("end", "\n")
                self.radtext.insert("end", r.varfor + "\n", "varfor")
                for steg in r.gor:
                    self.radtext.insert("end", f"•  {steg}\n", "steg")
        return nummer

    def _rita_sidrad(self, s) -> None:
        """Sidans råd, och därefter råden för varje annons på sidan.

        Utan annonsernas egna råd blir fliken tom för en sida med en enda annons:
        de jämförande sidråden ges bara när det finns flera att jämföra."""
        self.radtext.configure(state="normal")
        self.radtext.delete("1.0", "end")
        nummer = 0
        if s.sidrad:
            self.radtext.insert("end", "Sidan som helhet\n", "annonsrubrik")
            nummer = self._skriv_radlista(s.sidrad, nummer)
        for fynd, _analys, rad in s.poster:
            self.radtext.insert(
                "end",
                f"Annons {fynd.id}  ·  {fynd.format} px  ·  {fynd.plats or 'okänd annonsplats'}\n",
                "annonsrubrik",
            )
            if rad:
                nummer = self._skriv_radlista(rad, nummer)
            else:
                self.radtext.insert("end", "Inga anmärkningar för den här annonsen.\n", "varfor")
        if not s.poster:
            self.radtext.insert("end", "Inga BannerBoo-annonser hittades på sidan.\n", "varfor")
        self.radtext.configure(state="disabled")
        self.radtext.see("1.0")

    def _rita_rad(self, rad: list) -> None:
        self.radtext.configure(state="normal")
        self.radtext.delete("1.0", "end")
        if not rad:
            self.radtext.insert("end", "Inga anmärkningar — annonsen är redan välbyggd.\n")
        self._skriv_radlista(rad)
        self.radtext.configure(state="disabled")
        self.radtext.see("1.0")  # annars står vyn kvar vid sista insatta raden

    @staticmethod
    def _krymp(bild: tk.PhotoImage, maxbredd: int, maxhojd: int) -> tk.PhotoImage:
        """Tk krymper bara i heltalssteg — räkna ut det minsta som får plats."""
        faktor = max(
            1,
            -(-bild.width() // maxbredd),  # avrundat uppåt
            -(-bild.height() // maxhojd),
        )
        return bild.subsample(faktor) if faktor > 1 else bild

    def _passa_forhandsbild(self) -> None:
        """Skala förhandsbilden efter den storlek rutan har just nu.

        Tk skalar bara i heltalssteg. Små bilder förstoras högst tre gånger — mer
        än så blir bara större pixlar, inte mer att se."""
        if self._forhandshel is None:
            return
        rb = max(40, self.forhandsram.winfo_width() - 16)
        rh = max(40, self.forhandsram.winfo_height() - 16)
        hel = self._forhandshel
        if hel.width() > rb or hel.height() > rh:
            self._forhandsbild = self._krymp(hel, rb, rh)
        else:
            zoom = max(1, min(3, rb // max(1, hel.width()), rh // max(1, hel.height())))
            self._forhandsbild = hel.zoom(zoom) if zoom > 1 else hel
        self.forhandsyta.configure(image=self._forhandsbild, text="")

    def _anpassa_forhandsvy(self, händelse) -> None:
        # Informationstexten bryts efter rutans bredd, bilden skalas om.
        self.forhandsinfo.configure(wraplength=max(200, händelse.width - 4))
        matt = (händelse.width, händelse.height)
        if matt != self._forhandsmatt:
            self._forhandsmatt = matt
            self._passa_forhandsbild()

    def _visa_forhandsgranskning(self, _händelse=None) -> None:
        """Visar den valda filens bild, när det är en bild vi kunnat rita av."""
        rad = self.trad_filer.focus()
        r = self._resurs_per_rad.get(rad)
        self._forhandskalla = None

        if r is None or not getattr(r, "miniatyr", ""):
            self._forhandsbild = None
            self._forhandshel = None
            self.knapp_forstora.state(["disabled"])
            saknas = "välj en bildfil eller ett typsnitt"
            if r is not None and r.kategori == "bild":
                saknas = "bilden kunde inte ritas av — den ligger på en annan domän"
            elif r is not None and r.kategori == "typsnitt":
                saknas = "inget prov — typsnittet kunde inte kopplas till annonsens text"
            elif r is not None:
                saknas = "ingen förhandsgranskning för den här filtypen"
            self.forhandsyta.configure(image="", text=saknas)
            self.forhandsinfo.configure(text="")
            return

        try:
            hel = tk.PhotoImage(data=r.miniatyr)
        except tk.TclError:
            self.forhandsyta.configure(image="", text="förhandsgranskningen gick inte att visa")
            self.forhandsinfo.configure(text="")
            return

        self._forhandshel = hel
        self._passa_forhandsbild()
        self.knapp_forstora.state(["!disabled"])

        format_och_vikt = f"{(r.underformat or r.kategori).upper()} · {av.fmt(r.storlek)}"
        if r.kategori == "typsnitt":
            namn = r.typsnitt_familj or r.filnamn
            self._forhandskalla = (r.miniatyr, f"{namn}.png")
            delar = [namn, format_och_vikt]
            if r.typsnitt_text:
                delar.append(f"Används till: \u201d{r.typsnitt_text}\u201d")
            if r.unika_tecken:
                delar.append(f"Annonsen använder {r.unika_tecken} olika tecken ur filen.")
        else:
            self._forhandskalla = (r.miniatyr, r.filnamn)
            delar = [r.filnamn, format_och_vikt]
            if r.nat_b:
                delar.append(f"Verklig storlek: {r.nat_b} × {r.nat_h} px")
            if r.vis_b:
                delar.append(f"Visas i en ruta på: {r.vis_b} × {r.vis_h} px")
            if r.beskuren_andel >= av.BESKURET_TROSKEL:
                delar.append(
                    f"Syns: {r.synlig_b} × {r.synlig_h} px — "
                    f"{av.procent(r.beskuren_andel, 1)} klipps bort"
                )
            if r.bildatgard:
                delar.append(f"Exportera som: {r.mal_b} × {r.mal_h} px")
            if r.dold_orsak:
                delar.append(f"Dold: {r.dold_orsak}")
        self.forhandsinfo.configure(text="\n".join(delar))

    def _forstora_forhandsbild(self) -> None:
        if self._forhandskalla:
            data, namn = self._forhandskalla
            try:
                png = base64.b64decode(data)
            except ValueError:
                return
            self._visa_stor_bild([(png, 0)], f"Annonsvikt · {namn}", namn)

    def _forstora_annonsbilden(self) -> None:
        if not self._annonsrutor:
            return
        namn = "annons"
        kalla = getattr(self._annonsanalys, "kalla", "") or ""
        if kalla:
            namn = kalla.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or namn
        self._visa_stor_bild(
            self._annonsrutor, f"Annonsvikt · {namn}", f"{namn}.png",
            start=self.spelare.index,
        )

    @staticmethod
    def _sekunder(rutor: list) -> str:
        return f"{sum(ms for _png, ms in rutor) / 1000:.1f} s".replace(".", ",")

    def _visa_stor_bild(self, rutor: list, titel: str, filnamn: str, start: int = 0) -> None:
        """Eget fönster med bilden i full storlek — en animation spelas upp — och
        möjlighet att spara bilden, eller den bildruta som visas."""
        fonster = tk.Toplevel(self)
        fonster.title(titel)
        fonster.configure(bg=BG)
        ikon = ikonsokvag()
        if ikon:
            try:
                fonster.iconbitmap(ikon)
            except tk.TclError:
                pass

        maxbredd = fonster.winfo_screenwidth() - 120
        maxhojd = fonster.winfo_screenheight() - 220
        yta = tk.Label(fonster, bg=BILDBAKGRUND, relief="solid", bd=1)
        yta.pack(padx=16, pady=(16, 8))
        spelare = Bildspelare(yta)
        if not spelare.visa(rutor, maxbredd, maxhojd, forstoring=self._annonszoom, start=start):
            fonster.destroy()
            messagebox.showerror("Annonsvikt", "Bilden gick inte att visa.")
            return
        # Stängs fönstret ska uppspelningen sluta och rutorna släppas.
        fonster.bind("<Destroy>", lambda e: spelare.stoppa() if e.widget is fonster else None)

        fot = ttk.Frame(fonster, padding=(16, 0, 16, 14))
        fot.pack(fill="x")

        if spelare.animerad:
            def pausa():
                knapp_paus.configure(text="Spela" if spelare.vaxla_paus() else "Pausa")

            knapp_paus = ttk.Button(fot, text="Pausa", command=pausa)
            knapp_paus.pack(side="left", padx=(0, 8))
            fonster.bind("<space>", lambda _e: pausa())

        def zoomtext():
            return "Visa 1×" if spelare.skalning[0] >= 2 else "Förstora 2×"

        def vaxla():
            spelare.anpassa(maxbredd, maxhojd, forstoring=1 if spelare.skalning[0] >= 2 else 2)
            knapp_zoom.configure(text=zoomtext())

        def spara():
            png = spelare.aktuell_png  # rutan som visades när man klickade
            stig = filedialog.asksaveasfilename(
                parent=fonster, defaultextension=".png", initialfile=filnamn,
                filetypes=[("PNG-bild", "*.png")],
            )
            if stig and png:
                with open(stig, "wb") as f:
                    f.write(png)

        knapp_zoom = ttk.Button(fot, text=zoomtext(), command=vaxla)
        knapp_zoom.pack(side="left")
        bredd, hojd = spelare.matt
        # Ryms bilden inte större på skärmen skulle knappen inte göra något.
        if (valj_skalning(bredd, hojd, maxbredd, maxhojd, 2)
                == valj_skalning(bredd, hojd, maxbredd, maxhojd, 1)):
            knapp_zoom.state(["disabled"])
        ttk.Button(
            fot, text="Spara bildrutan…" if spelare.animerad else "Spara bild…", command=spara
        ).pack(side="left", padx=8)
        ttk.Button(fot, text="Stäng", command=fonster.destroy).pack(side="right")
        besked = f"{bredd} × {hojd} px"
        if spelare.animerad:
            besked += f" · animerad, {self._sekunder(rutor)}"
        ttk.Label(fot, text=besked, style="Svag.TLabel").pack(side="right", padx=10)

        fonster.bind("<Escape>", lambda _e: fonster.destroy())
        fonster.transient(self)

    def _rita_bild(self, bild: bytes | None, analys=None, rubrik: str = "Så såg annonsen ut",
                   tomtext: str = "ingen skärmbild") -> None:
        """Annonsen i förhandsvyn: animationen när den fångats, annars skärmbilden."""
        rutor = list(getattr(analys, "bildrutor", None) or [])
        if len(rutor) < 2:
            rutor = [(bild, 0)] if bild else []
        self._annonsanalys = analys
        self.knapp_paus.configure(text="Pausa")
        self.bildrubrik.configure(text=rubrik)
        self.bildinfo.pack_forget()

        if not rutor or not self.spelare.visa(
            rutor, *self._annonsvyns_matt(), forstoring=self._annonszoom
        ):
            self.spelare.stoppa()
            self._annonsrutor = []
            self.bildyta.configure(
                image="", text="kunde inte visa skärmbilden" if rutor else tomtext
            )
            self.knapp_paus.state(["disabled"])
            self.knapp_storre.state(["disabled"])
            return

        self._annonsrutor = rutor
        self.knapp_storre.state(["!disabled"])
        if self.spelare.animerad:
            self.knapp_paus.state(["!disabled"])
            self.bildrubrik.configure(text=f"{rubrik}  ·  animerad, {self._sekunder(rutor)}")
            return
        self.knapp_paus.state(["disabled"])
        if getattr(analys, "fangad_tid_s", 0):
            # Animationen fångades men har rensats för att spara minne.
            self.bildinfo.configure(
                text=f"Stillbild: animationen sparas för de {BEHALL_ANIMATIONER} senaste "
                     "annonserna. Mät igen för att se den spelas."
            )
            self.bildinfo.pack(side="bottom", fill="x", pady=(4, 0), before=self.bildbehallare)

    def _annonsvyns_matt(self) -> tuple[int, int]:
        """Utrymmet för annonsen i förhandsvyn, med lite luft runt om."""
        try:
            self.bildbehallare.update_idletasks()
        except tk.TclError:
            pass
        bredd = self.bildbehallare.winfo_width() - 8
        hojd = self.bildbehallare.winfo_height() - 8
        if bredd < 60 or hojd < 40:  # inte utlagd ännu
            return 300, 260
        return bredd, hojd

    def _planera_annonsvy(self, händelse=None) -> None:
        """Skala om annonsen när ytan ändrat storlek — men först när den slutat
        ändras, så att rutorna inte avkodas om för varje steg mellanlisten dras."""
        if händelse is not None:
            bredd = max(200, händelse.width - 4)
            if bredd != self._bildinfo_bredd:  # bara vid ändring, annars kan det slå i slinga
                self._bildinfo_bredd = bredd
                self.bildinfo.configure(wraplength=bredd)
        if self._annonsvy_jobb is not None:
            self.after_cancel(self._annonsvy_jobb)
        self._annonsvy_jobb = self.after(150, self._passa_annonsvy)

    def _passa_annonsvy(self) -> None:
        self._annonsvy_jobb = None
        if self._annonsrutor:
            self.spelare.anpassa(*self._annonsvyns_matt())

    def _vaxla_paus(self) -> None:
        pausad = self.spelare.vaxla_paus()
        self.knapp_paus.configure(text="Spela" if pausad else "Pausa")

    # ── Knappar ───────────────────────────────────────────────────────────────

    def _satt_knapplage(self) -> None:
        har = self.aktiv is not None
        self.knapp_mat.configure(
            state="disabled" if self.korr else "normal",
            text="Arbetar…" if self.korr else "Mät",
        )
        lage = "normal" if har and not self.korr else "disabled"
        for knapp in (self.knapp_html, self.knapp_json, self.knapp_oppna):
            knapp.configure(state=lage)

    def _oppna_fil(self, _händelse=None) -> None:
        # I sidvyn har raderna formen "<annons-id>|<url>" för att bli unika.
        val = self.trad_filer.focus()
        if "|" in val:
            val = val.split("|", 1)[1]
        if val.startswith("http"):
            webbrowser.open(val)

    def spara_html(self) -> None:
        if not self.aktiv:
            return
        stig = filedialog.asksaveasfilename(
            defaultextension=".html", initialfile="rapport.html",
            filetypes=[("HTML-rapport", "*.html")],
        )
        if not stig:
            return
        with open(stig, "w", encoding="utf-8") as f:
            if self.aktiv[0] == "sida":
                f.write(av.html_sidrapport(self.aktiv[1]))
            else:
                f.write(av.html_rapport(self.aktiv[1], self.aktiv[2]))
        self.status.configure(text=f"HTML-rapport sparad: {stig}")

    def spara_json(self) -> None:
        if not self.aktiv:
            return
        import json

        stig = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile="matning.json",
            filetypes=[("JSON", "*.json")],
        )
        if not stig:
            return
        if self.aktiv[0] == "sida":
            d = av.sida_till_dict(self.aktiv[1])
        else:
            d = av.analys_till_dict(self.aktiv[1], self.aktiv[2])
        with open(stig, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.status.configure(text=f"JSON sparad: {stig}")

    def oppna_rapport(self) -> None:
        if not self.aktiv:
            return
        katalog = tempfile.mkdtemp(prefix="annonsvikt_")
        stig = os.path.join(katalog, "rapport.html")
        with open(stig, "w", encoding="utf-8") as f:
            if self.aktiv[0] == "sida":
                f.write(av.html_sidrapport(self.aktiv[1]))
            else:
                f.write(av.html_rapport(self.aktiv[1], self.aktiv[2]))
        webbrowser.open("file:///" + stig.replace("\\", "/"))
        self.status.configure(text=f"Rapport öppnad i webbläsaren: {stig}")


def main() -> None:
    forifylld = sys.argv[1] if len(sys.argv) > 1 else ""
    if forifylld in ("-h", "--help"):
        print(__doc__)
        return
    gor_dpi_medveten()
    satt_app_id()
    Annonsviktsfonster(forifylld).mainloop()


if __name__ == "__main__":
    main()
