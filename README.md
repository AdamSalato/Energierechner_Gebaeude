# Gebäude-Energierechner (Web-Version)

Jahresverbrauch **Strom · Wärme · Wasser** von Gebäuden – Grobabschätzung nach DIN V 4108-6 / DIN V 18599 (vereinfacht), DIN EN 12831, VDI 3807/4707/4650, DIN EN ISO 52120-1, GEG.

Die komplette Anwendung steckt in **einer Datei: `index.html`** (HTML + CSS + JavaScript, keine externen Bibliotheken, kein Server nötig). Rechenkern und Ergebnisse sind identisch mit der Python-Version `energierechner.py`.

## Auf GitHub Pages veröffentlichen

1. Neues Repository anlegen (z. B. `energierechner`).
2. `index.html` (und optional diese `README.md`) hochladen: *Add file → Upload files → Commit*.
3. *Settings → Pages → Build and deployment*: **Source: Deploy from a branch**, Branch **main**, Ordner **/(root)** → *Save*.
4. Nach ca. 1 Minute ist der Rechner erreichbar unter `https://<benutzername>.github.io/energierechner/`.

Lokal funktioniert die Datei auch ohne Internet: einfach `index.html` im Browser öffnen.

## Funktionen

- Alle Eingaben wie in der Python-Version, jeweils mit **Ø-Option** („weiß ich nicht“ → Durchschnittswert, grün angezeigt)
- Auswertungen mit Grafiken: Übersicht (Effizienzklasse bzw. Bandtacho NWG, Soll-Ist), Energiefluss, Gebäudehülle, Strom, Wärme (Jahresdauerlinie), Wasser, Kosten & CO₂, Monatsverlauf, Maßnahmen
- **PDF-Bericht**: Knopf „PDF-Bericht“ → im Druckdialog „Als PDF speichern“ (A4 quer, mit Seitenzahlen)
- Parameter speichern/laden als JSON (kompatibel mit der Python-Version), Export TXT, CSV (Excel), Diagramm als PNG
- Eingaben werden automatisch im Browser gemerkt (localStorage)

## Datenschutz

Alle Berechnungen laufen ausschließlich im Browser. Es werden keine Daten an einen Server übertragen.

## Anpassen

Durchschnittswerte, Nutzungsprofile, Preise und Faktoren stehen gesammelt im ersten `<script>`-Block (`const D = {...}`), z. B. `PROFILES`, `BAUJAHR`, `U_AGE`, `CARRIERS`.

---
Grobabschätzung für Planung, Budget und Plausibilisierung – ersetzt keinen Energieausweis nach GEG und keine Heizlastberechnung nach DIN EN 12831.
