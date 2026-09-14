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

### Uppdateringar

Programmet ser efter en gång per dygn om en nyare version finns, i bakgrunden
vid start. Finns det en dyker en grön rad upp högst upp i fönstret med
versionsnumret och vad som är nytt, och ett klick på **Uppdatera nu** hämtar och
installerar. Appen stänger sig under installationen och startar om av sig själv —
den måste stängas, eftersom en körande app låser filerna som ska bytas ut.

Ingenting installeras utan att du klickat. Vill du kolla när som helst finns
**Sök efter uppdateringar** längst ned i fönstret, eller `annonsvikt
--kolla-uppdatering` på kommandoraden.

Manifestet hämtas från en fast adress som alltid pekar på senaste releasen:

```
https://github.com/bonordlin/annonsvikt/releases/latest/download/version.json
```

Den hämtade filen kontrolleras mot en **SHA-256-summa** ur manifestet innan den
körs. Stämmer den inte — eller kommer filen från någon annan värd än GitHub, eller
över något annat än https — installeras ingenting och du får veta varför. Appen
hämtar och kör kod från nätet, och den kontrollen är därför inte valfri.

Inställningarna ligger i `%LOCALAPPDATA%\Annonsvikt\installningar.json`, alltså
utanför programmappen, så att de överlever en uppdatering.

### Ge ut en ny version

1. Skriv en rubrik för versionen i `NYHETER.md` med punkterna som ska visas i notisen
2. Höj `VERSION` i `annonsvikt.py`
3. `bygg-installerare.cmd publicera`

Sista steget bygger installeraren, skriver manifestet och skapar GitHub-releasen
med taggen `v<version>`, titeln och släppnoterna hämtade ur `NYHETER.md`. Därefter
erbjuder alla installationer uppdateringen inom ett dygn.

Utan argument bygger `bygg-installerare.cmd` bara — ett bygge under utveckling
ska inte råka lägga upp något publikt.

Publiceringen kräver GitHub CLI, inloggat en gång:

```bat
winget install -e --id GitHub.cli
gh auth login
```

Går det inte att använda `gh` fungerar det lika bra för hand: skapa releasen på
GitHub med taggen `v<version>` och ladda upp `dist\AnnonsviktSetup.exe` och
`dist\version.json`.

**Ladda alltid upp båda filerna från samma bygge.** Installeraren stämplar in
byggtiden, så varje bygge ger en ny fil med ny checksumma. Blandas ett manifest
med en exe från ett annat bygge vägrar uppdateringen att installera — vilket är
precis vad kontrollen är till för, men förvirrande om orsaken är en förväxling.

Versionsnumret finns bara på ett ställe, `VERSION` i `annonsvikt.py`. Bygget
läser det därifrån till installeraren, manifestet och releasens tagg.

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

Tar full URL, URL utan protokoll, bara BannerBoo-id:t — eller hela
inbäddningskoden som BannerBoo ger dig att klistra in på sajten:

```html
<center><script src="//embed.bannerboo.com/b990849fd8c4b?responsive=1" async></script></center>
```

Adressen plockas ut ur koden, och en iframe-inbäddning översätts till laddarens
adress, så att samma annons mäts likadant oavsett vilken form du råkat kopiera.

Samma sak gäller en adress som fått en annan sajts adress framför sig. Slack gör
det med den protokollrelativa adressen i inbäddningskoden:
`https://arbetsyta.slack.com//embed.bannerboo.com/b990849fd8c4b` mäts som
`https://embed.bannerboo.com/b990849fd8c4b`. Svarar adressen med ett fel, som
HTTP 404, säger rapporten att där inte finns någon annons — i stället för att
väga felsidan.

| Flagga | Betydelse |
|---|---|
| `--vantetid SEK` | hur länge annonsen får rulla innan mätningen avslutas (standard 12) |
| `--alla` | lista alla filer, inte bara de tyngsta |
| `--html FIL` | skriv en HTML-rapport |
| `--json FIL` | skriv rådata som JSON |
| `--bild FIL` | spara en skärmbild av annonsen |
| `--huvud` | visa webbläsarfönstret (felsökning) |
| `--tyst` | inga statusrader |
| `--version` | visar vilken version som körs |
| `--kolla-uppdatering` | ser efter om en nyare version finns |
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

**Räkna med ungefär en minut per varv.** Sidan ska laddas, samtyckas, scrollas
igenom och få tid att injicera sina annonser, och sedan mäts varje funnen annons
för sig i en egen webbläsare med tom cache. Både kommandoraden och fönstret
berättar löpande vad som pågår, inklusive hur många annonser som hittats.

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
- **Översikt** — vikt per del med staplar, och annonsen som den faktiskt
  renderades. En animerad annons spelas upp i sin egen takt och kan pausas med
  **Pausa**; en annons som står still visas som skärmbild. Klicka på annonsen,
  eller på **Förstora**, för att se den i ett eget fönster — i 1× eller 2×, med
  möjlighet att spara bildrutan som visas som PNG. Samma sak fungerar på varje
  förhandsgranskad bildfil
- **Filer** — alla resurser med storlek, andel och anmärkningar. Väljer du en
  bildfil visas den i förhandsgranskningen till höger, med verkligt pixelmått,
  hur stor rutan är, hur mycket av bilden rutan klipper bort och vilket mått den
  borde exporteras i. Väljer du ett typsnitt visas ett prov: texten annonsen
  faktiskt sätter i det, en rad med å, ä och ö, och hur många olika tecken
  annonsen använder ur filen. **Förstora** öppnar förhandsvyn i ett eget
  fönster. Dubbelklick på en rad öppnar filen i webbläsaren
- **Råd** — samma råd som kommandoraden ger, grupperade efter vem som kan göra
  något åt dem. Vid sidskanning visas sidans råd först och därefter råden för varje annons
- Knappar för att spara HTML-rapport och JSON, eller öppna rapporten direkt
- Vid sidskanning fylls resultatlistan med sidan överst och en rad per annons.
  Sidposten visar annonserna i Översikt, **varje annons samtliga filer med vikt** i
  Filer — grupperade per annons, med delade filer markerade — och sidnivåråden i Råd

Förhandsgranskningen ritas av webbläsaren under mätningen: varje bild ritas till
en duk och plockas ut som PNG. Därför går även jpeg, svg och webp att visa, trots
att Tk bara klarar PNG och GIF — och inget bildbibliotek behöver installeras.
Ligger en bild på en annan domän utan CORS smittas duken, och då visas ingen
förhandsgranskning; det syns i så fall i rutan. Bakom bilderna ligger en
tonad bakgrund, annars skulle vita masker och genomskinliga logotyper se ut som
tomma rutor.

**Mellanlisten.** Både Översikt och Filer delas av en mellanlist med ett grepp
mitt på. Dra i den för att ge förhandsvyn mer eller mindre plats. Läget sparas
till nästa start i `installningar.json`, och dubbelklick på listen återställer
det. Tills listen dragits på Översikt får tabellen den bredd den behöver och
annonsen resten.

**Animationen** fångas under den väntetid mätningen redan har, så mätningen blir
inte längre. Annonsen fotograferas drygt sex gånger per sekund under en cykel:
längden läses ur BannerBoos konfiguration (`animtime`), annars används hela
väntetiden, högst 15 sekunder. Rutor som är exakt lika den förra slås ihop, och
en annons som aldrig rör sig får ingen animation alls. Varje ruta visas så länge
den faktiskt stod kvar, så uppspelningen går i annonsens egen takt.

Rutorna tar några MB per annons. De följer inte med till JSON eller
HTML-rapporten, och fönstret sparar dem för de sex senaste annonserna — äldre
mätningar i resultatlistan visar skärmbilden. Tk kan bara skala bilder i hela
steg, så annonsen visas i 1× eller 2×, eller förminskad till hälften, en
tredjedel och så vidare. Med förstoring i Windows, till exempel 175 %, visas den
i 2× — ungefär lika stor som i webbläsaren.

Mätningen körs i en egen tråd så att fönstret inte fryser medan annonsen laddas,
och statusraden visar varje steg: vilket varv som pågår, om samtyckesbanderollen
klickats, när sidan scrollas, hur många annonser som hittats och vilken som mäts
just nu.

Versionsnumret står i namnlisten och längst ned i fönstret, och fås på
kommandoraden med `annonsvikt --version`. Det finns på ett enda ställe i koden —
`VERSION` i `annonsvikt.py` — och bygget skickar det vidare till installeraren,
så att posten i *Appar och funktioner* alltid stämmer med det som körs.

## Så mäts vikten

En syntetisk värdsida byggs med `<script src="…">`, precis som en riktig sajt
bäddar in annonsen. Sidan laddas med tom cache och varje nätverkssvar spelas in.
Överförda byte kommer från Playwrights `sizes()` — alltså vad som faktiskt gick
över tråden **efter** komprimering, inte filstorleken på disk.

Hämtas samma fil flera gånger räknas den en gång. Video hämtas med
range-förfrågningar, och en video som spelas automatiskt hämtas två gånger:
webbläsaren börjar hämta den när sidan läses in, och BannerBoos spelare anropar
sedan `load()`, som börjar om. Om den första hämtningen hinner bli klar eller
avbryts beror på tajmingen, och en avbruten hämtning har inga mått i Playwright. Därför är det
hämtningen som fick med hela filen som räknas, så att vikten blir densamma
varje gång. Anmärkningen visar vad som hände: *avbröts och hämtades om*, eller
*hämtades 2 gånger, räknad en gång* när båda blev klara. Kom ingen hämtning i mål
räknas filens storlek enligt serverns svar, med anmärkningen *avbröts — räknad
som hela filen*, eftersom det inte syns hur mycket som hann komma fram.

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

## Råden

Annonserna görs internt i BannerBoo, och råden är skrivna för den som bygger dem.
De är grupperade efter vem som kan göra något åt saken:

| Grupp | Innehåll |
|---|---|
| Det här gör ni i BannerBoo | beskära och komprimera bilder, radera dolda lager, använda färre typsnitt och inga gjorda för andra skriftsystem, begränsa animationen |
| Det här gör ni på sajten | lazy load och placering i Advanced Ads |
| Det här styrs av BannerBoo | serverkomprimering, typsnittsformat, cache, GSAP — sådant som bara BannerBoo kan ändra |

Prognosen längst ned har två nivåer av samma skäl: **det ni kan göra i BannerBoo**,
och **om BannerBoo också gör sin del**. Den första är den ni faktiskt styr över.

### Bilder som rutan beskär

BannerBoo lägger bilder med `background-size: cover`: bilden skalas tills den
fyller rutan, och det som sticker utanför klipps bort. De bortklippta pixlarna
laddas ändå. En bild på 2400 × 1600 px i en ruta på 1200 × 700 px visar bara
2400 × 1400 px — rådet blir att beskära den till det måttet innan uppladdning.
Är bilden dessutom större än 2× rutan föreslås att den skalas ner.

### Typsnitt och SVG

Ett typsnitt laddas i sin helhet även när annonsen bara använder några tecken ur
det. Råden visar vilken text varje typsnitt bär och föreslår att två behålls. Bär
något typsnitt längre text behålls det; bär alla bara korta ord behålls de
lättaste, och orden i de tyngre görs som SVG med konturerade bokstäver — ett par
kB i stället för ett helt typsnitt.

Ett typsnitt gjort för ett annat skriftsystem får ett eget råd. Noto Sans TC är
gjort för traditionell kinesiska och väger 5–6 MB per snitt, medan ett typsnitt
för latinska alfabet väger 75–165 kB hos BannerBoo. Varje typsnittsfil över 1 MB
räknas som ett sådant, oavsett hur lite text den bär. Rådet är att sätta texten
i ett typsnitt som annonsen redan laddar, eller göra korta ord som SVG, och ett
sådant typsnitt räknas aldrig till de två som behålls.

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
        ansvar="ni",            # ni | sajten | bannerboo
    )]
```

Inget annat i filen behöver röras. Råden grupperas efter `ansvar` och sorteras
inom gruppen efter uppskattad besparing.

Sidnivåråd skrivs på samma sätt med `@sidregel(prioritet)`. Handlar ett sidråd om
att jämföra annonser med varandra ska det bara ges när sidan har minst två —
med en enda annons säger dess egna råd redan samma sak.

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
