# Gebäude-Energierechner (Web-Version 3)

Jahresverbrauch **Strom · Wärme · Wasser**, Heizlast, PV-Ertrag, CO₂ und Kosten von Gebäuden – mit Kennzahlen, Lastgang, Maßnahmen und automatisch erzeugtem **Energiekonzept**.

Die komplette Anwendung ist **eine Datei: `index.html`** (keine externen Bibliotheken, kein Server). Zum Aktualisieren auf GitHub Pages einfach die alte `index.html` im Repository ersetzen.

## Neu in Version 3

- **Energiekonzept** (eigener Reiter und Kapitel im PDF): Zusammenfassung, Ausgangslage, Bewertung mit Stärken und Schwachstellen, Stufenplan (kurzfristig → Gebäudehülle → Anlagentechnik → Eigenstrom), Zielzustand, Wirtschaftlichkeit, Netzanschluss, Rechtsrahmen und Vorgehen
- **Kennzahlen**: rund 35 Kennwerte mit Einordnung (günstig / mittel / hoch)
- **Lastgang & PV**: stündliche Jahresbilanz mit Eigenverbrauch, Autarkiegrad, Einspeisung, Lastspitzen; PV mit Ausrichtung und Neigung
- **Netzanschluss**: Leistungsbedarf (Wohnen: Kennlinien DIN 18015-1, sonst aus dem Lastgang), Absicherung, Hinweise zu VDE-AR-N 4100/4105 und § 14a EnWG
- **Wirtschaftlichkeit**: Kapitalwert, dynamische Amortisation, CO₂-Vermeidungskosten (Zins, Preissteigerung, Zeitraum, Förderquote einstellbar)
- **Bedienung**: Basis-/Expertenansicht, Suche nach Eingaben, Beispielgebäude, anklickbare Kacheln
- Strom getrennt nach Wärmepumpe (Heizung / Warmwasser), Direktheizung, elektrischem Warmwasser und sonstiger Technik

## Hinweise

- Gespeicherte Parameterdateien (.json) der Vorversion lassen sich weiter laden.
- Die Python-Version (`energierechner.py`) hat den Stand der Vorversion; Eigenverbrauch und Maßnahmenpaket werden dort noch vereinfacht gerechnet.
- Grobabschätzung für Planung, Budget und Plausibilisierung – ersetzt keinen Energieausweis, keine Heizlastberechnung nach DIN EN 12831 und keine Fachplanung.
