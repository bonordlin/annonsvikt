# annonsvikt

Mäter hur tung en display-annons är, och varför.

Programmet laddar annonsen i en riktig headless Chromium, spelar in all
nätverkstrafik, redovisar vikten per del, räknar ut totalen, sätter betyg mot
IAB:s budget och ger konkreta råd om vad som bör göras för att minska tyngden.

Byggt för BannerBoo-länkar (`embed.bannerboo.com/<id>`) men fungerar på vilken
annons-URL som helst — både inbäddningsskript och färdiga HTML-kreativ.

## Installation på Windows

Kör **AnnonsviktSetup.exe**. Guiden lägger programmet i din användarprofil, så
ingen administratörsbehörighet behövs och ingen UAC-ruta dyker upp.

Installeraren sköter allt annat också: den letar rätt på en Python på datorn och
installerar en om ingen finns, bygger en egen Python-miljö åt Annonsvikt så att
inget annat på datorn påverkas, hämtar Playwright och webbläsaren Chromium, och
provstartar programmet innan den säger sig vara klar. Räkna med några minuter
första gången — Chromium är ungefär 150 MB.

Efteråt ligger Annonsvikt på Start-menyn och i Windows sökruta, med egen ikon i
aktivitetsfältet, och avinstalleras från *Appar och funktioner* som vilket annat
program som helst. På Start-menyn finns dessutom:

- **Annonsvikt på kommandoraden** — ett fönster där kommandot `annonsvikt` är redo
- **Reparera Annonsvikt** — bygger om Python-miljön om något gått sönder

### Bygga installeraren själv

```bat
winget install -e --id JRSoftware.InnoSetup
bygg-installerare.cmd
```

Resultatet hamnar i `dist\AnnonsviktSetup.exe`. Bygget ritar först om ikonen med
`verktyg\skapa_ikon.py` och kompilerar sedan `installer\annonsvikt.iss`.
Inno Setup behövs bara för att bygga, aldrig för att köra.

## Installation från källkod

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Användning

```bash
python annonsvikt.py b990849fd8c4b
python annonsvikt.py https://embed.bannerboo.com/b990849fd8c4b --html rapport.html --alla
python annonsvikt.py <url1> <url2> <url3>      # flera annonser + jämförelsetabell
```

Tar full URL, URL utan protokoll, eller bara BannerBoo-id:t.

| Flagga | Betydelse |
|---|---|
| `--vantetid SEK` | hur länge annonsen får rulla innan mätningen avslutas (standard 12) |
| `--alla` | lista alla filer, inte bara de tyngsta |
| `--html FIL` | skriv en HTML-rapport |
| `--json FIL` | skriv rådata som JSON |
| `--bild FIL` | spara en skärmbild av annonsen |
| `--huvud` | visa webbläsarfönstret (felsökning) |
| `--tyst` | inga statusrader |
| `--varv N` | antal omladdningar vid sidskanning (standard 3) |
| `--sida` / `--annons` | tvinga läge i stället för automatiskt val |
| `--utan-samtycke` | klicka inte i samtyckesbanderollen |

## Skanna en hel sida

```bash
python annonsvikt.py upphandling24.se
python annonsvikt.py https://upphandling24.se/karriar/ --varv 5 --html sida.html
```

Peka verktyget på en sida i stället för en annons, så hittas alla BannerBoo-annonser
på sidan och mäts var för sig. Läget väljs automatiskt: en URL hos `bannerboo.com`
eller ett rent id mäts som annons, allt annat skannas som sida. `--annons` och
`--sida` tvingar valet.

Rapporten visar tre tal som svarar på olika frågor:

| Tal | Betyder |
|---|---|
| Summa var för sig | vad annonserna väger om var och en mäts isolerat |
| Faktisk kostnad för besökaren | unionen — delade filer räknas en gång |
| Vinst av delade resurser | skillnaden, med de delade filerna namngivna |

Utöver råden per annons ges råd för sidan som helhet: annonser under vecket som
laddas direkt, annonser ovanför vecket som är tyngre än budget, sidans totala
annonsvikt mot antal annonser × 150 kB, och typsnitten sammanräknade över alla
annonser.

### Så hittas annonserna

Annonserna injiceras av JavaScript — på upphandling24.se av Advanced Ads via
`postscribe`, och sidans HTML innehåller inte ett enda annons-id. Därför laddas
sidan i en riktig webbläsare, som scrollas igenom så att lazy-laddade annonser
triggas. Både nätverkstrafiken och den renderade DOM:en avsöks, vilket ger id,
exakt laddar-URL med query, renderad storlek, position på sidan och vilken
annonsplats annonsen sitter i.

### Samtyckesbanderoll

Hittas en samtyckesbanderoll klickas "tillåt allt", så att annonser inte hålls
tillbaka. Knappen som klickades skrivs ut i rapporten. Sökningen börjar med kända
selektorer (`.cc-allowall`, `.cc-allow`, `[class*=accept-all]` …) och faller
tillbaka på knappar med rätt text — men bara inuti en samtyckesbehållare, så att
verktyget inte klickar på ett "OK" någon annanstans på sidan.

Varje körning sker i en webbläsarkontext som slängs efteråt, så inget samtycke
sparas mellan körningar. `--utan-samtycke` hoppar över klicket och visar vad en
besökare som inte godkänner får se.

### Om rotation och `--varv`

Sidan laddas om `--varv` gånger (standard 3) för att fånga annonsplatser som
roterar mellan kreativ. Ger två varv i rad exakt samma annonser avbryts skanningen
i förtid och rapporten skriver "stabilt annonsval" — då tillför fler varv bara
väntetid.

På upphandling24.se visade sig annonsvalet vara stabilt per URL: samma annons
oavsett omladdning, session eller tom cache. Olika sidor visar däremot olika
annonser. Vill du täcka fler annonser är det alltså flera sid-URL:er som behövs,
inte fler varv — flera URL:er kan anges efter varandra på kommandoraden.

## Grafiskt gränssnitt

```bash
python annonsvikt_gui.py
python annonsvikt_gui.py b990849fd8c4b      # förifyllt fält
```

Ett fönster där du klistrar in länken och trycker Mät. Tar både en annonslänk och
en sida att skanna, precis som kommandoraden. Samma mätmotor, ingen extra
installation — tkinter ingår i Python.

- **Betygskort** med vikt, motivering, format och prognos efter åtgärd
- **Översikt** — vikt per del med staplar, plus en skärmbild av annonsen som
  den faktiskt renderades
- **Filer** — alla resurser med storlek, andel och anmärkningar; dubbelklicka
  på en rad för att öppna filen i webbläsaren
- **Råd** — samma råd som kommandoraden ger, färgade efter allvarsgrad
- Knappar för att spara HTML-rapport och JSON, eller öppna rapporten direkt
- Vid sidskanning fylls resultatlistan med sidan överst och en rad per annons.
  Sidposten visar annonserna i Översikt, **varje annons samtliga filer med vikt** i
  Filer — grupperade per annons, med delade filer markerade — och sidnivåråden i Råd

Mätningen körs i en egen tråd så att fönstret inte fryser medan annonsen laddas.

## Så mäts vikten

En syntetisk värdsida byggs med `<script src="…">`, precis som en riktig sajt
bäddar in annonsen. Sidan laddas med tom cache och varje nätverkssvar spelas in.
Överförda byte kommer från Playwrights `sizes()` — alltså vad som faktiskt gick
över tråden **efter** komprimering, inte filstorleken på disk.

Därefter körs en sond inne i annonsens egen iframe. Den läser sådant som inte
går att få fram statiskt:

- verkliga pixelmått mot visningsmått (överdimensionerade bilder)
- dolda lager som ändå laddar sina bilder
- vilka typsnitt som verkligen laddades, inte bara vilka som deklarerades

För BannerBoo-annonser läses dessutom annonsens egen konfiguration: format,
lager, animationslängd och antal loopar.

## Betygsskala

Mot IAB:s riktvärde på 150 kB för initial laddning.

| Betyg | Gräns |
|---|---|
| A | ≤ 150 kB |
| B | ≤ 250 kB |
| C | ≤ 500 kB |
| D | ≤ 1 MB |
| F | > 1 MB |

## Egna råd

Råden ligger i en egen sektion högst upp i `annonsvikt.py`, märkt
`RÅDBANK — REDIGERA HÄR`. Varje råd är en funktion med `@regel(prioritet)`
ovanför som returnerar noll eller flera `Rad`-objekt:

```python
@regel(85)
def rad_eget(a: "Analys") -> list[Rad]:
    if inget_problem:
        return []
    return [Rad(
        rubrik="Kort imperativ mening",
        varfor="Varför det är ett problem, i en mening.",
        gor=["Konkret steg", "Konkret steg"],
        sparar=antal_byte,      # 0 om okänt
        allvar="hög",           # kritisk | hög | medel | låg
        valfritt=False,         # True = större ingrepp, utanför huvudprognosen
    )]
```

Inget annat i filen behöver röras. Råden sorteras automatiskt efter uppskattad
besparing.

Besparingar räknas per resurs, inte per råd, så samma vinst kan aldrig räknas
två gånger när flera råd berör samma fil.

## Exempel

`exempel-rapport.html` och `exempel-annons.png` är utdata från en verklig
mätning: en 600 × 300-banner som vägde 920 kB, varav 665 kB typsnitt i
ttf-format och 152 kB i en bild som var dold med `visibility:hidden` men
laddades ändå.

`test-tvaannonser.html` är en testsida med två BannerBoo-annonser, för att pröva
sidskanningen och delningsmatten utan att vara beroende av vilka annonser en
riktig sajt råkar visa:

```bash
python -m http.server 8731 &
python annonsvikt.py http://127.0.0.1:8731/test-tvaannonser.html --varv 2
```
