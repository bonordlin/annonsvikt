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
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

import annonsvikt as av

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
        bredd = min(1240, self.winfo_screenwidth() - 60)
        hojd = min(830, self.winfo_screenheight() - 80)
        x = max(0, (self.winfo_screenwidth() - bredd) // 2)
        y = max(0, (self.winfo_screenheight() - hojd) // 2 - 20)
        self.geometry(f"{bredd}x{hojd}+{x}+{y}")
        self.minsize(min(860, bredd), min(620, hojd))

        self.ko: queue.Queue = queue.Queue()
        self.matningar: list[tuple] = []  # (analys, råd, skärmbild)
        self.aktiv: tuple | None = None
        self.korr = False
        self._trad = None  # arbetstråden, så att en tyst död går att upptäcka
        self._bild = None  # referens så att Tk inte slänger bilden

        self._stil()
        self._bygg_topp(forifylld)
        self._bygg_betygskort()
        self._bygg_flikar()
        self._bygg_botten()
        self._satt_knapplage()

        self.bind("<Return>", lambda _e: self.starta_matning())
        self.after(100, self._tom_ko)

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
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Kort.TLabel", background=KORT)
        s.configure("Svag.TLabel", foreground=SVAG)
        s.configure("SvagKort.TLabel", background=KORT, foreground=SVAG)
        s.configure("Rubrik.TLabel", font=("Segoe UI", 11, "bold"))
        s.configure("Treeview", font=TABELL, rowheight=23, fieldbackground=KORT, background=KORT)
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(16, 7), font=BRODTEXT)
        s.configure("TButton", padding=(12, 5))
        s.configure("Kor.TButton", padding=(18, 6), font=RUBRIK)

    def _bygg_topp(self, forifylld: str) -> None:
        ram = ttk.Frame(self, padding=(16, 14, 16, 8))
        ram.pack(fill="x")

        ttk.Label(
            ram, text="Annonslänk, BannerBoo-id eller en sida att skanna", style="Svag.TLabel"
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

    PLATSHALLARE = "t.ex. bb6a6b2536dcc  eller  upphandling24.se"

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

        hoger = ttk.Frame(kort, style="Kort.TFrame")
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

        self.kategoriram = ttk.Frame(oversikt)
        self.kategoriram.pack(side="left", fill="both", expand=True, anchor="n")

        bildram = ttk.Frame(oversikt, padding=(14, 0, 0, 0))
        bildram.pack(side="right", fill="y")
        self.bildrubrik = ttk.Label(bildram, text="Så såg annonsen ut", style="Svag.TLabel")
        self.bildrubrik.pack(anchor="w")
        self.bildyta = tk.Label(bildram, bg=KORT, relief="solid", bd=1, text="", width=34, height=10)
        self.bildyta.pack(pady=(4, 0))

        # Filer
        filer = ttk.Frame(self.flikar, padding=12)
        self.flikar.add(filer, text="Filer")
        kolumner = ("typ", "vikt", "andel", "anm")
        self.trad_filer = ttk.Treeview(filer, columns=kolumner, show="tree headings")
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
        rull = ttk.Scrollbar(filer, orient="vertical", command=self.trad_filer.yview)
        self.trad_filer.configure(yscrollcommand=rull.set)
        self.trad_filer.pack(side="left", fill="both", expand=True)
        rull.pack(side="right", fill="y")
        self.trad_filer.tag_configure("varning", foreground=BETYGSFARG["F"])
        self.trad_filer.tag_configure("annonsrad", font=("Consolas", 10, "bold"))
        self.trad_filer.tag_configure("delad", foreground="#1f7a4d")
        self.trad_filer.bind("<Double-1>", self._oppna_fil)

        # Råd
        radram = ttk.Frame(self.flikar, padding=12)
        self.flikar.add(radram, text="Råd")
        self.radtext = tk.Text(
            radram, wrap="word", font=BRODTEXT, bg=KORT, fg=TEXT, relief="solid", bd=1,
            padx=16, pady=14, spacing1=2, spacing3=4, cursor="arrow",
        )
        radrull = ttk.Scrollbar(radram, orient="vertical", command=self.radtext.yview)
        self.radtext.configure(yscrollcommand=radrull.set)
        self.radtext.pack(side="left", fill="both", expand=True)
        radrull.pack(side="right", fill="y")
        self.radtext.tag_configure("rubrik", font=("Segoe UI", 11, "bold"), spacing1=14)
        self.radtext.tag_configure("varfor", foreground=SVAG, spacing3=6)
        self.radtext.tag_configure("steg", lmargin1=18, lmargin2=30)
        self.radtext.tag_configure("vinst", font=("Segoe UI", 10, "bold"), foreground="#1f7a4d")
        for namn, farg in ALLVARSFARG.items():
            self.radtext.tag_configure(f"allvar_{namn}", foreground=farg, font=RUBRIK)
        self.radtext.configure(state="disabled")

    def _bygg_botten(self) -> None:
        ram = ttk.Frame(self, padding=(16, 10, 16, 14))
        ram.pack(fill="x")
        self.knapp_html = ttk.Button(ram, text="Spara HTML-rapport…", command=self.spara_html)
        self.knapp_html.pack(side="left")
        self.knapp_json = ttk.Button(ram, text="Spara JSON…", command=self.spara_json)
        self.knapp_json.pack(side="left", padx=8)
        self.knapp_oppna = ttk.Button(ram, text="Öppna rapport i webbläsare", command=self.oppna_rapport)
        self.knapp_oppna.pack(side="left")
        ttk.Label(
            ram,
            text=f"Annonsvikt {av.VERSION}  ·  mäter i headless Chromium med tom cache",
            style="Svag.TLabel",
        ).pack(side="right")

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
        self.historik.configure(values=[self._etikett(p) for p in self.matningar])
        self.historik.current(forsta)
        self._rita(self.matningar[forsta])
        self._satt_knapplage()

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

        mal = analys.potentialvikt
        valfritt = sum(r.sparar for r in rad if r.valfritt)
        rader = [
            "Möjlig vikt efter åtgärd",
            f"{av.fmt(mal)} · betyg {av.satt_betyg(mal)[0]} · −{av.procent(analys.totalvikt - mal, analys.totalvikt)}",
        ]
        if valfritt:
            mv = max(mal - valfritt, 0)
            rader.append(f"med större ingrepp: {av.fmt(mv)} · {av.satt_betyg(mv)[0]}")
        self.etikett_prognos.configure(text="\n".join(rader))

        self.status.configure(
            text=f"Klar · {analys.kalla} · mätt {analys.tidpunkt}"
        )

        self.flikar.select(0)
        self.bildrubrik.configure(text="Så såg annonsen ut")
        self._rita_kategorier(analys)
        self._rita_filer(analys)
        self._rita_rad(rad)
        self._rita_bild(bild)

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
                "Kontrollera också att adressen inte råkat bli hopklistrad,\n"
                "till exempel embed.bannerboo.com/embed.bannerboo.com/…"
            ),
        ).grid(row=0, column=0, sticky="w", pady=8)

        self.trad_filer.delete(*self.trad_filer.get_children())
        self.trad_filer.insert("", "end", text="—", values=("", "", "", "ingen annons att mäta"))
        self.radtext.configure(state="normal")
        self.radtext.delete("1.0", "end")
        self.radtext.insert("end", analys.misslyckande + "\n")
        self.radtext.configure(state="disabled")
        self.bildyta.configure(image="", text="ingen annons", width=34, height=10)
        self._bild = None
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
            lage = "ovanför vecket" if fynd.ovanfor_veck else f"{fynd.topp_px} px ned"
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
        tyngst = max(s.poster, key=lambda p: p[1].totalvikt, default=None)
        bild = s.bilder.get(tyngst[0].id) if tyngst else None
        if tyngst and bild:
            self.bildrubrik.configure(
                text=("Annonsen på sidan" if len(s.poster) == 1
                      else f"Tyngsta annonsen: {tyngst[0].id}")
            )
            self._rita_bild(bild)
        else:
            self.bildrubrik.configure(text="Så såg annonsen ut")
            self.bildyta.configure(image="", text="ingen skärmbild", width=34, height=10)
            self._bild = None

        self._rita_sidfiler(s)

        self._rita_rad(s.sidrad)
        self.flikar.select(0)

    def _rita_sidfiler(self, s) -> None:
        """Varje annons filer under en egen rubrikrad, delade filer markerade."""
        self.trad_filer.delete(*self.trad_filer.get_children())
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

    @staticmethod
    def _filanmarkning(r) -> list[str]:
        """Samma anmärkningar som kommandoraden skriver ut för en fil."""
        anm = []
        if r.dold_orsak:
            anm.append(f"DOLD ({r.dold_orsak})")
        if r.overdim_faktor and r.overdim_faktor > 1.15:
            anm.append(f"{r.nat_b}×{r.nat_h} → {r.vis_b}×{r.vis_h} px")
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
        return anm

    def _rita_filer(self, analys) -> None:
        self.trad_filer.delete(*self.trad_filer.get_children())
        total = analys.totalvikt or 1
        for r in sorted(analys.resurser, key=lambda x: -x.storlek):
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

    def _rita_rad(self, rad: list) -> None:
        self.radtext.configure(state="normal")
        self.radtext.delete("1.0", "end")
        if not rad:
            self.radtext.insert("end", "Inga anmärkningar — annonsen är redan välbyggd.\n")
        for i, r in enumerate(rad, 1):
            self.radtext.insert("end", f"{i}. {r.rubrik}", ("rubrik", f"allvar_{r.allvar}"))
            if r.sparar:
                self.radtext.insert("end", f"   −{av.fmt(r.sparar)}", "vinst")
            if r.valfritt:
                self.radtext.insert("end", "   (större ingrepp)", "varfor")
            self.radtext.insert("end", "\n")
            self.radtext.insert("end", r.varfor + "\n", "varfor")
            for steg in r.gor:
                self.radtext.insert("end", f"•  {steg}\n", "steg")
        self.radtext.configure(state="disabled")
        self.radtext.see("1.0")  # annars står vyn kvar vid sista insatta raden

    def _rita_bild(self, bild: bytes | None) -> None:
        if not bild:
            self.bildyta.configure(image="", text="ingen skärmbild")
            self._bild = None
            return
        try:
            self._bild = tk.PhotoImage(data=base64.b64encode(bild))
            # Tk kan bara krympa i heltalssteg — räcker för bannerformat.
            if self._bild.width() > 320:
                self._bild = self._bild.subsample(max(1, round(self._bild.width() / 300)))
            self.bildyta.configure(image=self._bild, text="", width=0, height=0)
        except tk.TclError:
            self.bildyta.configure(image="", text="kunde inte visa skärmbilden")
            self._bild = None

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
