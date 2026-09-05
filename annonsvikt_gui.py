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


class Annonsviktsfonster(tk.Tk):
    def __init__(self, forifylld: str = ""):
        super().__init__()
        self.title("Annonsvikt")
        self.configure(bg=BG)
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

        ttk.Label(ram, text="Annonslänk eller BannerBoo-id", style="Svag.TLabel").grid(
            row=0, column=0, sticky="w", columnspan=2
        )

        self.falt = ttk.Entry(ram, font=("Segoe UI", 11))
        self.falt.grid(row=1, column=0, sticky="ew", pady=(3, 0), ipady=4)
        self.falt.insert(0, forifylld or "https://embed.bannerboo.com/")
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

        self.visa_webblasare = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            val, text="Visa webbläsarfönstret", variable=self.visa_webblasare
        ).pack(side="left", padx=(24, 0))

        ttk.Label(val, text="Tidigare mätningar:", style="Svag.TLabel").pack(side="left", padx=(24, 6))
        self.historik = ttk.Combobox(val, state="readonly", width=34, font=("Segoe UI", 9))
        self.historik.pack(side="left")
        self.historik.bind("<<ComboboxSelected>>", self._byt_matning)

        self.status = ttk.Label(ram, text="Klistra in en länk och tryck Mät.", style="Svag.TLabel")
        self.status.grid(row=3, column=0, sticky="w", pady=(10, 0))

        self.progress = ttk.Progressbar(ram, mode="indeterminate", length=190)
        self.progress.grid(row=3, column=1, sticky="e", pady=(10, 0))

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
        ttk.Label(bildram, text="Så såg annonsen ut", style="Svag.TLabel").pack(anchor="w")
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
        self.trad_filer.column("#0", width=mat * 34, anchor="w", stretch=False)
        self.trad_filer.column("typ", width=mat * 7, anchor="w", stretch=False)
        self.trad_filer.column("vikt", width=mat * 10, anchor="e", stretch=False)
        self.trad_filer.column("andel", width=mat * 8, anchor="e", stretch=False)
        self.trad_filer.column("anm", width=mat * 40, anchor="w")
        rull = ttk.Scrollbar(filer, orient="vertical", command=self.trad_filer.yview)
        self.trad_filer.configure(yscrollcommand=rull.set)
        self.trad_filer.pack(side="left", fill="both", expand=True)
        rull.pack(side="right", fill="y")
        self.trad_filer.tag_configure("varning", foreground=BETYGSFARG["F"])
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
            ram, text="Mätt i headless Chromium med tom cache", style="Svag.TLabel"
        ).pack(side="right")

    # ── Mätning ───────────────────────────────────────────────────────────────

    def starta_matning(self) -> None:
        if self.korr:
            return
        rå = self.falt.get().strip()
        if not rå or rå.rstrip("/").endswith("bannerboo.com"):
            messagebox.showinfo("Annonsvikt", "Fyll i en annonslänk eller ett BannerBoo-id först.")
            return
        try:
            vantetid = max(1.0, float(self.vantetid.get()))
        except ValueError:
            vantetid = 12.0

        url = av.normalisera_url(rå)
        self.korr = True
        self._satt_knapplage()
        self.progress.start(12)
        self.status.configure(text=f"Mäter {url} … laddar i webbläsaren")

        args = SimpleNamespace(
            vantetid=vantetid,
            huvud=self.visa_webblasare.get(),
            bredd=1200,
            hojd=800,
            tyst=True,
        )
        threading.Thread(target=self._matarbete, args=(url, args), daemon=True).start()

    def _matarbete(self, url: str, args) -> None:
        """Körs i egen tråd — Playwright blockerar, gränssnittet får inte frysa."""
        try:
            analys, rad, bild = av.analysera(url, args)
            self.ko.put(("klar", (analys, rad, bild)))
        except Exception as fel:  # nätverk, tidsgräns, saknad webbläsare …
            self.ko.put(("fel", f"{type(fel).__name__}: {fel}"))

    def _tom_ko(self) -> None:
        try:
            while True:
                sort, nyttolast = self.ko.get_nowait()
                if sort == "klar":
                    self._visa_resultat(nyttolast)
                elif sort == "fel":
                    self.korr = False
                    self.progress.stop()
                    self._satt_knapplage()
                    self.status.configure(text="Mätningen misslyckades.")
                    messagebox.showerror("Mätningen misslyckades", nyttolast)
        except queue.Empty:
            pass
        self.after(100, self._tom_ko)

    # ── Presentation ──────────────────────────────────────────────────────────

    def _visa_resultat(self, resultat: tuple) -> None:
        self.korr = False
        self.progress.stop()
        self.matningar.append(resultat)
        namn = [a.kalla.replace("https://", "") for a, _, _ in self.matningar]
        self.historik.configure(values=namn)
        self.historik.current(len(namn) - 1)
        self._rita(resultat)
        self._satt_knapplage()

    def _byt_matning(self, _händelse=None) -> None:
        i = self.historik.current()
        if 0 <= i < len(self.matningar):
            self._rita(self.matningar[i])

    def _rita(self, resultat: tuple) -> None:
        analys, rad, bild = resultat
        self.aktiv = resultat

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

    def _rita_filer(self, analys) -> None:
        self.trad_filer.delete(*self.trad_filer.get_children())
        total = analys.totalvikt or 1
        for r in sorted(analys.resurser, key=lambda x: -x.storlek):
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
            self.trad_filer.insert(
                "", "end", text=r.filnamn, iid=r.url,
                tags=("varning",) if r.dold_orsak else (),
                values=(
                    r.underformat or r.kategori,
                    av.fmt(r.storlek),
                    av.procent(r.storlek, total),
                    ", ".join(anm),
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
        self.knapp_mat.configure(state="disabled" if self.korr else "normal", text="Mäter…" if self.korr else "Mät")
        lage = "normal" if har and not self.korr else "disabled"
        for knapp in (self.knapp_html, self.knapp_json, self.knapp_oppna):
            knapp.configure(state=lage)

    def _oppna_fil(self, _händelse=None) -> None:
        val = self.trad_filer.focus()
        if val.startswith("http"):
            webbrowser.open(val)

    def spara_html(self) -> None:
        if not self.aktiv:
            return
        analys, rad, _ = self.aktiv
        stig = filedialog.asksaveasfilename(
            defaultextension=".html", initialfile="rapport.html",
            filetypes=[("HTML-rapport", "*.html")],
        )
        if not stig:
            return
        with open(stig, "w", encoding="utf-8") as f:
            f.write(av.html_rapport(analys, rad))
        self.status.configure(text=f"HTML-rapport sparad: {stig}")

    def spara_json(self) -> None:
        if not self.aktiv:
            return
        import json
        from dataclasses import asdict

        analys, rad, _ = self.aktiv
        stig = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile="matning.json",
            filetypes=[("JSON", "*.json")],
        )
        if not stig:
            return
        d = asdict(analys)
        d["typsnitt_laddade"] = sorted(analys.typsnitt_laddade)
        d["animationstyper"] = sorted(analys.animationstyper)
        d["totalvikt"] = analys.totalvikt
        d["potentialvikt"] = analys.potentialvikt
        d["betyg"] = av.satt_betyg(analys.totalvikt)[0]
        d["rad"] = [asdict(r) for r in rad]
        with open(stig, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.status.configure(text=f"JSON sparad: {stig}")

    def oppna_rapport(self) -> None:
        if not self.aktiv:
            return
        analys, rad, _ = self.aktiv
        katalog = tempfile.mkdtemp(prefix="annonsvikt_")
        stig = os.path.join(katalog, "rapport.html")
        with open(stig, "w", encoding="utf-8") as f:
            f.write(av.html_rapport(analys, rad))
        webbrowser.open("file:///" + stig.replace("\\", "/"))
        self.status.configure(text=f"Rapport öppnad i webbläsaren: {stig}")


def main() -> None:
    forifylld = sys.argv[1] if len(sys.argv) > 1 else ""
    if forifylld in ("-h", "--help"):
        print(__doc__)
        return
    gor_dpi_medveten()
    Annonsviktsfonster(forifylld).mainloop()


if __name__ == "__main__":
    main()
