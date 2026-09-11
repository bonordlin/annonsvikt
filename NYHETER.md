# Nyheter

Punkterna under den översta rubriken som matchar `VERSION` i `annonsvikt.py`
hamnar i `version.json` och visas i uppdateringsnotisen i programmet. Håll dem
korta — de ska rymmas på en rad i fönstret.

## 1.3.0

- Förhandsgranska bildfilerna i en annons, direkt i Filer-fliken
- Visa annonsen och varje bild i större format, med möjlighet att spara
- HTML-rapporten visar en tumnagel för varje bild

## 1.2.1

- Fältet tar emot BannerBoos inbäddningskod rakt av, inte bara en länk

## 1.2.0

- Programmet upptäcker själv när en ny version finns och installerar den på ett klick
- Uppdateringar bygger inte om Python-miljön i onödan och går på sekunder
- Ny flagga `--kolla-uppdatering` på kommandoraden

## 1.1.2

- Annonsens skärmbild visas nu även vid sidskanning

## 1.1.1

- Rättat att fönstret kunde bli hängande utan besked när Playwright saknades
- Installationen provstartar nu så som genvägen gör

## 1.1.0

- Versionsnumret syns i namnlisten och via `--version`
- Löpande besked under sidskanning i stället för en still statusrad

## 1.0.0

- Mäter en annons eller alla BannerBoo-annonser på en sida
- Betyg mot IAB:s budget, råd sorterade efter effekt, rapport som HTML och JSON
- Grafiskt gränssnitt och Windows-installerare
