# annonsvikt

Mäter hur tung en display-annons är, och varför.

Programmet laddar annonsen i en riktig headless Chromium, spelar in all
nätverkstrafik, redovisar vikten per del, räknar ut totalen, sätter betyg mot
IAB:s budget och ger konkreta råd om vad som bör göras för att minska tyngden.

Byggt för BannerBoo-länkar (`embed.bannerboo.com/<id>`) men fungerar på vilken
annons-URL som helst — både inbäddningsskript och färdiga HTML-kreativ.

## Installation

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

## Grafiskt gränssnitt

```bash
python annonsvikt_gui.py
python annonsvikt_gui.py b990849fd8c4b      # förifyllt fält
```

Ett fönster där du klistrar in länken och trycker Mät. Samma mätmotor som
kommandoraden, ingen extra installation — tkinter ingår i Python.

- **Betygskort** med vikt, motivering, format och prognos efter åtgärd
- **Översikt** — vikt per del med staplar, plus en skärmbild av annonsen som
  den faktiskt renderades
- **Filer** — alla resurser med storlek, andel och anmärkningar; dubbelklicka
  på en rad för att öppna filen i webbläsaren
- **Råd** — samma råd som kommandoraden ger, färgade efter allvarsgrad
- Knappar för att spara HTML-rapport och JSON, eller öppna rapporten direkt
- Rullgardin med tidigare mätningar i samma session, för att jämföra annonser

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
