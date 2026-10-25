#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gebäude-Energierechner  –  Strom · Wärme · Wasser
=================================================
Überschlägige Jahresbedarfsrechnung für Wohn- und Nichtwohngebäude mit GUI.

Methodik (vereinfacht, angelehnt an):
  * DIN V 4108-6 / DIN EN ISO 13790   Monatsbilanzverfahren Heizwärmebedarf
  * DIN EN 12831                      Heizlast (vereinfacht)
  * DIN V 18599-4 / -8 / -10          Beleuchtung, Warmwasser, Nutzungsprofile
  * DIN EN 16798-1                    Außenluftvolumenstrom je Person
  * DIN EN 12897 / DVGW W 551         Speicher- und Zirkulationsverluste, Legionellen
  * VDI 4707                          Energiebedarf Aufzüge (Nutzungskategorien)
  * VDI 3807 / Vergleichswerte NWG    Plausibilitätsvergleich Kennwerte
  * GEG 2024 Anlage 4 / 9             Primärenergie- und CO2-Faktoren
  * Energieausweis-Skala (GEG)        Effizienzklassen Wohngebäude
  * DIN EN ISO 52120-1                Gebäudeautomation (Klassen A–D)
  * VDI 4650-1                        Jahresarbeitszahlen Wärmepumpen
  * IWU-Gebäudetypologie / TABULA     U-Werte nach Baualter, Prebound-Effekt

Benötigt: Python 3.8+, tkinter (Standard), matplotlib (Diagramme + PDF-Export)
          pip install matplotlib
Start:    python energierechner.py
          python energierechner.py --pdf bericht.pdf [parameter.json]   (PDF ohne GUI)

Alle Werte sind Richtwerte für eine Grobabschätzung – kein Ersatz für eine
normgerechte Berechnung (z. B. Energieausweis nach GEG oder Heizlast nach DIN EN 12831).
"""

import csv
import datetime
import json
import math
import os
import re
import sys
import time
import traceback

# =============================================================================
#  Stammdaten
# =============================================================================

AVG_LABEL = "Ø unbekannt / Durchschnitt"

MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
DAYS_M = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
# Monatsmitteltemperaturen Referenzklima Potsdam (DIN V 18599-10), °C
T_REF = [1.0, 1.9, 4.7, 9.2, 14.1, 16.7, 19.0, 18.6, 14.3, 9.5, 4.1, 0.9]
# Strahlung auf Fenster, Mittel über alle Orientierungen, kWh/(m²·Monat)
SOLAR_M = [20, 33, 55, 75, 90, 90, 95, 85, 65, 45, 22, 15]
# relative Gewichtung Beleuchtung (dunkle Monate mehr) und Kühlung
LIGHT_W = [1.30, 1.15, 1.00, 0.85, 0.75, 0.70, 0.70, 0.80, 0.95, 1.10, 1.25, 1.35]
COOL_SHARE = [0, 0, 0, 0.02, 0.08, 0.20, 0.30, 0.27, 0.11, 0.02, 0, 0]

REGIONS = {
    "Norddeutschland / Küste": dict(dT=0.4, tdes=-10, pv=880),
    "West (Rhein-Ruhr, Bergisches Land, Rheinland)": dict(dT=0.6, tdes=-10, pv=900),
    "Mitte / Ø Deutschland (Referenzklima Potsdam)": dict(dT=0.0, tdes=-12, pv=950),
    "Ost (kontinental: Brandenburg, Sachsen)": dict(dT=-0.4, tdes=-14, pv=960),
    "Süddeutschland (Bayern, Baden-Württemberg)": dict(dT=-0.6, tdes=-14, pv=1050),
    "Mittelgebirge / Höhenlage > 500 m": dict(dT=-2.0, tdes=-16, pv=950),
    "Alpenvorland / Hochlage > 800 m": dict(dT=-3.0, tdes=-18, pv=1100),
}
DEFAULT_REGION = "Mitte / Ø Deutschland (Referenzklima Potsdam)"


def _p(**kw):
    return kw


# Nutzungsprofile (angelehnt an DIN V 18599-10, VDI 3807, Erfahrungswerte)
#  m2pp      m² Nutzfläche je Person (Bewohner, Beschäftigte, Schüler, Betten, Sitzplätze ...)
#  presence  Anwesenheit h/Tag je Person (für innere Wärmegewinne, Belegung)
#  lux       Wartungswert Beleuchtungsstärke Hauptnutzung
#  light_h   Betriebsstunden Beleuchtung Hauptnutzung (vor Tageslicht/Regelung)
#  dl        Anteil der Betriebszeit mit nutzbarem Tageslicht
#  zones     Flächenanteile % (Hauptnutzung, Verkehr, Sanitär, Neben/Technik)
#  plug_pp   Geräte-/Arbeitshilfenstrom je Person kWh/a (Nichtwohngebäude)
#  base      Grundlast W/m² rund um die Uhr (Standby, Netzwerk, BMA, ...)
#  wc/hand/shower  Nutzungen je Person und Tag
#  misc_l    sonstiges Kaltwasser L/(P·d) (Wäsche, Trinken, Küche ...)
#  misc_ww   sonstiges Warmwasser L/(P·d) als 60-°C-Äquivalent
#  clean     Reinigungswasser L/(m²·Nutzungstag)
#  win       Fensterfläche / Nutzfläche
#  cool_q    Nutzkältebedarf kWh/(m²a) der gekühlten Fläche
#  air_pp    Außenluft m³/(h·Person) (DIN EN 16798-1, Kat. II inkl. Gebäudeemission)
#  bench     Vergleichswerte (Wärme, Strom) kWh/(m²a) für NWG
PROFILES = {
    "Einfamilienhaus": _p(hh=(1000, 650), bath=0.35,
        res=True, days=365, hours=24, theta=20.0, m2pp=45, presence=14, h=2.6, floor_m2=70, max_floors=3,
        room=18, we_m2=140, lux=150, light_h=1100, dl=0.25, zones=(80, 8, 7, 5), plug_pp=0, base=0.0,
        wc=4.5, hand=6, shower=0.75, misc_l=30, misc_ww=4, clean=0.0, win=0.20, cool=("keine", 0),
        cool_q=8, n_hyg=0.45, air_pp=0, absenk="Nachtabsenkung", heated=100, bench=None, lift_cat=1,
        meals=0, kitchen="keine", mech=False, ww="zentral", circ="keine",
        wc_we=1.5, sink_we=2.5, shower_we=1.2, sinks_ww=True),
    "Mehrfamilienhaus": _p(hh=(600, 600), bath=0.25,
        res=True, days=365, hours=24, theta=20.0, m2pp=42, presence=14, h=2.6, floor_m2=300, max_floors=8,
        room=17, we_m2=70, lux=150, light_h=1100, dl=0.25, zones=(82, 10, 6, 2), plug_pp=0, base=0.0,
        wc=4.5, hand=6, shower=0.75, misc_l=30, misc_ww=4, clean=0.0, win=0.18, cool=("keine", 0),
        cool_q=8, n_hyg=0.45, air_pp=0, absenk="Nachtabsenkung", heated=100, bench=None, lift_cat=2,
        meals=0, kitchen="keine", mech=False, ww="zentral", circ="ja, 24 h",
        wc_we=1.1, sink_we=1.8, shower_we=1.0, sinks_ww=True),
    "Büro / Verwaltung": _p(
        res=False, days=250, hours=11, theta=21.0, m2pp=20, presence=8.5, h=2.9, floor_m2=600, max_floors=8,
        room=22, we_m2=1, lux=400, light_h=2750, dl=0.55, zones=(65, 18, 7, 10), plug_pp=450, base=1.2,
        wc=2.5, hand=3.0, shower=0.02, misc_l=1.5, misc_ww=0.5, clean=0.12, win=0.25, cool=("keine", 0),
        cool_q=30, n_hyg=0, air_pp=36, absenk="Nacht- und Wochenendabsenkung", heated=95, bench=(95, 40),
        lift_cat=3, meals=0, kitchen="Teeküche (im Gerätestrom enthalten)", mech=False, ww="Kleinspeicher",
        circ="keine", wc_per=15, sink_per=12, shower_per=100, sinks_ww=True),
    "Schule": _p(
        res=False, days=200, hours=8, theta=20.0, m2pp=7, presence=6.5, h=3.2, floor_m2=1000, max_floors=4,
        room=60, we_m2=1, lux=350, light_h=1600, dl=0.60, zones=(62, 22, 8, 8), plug_pp=70, base=0.6,
        wc=1.5, hand=2.0, shower=0.01, misc_l=1.0, misc_ww=0.2, clean=0.15, win=0.28, cool=("keine", 0),
        cool_q=15, n_hyg=0, air_pp=25, absenk="Nacht- und Wochenendabsenkung", heated=95, bench=(110, 15),
        lift_cat=1, meals=0.3, kitchen="Aufwärm-/Verteilerküche", mech=False, ww="Kleinspeicher", circ="keine",
        wc_per=20, sink_per=15, shower_per=300, sinks_ww=False),
    "Kindertagesstätte": _p(
        res=False, days=235, hours=10, theta=21.0, m2pp=9, presence=8, h=2.9, floor_m2=500, max_floors=2,
        room=35, we_m2=1, lux=300, light_h=2350, dl=0.55, zones=(65, 15, 12, 8), plug_pp=120, base=0.8,
        wc=3.0, hand=6, shower=0.02, misc_l=5, misc_ww=2, clean=0.20, win=0.25, cool=("keine", 0),
        cool_q=10, n_hyg=0, air_pp=20, absenk="Nacht- und Wochenendabsenkung", heated=100, bench=(120, 20),
        lift_cat=1, meals=1.0, kitchen="Aufwärm-/Verteilerküche", mech=False, ww="zentral",
        circ="ja, zeitgesteuert", wc_per=8, sink_per=6, shower_per=50, sinks_ww=True),
    "Hotel / Beherbergung": _p(bath=0.15,
        res=False, days=365, hours=24, theta=21.0, m2pp=35, presence=12, h=2.7, floor_m2=600, max_floors=8,
        room=25, we_m2=1, lux=200, light_h=3000, dl=0.30, zones=(60, 20, 12, 8), plug_pp=700, base=2.0,
        wc=4, hand=6, shower=0.9, misc_l=25, misc_ww=10, clean=0.15, win=0.20,
        cool=("Splitgeräte / Teilklimatisierung", 40), cool_q=25, n_hyg=0, air_pp=30, absenk="keine",
        heated=100, bench=(150, 75), lift_cat=3, meals=1.5, kitchen="Vollküche (elektrisch)", mech=True,
        ww="zentral", circ="ja, 24 h", wc_per=1.6, sink_per=1.4, shower_per=1.7, sinks_ww=True),
    "Krankenhaus / Pflegeheim": _p(bath=0.5,
        res=False, days=365, hours=24, theta=22.0, m2pp=45, presence=20, h=3.0, floor_m2=1500, max_floors=8,
        room=25, we_m2=1, lux=300, light_h=4500, dl=0.35, zones=(55, 25, 10, 10), plug_pp=1800, base=3.0,
        wc=5, hand=15, shower=0.7, misc_l=120, misc_ww=40, clean=0.30, win=0.20,
        cool=("Zentrale Kälteanlage (Kaltwasser)", 40), cool_q=35, n_hyg=0, air_pp=50, absenk="keine",
        heated=100, bench=(230, 110), lift_cat=4, meals=3.0, kitchen="Vollküche (elektrisch)", mech=True,
        ww="zentral", circ="ja, 24 h", wc_per=2, sink_per=1.5, shower_per=3, sinks_ww=True),
    "Einzelhandel": _p(
        res=False, days=300, hours=12, theta=19.0, m2pp=25, presence=6, h=3.8, floor_m2=1500, max_floors=2,
        room=120, we_m2=1, lux=500, light_h=3600, dl=0.20, zones=(80, 6, 4, 10), plug_pp=200, base=4.0,
        wc=0.6, hand=1.0, shower=0.0, misc_l=1.0, misc_ww=0.2, clean=0.15, win=0.10,
        cool=("Zentrale Kälteanlage (Kaltwasser)", 80), cool_q=35, n_hyg=0, air_pp=20, absenk="Nachtabsenkung",
        heated=95, bench=(80, 100), lift_cat=2, meals=0, kitchen="keine", mech=True, ww="Kleinspeicher",
        circ="keine", wc_per=40, sink_per=40, shower_per=0, sinks_ww=True),
    "Gaststätte / Restaurant": _p(
        res=False, days=300, hours=13, theta=21.0, m2pp=3, presence=2.5, h=3.0, floor_m2=200, max_floors=2,
        room=40, we_m2=1, lux=300, light_h=3900, dl=0.30, zones=(75, 8, 10, 7), plug_pp=150, base=4.0,
        wc=1.5, hand=2.0, shower=0.0, misc_l=5, misc_ww=2, clean=0.25, win=0.15,
        cool=("Splitgeräte / Teilklimatisierung", 50), cool_q=30, n_hyg=0, air_pp=25, absenk="Nachtabsenkung",
        heated=100, bench=(150, 180), lift_cat=1, meals=1.5, kitchen="Vollküche (elektrisch)", mech=True,
        ww="zentral", circ="ja, zeitgesteuert", wc_per=25, sink_per=25, shower_per=0, sinks_ww=True),
    "Werkstatt / Bauhof / leichte Produktion": _p(
        res=False, days=250, hours=9, theta=17.0, m2pp=40, presence=8, h=5.0, floor_m2=1000, max_floors=2,
        room=150, we_m2=1, lux=300, light_h=2250, dl=0.40, zones=(75, 8, 7, 10), plug_pp=1500, base=1.0,
        wc=2, hand=4, shower=0.3, misc_l=5, misc_ww=0.5, clean=0.10, win=0.12, cool=("keine", 0),
        cool_q=10, n_hyg=0, air_pp=40, absenk="Nacht- und Wochenendabsenkung", heated=90, bench=(130, 45),
        lift_cat=1, meals=0, kitchen="Teeküche (im Gerätestrom enthalten)", mech=False, ww="zentral",
        circ="ja, zeitgesteuert", wc_per=15, sink_per=8, shower_per=10, sinks_ww=True),
    "Lager / Logistik": _p(
        res=False, days=250, hours=10, theta=15.0, m2pp=150, presence=8, h=7.0, floor_m2=3000, max_floors=1,
        room=400, we_m2=1, lux=150, light_h=2500, dl=0.25, zones=(85, 5, 3, 7), plug_pp=600, base=0.5,
        wc=2, hand=3, shower=0.1, misc_l=1, misc_ww=0.2, clean=0.05, win=0.05, cool=("keine", 0),
        cool_q=5, n_hyg=0, air_pp=30, absenk="Nacht- und Wochenendabsenkung", heated=80, bench=(70, 25),
        lift_cat=1, meals=0, kitchen="Teeküche (im Gerätestrom enthalten)", mech=False, ww="Kleinspeicher",
        circ="keine", wc_per=15, sink_per=15, shower_per=20, sinks_ww=True),
    "Sporthalle": _p(
        res=False, days=280, hours=14, theta=17.0, m2pp=12, presence=1.5, h=7.0, floor_m2=1200, max_floors=1,
        room=300, we_m2=1, lux=300, light_h=3900, dl=0.40, zones=(70, 10, 12, 8), plug_pp=15, base=0.5,
        wc=1.0, hand=1.0, shower=0.5, misc_l=1, misc_ww=0.2, clean=0.10, win=0.12, cool=("keine", 0),
        cool_q=5, n_hyg=0, air_pp=30, absenk="Nachtabsenkung", heated=95, bench=(140, 25), lift_cat=1,
        meals=0, kitchen="keine", mech=True, ww="zentral", circ="ja, zeitgesteuert",
        wc_per=30, sink_per=30, shower_per=15, sinks_ww=True),
}

# Baualtersklassen: H'T in W/(m²K) (inkl. Wärmebrücken), Infiltration 1/h, g-Wert Fenster,
# typische Vorlauftemperatur, Zustand Verteilung (orientiert an IWU-Gebäudetypologie)
BAUJAHR = {
    "bis 1918": dict(y=1900, ht=1.45, ninf=0.40, g=0.75, vl=70, dist=2),
    "1919–1948": dict(y=1930, ht=1.40, ninf=0.40, g=0.75, vl=70, dist=2),
    "1949–1957": dict(y=1953, ht=1.35, ninf=0.38, g=0.75, vl=70, dist=2),
    "1958–1968": dict(y=1963, ht=1.20, ninf=0.35, g=0.75, vl=70, dist=2),
    "1969–1978": dict(y=1974, ht=1.00, ninf=0.30, g=0.75, vl=70, dist=2),
    "1979–1983 (1. WSchVO)": dict(y=1981, ht=0.85, ninf=0.25, g=0.75, vl=55, dist=1),
    "1984–1994 (2. WSchVO)": dict(y=1990, ht=0.72, ninf=0.22, g=0.75, vl=55, dist=1),
    "1995–2001 (3. WSchVO)": dict(y=1998, ht=0.55, ninf=0.15, g=0.60, vl=55, dist=1),
    "2002–2008 (EnEV 2002/07)": dict(y=2005, ht=0.45, ninf=0.12, g=0.60, vl=45, dist=0),
    "2009–2015 (EnEV 2009)": dict(y=2012, ht=0.38, ninf=0.10, g=0.60, vl=35, dist=0),
    "2016–2020 (EnEV 2016)": dict(y=2018, ht=0.32, ninf=0.08, g=0.50, vl=35, dist=0),
    "ab 2021 (GEG)": dict(y=2023, ht=0.30, ninf=0.07, g=0.50, vl=35, dist=0),
}
DEFAULT_BAUJAHR = "1969–1978"

SAN_OPTS = ["unsaniert / Originalzustand", "teilsaniert (z. B. Fenster, Dach)",
            "umfassend saniert (Hülle gedämmt, neue Fenster)"]

# Energiestandard (überschreibt Baujahr/Sanierung)
STANDARDS = {
    "GEG-Neubau-Mindeststandard": dict(ht=0.32, ninf=0.08, g=0.50, wrg=False),
    "KfW-Effizienzhaus 115": dict(ht=0.45, ninf=0.12, g=0.60, wrg=False),
    "KfW-Effizienzhaus 100": dict(ht=0.40, ninf=0.10, g=0.60, wrg=False),
    "KfW-Effizienzhaus 85": dict(ht=0.35, ninf=0.09, g=0.55, wrg=False),
    "KfW-Effizienzhaus 70": dict(ht=0.30, ninf=0.08, g=0.50, wrg=False),
    "KfW-Effizienzhaus 55": dict(ht=0.26, ninf=0.07, g=0.50, wrg=False),
    "KfW-Effizienzhaus 40": dict(ht=0.22, ninf=0.05, g=0.50, wrg=True),
    "Passivhaus": dict(ht=0.15, ninf=0.03, g=0.50, wrg=True),
    "Nullemissionsgebäude (EPBD-ZEB)": dict(ht=0.15, ninf=0.03, g=0.50, wrg=True),
}

FENSTER = {
    "Einfachverglasung": 0.85,
    "Zweischeiben-Isolierglas (bis ca. 1995)": 0.75,
    "Zweischeiben-Wärmeschutzglas": 0.60,
    "Dreischeiben-Wärmeschutzglas": 0.50,
}
FORMS = {
    "freistehend": 1.0,
    "einseitig angebaut (DHH, Reihenendhaus)": 0.75,
    "beidseitig angebaut (Reihenmitte, Blockrand)": 0.50,
}
MASS = {"leicht (Holz-/Leichtbau)": 12, "mittel (Mischbauweise)": 15, "schwer (Massivbau)": 18}

# U-Werte je Baualtersklasse: Außenwand, Dach/oberste Geschossdecke, Boden/Kellerdecke, Wärmebrückenzuschlag
# (W/(m²K), orientiert an IWU-Gebäudetypologie / Bekanntmachung Regeln Datenaufnahme Wohngebäudebestand)
U_AGE = {
    "bis 1918": (1.70, 1.40, 1.20, 0.10),
    "1919–1948": (1.60, 1.40, 1.20, 0.10),
    "1949–1957": (1.40, 1.40, 1.15, 0.10),
    "1958–1968": (1.20, 1.00, 1.00, 0.10),
    "1969–1978": (1.00, 0.70, 0.95, 0.10),
    "1979–1983 (1. WSchVO)": (0.80, 0.50, 0.80, 0.10),
    "1984–1994 (2. WSchVO)": (0.60, 0.40, 0.60, 0.10),
    "1995–2001 (3. WSchVO)": (0.45, 0.30, 0.45, 0.05),
    "2002–2008 (EnEV 2002/07)": (0.35, 0.25, 0.40, 0.05),
    "2009–2015 (EnEV 2009)": (0.28, 0.20, 0.30, 0.05),
    "2016–2020 (EnEV 2016)": (0.24, 0.18, 0.28, 0.05),
    "ab 2021 (GEG)": (0.22, 0.16, 0.26, 0.03),
}
FENSTER_U = {  # U_w inkl. Rahmen
    "Einfachverglasung": 5.0,
    "Zweischeiben-Isolierglas (bis ca. 1995)": 2.8,
    "Zweischeiben-Wärmeschutzglas": 1.3,
    "Dreischeiben-Wärmeschutzglas": 0.9,
}
DACH_OPTS = {  # Temperatur-Korrekturfaktor Fx
    "Steildach mit beheiztem Dachgeschoss": 1.0,
    "Flachdach": 1.0,
    "oberste Geschossdecke (unbeheizter Dachraum)": 0.8,
}
UNTEN_OPTS = {
    "Kellerdecke zum unbeheizten Keller": 0.6,
    "Bodenplatte auf Erdreich (kein Keller)": 0.5,
    "beheizter Keller (Wände/Boden gegen Erdreich)": 0.6,
}
VERSCHATTUNG = {"gering (freie Lage)": 1.0, "normal (übliche Bebauung)": 0.9, "stark (enge Bebauung, Bäume)": 0.6}
AUSRICHTUNG = {"gleichmäßig verteilt / unbekannt": 1.0, "überwiegend Süd": 1.3,
               "überwiegend Ost/West": 0.95, "überwiegend Nord": 0.6}
MODI = ["realitätsnaher Verbrauch (Nutzerverhalten, Prebound-Effekt)",
        "Normbedarf (Standard-Randbedingungen)"]
WITTERUNG = {"Normjahr (langjähriges Mittel)": 0.0, "mildes Jahr (z. B. 2020, 2022–2024)": 1.2,
             "kaltes Jahr (z. B. 2010)": -1.3}
# Gebäudeautomation DIN EN ISO 52120-1 (ehem. DIN EN 15232): (thermisch, elektrisch) – Wohnen / Nichtwohnen
BACS = {
    "D – keine Automation": ((1.10, 1.08), (1.20, 1.10)),
    "C – Standard (Referenz)": ((1.00, 1.00), (1.00, 1.00)),
    "B – erweiterte Automation": ((0.88, 0.93), (0.80, 0.93)),
    "A – hocheffiziente Automation": ((0.81, 0.92), (0.70, 0.87)),
}
PV_M = [0.025, 0.045, 0.08, 0.11, 0.13, 0.135, 0.135, 0.12, 0.09, 0.06, 0.03, 0.02]

VENT_OPTS = ["Fensterlüftung (freie Lüftung)", "Abluftanlage (ohne WRG)",
             "Zu-/Abluftanlage mit Wärmerückgewinnung"]
ABSENK_OPTS = ["keine", "Nachtabsenkung", "Nacht- und Wochenendabsenkung"]

HEIZFL = {
    "Fußboden-/Flächenheizung (35 °C)": 35,
    "Niedertemperatur-Heizkörper (45 °C)": 45,
    "Heizkörper (55 °C)": 55,
    "Heizkörper Altbau (70 °C)": 70,
}
DIST_OPTS = ["neu (gedämmt, hydraulisch abgeglichen)", "Standard", "alt (ungedämmte Leitungen, kein Abgleich)"]
DIST_F = [1.05, 1.10, 1.18]
PUMP_OPTS = {"Hocheffizienzpumpe": 0.6, "geregelte Standardpumpe": 1.2, "alte ungeregelte Pumpe": 2.2}

GEN_TYPES = {
    "Gas-Brennwertkessel": dict(kind="cond", carrier="Erdgas", eta=0.94),
    "Gas-Brennwerttherme (Etage/Wand)": dict(kind="cond", carrier="Erdgas", eta=0.93),
    "Gas-Niedertemperaturkessel": dict(kind="boiler", carrier="Erdgas", eta=0.86),
    "Gas-Konstanttemperaturkessel (vor 1985)": dict(kind="boiler", carrier="Erdgas", eta=0.75),
    "Flüssiggas-Brennwertkessel": dict(kind="cond", carrier="Flüssiggas", eta=0.93),
    "Öl-Brennwertkessel": dict(kind="cond", carrier="Heizöl", eta=0.92),
    "Öl-Niedertemperaturkessel": dict(kind="boiler", carrier="Heizöl", eta=0.85),
    "Öl-Standardkessel (vor 1985)": dict(kind="boiler", carrier="Heizöl", eta=0.74),
    "Pelletkessel": dict(kind="boiler", carrier="Holzpellets", eta=0.86),
    "Hackschnitzelkessel": dict(kind="boiler", carrier="Hackschnitzel", eta=0.82),
    "Scheitholzkessel (Holzvergaser)": dict(kind="boiler", carrier="Scheitholz", eta=0.78),
    "Kaminofen / Kachelofen (Scheitholz)": dict(kind="boiler", carrier="Scheitholz", eta=0.70),
    "Fernwärme / Nahwärme (Übergabestation)": dict(kind="fw", carrier="Fernwärme", eta=0.97),
    "Wärmepumpe Luft/Wasser": dict(kind="wp", src="luft"),
    "Wärmepumpe Sole/Wasser (Erdsonde)": dict(kind="wp", src="sole"),
    "Wärmepumpe Wasser/Wasser (Grundwasser)": dict(kind="wp", src="wasser"),
    "BHKW (Erdgas, wärmegeführt)": dict(kind="bhkw", carrier="Erdgas", eta=0.58, eta_el=0.32),
    "Elektro-Direktheizung / Infrarot": dict(kind="el", carrier="Strom", eta=0.99),
    "Nachtspeicherheizung": dict(kind="el", carrier="Strom", eta=0.95),
    "Elektro-Heizstab (Spitzenlast)": dict(kind="el", carrier="Strom", eta=0.99),
}
NO_GEN = "— keiner —"
# Jahresarbeitszahlen Raumheizung nach Wärmequelle und Vorlauftemperatur
JAZ = {"luft": {35: 3.6, 45: 3.1, 55: 2.7, 70: 2.2},
       "sole": {35: 4.4, 45: 3.8, 55: 3.2, 70: 2.6},
       "wasser": {35: 4.8, 45: 4.1, 55: 3.5, 70: 2.8}}

WW_OPTS = {
    "zentral über Wärmeerzeuger (Speicher)": "zentral",
    "dezentral elektrisch (Durchlauferhitzer)": "dle",
    "dezentral elektrisch (Kleinspeicher/Boiler)": "Kleinspeicher",
    "Warmwasser-Wärmepumpe (Brauchwasser-WP)": "wwwp",
}
CIRC_OPTS = ["keine", "ja, 24 h", "ja, zeitgesteuert"]
WC_OPTS = {"Spartaste 6/3 L (Ø 4,5 L)": 4.5, "Standard 6 L": 6.0, "Altbestand 9 L": 9.0}
ARM_OPTS = {  # Duschkopf L/min, Handwaschen L je Vorgang
    "wassersparend (Spar-Duschkopf, Perlatoren)": (6.5, 1.0),
    "Standardarmaturen": (9.0, 1.5),
    "Komfort / Regendusche": (12.0, 2.0),
}
KITCHEN = {  # kWh je Essen, Liter je Essen, davon Warmwasser (60 °C) Liter
    "keine": (0.0, 0.0, 0.0),
    "Teeküche (im Gerätestrom enthalten)": (0.0, 0.0, 0.0),
    "Aufwärm-/Verteilerküche": (0.45, 6.0, 3.0),
    "Vollküche (elektrisch)": (1.1, 15.0, 6.0),
}

LEUCHTMITTEL = {  # W/(m²·100 lx)
    "LED (neu, effizient)": 1.6,
    "LED-Retrofit / ältere LED": 2.2,
    "Mischbestand LED / Leuchtstoff": 2.6,
    "Leuchtstoff T5 (EVG)": 2.8,
    "Leuchtstoff T8 (KVG/VVG)": 4.0,
    "Halogen / Glühlampe": 12.0,
}
STEUERUNG = {  # (Tageslichtnutzung, Präsenzfaktor)
    "manuell (Schalter)": (0.50, 1.00),
    "Zeitschaltung / Gebäudeleittechnik": (0.55, 0.95),
    "Präsenz-/Bewegungsmelder": (0.50, 0.85),
    "Präsenzmelder + tageslichtabhängige Regelung": (0.85, 0.85),
}
LIFT_CAT = {  # VDI 4707 Nutzungskategorie: Fahrstunden/Tag
    "1 – sehr gering (0,2 h/d)": 0.2,
    "2 – gering (0,5 h/d)": 0.5,
    "3 – mittel (1,5 h/d)": 1.5,
    "4 – hoch (3 h/d)": 3.0,
    "5 – sehr hoch (6 h/d)": 6.0,
}
LIFT_TECH = {  # Standby W, Wirkungsgrad Antrieb
    "modern (getriebelos, Standby-Abschaltung)": (50, 0.75),
    "Seilaufzug mit Getriebe (älter)": (150, 0.55),
    "Hydraulikaufzug": (200, 0.40),
}
COOL_OPTS = {"keine": None, "Splitgeräte / Teilklimatisierung": 3.2, "Zentrale Kälteanlage (Kaltwasser)": 3.0}
GERAETE_MODI = ["pauschal + Geräteliste", "nur pauschal", "nur Geräteliste"]

# Energieträger: Einheit, kWh je Einheit (Hi), Preis €/kWh, CO2 g/kWh (GEG Anl. 9), fP (GEG Anl. 4)
CARRIERS = {
    "Erdgas": dict(unit="m³", per=10.0, price=0.11, co2=240, fp=1.1, color="#f4a259"),
    "Flüssiggas": dict(unit="Liter", per=6.6, price=0.13, co2=270, fp=1.1, color="#f7c873"),
    "Heizöl": dict(unit="Liter", per=10.0, price=0.105, co2=310, fp=1.1, color="#8d6e63"),
    "Holzpellets": dict(unit="kg", per=4.9, price=0.075, co2=20, fp=0.2, color="#8bc34a"),
    "Hackschnitzel": dict(unit="Srm", per=750.0, price=0.05, co2=20, fp=0.2, color="#aed581"),
    "Scheitholz": dict(unit="rm", per=1800.0, price=0.07, co2=20, fp=0.2, color="#689f38"),
    "Fernwärme": dict(unit="kWh", per=1.0, price=0.14, co2=180, fp=0.7, color="#e57373"),
    "Strom": dict(unit="kWh", per=1.0, price=0.33, co2=380, fp=1.8, color="#26a69a"),
}

ENERGY_CLASSES = [("A+", 30, "#00a650"), ("A", 50, "#4cb848"), ("B", 75, "#bed62f"),
                  ("C", 100, "#fff200"), ("D", 130, "#fdb913"), ("E", 160, "#f7941d"),
                  ("F", 200, "#f15a29"), ("G", 250, "#ed1c24"), ("H", 300, "#be1e2d")]

EL_COLORS = {
    "Beleuchtung": "#f2c14e",
    "Geräte & Arbeitshilfen": "#5b8def",
    "Kleingeräte (Liste)": "#9db8ff",
    "Küche / Kantine": "#c48df2",
    "IT / Serverraum": "#7a6ff0",
    "Sonderverbraucher": "#9aa5b1",
    "Wärmepumpe": "#26a69a",
    "Elektroheizung / el. Warmwasser": "#ef6f6c",
    "Lüftung": "#7cc6fe",
    "Kühlung / Klima": "#3aa0d8",
    "Aufzüge & Fahrtreppen": "#a1887f",
    "Hilfsenergie Heizung/WW": "#b0bec5",
    "E-Mobilität (Laden)": "#ff8a65",
}
EL_GROUP = {  # Grobgliederung (Beleuchtung / Nutzung / Wärmepumpe / Heizung & Technik)
    "Beleuchtung": "Beleuchtung",
    "Geräte & Arbeitshilfen": "Nutzung", "Kleingeräte (Liste)": "Nutzung", "Küche / Kantine": "Nutzung",
    "IT / Serverraum": "Nutzung", "Sonderverbraucher": "Nutzung",
    "Wärmepumpe": "Wärmepumpe",
    "Elektroheizung / el. Warmwasser": "Heizung & Technik", "Lüftung": "Heizung & Technik",
    "Kühlung / Klima": "Heizung & Technik", "Aufzüge & Fahrtreppen": "Heizung & Technik",
    "Hilfsenergie Heizung/WW": "Heizung & Technik",
    "E-Mobilität (Laden)": "E-Mobilität",
}

# Kleingeräte-Vorlagen: Leistung kW, Volllaststunden h/a (≈ Jahresverbrauch / Leistung)
DEVICE_PRESETS = {
    "Kühlschrank": (0.10, 1800), "Gefrierschrank": (0.12, 2000), "Kühl-/Getränkeautomat": (0.40, 5000),
    "Wasserspender (gekühlt)": (0.10, 3000), "Kaffeevollautomat": (1.30, 250), "Wasserkocher": (2.00, 100),
    "Mikrowelle": (0.90, 100), "Spülmaschine": (2.00, 150), "Waschmaschine": (2.00, 90),
    "Wäschetrockner": (2.50, 120), "Kochfeld (je Zone)": (2.00, 150), "Backofen": (3.00, 100),
    "Drucker / Kopierer": (0.40, 1000), "Arbeitsplatz-PC + Monitor": (0.08, 2000), "Notebook": (0.04, 2000),
    "Server / NAS": (0.15, 8760), "Netzwerk-Switch / WLAN": (0.05, 8760), "Beamer / Display": (0.30, 800),
    "Heizlüfter": (2.00, 150), "Ventilator": (0.05, 300), "Akku-Ladestation (Werkzeug/E-Bike)": (0.30, 500),
}


# =============================================================================
#  Hilfsfunktionen
# =============================================================================

def fnum(x, d=0):
    """Zahl im deutschen Format (1.234,5)."""
    try:
        s = f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return str(x)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def parse_num(s):
    """Akzeptiert 1500 | 1.500 | 1500,5 | 1.500,5 | 1500.5"""
    s = str(s).strip().replace(" ", "").replace(" ", "")
    if not s:
        raise ValueError("leer")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


def fplain(v):
    """Zahl für Eingabefelder (ohne Tausenderpunkt, Dezimalkomma)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v - round(v)) < 1e-6 or abs(v) >= 1000:
        return str(int(round(v)))
    d = 1 if abs(v) >= 100 else 2
    return f"{v:.{d}f}".rstrip("0").rstrip(".").replace(".", ",")


def eta_util(gamma, a):
    """Ausnutzungsgrad der Wärmegewinne (DIN V 4108-6 / EN ISO 13790)."""
    if gamma <= 0:
        return 1.0
    if abs(gamma - 1.0) < 1e-6:
        return a / (a + 1.0)
    return (1.0 - gamma ** a) / (1.0 - gamma ** (a + 1.0))


def jaz_lookup(src, vl):
    tab = JAZ[src]
    keys = sorted(tab)
    if vl <= keys[0]:
        return tab[keys[0]]
    if vl >= keys[-1]:
        return tab[keys[-1]]
    for k0, k1 in zip(keys, keys[1:]):
        if k0 <= vl <= k1:
            return tab[k0] + (tab[k1] - tab[k0]) * (vl - k0) / (k1 - k0)
    return tab[55]


def monthly_by_days():
    return [d / 365.0 for d in DAYS_M]


def monthly_light():
    w = [LIGHT_W[i] * DAYS_M[i] for i in range(12)]
    s = sum(w)
    return [x / s for x in w]


def heat_duration_curve(months, H_tot, theta_set, Q_heat, Q_ww):
    """Stündliche Heizlast aus synthetischem Temperaturjahr (Monatsmittel + Tagesanomalie AR(1) +
    Tagesgang), monatsweise auf die Monatsbilanz normiert, plus konstante Warmwasserlast.
    Rückgabe: absteigend sortierte Lastwerte [kW] (8760) und Anzahl Heizstunden."""
    import random
    rnd = random.Random(4108)
    loads = []
    q_h_tot = sum(m["Q_h"] for m in months)
    f_heat = Q_heat / q_h_tot if q_h_tot > 0 else 0.0
    ww_kw = Q_ww / 8760.0
    anom = 0.0
    hours_heat = 0
    for i, m in enumerate(months):
        t_h = DAYS_M[i] * 24
        amp = 2.5 if i in (0, 1, 10, 11) else 4.0
        g_kw = m["Q_gu"] / t_h if t_h else 0.0
        vals = []
        for d in range(DAYS_M[i]):
            anom = 0.75 * anom + rnd.gauss(0.0, 2.6)
            for hh in range(24):
                te = m["th_e"] + anom + amp * math.sin((hh - 9) / 24.0 * 2 * math.pi)
                vals.append(max(0.0, H_tot * (theta_set - te) / 1000.0 - g_kw))
        sv = sum(vals)
        target = m["Q_h"] * f_heat
        fac = target / sv if sv > 0 else 0.0
        if target > 0 and sv == 0:
            vals = [target / t_h] * t_h
            fac = 1.0
        for v in vals:
            x = v * fac
            if x > 0:
                hours_heat += 1
            loads.append(x + ww_kw)
    loads.sort(reverse=True)
    return loads, hours_heat


def energy_flows(R):
    """Energieflüsse für das Sankey-Diagramm: (Quelle, Ziel, kWh)."""
    F = []
    add = lambda a, b, v: F.append((a, b, v)) if v > 0.5 else None
    bh = R["bhkw_el"]
    for k, q in R["heat_end"].items():
        add(k, "Wärmeerzeugung", q - (bh if k == "Erdgas" else 0.0))
    add("Netzstrom", "Stromverteilung", R["grid"])
    add("PV-Eigenverbrauch", "Stromverteilung", R["pv_ev"])
    add("BHKW-Strom", "Stromverteilung", R["bhkw_ev"])
    add("Umweltwärme", "Wärmeerzeugung", R["amb"])
    add("Solarthermie", "Wärmeerzeugung", R["Q_sol"])
    add("Stromverteilung", "Wärmeerzeugung", R["E_wp"] + R["E_elheat"])
    el_ww = R["E_ww_el"]
    if R["ww_central"]:
        add("Wärmeerzeugung", "Warmwasser", R["Q_ww_use"])
    else:
        add("Stromverteilung", "Warmwasser", el_ww)
        add("Umweltwärme", "Warmwasser", R["amb_ww"])
    add("Wärmeerzeugung", "Raumwärme", R["Q_h"])
    loss = R["Q_sto"] + R["Q_circ"] if R["ww_central"] else 0.0
    loss += R["Q_dist_loss"] + max(0.0, R["gen_loss"] - bh)
    add("Wärmeerzeugung", "Verluste", loss)
    el = R["el"]
    add("Stromverteilung", "Beleuchtung", el["Beleuchtung"])
    add("Stromverteilung", "Geräte & Nutzung", el["Geräte & Arbeitshilfen"] + el["Kleingeräte (Liste)"]
        + el["Küche / Kantine"] + el["IT / Serverraum"] + el["Sonderverbraucher"])
    add("Stromverteilung", "Gebäudetechnik", el["Lüftung"] + el["Kühlung / Klima"] + el["Aufzüge & Fahrtreppen"]
        + el["Hilfsenergie Heizung/WW"])
    add("Stromverteilung", "E-Mobilität", el["E-Mobilität (Laden)"])
    return F


class Ctx:
    """Liest Parameter; None = 'Ø unbekannt' -> Standardwert. Merkt sich verwendete Werte."""

    def __init__(self, p):
        self.p = p
        self.used = {}

    def num(self, key, default, lo=None, hi=None):
        v = self.p.get(key)
        try:
            v = float(default if v is None else v)
        except (TypeError, ValueError):
            v = float(default)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        self.used[key] = v
        return v

    def choice(self, key, default, valid=None):
        v = self.p.get(key)
        if not v or (valid is not None and v not in valid):
            v = default
        self.used[key] = v
        return v


# =============================================================================
#  Berechnungsmodell
# =============================================================================

def compute(p, devices=None):
    devices = devices or []
    c = Ctx(p)
    warn = []
    R = {}

    prof_name = p.get("profil") if p.get("profil") in PROFILES else "Mehrfamilienhaus"
    pr = PROFILES[prof_name]
    res = pr["res"]
    c.used["profil"] = prof_name

    # ------------------------------------------------------------------ Grunddaten
    A = c.num("flaeche", 1000, lo=10)
    floors = int(round(c.num("geschosse", min(pr["max_floors"], max(1, round(A / pr["floor_m2"]))), lo=1, hi=80)))
    h = c.num("raumhoehe", pr["h"], lo=2.0, hi=25)
    rooms = int(round(c.num("raeume", max(1, round(A / pr["room"])), lo=1)))
    persons = c.num("personen", max(1, round(A / pr["m2pp"])), lo=0)
    we = int(round(c.num("wohneinheiten", max(1, round(A / pr["we_m2"])) if res else 0, lo=0)))
    if res:
        we = max(1, we)
    days = c.num("nutzungstage", pr["days"], lo=1, hi=365)
    hours = c.num("nutzungsstunden", pr["hours"], lo=0.5, hi=24)
    theta_i = c.num("raumtemp", pr["theta"], lo=10, hi=26)
    region = c.choice("region", DEFAULT_REGION, REGIONS)
    rg = REGIONS[region]
    use_frac = days * hours / 8760.0

    # ------------------------------------------------------------------ Gebäudehülle (Bauteilverfahren)
    by_key = c.choice("baujahr", DEFAULT_BAUJAHR, BAUJAHR)
    by = BAUJAHR[by_key]
    san = c.choice("sanierung", SAN_OPTS[1] if by["y"] < 1979 else SAN_OPTS[0], SAN_OPTS)
    ninf, vl_def, dist_def = by["ninf"], by["vl"], by["dist"]
    u_w, u_r, u_b, du_wb = U_AGE[by_key]
    fen_def = min(FENSTER, key=lambda k: abs(FENSTER[k] - by["g"]))
    if by["y"] >= 2016:
        fen_def = "Dreischeiben-Wärmeschutzglas"
    if san == SAN_OPTS[1]:
        u_w = u_w * 0.85
        u_r = min(u_r, 0.30)
        ninf *= 0.75
        if FENSTER_U[fen_def] > 1.3:
            fen_def = "Zweischeiben-Wärmeschutzglas"
        dist_def = min(dist_def, 1) if by["y"] >= 1958 else dist_def
    elif san == SAN_OPTS[2]:
        u_w, u_r, u_b = min(u_w, 0.24), min(u_r, 0.18), min(u_b, 0.35)
        du_wb = min(du_wb, 0.05)
        ninf = min(ninf, 0.12)
        fen_def = "Dreischeiben-Wärmeschutzglas"
        vl_def = min(vl_def, 55)
        dist_def = min(dist_def, 1)
    std_key = p.get("standard") if p.get("standard") in STANDARDS else None
    high_std = False
    ht_target = None
    if std_key:
        st = STANDARDS[std_key]
        ht_target, ninf, vl_def, dist_def = st["ht"], st["ninf"], 35, 0
        u_w, u_r, u_b, du_wb = U_AGE["ab 2021 (GEG)"]
        fen_def = "Dreischeiben-Wärmeschutzglas" if st["g"] <= 0.55 else "Zweischeiben-Wärmeschutzglas"
        du_wb = min(du_wb, 0.05 if st["ht"] > 0.25 else 0.03)
        high_std = st["wrg"]
        c.used["standard"] = std_key
    else:
        c.used["standard"] = "aus Baujahr/Sanierung"
    is_new = std_key is not None or by["y"] >= 2016
    fen = c.choice("fenster", fen_def, FENSTER)
    g_win = FENSTER[fen]
    u_f = FENSTER_U[fen]
    form = c.choice("gebaeudeform", "freistehend", FORMS)
    mass = c.choice("bauweise", "mittel (Mischbauweise)", MASS)
    dach = c.choice("dach", "Steildach mit beheiztem Dachgeschoss" if res else "Flachdach", DACH_OPTS)
    unten = c.choice("unten", "Kellerdecke zum unbeheizten Keller" if by["y"] < 2002 or res
                     else "Bodenplatte auf Erdreich (kein Keller)", UNTEN_OPTS)

    # Geometrie (Quader, Seitenverhältnis 1 : 1,6)
    ratio_ngf = 0.80 if res else 0.85
    bgf_f = A / floors / ratio_ngf
    L = math.sqrt(1.6 * bgf_f)
    Wd = bgf_f / L
    perim = 2 * (L + Wd)
    hs = h + 0.35
    Ve = bgf_f * floors * hs
    A_wall = perim * floors * hs * FORMS[form]
    f_win = c.num("fensteranteil", pr["win"] * 100, lo=0, hi=80) / 100.0
    A_win = min(A * f_win, A_wall * 0.8)
    A_roof = bgf_f * (1.25 if dach.startswith("Steildach") else 1.0)
    A_floor = bgf_f
    A_kw = perim * 2.4 * FORMS[form] if unten.startswith("beheizter") else 0.0
    A_env = A_wall + A_roof + A_floor + A_kw
    AV = A_env / Ve
    V_net = A * h

    # Bauteile: Name, Fläche, U, Fx (Temperatur-Korrekturfaktor DIN V 4108-6 Tab. 3), Schlüssel
    comp = [
        ["Außenwand", A_wall - A_win, u_w, 1.0, "u_wand"],
        ["Fenster / Türen", A_win, u_f, 1.0, "u_fenster"],
        ["Dach / oberste Geschossdecke", A_roof, u_r, DACH_OPTS[dach], "u_dach"],
        ["Boden / Kellerdecke", A_floor, u_b, UNTEN_OPTS[unten], "u_boden"],
    ]
    if A_kw:
        comp.append(["Kellerwand gegen Erdreich", A_kw, u_b, 0.6, "u_boden"])
    over = {}
    for k in ("u_wand", "u_fenster", "u_dach", "u_boden"):
        v = p.get(k)
        if v is not None:
            try:
                over[k] = max(0.08, min(6.0, float(v)))
            except (TypeError, ValueError):
                pass
    for cp in comp:
        if cp[4] in over:
            cp[2] = over[cp[4]]
    if p.get("du_wb") is not None:
        du_wb = float(p["du_wb"])
    ht_user = p.get("ht_wert")
    if ht_user is not None:
        try:
            ht_target = max(0.08, min(3.5, float(ht_user)))
        except (TypeError, ValueError):
            pass
    H_wb = du_wb * A_env
    if ht_target is not None and not p.get("_u_fixed"):
        keep = set(over)
        for _ in range(2):
            H_fix = sum(cp[1] * cp[2] * cp[3] for cp in comp if cp[4] in keep)
            H_free = sum(cp[1] * cp[2] * cp[3] for cp in comp if cp[4] not in keep) + H_wb
            fac = max(0.15, (ht_target * A_env - H_fix) / H_free) if H_free > 0 else 1.0
            if fac < 1.0 and "u_fenster" not in keep:
                keep.add("u_fenster")      # Fenster werden nicht künstlich verbessert (U_w bleibt realistisch)
                continue
            break
        for cp in comp:
            if cp[4] not in keep:
                cp[2] *= fac
        H_wb *= fac
        du_wb *= fac
    for cp in comp:
        c.used.setdefault(cp[4], round(cp[2], 2))
    H_T = sum(cp[1] * cp[2] * cp[3] for cp in comp) + H_wb
    ht = H_T / A_env
    c.used["ht_wert"] = round(ht, 2)
    comp_out = [dict(name=cp[0], A=cp[1], U=cp[2], Fx=cp[3], H=cp[1] * cp[2] * cp[3]) for cp in comp]
    comp_out.append(dict(name="Wärmebrücken (ΔU_WB)", A=A_env, U=du_wb, Fx=1.0, H=H_wb))
    if ht > 1.6:
        warn.append(f"H'T = {fnum(ht, 2)} W/(m²K) ist sehr hoch – Eingaben zur Gebäudehülle prüfen.")

    # Solare Gewinne: Rahmenanteil 0,7 · nicht senkrechter Einfall 0,9 · Verschattung · Ausrichtung
    shade = c.choice("verschattung", "normal (übliche Bebauung)", VERSCHATTUNG)
    orient = c.choice("ausrichtung", "gleichmäßig verteilt / unbekannt", AUSRICHTUNG)
    f_sol = 0.7 * 0.9 * VERSCHATTUNG[shade] * AUSRICHTUNG[orient]
    wit = c.choice("witterung", list(WITTERUNG)[0], WITTERUNG)
    dT_clim = rg["dT"] + WITTERUNG[wit]

    # ------------------------------------------------------------------ Lüftung
    vent_def = VENT_OPTS[0]
    if high_std or (not res and pr["mech"]):
        vent_def = VENT_OPTS[2]
    elif res and by["y"] >= 2016 and not std_key:
        vent_def = VENT_OPTS[1]
    vent = c.choice("lueftung", vent_def, VENT_OPTS)
    wrg = c.num("wrg", 85 if high_std else 75, lo=0, hi=95) / 100.0
    if res:
        V_air = pr["n_hyg"] * V_net                       # m³/h, durchgehend
        vent_h_def = 8760
        n_hyg = pr["n_hyg"]
    else:
        pres_frac = min(1.0, pr["presence"] / max(hours, 0.5))
        V_air = max(persons * pres_frac * pr["air_pp"] + 0.7 * A, 0.3 * V_net)   # m³/h in Nutzungszeit
        vent_h_def = min(24, hours + 2) * days
        n_hyg = V_air / V_net * (vent_h_def / 8760.0)
    if vent == VENT_OPTS[0]:
        n_eff = n_hyg * (1.0 if res else 1.1) + ninf
    elif vent == VENT_OPTS[1]:
        n_eff = n_hyg + ninf
    else:
        n_eff = n_hyg * (1 - wrg) + ninf + 0.03
    H_V = 0.34 * n_eff * V_net
    vent_h = c.num("lueft_h", vent_h_def, lo=0, hi=8760)
    sfp_def = 0.0 if vent == VENT_OPTS[0] else ((0.30 if res else 0.45) if vent == VENT_OPTS[2] else (0.15 if res else 0.25))
    sfp = c.num("sfp", sfp_def, lo=0, hi=3)
    E_vent = 0.0 if vent == VENT_OPTS[0] else V_air * sfp * vent_h / 1000.0

    # ------------------------------------------------------------------ Wasser & Warmwasser
    use_days_w = 365 * 0.92 if res else days
    T_ww = c.num("ww_temp", 60, lo=40, hi=75)
    wc_def = "Altbestand 9 L" if by["y"] < 1995 and san == SAN_OPTS[0] else "Standard 6 L"
    if is_new:
        wc_def = "Spartaste 6/3 L (Ø 4,5 L)"
    wc_type = c.choice("wc_typ", wc_def, WC_OPTS)
    arm = c.choice("armaturen", "Standardarmaturen", ARM_OPTS)
    shower_flow, hand_l = ARM_OPTS[arm]
    if res:
        n_wc_def = max(1, round(we * pr["wc_we"]))
        n_sink_def = max(1, round(we * pr["sink_we"]))
        n_shower_def = max(1, round(we * pr["shower_we"]))
    else:
        n_wc_def = max(1, math.ceil(persons / pr["wc_per"]))
        n_sink_def = max(1, math.ceil(persons / pr["sink_per"]))
        n_shower_def = math.ceil(persons / pr["shower_per"]) if pr["shower_per"] else 0
    n_wc = int(round(c.num("anz_wc", n_wc_def, lo=0)))
    n_sink = int(round(c.num("anz_waschbecken", n_sink_def, lo=0)))
    n_shower = int(round(c.num("anz_duschen", n_shower_def, lo=0)))
    n_washroom = int(round(c.num("anz_waschraeume", max(1, round(n_wc / 2.5)) if not res else max(1, round(we * 1.1)), lo=0)))
    sinks_ww = c.choice("waschbecken_ww", "ja" if pr["sinks_ww"] else "nein (nur Kaltwasser)",
                        ["ja", "nein (nur Kaltwasser)"]) == "ja"
    shower_events_def = persons * pr["shower"]
    per_shower = c.num("duschgaenge", (shower_events_def / n_shower) if n_shower else 0, lo=0)
    shower_events = per_shower * n_shower
    shower_min = c.num("duschdauer", 6.0, lo=1, hi=30)
    rain = c.choice("regenwasser", "nein", ["nein", "ja (WC-Spülung)"]) != "nein"
    kitchen = c.choice("kueche", pr["kitchen"], KITCHEN)
    meals = c.num("essen", round(persons * pr["meals"]) if KITCHEN[kitchen][0] > 0 else 0, lo=0)
    garden = c.num("garten", 0, lo=0)
    water_extra = c.num("wasser_sonst", 0, lo=0)

    hot_e = lambda litres, tmix, share=1.0: litres * share * (tmix - 10) * 1.163 / 1000.0   # kWh
    kitchen_days = 365 if res or days > 330 else days

    w_rows = []   # (Zweck, m³/a, kWh Warmwasser-Nutzenergie, Grundlage)
    L_sh = shower_events * shower_min * shower_flow * (365 if res else days) * (0.92 if res else 1)
    w_rows.append(("Duschen", L_sh / 1000, hot_e(L_sh, 40),
                    f"{fnum(shower_events, 1)} Duschgänge/Tag × {fnum(shower_min, 1)} min × {fnum(shower_flow, 1)} L/min (40 °C)"))
    bath = c.num("baeder", pr.get("bath", 0.0), lo=0, hi=14)
    L_bath = persons * bath / 7.0 * 140.0 * (365 if res else days) * (0.92 if res else 1)
    if L_bath > 0:
        w_rows.append(("Wannenbäder", L_bath / 1000, hot_e(L_bath, 40),
                       f"{fnum(bath, 2)} Bäder je Person und Woche × 140 L (40 °C)"))
    flush = WC_OPTS[wc_type]
    L_wc = persons * pr["wc"] * flush * use_days_w
    L_rain = L_wc * 0.9 if rain else 0.0
    w_rows.append(("WC-Spülung", (L_wc - L_rain) / 1000, 0.0,
                   f"{fnum(pr['wc'], 1)} Spülungen/(P·d) × {fnum(flush, 1)} L" + (" – 90 % aus Regenwasser" if rain else "")))
    L_hand = persons * pr["hand"] * hand_l * use_days_w
    w_rows.append(("Händewaschen", L_hand / 1000, hot_e(L_hand, 35, 0.4) if sinks_ww else 0.0,
                   f"{fnum(pr['hand'], 1)} × {fnum(hand_l, 1)} L/(P·d)" + (", 40 % warm" if sinks_ww else ", nur kalt")))
    L_misc = persons * (pr["misc_l"] + pr["misc_ww"]) * use_days_w
    w_rows.append(("Wäsche, Küche, Trinken, Sonstiges", L_misc / 1000, hot_e(persons * pr["misc_ww"] * use_days_w, 60),
                   f"{fnum(pr['misc_l'] + pr['misc_ww'], 1)} L/(P·d), davon {fnum(pr['misc_ww'], 1)} L warm"))
    kw_meal, l_meal, lww_meal = KITCHEN[kitchen]
    L_kit = meals * l_meal * kitchen_days
    if L_kit > 0:
        w_rows.append(("Küche / Kantine", L_kit / 1000, hot_e(meals * lww_meal * kitchen_days, 60),
                       f"{fnum(meals)} Essen/Tag × {fnum(l_meal, 0)} L"))
    L_clean = A * pr["clean"] * days
    if L_clean > 0:
        w_rows.append(("Gebäudereinigung", L_clean / 1000, hot_e(L_clean, 40, 0.3),
                       f"{fnum(pr['clean'], 2)} L/(m²·Nutzungstag)"))
    if garden > 0:
        w_rows.append(("Außenanlagen / Bewässerung", garden * 0.1, 0.0, f"{fnum(garden)} m² × 100 L/(m²a)"))
    if water_extra > 0:
        w_rows.append(("Sonstiges (Vorgabe)", water_extra, 0.0, "manuelle Vorgabe"))
    water_m3 = sum(r[1] for r in w_rows)
    Q_ww_use = sum(r[2] for r in w_rows)
    hot_m3 = Q_ww_use / ((T_ww - 10) * 1.163 / 1000.0) / 1000.0
    abwasser = water_m3 - garden * 0.1
    l_pd = water_m3 * 1000 / max(persons, 1) / (365 if res else days)

    # Warmwasserbereitung
    ww_def_label = next(k for k, v in WW_OPTS.items() if v == pr["ww"])
    ww_label = c.choice("ww_system", ww_def_label, WW_OPTS)
    ww_mode = WW_OPTS[ww_label]
    circ_def = pr["circ"] if ww_mode == "zentral" else "keine"
    if ww_mode == "zentral" and not res and A < 500 and pr["circ"] != "keine":
        circ_def = "keine"
    circ = c.choice("zirkulation", circ_def, CIRC_OPTS)
    hot_day_L = hot_m3 * 1000 / 365.0
    V_sp = c.num("speicher", max(120, round(hot_day_L * 1.2 / 10) * 10) if ww_mode in ("zentral", "wwwp") else 0, lo=0)
    taps_ww = (n_sink if sinks_ww else 0) + n_shower + (1 if L_kit > 0 else 0) + (we if res else 0)
    Q_sto = Q_circ = E_circ = 0.0
    E_ww_el = 0.0
    E_wwwp_amb = 0.0
    if ww_mode in ("zentral", "wwwp") and V_sp > 0:
        Q_sto = (0.4 + 0.2 * V_sp ** 0.4) * 365 * max(0.3, (T_ww - 20) / 40.0)
    L_circ = 0.0
    if ww_mode == "zentral" and circ != "keine":
        L_circ = 5 + 0.04 * A + 1.5 * taps_ww
        w_m = 6.0 if (by["y"] >= 1995 or san != SAN_OPTS[0] or std_key) else 10.0
        circ_h = 8760 if circ == "ja, 24 h" else (16 * 365 if res else min(24, hours + 2) * days)
        Q_circ = L_circ * w_m * circ_h / 1000.0 * max(0.3, (T_ww - 20) / 40.0)
        E_circ = (0.010 if A < 300 else 0.025 + 0.00001 * A) * circ_h
    if ww_mode == "zentral":
        Q_ww_gen = Q_ww_use + Q_sto + Q_circ
    else:
        Q_ww_gen = 0.0
        if ww_mode == "dle":
            E_ww_el = Q_ww_use / 0.98
        elif ww_mode == "Kleinspeicher":
            E_ww_el = Q_ww_use / 0.98 + max(1, taps_ww) * 0.25 * 365
        else:  # Warmwasser-WP
            cop = 2.8
            E_ww_el = (Q_ww_use + Q_sto) / cop
            E_wwwp_amb = (Q_ww_use + Q_sto) - E_ww_el
    if ww_mode == "zentral" and T_ww < 60 and (V_sp > 400 or circ != "keine"):
        warn.append("Warmwasser < 60 °C bei Großanlage (> 400 L oder Zirkulation): Legionellenschutz nach DVGW W 551 prüfen.")
    if n_wc > 0 and persons / n_wc > 25 and not res:
        warn.append(f"{fnum(persons / n_wc, 0)} Personen je WC – Ausstattung nach ASR A4.1 prüfen.")
    if n_shower > 0 and per_shower > 25:
        warn.append(f"{fnum(per_shower, 0)} Duschgänge je Dusche und Tag erscheinen sehr hoch.")

    # ------------------------------------------------------------------ Strom: Beleuchtung
    lm = c.choice("leuchtmittel", "LED (neu, effizient)" if is_new else "Mischbestand LED / Leuchtstoff", LEUCHTMITTEL)
    pd = LEUCHTMITTEL[lm]
    ctrl_def = "Präsenzmelder + tageslichtabhängige Regelung" if (is_new and not res) else "manuell (Schalter)"
    ctrl = c.choice("lichtsteuerung", ctrl_def, STEUERUNG)
    du, pf = STEUERUNG[ctrl]
    room_size = A / max(rooms, 1)
    k_room = 1.25 if room_size < 15 else 1.10 if room_size < 30 else 1.0 if room_size < 60 else 0.9
    z_names = ["Hauptnutzung", "Verkehrsflächen (Flure, Treppen)", "Sanitär / Waschräume", "Neben-/Technik-/Kellerräume"]
    z_keys = ["haupt", "verkehr", "sanitaer", "neben"]
    if res:
        z_h_def = [pr["light_h"], 600 if prof_name == "Mehrfamilienhaus" else 400, 400, 150]
        z_dl = [pr["dl"], 0.1, 0.0, 0.0]
    else:
        z_h_def = [pr["light_h"], pr["light_h"] * 0.9, pr["light_h"] * 0.5, pr["light_h"] * 0.2]
        z_dl = [pr["dl"], 0.25, 0.0, 0.0]
    z_lux_def = [pr["lux"], 100, 200, 100]
    zones = []
    sh_raw = [c.num(f"lz_{k}_anteil", pr["zones"][i], lo=0, hi=100) for i, k in enumerate(z_keys)]
    sh_sum = sum(sh_raw) or 1.0
    E_light_int = 0.0
    P_light = 0.0
    for i, k in enumerate(z_keys):
        share = sh_raw[i] / sh_sum
        lux = c.num(f"lz_{k}_lux", z_lux_def[i], lo=0, hi=2000)
        hz = c.num(f"lz_{k}_h", z_h_def[i], lo=0, hi=8760)
        area_z = A * share
        p_z = area_z * lux / 100.0 * pd * k_room       # W
        h_eff = hz * (1 - z_dl[i] * du) * (pf if i != 2 or ctrl != "manuell (Schalter)" else 1.0)
        e_z = p_z * h_eff / 1000.0
        zones.append(dict(name=z_names[i], area=area_z, lux=lux, h=hz, h_eff=h_eff, P=p_z, E=e_z))
        E_light_int += e_z
        P_light += p_z
    garage = c.num("garage", 0, lo=0)
    garage_mode = c.choice("garage_licht", "Bewegungsmelder", ["Bewegungsmelder", "Dauerlicht"])
    E_garage = garage * 75 / 100 * pd * (8760 if garage_mode == "Dauerlicht" else 2500) / 1000.0
    out_w = c.num("aussen_w", 0 if res else round(A * 0.3), lo=0)
    out_h = c.num("aussen_h", 4100, lo=0, hi=8760)
    E_out = out_w * out_h / 1000.0
    n_exit = int(round(c.num("rettungszeichen", 0 if res else max(2, round(A / 150)), lo=0)))
    E_exit = n_exit * 4 * 8760 / 1000.0
    E_light = E_light_int + E_garage + E_out + E_exit

    # ------------------------------------------------------------------ Strom: Nutzung
    mode = c.choice("geraete_modus", GERAETE_MODI[0], GERAETE_MODI)
    if res:
        ppw = min(persons / max(we, 1), 6)
        hb, hp = pr.get("hh", (600, 600))
        plug_def = we * (hb + hp * ppw)
        plug_basis = f"{we} Haushalte × ({hb} + {hp} × {fnum(ppw, 1)} Pers.) kWh (Stromspiegel, ohne el. WW)"
    else:
        plug_def = persons * pr["plug_pp"] + pr["base"] * A * 8.76
        plug_basis = f"{fnum(persons)} Pers. × {fnum(pr['plug_pp'])} kWh + Grundlast {fnum(pr['base'], 1)} W/m²"
    plug_pauschal = c.num("geraete_pauschal", round(plug_def), lo=0)
    E_dev_list = 0.0
    dev_rows = []
    for d in devices:
        try:
            kw, n, hh = float(d["kw"]), float(d["n"]), float(d["h"])
        except (KeyError, TypeError, ValueError):
            continue
        e = kw * n * hh
        dev_rows.append((d.get("name", "Gerät"), kw, n, hh, e))
        E_dev_list += e
        if kw > 3.0:
            warn.append(f"Kleingerät „{d.get('name', '')}“ hat {fnum(kw, 1)} kW (> 3 kW) – besser als Sonderverbraucher erfassen.")
    E_plug = plug_pauschal if mode != "nur Geräteliste" else 0.0
    E_devices = E_dev_list if mode != "nur pauschal" else 0.0
    E_kitchen = meals * kw_meal * kitchen_days
    it_kw = c.num("it_kw", 0, lo=0)
    pue = c.num("pue", 1.6, lo=1.0, hi=3.0)
    E_it = it_kw * 8760 * pue
    sp_kw = c.num("sonder_kw", 0, lo=0)
    sp_h = c.num("sonder_h", 1000, lo=0, hi=8760)
    E_special = sp_kw * sp_h
    ev_n = c.num("emob_n", 0, lo=0)
    ev_km = c.num("emob_km", 12000 if res else 15000, lo=0)
    ev_cons = c.num("emob_verbrauch", 19, lo=8, hi=40)
    ev_share = c.num("emob_anteil", 70 if res else 60, lo=0, hi=100) / 100.0
    E_emob = ev_n * ev_km / 100.0 * ev_cons * ev_share * 1.10      # inkl. 10 % Ladeverluste

    # Aufzüge (VDI 4707) & Fahrtreppen
    if res:
        n_lift_def = 0 if floors < 5 else max(1, round(we / 40))
    else:
        n_lift_def = 0 if floors < 3 and prof_name not in ("Krankenhaus / Pflegeheim", "Hotel / Beherbergung") \
            else (max(1, round(A / 3000)) if floors > 1 else 0)
    n_lift = int(round(c.num("aufzuege", n_lift_def, lo=0)))
    lift_cat = c.choice("aufzug_kat", list(LIFT_CAT)[pr["lift_cat"] - 1], LIFT_CAT)
    lift_load = c.num("aufzug_last", 630 if res else 1000, lo=100, hi=5000)
    lift_tech = c.choice("aufzug_technik", list(LIFT_TECH)[0] if by["y"] >= 2002 else list(LIFT_TECH)[1], LIFT_TECH)
    sb_w, eta_l = LIFT_TECH[lift_tech]
    v_lift = 1.6 if floors > 6 else 1.0
    P_drive = lift_load * v_lift * 9.81 / 1000.0 / eta_l
    E_lift_each = sb_w * 8760 / 1000.0 + P_drive * 0.5 * LIFT_CAT[lift_cat] * 365
    E_lift = n_lift * E_lift_each
    n_esc = int(round(c.num("fahrtreppen", 0, lo=0)))
    esc_h = c.num("fahrtreppen_h", round(hours * days), lo=0, hi=8760)
    E_esc = n_esc * 3.5 * esc_h
    E_lifts = E_lift + E_esc

    # Kühlung
    cool_mode = c.choice("kuehlung", pr["cool"][0], COOL_OPTS)
    cool_share = c.num("kuehl_anteil", pr["cool"][1] if cool_mode == pr["cool"][0] and pr["cool"][1] else
                       (60 if cool_mode != "keine" else 0), lo=0, hi=100) / 100.0
    eer = c.num("eer", COOL_OPTS[cool_mode] or 3.0, lo=1.0, hi=8.0)
    Q_cool = 0.0 if cool_mode == "keine" else A * cool_share * pr["cool_q"] * max(0.5, 1 + 0.08 * dT_clim)
    E_cool = Q_cool / eer if Q_cool else 0.0

    # Gebäudeautomation (DIN EN ISO 52120-1) – Faktoren auf Heizwärme/Kälte bzw. Licht/Lüftung/Hilfsenergie
    bacs = c.choice("gebaeudeautomation", "C – Standard (Referenz)", BACS)
    f_bt, f_be = BACS[bacs][0 if res else 1]
    if f_be != 1.0:
        E_light_int *= f_be
        for z in zones:
            z["E"] *= f_be
        E_light = E_light_int + E_garage + E_out + E_exit
        E_vent *= f_be
    Q_cool *= f_bt
    E_cool = Q_cool / eer if Q_cool else 0.0

    # Monatliche Verteilung der nicht-thermischen Stromanteile
    md = monthly_by_days()
    ml = monthly_light()
    el_internal_m = [(E_light_int * ml[i] + (E_plug + E_devices + E_special) * md[i] + E_kitchen * 0.3 * md[i]) * 0.8
                     for i in range(12)]

    # ------------------------------------------------------------------ Heizwärme (Monatsbilanz)
    absenk = c.choice("absenkung", pr["absenk"], ABSENK_OPTS)
    if absenk == "keine":
        d_abs = 0.0
    elif absenk == "Nachtabsenkung":
        d_abs = (24 - min(hours, 16) if not res else 8) / 24.0 * 4 * 0.6
    else:
        d_abs = (1 - (min(hours, 16) * days) / 8760.0) * 4 * 0.6
    heated = c.num("beheizt", pr["heated"], lo=0, hi=100) / 100.0
    C_mass = MASS[mass] * Ve
    tau = C_mass / max(H_T + H_V, 1e-6)
    a_par = 1 + tau / 16.0
    gain_pp_w = 70.0
    pres_days = 365 * 0.92 if res else days
    months = []
    Q_h = 0.0
    for i in range(12):
        th_e = T_REF[i] + dT_clim
        t_h = DAYS_M[i] * 24
        dT_m = max(0.0, theta_i - d_abs - th_e)
        Q_l = (H_T + H_V) * dT_m * t_h / 1000.0
        Q_s = A_win * g_win * f_sol * SOLAR_M[i]
        Q_ip = persons * gain_pp_w * pr["presence"] * pres_days * DAYS_M[i] / 365.0 / 1000.0
        Q_i = Q_ip + el_internal_m[i]
        Q_g = Q_s + Q_i
        if Q_l <= 0:
            qh, eta = 0.0, 0.0
        else:
            gam = Q_g / Q_l
            eta = eta_util(gam, a_par)
            qh = max(0.0, Q_l - eta * Q_g)
            if gam > 2.5:
                qh = 0.0
        qh *= heated * f_bt
        months.append(dict(Q_l=Q_l, Q_T=H_T * dT_m * t_h / 1000.0, Q_V=H_V * dT_m * t_h / 1000.0, Q_s=Q_s,
                           Q_i=Q_i, eta=eta, Q_h=qh, th_e=th_e, Q_gu=eta * Q_g))
        Q_h += qh

    # Realitätsnaher Verbrauch: Prebound-/Rebound-Effekt (IWU/TABULA, Sunikka-Blank & Galvin)
    modus = c.choice("rechenmodus", MODI[0] if res else MODI[1], MODI)
    Q_h_norm = Q_h
    f_nutzer = 1.0
    if modus == MODI[0] and Q_h > 0:
        q_end_est = Q_h / A * 1.25
        f_nutzer = max(0.50, min(1.10, 1.15 - 0.0019 * q_end_est))
        for m in months:
            m["Q_h"] *= f_nutzer
        Q_h *= f_nutzer

    # Heizlast (vereinfacht DIN EN 12831)
    if res:
        n_des = n_eff
    else:
        n_des = (V_air / V_net) * (1 - wrg if vent == VENT_OPTS[2] else 1.0) * 0.7 + ninf
    phi_hl = (H_T + 0.34 * n_des * V_net) * (theta_i - rg["tdes"]) / 1000.0 * heated
    phi_ww = (Q_ww_gen / 365.0) / 8.0 if ww_mode == "zentral" else 0.0
    phi_rec = phi_hl + phi_ww

    # Verteilung / Übergabe
    hf_def = next(k for k, v in HEIZFL.items() if v == vl_def)
    hf = c.choice("heizflaechen", hf_def, HEIZFL)
    vl = HEIZFL[hf]
    dist = c.choice("verteilung", DIST_OPTS[dist_def], DIST_OPTS)
    e_dist = DIST_F[DIST_OPTS.index(dist)]
    Q_heat_gen = Q_h * e_dist
    Q_dist_loss = Q_heat_gen - Q_h

    # Solarthermie
    st_area = c.num("solarthermie", 0, lo=0)
    sol_ww = sol_h = 0.0
    if st_area > 0:
        y_pot = st_area * 380.0
        if ww_mode == "zentral":
            sol_ww = min(y_pot, 0.55 * Q_ww_gen)
            sol_h = min(max(0.0, y_pot - sol_ww) * 0.4, 0.12 * Q_heat_gen)
        else:
            sol_h = min(y_pot * 0.4, 0.12 * Q_heat_gen)
    Q_sol = sol_ww + sol_h
    Q_heat_net = Q_heat_gen - sol_h
    Q_ww_net = Q_ww_gen - sol_ww
    Q_total = Q_heat_net + Q_ww_net

    # ------------------------------------------------------------------ Jahresdauerlinie (stündlich)
    dl_curve, heat_hours = heat_duration_curve(months, H_T + H_V, theta_i - d_abs, Q_heat_net, Q_ww_net)
    dl_peak = dl_curve[0] if dl_curve else 0.0

    # ------------------------------------------------------------------ Wärmeerzeuger
    gens = []
    for i in range(1, 5):
        t = p.get(f"gen{i}_typ")
        if i == 1 and (t is None or t == AVG_LABEL):
            if std_key in ("KfW-Effizienzhaus 40", "Passivhaus", "Nullemissionsgebäude (EPBD-ZEB)") or by["y"] >= 2021:
                t = "Wärmepumpe Luft/Wasser"
            elif by["y"] >= 1979 or san != SAN_OPTS[0]:
                t = "Gas-Brennwertkessel"
            else:
                t = "Gas-Niedertemperaturkessel"
            c.used["gen1_typ"] = t
        if not t or t not in GEN_TYPES:
            continue
        gens.append(dict(idx=i, typ=t, **GEN_TYPES[t]))
    k_g = len(gens)
    for j, g in enumerate(gens):
        i = g["idx"]
        if g["kind"] == "bhkw" and k_g > 1:
            kw_def = phi_rec * 0.2           # BHKW für hohe Laufzeit (> 5.000 h/a) auf Grundlast ausgelegt
        elif k_g == 1:
            kw_def = phi_rec * 1.1
        elif j == 0:
            kw_def = phi_rec * 0.5
        else:
            kw_def = phi_rec * 0.8 / (k_g - 1)
        n = max(1, int(round(c.num(f"gen{i}_n", 1, lo=1))))
        kw_def_each = max(1.0, round(kw_def / n, 1))
        g["kw"] = c.num(f"gen{i}_kw", kw_def_each, lo=0.1)
        g["n"] = n
        g["kw_tot"] = g["kw"] * n
        g["share_spec"] = p.get(f"gen{i}_anteil")
        g["hours_spec"] = p.get(f"gen{i}_h")
        g["Q"] = 0.0
    # Verteilung der Wärmemenge
    unmet = 0.0
    if gens:
        fixed = [g for g in gens if g["hours_spec"] is not None]
        for g in fixed:
            g["Q"] = g["kw_tot"] * float(g["hours_spec"])
        Q_fixed = sum(g["Q"] for g in fixed)
        if Q_fixed > Q_total * 1.05:
            warn.append("Vorgegebene Volllaststunden ergeben mehr Wärme als berechnet – Vorgabe wurde übernommen.")
        remain = max(0.0, Q_total - Q_fixed)
        shared = [g for g in gens if g["hours_spec"] is None and g["share_spec"] is not None]
        s_sum = sum(float(g["share_spec"]) for g in shared) / 100.0
        scale = 1.0 if s_sum * Q_total <= remain or s_sum == 0 else remain / (s_sum * Q_total)
        for g in shared:
            g["Q"] = float(g["share_spec"]) / 100.0 * Q_total * scale
        remain2 = max(0.0, remain - sum(g["Q"] for g in shared))
        auto = [g for g in gens if g["hours_spec"] is None and g["share_spec"] is None]
        if auto:
            # Aufteilung über die geordnete Jahresdauerlinie: Erzeuger 1 deckt die Grundlast bis zu seiner
            # Leistung, die folgenden Erzeuger jeweils den darüber liegenden Lastanteil (bivalent-parallel)
            fsc = remain2 / Q_total if Q_total > 0 else 0.0
            curve = [x * fsc for x in dl_curve]
            cum = 0.0
            prev_cov = 0.0
            for g in auto:
                cum += g["kw_tot"]
                cov = sum(min(x, cum) for x in curve)
                g["Q"] = cov - prev_cov
                prev_cov = cov
            rest = remain2 - prev_cov
            if rest > 1:
                auto[-1]["Q"] += rest
                auto[-1]["overload"] = rest
        else:
            unmet = remain2
        for g in gens:
            c.used[f"gen{g['idx']}_anteil"] = g["Q"] / Q_total * 100 if Q_total > 0 else 0
            c.used[f"gen{g['idx']}_h"] = g["Q"] / g["kw_tot"] if g["kw_tot"] > 0 else 0
    else:
        unmet = Q_total
    if unmet > 1:
        warn.append(f"{fnum(unmet)} kWh/a Wärmebedarf sind keinem Wärmeerzeuger zugeordnet.")
    kw_inst = sum(g["kw_tot"] for g in gens)
    if gens and kw_inst < 0.9 * phi_rec:
        warn.append(f"Installierte Leistung {fnum(kw_inst, 1)} kW < geschätzte Heizlast inkl. WW {fnum(phi_rec, 1)} kW – Anlage evtl. unterdimensioniert.")
    if gens and kw_inst > 2.0 * phi_rec and phi_rec > 3:
        warn.append(f"Installierte Leistung {fnum(kw_inst, 1)} kW > 2× Heizlast ({fnum(phi_rec, 1)} kW) – Anlage überdimensioniert (Taktverluste).")

    jaz_user = p.get("jaz_wp")
    if jaz_user is not None:
        try:
            jaz_user = max(1.2, min(7.0, float(jaz_user)))
        except (TypeError, ValueError):
            jaz_user = None
    heat_end = {}      # Energieträger -> kWh Endenergie
    E_wp = E_elheat = 0.0
    amb = 0.0
    E_bhkw_el = 0.0
    fh = Q_heat_net / Q_total if Q_total > 0 else 1.0
    for g in gens:
        Q = g["Q"]
        if g["kind"] == "wp":
            jh = jaz_lookup(g["src"], vl)
            jw = jaz_lookup(g["src"], max(55, T_ww - 5)) - 0.3
            if g["src"] == "luft":
                jh += 0.06 * dT_clim
                jw += 0.06 * dT_clim
            if g.get("overload"):
                # nicht gedeckte Spitzenlast über Heizstab (COP 1)
                jh = Q * fh / (Q * fh / jh + g["overload"] * (1 - 1 / jh)) if Q > 0 else jh
            if jaz_user is not None:
                jh, jw = jaz_user, max(1.5, jaz_user - 0.6)
            e = Q * fh / jh + Q * (1 - fh) / jw
            g["eff"] = Q / e if e > 0 else 0
            g["eff_lbl"] = f"JAZ {fnum(g['eff'], 2)}"
            g["E"] = e
            g["carrier"] = "Strom"
            E_wp += e
            amb += Q - e
            if vl >= 60:
                warn.append(f"{g['typ']}: Vorlauf {vl} °C – Wärmepumpe arbeitet ineffizient, Heizflächen prüfen.")
        elif g["kind"] == "bhkw":
            fuel = Q / g["eta"]
            g["E"] = fuel
            g["eff"] = g["eta"]
            g["eff_lbl"] = f"ηth {fnum(g['eta'] * 100)} %, ηel {fnum(g['eta_el'] * 100)} %"
            E_bhkw_el += fuel * g["eta_el"]
            heat_end[g["carrier"]] = heat_end.get(g["carrier"], 0) + fuel
        else:
            eta = g["eta"]
            if g["kind"] == "cond":
                eta = min(0.99, eta + (55 - vl) * 0.0012)
            e = Q / eta
            g["E"] = e
            g["eff"] = eta
            g["eff_lbl"] = f"η {fnum(eta * 100)} %"
            if g["carrier"] == "Strom":
                E_elheat += e
            else:
                heat_end[g["carrier"]] = heat_end.get(g["carrier"], 0) + e
        g["vbh"] = Q / g["kw_tot"] if g["kw_tot"] > 0 else 0
        if g["kind"] == "wp":
            c.used.setdefault("jaz_wp", round(g["eff"], 2))
        if g["kind"] in ("wp", "cond", "boiler", "bhkw") and 0 < g["vbh"] < 800 and Q > 0 and len(gens) == 1:
            warn.append(f"{g['typ']}: nur {fnum(g['vbh'])} Volllaststunden – Leistung vermutlich zu groß.")
    E_elheat_total = E_elheat + E_ww_el

    # Hilfsenergie
    pump = c.choice("pumpe", "Hocheffizienzpumpe" if by["y"] >= 2009 or std_key else "geregelte Standardpumpe", PUMP_OPTS)
    hydronic = any(g["kind"] != "el" for g in gens)
    E_aux = ((A * PUMP_OPTS[pump] * (500.0 / max(A, 100)) ** 0.15 if hydronic else 0.0) + E_circ) * f_be

    # ------------------------------------------------------------------ Strombilanz
    el = {
        "Beleuchtung": E_light,
        "Geräte & Arbeitshilfen": E_plug,
        "Kleingeräte (Liste)": E_devices,
        "Küche / Kantine": E_kitchen,
        "IT / Serverraum": E_it,
        "Sonderverbraucher": E_special,
        "Wärmepumpe": E_wp,
        "Elektroheizung / el. Warmwasser": E_elheat_total,
        "Lüftung": E_vent,
        "Kühlung / Klima": E_cool,
        "Aufzüge & Fahrtreppen": E_lifts,
        "Hilfsenergie Heizung/WW": E_aux,
        "E-Mobilität (Laden)": E_emob,
    }
    el_basis = {
        "Beleuchtung": f"{fnum(P_light / A, 1)} W/m² installiert ({lm}, k={fnum(k_room, 2)}), {ctrl}"
                       + (f"; außen {fnum(out_w)} W × {fnum(out_h)} h" if out_w else "")
                       + (f"; {n_exit} Rettungszeichen" if n_exit else "") + (f"; Garage {fnum(garage)} m²" if garage else ""),
        "Geräte & Arbeitshilfen": plug_basis if mode != "nur Geräteliste" else "deaktiviert (nur Geräteliste)",
        "Kleingeräte (Liste)": f"{len(dev_rows)} Positionen" if mode != "nur pauschal" else "deaktiviert",
        "Küche / Kantine": f"{fnum(meals)} Essen/Tag × {fnum(kw_meal, 2)} kWh × {kitchen_days:.0f} d ({kitchen})",
        "IT / Serverraum": f"{fnum(it_kw, 1)} kW IT-Last × 8.760 h × PUE {fnum(pue, 2)}",
        "Sonderverbraucher": f"{fnum(sp_kw, 1)} kW × {fnum(sp_h)} h",
        "Wärmepumpe": "aus Wärmebedarf ÷ Jahresarbeitszahl",
        "Elektroheizung / el. Warmwasser": f"Direktheizung {fnum(E_elheat)} kWh + el. WW {fnum(E_ww_el)} kWh ({ww_label})",
        "Lüftung": f"{fnum(V_air)} m³/h × {fnum(sfp, 2)} W/(m³/h) × {fnum(vent_h)} h" if E_vent else "keine Ventilatoren",
        "Kühlung / Klima": f"{fnum(Q_cool)} kWh Nutzkälte ÷ EER {fnum(eer, 1)} ({cool_mode}, {fnum(cool_share * 100)} % der Fläche)",
        "Aufzüge & Fahrtreppen": f"{n_lift} Aufzug/Aufzüge à {fnum(E_lift_each)} kWh (VDI 4707), {n_esc} Fahrtreppe(n)",
        "Hilfsenergie Heizung/WW": f"Pumpen/Regelung ({pump})" + (f" + Zirkulationspumpe {fnum(E_circ)} kWh" if E_circ else ""),
        "E-Mobilität (Laden)": f"{fnum(ev_n)} Fahrzeuge × {fnum(ev_km)} km × {fnum(ev_cons)} kWh/100 km × "
                               f"{fnum(ev_share * 100)} % am Gebäude geladen + 10 % Ladeverluste",
    }
    E_el = sum(el.values())

    # PV / Eigenverbrauch
    pv_kwp = c.num("pv_kwp", 0, lo=0)
    pv_y = c.num("pv_ertrag", rg["pv"], lo=300, hi=1500)
    bat = c.num("speicher_kwh", 0, lo=0)
    pv_gen = pv_kwp * pv_y
    pv_ev = 0.0
    if pv_gen > 0:
        s_base = 0.30 if res else 0.45
        bonus = min(0.30, 0.30 * bat / max(pv_kwp, 0.1))
        s = min(0.9, s_base * (E_el / pv_gen) ** 0.35 + bonus)
        pv_ev = min(pv_gen * s, E_el * 0.8)
    bhkw_ev = min(E_bhkw_el * 0.7, max(0.0, E_el - pv_ev) * 0.9)
    grid = max(0.0, E_el - pv_ev - bhkw_ev)
    feed_in = (pv_gen - pv_ev) + (E_bhkw_el - bhkw_ev)

    # ------------------------------------------------------------------ Preise / CO2 / Primärenergie
    prices = {k: c.num(f"preis_{k.lower()}", v["price"], lo=0) for k, v in CARRIERS.items()}
    p_water = c.num("preis_wasser", 5.5, lo=0)
    p_feed = c.num("preis_einspeisung", 0.08, lo=0)
    co2_el = c.num("co2_strom", CARRIERS["Strom"]["co2"], lo=0)
    cost = {k: q * prices[k] for k, q in heat_end.items()}
    p_wp = c.num("preis_wp", 0.27, lo=0)
    wp_grid = grid * (E_wp / E_el) if E_el > 0 else 0.0
    cost["Strom"] = (grid - wp_grid) * prices["Strom"]
    if wp_grid > 0:
        cost["Strom Wärmepumpe (WP-Tarif)"] = wp_grid * p_wp
    cost["Wasser/Abwasser"] = water_m3 * p_water
    cost["Einspeisung (Gutschrift)"] = -feed_in * p_feed
    n_meter = we if res else 1
    gp_def = n_meter * 130 + (250 if heat_end else 0) + (60 * max(1, n_meter) ** 0.5 if heat_end else 0) \
        + 120 + (30 * n_meter if res else 0)
    cost["Grundpreise / Zähler"] = c.num("grundpreise", round(gp_def, -1), lo=0)
    cost_total = sum(cost.values())
    co2_price = c.num("co2_preis", 55, lo=0)
    co2_levy = sum(q * CARRIERS[k]["co2"] / 1e6 for k, q in heat_end.items()
                   if k in ("Erdgas", "Flüssiggas", "Heizöl")) * co2_price
    co2 = {k: q * CARRIERS[k]["co2"] / 1e6 for k, q in heat_end.items()}
    co2["Strom (Netzbezug)"] = grid * co2_el / 1e6
    co2_total = sum(co2.values())
    pe = sum(q * CARRIERS[k]["fp"] for k, q in heat_end.items()) + grid * CARRIERS["Strom"]["fp"]

    # ------------------------------------------------------------------ Kennwerte
    heat_fuel_total = sum(heat_end.values())
    heat_el = E_wp + E_elheat_total
    heat_endenergy = heat_fuel_total + heat_el                      # Endenergie für Wärme inkl. Strom
    ea_value = (heat_fuel_total + E_wp + E_elheat_total + E_aux + E_vent + (E_cool if not res else 0)) / A
    ea_class = ENERGY_CLASSES[-1][0]
    for name, lim, _col in ENERGY_CLASSES:
        if ea_value <= lim:
            ea_class = name
            break
    if ea_value > ENERGY_CLASSES[-2][1]:
        ea_class = "H"

    # Monatswerte
    q_heat_m = [m["Q_h"] for m in months]
    sh_heat = [q / Q_h if Q_h > 0 else 0 for q in q_heat_m]
    heat_heiz_m = [(Q_heat_gen - sol_h) * sh_heat[i] for i in range(12)]
    heat_m = [heat_heiz_m[i] + (Q_ww_gen - sol_ww) * md[i] for i in range(12)]
    ww_m = [((Q_ww_gen - sol_ww) if ww_mode == "zentral" else Q_ww_use) * md[i] for i in range(12)]
    wp_m = [E_wp * ((Q_heat_net * sh_heat[i] + Q_ww_net * md[i]) / Q_total if Q_total > 0 else md[i]) for i in range(12)]
    elh_m = [E_elheat * sh_heat[i] + E_ww_el * md[i] for i in range(12)]
    el_m = {
        "Beleuchtung": [E_light * ml[i] for i in range(12)],
        "Nutzung": [(E_plug + E_devices + E_kitchen + E_it + E_special) * md[i] for i in range(12)],
        "Wärmepumpe": wp_m,
        "Heizung & Technik": [elh_m[i] + (E_vent + E_lifts) * md[i] + E_cool * COOL_SHARE[i] +
                              E_aux * (0.7 * sh_heat[i] + 0.3 * md[i]) for i in range(12)],
        "E-Mobilität": [E_emob * md[i] * (1.15 if i in (0, 1, 10, 11) else 0.9) / 1.0333 for i in range(12)],
    }
    pv_sum_m = sum(PV_M)
    pv_m = [pv_gen * x / pv_sum_m for x in PV_M]

    gen_rows = []
    for g in gens:
        gen_rows.append(dict(idx=g["idx"], typ=g["typ"], kw=g["kw"], n=g["n"], kw_tot=g["kw_tot"], Q=g["Q"],
                             share=g["Q"] / Q_total * 100 if Q_total > 0 else 0, E=g.get("E", 0),
                             carrier=g.get("carrier", "Strom"), eff=g.get("eff_lbl", ""), vbh=g.get("vbh", 0)))

    carriers_out = []
    for k, q in sorted(heat_end.items(), key=lambda x: -x[1]):
        carriers_out.append(dict(name=k, kwh=q, amount=q / CARRIERS[k]["per"], unit=CARRIERS[k]["unit"],
                                 color=CARRIERS[k]["color"]))
    if E_wp:
        carriers_out.append(dict(name="Strom (Wärmepumpe)", kwh=E_wp, amount=E_wp, unit="kWh", color="#26a69a"))
    if E_elheat_total:
        carriers_out.append(dict(name="Strom (Direktheizung/el. WW)", kwh=E_elheat_total, amount=E_elheat_total,
                                 unit="kWh", color="#ef6f6c"))
    renew_out = []
    if amb + E_wwwp_amb > 0:
        renew_out.append(dict(name="Umweltwärme (Wärmepumpe)", kwh=amb + E_wwwp_amb, color="#80cbc4"))
    if Q_sol > 0:
        renew_out.append(dict(name="Solarthermie", kwh=Q_sol, color="#ffd54f"))

    gen_loss = sum(g.get("E", 0) for g in gens if g["kind"] not in ("wp",)) - sum(g["Q"] for g in gens if g["kind"] not in ("wp",))
    heat_balance = [
        ("Heizwärme", Q_h),
        ("Warmwasser", Q_ww_use),
        ("Speicher/Zirkulation", Q_sto + Q_circ if ww_mode in ("zentral", "wwwp") else 0),
        ("Verteilverluste", Q_dist_loss),
        ("Erzeugerverluste", max(0.0, gen_loss)),
    ]

    R.update(dict(
        profil=prof_name, res=res, A=A, floors=floors, h=h, rooms=rooms, persons=persons, we=we, days=days,
        hours=hours, theta_i=theta_i, region=region, Ve=Ve, A_env=A_env, AV=AV, V_net=V_net, ht=ht, H_T=H_T,
        H_V=H_V, n_eff=n_eff, A_win=A_win, g_win=g_win, tau=tau, months=months, Q_h=Q_h, Q_ww_use=Q_ww_use,
        Q_sto=Q_sto, Q_circ=Q_circ, L_circ=L_circ, V_sp=V_sp, Q_ww_gen=Q_ww_gen, Q_heat_gen=Q_heat_gen,
        Q_dist_loss=Q_dist_loss, Q_sol=Q_sol, Q_total=Q_total, unmet=unmet, phi_hl=phi_hl, phi_ww=phi_ww,
        phi_rec=phi_rec, kw_inst=kw_inst, vl=vl, e_dist=e_dist, gens=gen_rows, carriers=carriers_out,
        renew=renew_out, heat_endenergy=heat_endenergy, heat_fuel_total=heat_fuel_total, heat_el=heat_el,
        heat_balance=heat_balance, el=el, el_basis=el_basis, E_el=E_el, zones=zones, P_light=P_light,
        E_light_int=E_light_int, E_garage=E_garage, E_out=E_out, E_exit=E_exit, dev_rows=dev_rows,
        pv_gen=pv_gen, pv_ev=pv_ev, bhkw_el=E_bhkw_el, bhkw_ev=bhkw_ev, grid=grid, feed_in=feed_in,
        water_m3=water_m3, hot_m3=hot_m3, abwasser=abwasser, rain_m3=L_rain / 1000, l_pd=l_pd, w_rows=w_rows,
        n_wc=n_wc, n_sink=n_sink, n_shower=n_shower, n_washroom=n_washroom, shower_events=shower_events,
        ww_label=ww_label, ww_mode=ww_mode, circ=circ, T_ww=T_ww,
        cost=cost, cost_total=cost_total, prices=prices, co2=co2, co2_total=co2_total, pe=pe,
        ea_value=ea_value, ea_class=ea_class, bench=pr["bench"], heat_m=heat_m, ww_m=ww_m, el_m=el_m,
        heat_heiz_m=heat_heiz_m,
        q_heat_m=q_heat_m, vent=vent, wrg=wrg, E_vent=E_vent, Q_cool=Q_cool, n_lift=n_lift,
        lm=lm, ctrl=ctrl, k_room=k_room, std_label=c.used.get("standard"), baujahr=by_key, sanierung=san,
        warn=warn, used=c.used,
        comp=comp_out, A_roof=A_roof, A_floor=A_floor, A_wall=A_wall, bgf_f=bgf_f, f_sol=f_sol,
        dl_curve=dl_curve[::24], dl_peak=dl_peak, heat_hours=heat_hours, pv_m=pv_m, pv_kwp=pv_kwp,
        E_emob=E_emob, ev_n=ev_n, bacs=bacs, f_bt=f_bt, f_be=f_be, witterung=wit, dach=dach, unten=unten,
        fenster=fen, u_f=u_f, gp=cost["Grundpreise / Zähler"], co2_levy=co2_levy, co2_price=co2_price,
        E_wp=E_wp, E_elheat=E_elheat, E_ww_el=E_ww_el, amb=amb, amb_ww=E_wwwp_amb, sol_ww=sol_ww, sol_h=sol_h,
        gen_loss=max(0.0, gen_loss), heat_end=heat_end, E_light=E_light, E_aux=E_aux, E_cool=E_cool,
        E_lifts=E_lifts, Q_ww_net=Q_ww_net, Q_heat_net=Q_heat_net, ww_central=(ww_mode == "zentral"),
        n_meter=n_meter, A_kw=A_kw, p_wp=p_wp, wp_grid=wp_grid, modus=modus, f_nutzer=f_nutzer, Q_h_norm=Q_h_norm,
        projekt={k: str(p.get(k) or "").strip() for k in ("projekt_name", "projekt_adresse", "projekt_bearbeiter")},
        ist={k: p.get(k) for k in ("ist_strom", "ist_waerme", "ist_wasser")},
    ))
    R["flows"] = energy_flows(R)
    R["report"] = build_report(R)
    return R


# =============================================================================
#  Maßnahmen / Variantenvergleich
# =============================================================================

def invest_wp(kw):
    return 9000 + 2300 * max(kw, 4) ** 0.85


def invest_pv(kwp):
    return kwp * (1000 + 6000 / (kwp + 5))


def measures(p, devices, R):
    """Rechnet typische Effizienzmaßnahmen als Varianten durch.
    Rückgabe: Liste dict(name, beschreibung, R, d_end, d_el, d_heat, d_cost, d_co2, invest, amort)."""
    base = dict(p)
    A = R["A"]
    u = R["used"]
    comp = {c["name"]: c for c in R["comp"]}
    # Hülle 'einfrieren', damit Varianten exakt vom Ist-Zustand ausgehen
    env = dict(_u_fixed=True, du_wb=R["comp"][-1]["U"])
    for k in ("u_wand", "u_fenster", "u_dach", "u_boden"):
        env[k] = u.get(k)
    A_wall_op = comp["Außenwand"]["A"]
    A_win = comp["Fenster / Türen"]["A"]
    roof = comp["Dach / oberste Geschossdecke"]
    floor = comp["Boden / Kellerdecke"]
    gens = R["gens"]
    has_wp_only = gens and all("Wärmepumpe" in g["typ"] for g in gens)
    fossil = any(g["carrier"] in ("Erdgas", "Heizöl", "Flüssiggas") for g in gens)
    cand = []

    def add(name, desc, changes, invest, env_change=False):
        q = dict(base)
        if env_change:
            q.update(env)
        q.update(changes)
        cand.append((name, desc, q, invest))

    if env["u_wand"] and env["u_wand"] > 0.30:
        add("Fassade dämmen", f"Außenwand auf U = 0,20 W/(m²K) (WDVS ca. 16 cm), {fnum(A_wall_op)} m²",
            {"u_wand": 0.20, "du_wb": min(env["du_wb"], 0.05)}, A_wall_op * 190, True)
    if env["u_dach"] and env["u_dach"] > 0.25:
        oben = "oberste Geschossdecke" if roof["Fx"] < 1 else "Dach"
        add(f"{oben.capitalize()} dämmen", f"{oben} auf U = 0,14 W/(m²K), {fnum(roof['A'])} m²", {"u_dach": 0.14},
            roof["A"] * (55 if roof["Fx"] < 1 else 210), True)
    if env["u_boden"] and env["u_boden"] > 0.40 and floor["Fx"] >= 0.55:
        add("Kellerdecke / Boden dämmen", f"auf U = 0,30 W/(m²K), {fnum(floor['A'])} m²", {"u_boden": 0.30},
            floor["A"] * 60, True)
    if env["u_fenster"] and env["u_fenster"] > 1.6:
        add("Fenster tauschen", f"Dreifachverglasung U_w = 0,9 W/(m²K), {fnum(A_win)} m²",
            {"u_fenster": 0.9, "fenster": "Dreischeiben-Wärmeschutzglas"}, A_win * 750, True)
    if u.get("verteilung") != DIST_OPTS[0] or u.get("pumpe") != "Hocheffizienzpumpe":
        add("Hydraulischer Abgleich + HE-Pumpe", "Optimierung Heizungsverteilung (GEG § 60c), Hocheffizienzpumpe",
            {"verteilung": DIST_OPTS[0], "pumpe": "Hocheffizienzpumpe"}, 1200 + 4 * A)
    if u.get("raumtemp", 20) > 19.5:
        add("Raumtemperatur −1 K", "Absenkung Sollwert, Thermostate einstellen (je K ca. 6 % Heizwärme)",
            {"raumtemp": u["raumtemp"] - 1.0}, 15 * R["rooms"] if not R["res"] else 30 * max(1, R["we"]))
    led_ok = u.get("leuchtmittel") == "LED (neu, effizient)"
    ctrl_ok = R["res"] or u.get("lichtsteuerung") == "Präsenzmelder + tageslichtabhängige Regelung"
    if not (led_ok and ctrl_ok):
        ch = {"leuchtmittel": "LED (neu, effizient)"}
        if not R["res"]:
            ch["lichtsteuerung"] = "Präsenzmelder + tageslichtabhängige Regelung"
        add("LED-Beleuchtung" + ("" if R["res"] else " + Präsenz-/Tageslichtregelung"),
            "Umrüstung der Innenbeleuchtung", ch, A * (6 if R["res"] else 32))
    if R["vent"] != VENT_OPTS[2]:
        add("Lüftung mit Wärmerückgewinnung", "Zu-/Abluftanlage, WRG 80 %", {"lueftung": VENT_OPTS[2], "wrg": 80},
            A * (65 if R["res"] else 110))
    if gens and not has_wp_only:
        kw = max(R["phi_rec"], 3.0)
        ch = {"gen1_typ": "Wärmepumpe Luft/Wasser", "gen1_kw": round(kw, 1), "gen1_n": 1,
              "gen1_anteil": None, "gen1_h": None}
        for i in range(2, 5):
            ch[f"gen{i}_typ"] = NO_GEN
        hint = " – Vorlauf ≥ 60 °C: Heizflächen prüfen!" if R["vl"] >= 60 else ""
        add("Wärmepumpe statt Kessel", f"Luft/Wasser-WP {fnum(kw, 1)} kW monovalent{hint}", ch, invest_wp(kw))
    if R["pv_kwp"] <= 0:
        kwp = max(3.0, min(750.0, round(R["bgf_f"] * 0.45 / 5.5, 1)))
        ch = {"pv_kwp": kwp}
        if R["res"]:
            ch["speicher_kwh"] = round(min(kwp, 20 * max(1, R["we"])), 0)
        add("Photovoltaik", f"{fnum(kwp, 1)} kWp auf ca. 45 % der Dachfläche" +
            (f" + {fnum(ch.get('speicher_kwh', 0))} kWh Speicher" if R["res"] else ""),
            ch, invest_pv(kwp) + ch.get("speicher_kwh", 0) * 650)
    if R["ww_central"] and R["Q_sol"] <= 0 and (R["res"] or R["Q_ww_use"] / A > 5):
        area = round(max(4.0, min(R["persons"] * 1.2, A * 0.04)), 0)
        add("Solarthermie Warmwasser", f"{fnum(area)} m² Kollektorfläche", {"solarthermie": area}, 3000 + 850 * area)
    if u.get("armaturen") != list(ARM_OPTS)[0] or u.get("wc_typ") != list(WC_OPTS)[0]:
        add("Wassersparende Armaturen", "Spar-Duschköpfe, Perlatoren, WC-Spartaste",
            {"armaturen": list(ARM_OPTS)[0], "wc_typ": list(WC_OPTS)[0]},
            R["n_shower"] * 60 + R["n_sink"] * 25 + R["n_wc"] * 120)

    out = []
    base_end = R["heat_endenergy"] + R["E_el"] - R["heat_el"]
    for name, desc, q, invest in cand:
        try:
            V = compute(q, devices)
        except Exception:      # pragma: no cover
            continue
        v_end = V["heat_endenergy"] + V["E_el"] - V["heat_el"]
        d = dict(name=name, desc=desc, R=V, d_end=base_end - v_end,
                 d_heat=R["heat_fuel_total"] - V["heat_fuel_total"], d_el=R["grid"] - V["grid"],
                 d_cost=R["cost_total"] - V["cost_total"], d_co2=R["co2_total"] - V["co2_total"],
                 d_water=R["water_m3"] - V["water_m3"], invest=invest)
        d["amort"] = invest / d["d_cost"] if d["d_cost"] > 1 else None
        out.append(d)
    # Gesamtpaket
    pk = dict(base)
    names = []
    inv = 0.0
    for name, desc, q, invest in cand:
        if name in ("Raumtemperatur −1 K",):
            continue
        for k, v in q.items():
            if k not in base or base.get(k) != v:
                pk[k] = v
        names.append(name)
        inv += invest
    if len(names) >= 2:
        V = compute(pk, devices)
        v_end = V["heat_endenergy"] + V["E_el"] - V["heat_el"]
        d = dict(name="Gesamtpaket (alle Maßnahmen)", desc=", ".join(names), R=V, d_end=base_end - v_end,
                 d_heat=R["heat_fuel_total"] - V["heat_fuel_total"], d_el=R["grid"] - V["grid"],
                 d_cost=R["cost_total"] - V["cost_total"], d_co2=R["co2_total"] - V["co2_total"],
                 d_water=R["water_m3"] - V["water_m3"], invest=inv, paket=True)
        d["amort"] = inv / d["d_cost"] if d["d_cost"] > 1 else None
        out.append(d)
    out.sort(key=lambda x: (x.get("paket", False), -x["d_co2"]))
    return out


# =============================================================================
#  Textbericht
# =============================================================================

def build_report(R):
    A = R["A"]
    L = []
    add = L.append
    line = "─" * 78
    add("GEBÄUDE-ENERGIERECHNER – Ergebnisbericht")
    add(f"erstellt: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}")
    add(line)
    add("Grobabschätzung nach vereinfachten Verfahren (DIN V 4108-6, DIN V 18599, VDI 3807/4707).")
    add("Ersetzt keinen Energieausweis und keine Heizlastberechnung nach DIN EN 12831.")
    pj = R.get("projekt", {})
    if any(pj.values()):
        add("")
        for k, lbl in (("projekt_name", "Projekt"), ("projekt_adresse", "Adresse"), ("projekt_bearbeiter", "Bearbeiter")):
            if pj.get(k):
                add(f"{lbl + ':':<25}{pj[k]}")
    add(f"{'Rechenmodus:':<25}{R['modus']}"
        + (f" (Faktor {fnum(R['f_nutzer'], 2)} auf Heizwärme)" if R["f_nutzer"] != 1 else ""))
    add(f"{'Witterung:':<25}{R['witterung']}")
    add("")
    add("1  GEBÄUDE & NUTZUNG")
    add(line)
    add(f"Nutzung:                 {R['profil']}")
    add(f"Nutzfläche (beheizt):    {fnum(A)} m²   |  Geschosse: {R['floors']}  |  Raumhöhe: {fnum(R['h'], 2)} m")
    add(f"Räume:                   {R['rooms']} (Ø {fnum(A / max(R['rooms'], 1), 1)} m²/Raum)")
    add(f"Personen:                {fnum(R['persons'])} ({fnum(A / max(R['persons'], 1), 1)} m²/Person)"
        + (f"  |  Wohneinheiten: {R['we']}" if R["res"] else ""))
    add(f"Nutzungszeit:            {fnum(R['days'])} Tage/Jahr × {fnum(R['hours'], 1)} h/Tag")
    add(f"Klimaregion:             {R['region']}")
    add(f"Raumsolltemperatur:      {fnum(R['theta_i'], 1)} °C")
    add("")
    add("2  GEBÄUDEHÜLLE & LÜFTUNG")
    add(line)
    add(f"Baualter / Sanierung:    {R['baujahr']} / {R['sanierung']}")
    add(f"Energiestandard:         {R['std_label']}")
    add(f"Bruttovolumen Ve:        {fnum(R['Ve'])} m³  |  Hüllfläche A: {fnum(R['A_env'])} m²  |  A/V: {fnum(R['AV'], 2)} 1/m")
    add(f"H'T:                     {fnum(R['ht'], 2)} W/(m²K)  →  H_T = {fnum(R['H_T'])} W/K")
    add(f"Lüftung:                 {R['vent']}" + (f" (WRG {fnum(R['wrg'] * 100)} %)" if R['vent'].startswith("Zu-") else ""))
    add(f"wirksamer Luftwechsel:   {fnum(R['n_eff'], 2)} 1/h  →  H_V = {fnum(R['H_V'])} W/K")
    add(f"Fensterfläche:           {fnum(R['A_win'])} m² (g = {fnum(R['g_win'], 2)}, solarer Faktor {fnum(R['f_sol'], 2)})"
        f"  |  Zeitkonstante τ = {fnum(R['tau'])} h")
    add(f"Gebäudeautomation:       {R['bacs']}")
    add("")
    add(f"  {'Bauteil':<32} {'Fläche m²':>10} {'U W/(m²K)':>10} {'Fx':>6} {'H W/K':>9} {'Anteil':>7}")
    for cp in R["comp"]:
        add(f"  {cp['name']:<32} {fnum(cp['A']):>10} {fnum(cp['U'], 2):>10} {fnum(cp['Fx'], 2):>6} {fnum(cp['H']):>9}"
            f" {fnum(cp['H'] / (R['H_T'] + R['H_V']) * 100, 1):>6} %")
    add(f"  {'Lüftung / Infiltration':<32} {'':>10} {'':>10} {'':>6} {fnum(R['H_V']):>9}"
        f" {fnum(R['H_V'] / (R['H_T'] + R['H_V']) * 100, 1):>6} %")
    add("")
    add("3  WÄRME")
    add(line)
    add(f"Heizlast (vereinfacht):  {fnum(R['phi_hl'], 1)} kW  + WW-Zuschlag {fnum(R['phi_ww'], 1)} kW  = {fnum(R['phi_rec'], 1)} kW"
        f"  ({fnum(R['phi_hl'] * 1000 / A)} W/m²)")
    add(f"installierte Leistung:   {fnum(R['kw_inst'], 1)} kW  |  Spitzenlast Dauerlinie {fnum(R['dl_peak'], 1)} kW"
        f"  |  {fnum(R['heat_hours'])} Heizstunden/a")
    add("")
    add("Monatsbilanz Heizwärme (kWh):")
    add("        " + "".join(f"{m:>8}" for m in MONTHS))
    add("θe °C   " + "".join(f"{fnum(m['th_e'], 1):>8}" for m in R["months"]))
    add("Verl.   " + "".join(f"{fnum(m['Q_l']):>8}" for m in R["months"]))
    add("Gew.    " + "".join(f"{fnum(m['Q_s'] + m['Q_i']):>8}" for m in R["months"]))
    add("η       " + "".join(f"{fnum(m['eta'], 2):>8}" for m in R["months"]))
    add("Q_h     " + "".join(f"{fnum(m['Q_h']):>8}" for m in R["months"]))
    add("")
    for name, val in R["heat_balance"]:
        add(f"  {name:<38} {fnum(val):>12} kWh/a  {fnum(val / A, 1):>7} kWh/(m²a)")
    if R["Q_sol"]:
        add(f"  {'– Solarthermie-Ertrag':<38} {fnum(R['Q_sol']):>12} kWh/a")
    add(f"  {'Wärme ab Erzeuger':<38} {fnum(R['Q_total']):>12} kWh/a")
    add("")
    add("Wärmeerzeuger:")
    for g in R["gens"]:
        add(f"  [{g['idx']}] {g['typ']}: {g['n']} × {fnum(g['kw'], 1)} kW | Wärme {fnum(g['Q'])} kWh ({fnum(g['share'], 0)} %) | "
            f"Endenergie {fnum(g['E'])} kWh {g['carrier']} | {g['eff']} | {fnum(g['vbh'])} Vollbenutzungsstunden")
    add("")
    add("Endenergie Wärme nach Energieträger:")
    for cr in R["carriers"]:
        amt = "" if cr["unit"] == "kWh" else f"  ≈ {fnum(cr['amount'], 0 if cr['amount'] > 100 else 1)} {cr['unit']}"
        add(f"  {cr['name']:<34} {fnum(cr['kwh']):>12} kWh/a{amt}")
    for rn in R["renew"]:
        add(f"  {rn['name']:<34} {fnum(rn['kwh']):>12} kWh/a  (Umgebung, nicht eingekauft)")
    add(f"  {'Summe Endenergie Wärme':<34} {fnum(R['heat_endenergy']):>12} kWh/a  = {fnum(R['heat_endenergy'] / A, 1)} kWh/(m²a)")
    add("")
    add("Warmwasser:")
    add(f"  System: {R['ww_label']} | {fnum(R['T_ww'])} °C | Speicher {fnum(R['V_sp'])} L | Zirkulation: {R['circ']}"
        + (f" (≈ {fnum(R['L_circ'])} m Leitung)" if R['L_circ'] else ""))
    add(f"  Nutzenergie {fnum(R['Q_ww_use'])} kWh/a, Speicherverlust {fnum(R['Q_sto'])} kWh/a, Zirkulationsverlust {fnum(R['Q_circ'])} kWh/a")
    add("")
    add("4  STROM")
    add(line)
    for k, v in R["el"].items():
        if v <= 0:
            continue
        add(f"  {k:<34} {fnum(v):>12} kWh/a  {fnum(v / A, 1):>7} kWh/(m²a)   [{EL_GROUP[k]}]")
        add(f"      ↳ {R['el_basis'][k]}")
    add(f"  {'SUMME Strom':<34} {fnum(R['E_el']):>12} kWh/a  {fnum(R['E_el'] / A, 1):>7} kWh/(m²a)")
    add("")
    add("  Beleuchtungszonen:")
    for z in R["zones"]:
        add(f"    {z['name']:<34} {fnum(z['area']):>7} m² | {fnum(z['lux']):>4} lx | {fnum(z['P'] / max(z['area'], 1), 1):>4} W/m² | "
            f"{fnum(z['h'])} h → eff. {fnum(z['h_eff'])} h | {fnum(z['E'])} kWh")
    if R["dev_rows"]:
        add("  Kleingeräte:")
        for d in R["dev_rows"]:
            add(f"    {d[0]:<30} {fnum(d[2])} × {fnum(d[1], 2)} kW × {fnum(d[3])} h = {fnum(d[4])} kWh")
    if R["pv_gen"] or R["bhkw_el"]:
        add("")
        add(f"  PV-Erzeugung {fnum(R['pv_gen'])} kWh, davon Eigenverbrauch {fnum(R['pv_ev'])} kWh")
        if R["bhkw_el"]:
            add(f"  BHKW-Strom {fnum(R['bhkw_el'])} kWh, davon Eigenverbrauch {fnum(R['bhkw_ev'])} kWh")
        add(f"  Netzbezug {fnum(R['grid'])} kWh | Einspeisung {fnum(R['feed_in'])} kWh")
    add("")
    add("5  WASSER")
    add(line)
    for name, m3, q, basis in R["w_rows"]:
        add(f"  {name:<34} {fnum(m3, 1):>9} m³/a   ({basis})")
    add(f"  {'SUMME Trinkwasser':<34} {fnum(R['water_m3'], 1):>9} m³/a = {fnum(R['l_pd'], 0)} L/(Person·Nutzungstag)")
    add(f"  davon erwärmt ({fnum(R['T_ww'])} °C-Äquivalent):    {fnum(R['hot_m3'], 1)} m³/a")
    if R["rain_m3"]:
        add(f"  Regenwassernutzung (WC):            {fnum(R['rain_m3'], 1)} m³/a")
    add(f"  Abwasser:                           {fnum(R['abwasser'], 1)} m³/a")
    add(f"  Ausstattung: {R['n_wc']} WC, {R['n_sink']} Waschbecken, {R['n_shower']} Duschen, {R['n_washroom']} Sanitärräume")
    add("")
    add("6  KOSTEN, CO2, PRIMÄRENERGIE")
    add(line)
    for k, v in R["cost"].items():
        if abs(v) > 0.5:
            add(f"  {k:<34} {fnum(v):>10} €/a")
    add(f"  {'SUMME':<34} {fnum(R['cost_total']):>10} €/a")
    if R["co2_levy"] > 1:
        add(f"  darin CO2-Abgabe (BEHG, {fnum(R['co2_price'])} €/t):  ca. {fnum(R['co2_levy'])} €/a")
    add(f"  CO2-Emissionen: {fnum(R['co2_total'], 1)} t/a ({fnum(R['co2_total'] * 1e3 / A, 1)} kg/(m²a))")
    add(f"  Primärenergie (GEG-Faktoren): {fnum(R['pe'])} kWh/a ({fnum(R['pe'] / A, 1)} kWh/(m²a))")
    add(f"  Energieausweis-ähnlicher Endenergiekennwert: {fnum(R['ea_value'], 1)} kWh/(m²a)"
        + (f" → Klasse {R['ea_class']}" if R["res"] else ""))
    if R["bench"]:
        add(f"  Vergleichswerte NWG: Wärme {R['bench'][0]} | Strom {R['bench'][1]} kWh/(m²a)"
            f"  – Ist: Wärme {fnum(R['heat_endenergy'] / A, 0)} | Strom {fnum(R['E_el'] / A, 0)}")
    ist = [(k, n, u, v) for k, n, u, v in (("ist_strom", "Strom", "kWh", R["E_el"]),
                                           ("ist_waerme", "Wärme (Endenergie)", "kWh", R["heat_endenergy"]),
                                           ("ist_wasser", "Wasser", "m³", R["water_m3"])) if R["ist"].get(k)]
    if ist:
        add("")
        add("  Vergleich mit gemessenem Verbrauch:")
        for k, n, u, v in ist:
            m = float(R["ist"][k])
            add(f"    {n:<22} berechnet {fnum(v):>10} {u} | gemessen {fnum(m):>10} {u} | Abweichung {fnum((v - m) / m * 100, 1):>6} %")
    if R["warn"]:
        add("")
        add("7  HINWEISE")
        add(line)
        for w in R["warn"]:
            add("  • " + w)
    return "\n".join(L)


# =============================================================================
#  Diagramme und Tabellen (gemeinsam für GUI und PDF-Export)
# =============================================================================

try:
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.path import Path as MPath
    from matplotlib.patches import PathPatch, Rectangle, FancyBboxPatch
    HAS_MPL = True
except Exception:            # pragma: no cover
    HAS_MPL = False

APP_TITLE = "Gebäude-Energierechner – Strom · Wärme · Wasser"
BG = "#eef1f5"
CARD = "#ffffff"
ACCENT = "#1f6feb"
TXT = "#1f2933"
MUTED = "#6b7785"
BORDER = "#d5dbe3"
DARK = "#1f2a37"
AVG_FG = "#2e7d32"
WATER_COLORS = ["#3aa0d8", "#7cc6fe", "#9fd8cb", "#5b8def", "#c48df2", "#f2c14e", "#8bc34a", "#b0bec5", "#80cbc4"]
GROUP_COLORS = {"Beleuchtung": "#f2c14e", "Nutzung": "#5b8def", "Wärmepumpe": "#26a69a",
                "Heizung & Technik": "#9aa5b1", "E-Mobilität": "#ff8a65"}
GROUP_ORDER = ["Beleuchtung", "Nutzung", "Wärmepumpe", "Heizung & Technik", "E-Mobilität"]
COMP_COLORS = ["#ef6f6c", "#5b8def", "#f2c14e", "#a1887f", "#8d6e63", "#b0bec5", "#7cc6fe"]
NODE_COLORS = {
    "Netzstrom": "#26a69a", "PV-Eigenverbrauch": "#f9a825", "BHKW-Strom": "#7e57c2", "Umweltwärme": "#80cbc4",
    "Solarthermie": "#ffd54f", "Stromverteilung": "#26a69a", "Wärmeerzeugung": "#ef6f6c", "Raumwärme": "#f4a259",
    "Warmwasser": "#f7c873", "Verluste": "#b0bec5", "Beleuchtung": "#f2c14e", "Geräte & Nutzung": "#5b8def",
    "Gebäudetechnik": "#9aa5b1", "E-Mobilität": "#ff8a65",
}
TABS = [("overview", "Übersicht"), ("flow", "Energiefluss"), ("huelle", "Hülle"), ("strom", "Strom"),
        ("waerme", "Wärme"), ("wasser", "Wasser"), ("kosten", "Kosten & CO₂"), ("monat", "Monate"),
        ("massnahmen", "Maßnahmen")]
TAB_TITLES = {
    "overview": "Übersicht und Kennwerte", "flow": "Energiefluss", "huelle": "Gebäudehülle und Wärmebilanz",
    "strom": "Strom", "waerme": "Wärme", "wasser": "Wasser", "kosten": "Kosten, CO₂ und Primärenergie",
    "monat": "Monatsverlauf", "massnahmen": "Maßnahmen und Einsparpotenziale",
}


def _scale(values):
    m = max([abs(v) for v in values] + [0])
    return (1000.0, "MWh") if m >= 20000 else (1.0, "kWh")


def _gen_color(g):
    if "Wärmepumpe" in g["typ"]:
        return "#26a69a"
    if "BHKW" in g["typ"]:
        return "#7e57c2"
    if g["carrier"] == "Strom":
        return "#ef6f6c"
    return CARRIERS.get(g["carrier"], {}).get("color", "#9aa5b1")


def _style_ax(ax, grid_axis=None):
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(BORDER)
    ax.tick_params(labelsize=7.5, colors=MUTED)
    if grid_axis:
        ax.grid(axis=grid_axis, color="#e9edf2", lw=0.8)
        ax.set_axisbelow(True)


def _title(ax, text):
    ax.set_title(text, fontsize=9.5, loc="left", fontweight="bold", color=TXT, pad=8)


def _donut(ax, items, center, unit="kWh", legend=True):
    items = [(l, v, c) for l, v, c in items if v > 0.5]
    if not items:
        ax.text(0.5, 0.5, "keine Werte", ha="center", va="center", color=MUTED, transform=ax.transAxes)
        ax.axis("off")
        return
    tot = sum(v for _, v, _ in items)
    wedges, _ = ax.pie([v for _, v, _ in items], colors=[c for _, _, c in items], startangle=90,
                       counterclock=False, wedgeprops=dict(width=0.36, edgecolor="white", linewidth=1.5))
    ax.text(0, 0.08, center, ha="center", va="center", fontsize=11, fontweight="bold", color=TXT)
    ax.text(0, -0.17, unit, ha="center", va="center", fontsize=7.5, color=MUTED)
    if legend:
        ax.legend(wedges, [f"{l}  {fnum(v / tot * 100, 1)} %" for l, v, _ in items], loc="center left",
                  bbox_to_anchor=(0.98, 0.5), frameon=False, fontsize=7.5, handlelength=1.0, handleheight=1.0)
    ax.set_aspect("equal")


def _barh(ax, labels, values, colors, title, unit, annot=None):
    y = list(range(len(labels)))[::-1]
    ax.barh(y, values, color=colors, height=0.62)
    ax.set_yticks(y)
    ax.set_yticklabels([l if len(l) <= 34 else l[:33] + "…" for l in labels], fontsize=7.8)
    mx = max([abs(v) for v in values] + [1e-9])
    mn = min(values + [0])
    for i, (yi, v) in enumerate(zip(y, values)):
        txt = fnum(v, 1 if mx < 100 else 0) + (annot[i] if annot else "")
        ax.text(v + (mx * 0.02 if v >= 0 else -mx * 0.02), yi, txt, va="center",
                ha="left" if v >= 0 else "right", fontsize=7.5, color=TXT)
    ax.set_xlim(mn * 1.35 if mn < 0 else 0, mx * 1.3)
    if mn < 0:
        ax.axvline(0, color=BORDER, lw=1)
    _title(ax, title)
    ax.set_xlabel(unit, fontsize=7.5, color=MUTED)
    _style_ax(ax)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0, colors=TXT)


def _ist_rows(R):
    out = []
    for k, n, u, v in (("ist_strom", "Strom", "kWh", R["E_el"]), ("ist_waerme", "Wärme", "kWh", R["heat_endenergy"]),
                       ("ist_wasser", "Wasser", "m³", R["water_m3"])):
        m = R["ist"].get(k)
        try:
            m = float(m) if m is not None else None
        except (TypeError, ValueError):
            m = None
        if m and m > 0:
            out.append((n, v, m, u))
    return out


# ----------------------------------------------------------------------------- Einzeldiagramme
def chart_overview(fig, R, M=None):
    A = R["A"]
    ist = _ist_rows(R)
    right0 = 0.60 if ist else 0.73
    if R["res"]:
        ax = fig.add_axes([0.03, 0.20, right0 - 0.12, 0.52])
        x0 = 0
        for name, lim, col in ENERGY_CLASSES:
            ax.barh(0, lim - x0, left=x0, color=col, height=1.0, edgecolor="white", linewidth=1.5)
            ax.text((x0 + lim) / 2, 0, name, ha="center", va="center", fontsize=9, fontweight="bold",
                    color="white" if name in ("A+", "A", "F", "G", "H") else TXT)
            ax.text(lim, -0.75, str(lim), ha="center", va="top", fontsize=7, color=MUTED)
            x0 = lim
        v = min(R["ea_value"], 298)
        ax.annotate(f"{fnum(R['ea_value'])} kWh/(m²a)", xy=(v, 0.55), xytext=(v, 1.6), ha="center",
                    fontsize=9, fontweight="bold", color=TXT, arrowprops=dict(arrowstyle="-|>", color=TXT, lw=1.6))
        ax.set_xlim(0, 300)
        ax.set_ylim(-1.3, 2.3)
        ax.axis("off")
        _title(ax, f"Endenergiekennwert – Effizienzklasse {R['ea_class']}")
    else:
        # Nichtwohngebäude: Bandtacho wie im NWG-Energieausweis (GEG) – je eine Skala für Wärme und Strom,
        # Farbverlauf grün → rot, Vergleichswert der Gebäudeart (VDI 3807 / Bekanntmachung) markiert
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("band", [c for _, _, c in ENERGY_CLASSES])
        b = R["bench"] or (100, 50)
        items = [("Wärme (Endenergie)", R["heat_endenergy"] / A, b[0]),
                 ("Strom", R["E_el"] / A, b[1])]
        w = right0 - (0.19 if ist else 0.13)
        for j, (name, val, ref) in enumerate(items):
            axb = fig.add_axes([0.03, 0.46 - j * 0.38, w, 0.22])
            xmax = max(2.0 * ref, val * 1.12, 10)
            grad = [[i / 255.0 for i in range(256)]]
            axb.imshow(grad, aspect="auto", cmap=cmap, extent=(0, xmax, 0, 1))
            for t in range(0, int(xmax) + 1, max(10, int(round(xmax / 8 / 10)) * 10)):
                axb.text(t, -0.12, str(t), ha="center", va="top", fontsize=7, color=MUTED)
            axb.plot([ref, ref], [-0.05, 1.05], color=TXT, lw=1.4, ls=(0, (3, 2)))
            axb.text(ref, -0.55, f"Vergleichswert {fnum(ref)}", ha="center", va="top", fontsize=7.5, color=TXT)
            v = min(val, xmax)
            pct = val / ref * 100 if ref else 0
            ha = "left" if v < 0.18 * xmax else ("right" if v > 0.82 * xmax else "center")
            axb.annotate(f"{fnum(val)} kWh/(m²a)  ({fnum(pct)} % des Vergleichswerts)", xy=(v, 1.05),
                         xytext=(v, 1.75), ha=ha, va="bottom", fontsize=8.3, fontweight="bold", color=TXT,
                         arrowprops=dict(arrowstyle="-|>", color=TXT, lw=1.5, shrinkA=1, shrinkB=0))
            axb.set_xlim(0, xmax)
            axb.set_ylim(-1.1, 3.4)
            axb.axis("off")
            axb.text(0, 3.4, name, fontsize=9, fontweight="bold", color=ACCENT, va="top")
        fig.text(0.03, 0.93, f"Energiekennwerte – Vergleich mit {R['profil']} (Bandtacho wie NWG-Energieausweis)",
                 fontsize=9.5, fontweight="bold", color=TXT, va="top")
    ax2 = fig.add_axes([right0, 0.14, 0.15 if ist else 0.24, 0.70])
    labels = ["Heizwärme (Nutz)", "Warmwasser (Nutz)", "Wärme Endenergie", "Strom gesamt", "Primärenergie"]
    vals = [R["Q_h"] / A, R["Q_ww_use"] / A, R["heat_endenergy"] / A, R["E_el"] / A, R["pe"] / A]
    _barh(ax2, labels, vals, ["#f4a259", "#f7c873", "#ef6f6c", "#26a69a", "#9aa5b1"], "Kennwerte", "kWh/(m²a)")
    if ist:
        ax3 = fig.add_axes([0.84, 0.14, 0.14, 0.70])
        names = [n for n, _, _, _ in ist]
        pct = [v / m * 100 for _, v, m, _ in ist]
        cols = ["#26a69a" if n == "Strom" else "#ef6f6c" if n == "Wärme" else "#3aa0d8" for n in names]
        ax3.bar(range(len(ist)), pct, color=cols, width=0.6)
        ax3.axhline(100, color=TXT, lw=1.2, ls="--")
        for i, v in enumerate(pct):
            ax3.text(i, v + 3, f"{fnum(v)} %", ha="center", fontsize=7.5)
        ax3.set_xticks(range(len(ist)))
        ax3.set_xticklabels(names, fontsize=7.8)
        ax3.set_ylim(0, max(pct + [100]) * 1.25)
        _title(ax3, "Berechnet / gemessen")
        _style_ax(ax3, "y")


def chart_flow(fig, R, M=None):
    flows = R["flows"]
    ax = fig.add_axes([0.0, 0.0, 1.0, 0.91])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    fig.text(0.01, 0.975, "Energiefluss – vom Energiebezug bis zur Nutzung [kWh/a]", fontsize=10,
             fontweight="bold", color=TXT, va="top")
    if not flows:
        return
    heat_src = list(R["heat_end"].keys()) + ["Umweltwärme", "Solarthermie"]
    orders = [heat_src + ["Netzstrom", "PV-Eigenverbrauch", "BHKW-Strom"], ["Stromverteilung"],
              ["Wärmeerzeugung", "Beleuchtung", "Geräte & Nutzung", "Gebäudetechnik", "E-Mobilität"],
              ["Raumwärme", "Warmwasser", "Verluste"]]
    present = {a for a, _, _ in flows} | {b for _, b, _ in flows}
    cols = [[n for n in o if n in present] for o in orders]
    vin, vout = {}, {}
    for a, b, v in flows:
        vout[a] = vout.get(a, 0) + v
        vin[b] = vin.get(b, 0) + v
    nv = {n: max(vin.get(n, 0), vout.get(n, 0)) for n in present}
    X = [0.20, 0.42, 0.62, 0.84]
    gap = 0.035
    top_y = 0.93
    usable = 0.88
    k = min((usable - gap * (len(c) - 1)) / max(sum(nv[n] for n in c), 1e-9) for c in cols if c)
    pos = {}
    for ci, c in enumerate(cols):
        if ci == 1:
            continue
        y = top_y
        for n in c:
            h = max(nv[n] * k, 0.004)
            pos[n] = [X[ci], y, h]
            y -= h + gap
    if cols[1]:
        n = cols[1][0]
        h = max(nv[n] * k, 0.004)
        srcs = [pos[a] for a, b, _ in flows if b == n and a in pos]
        yc = sum(p[1] - p[2] / 2 for p in srcs) / len(srcs) if srcs else 0.5
        top = max(min(top_y, yc + h / 2), h + 0.01)
        pos[n] = [X[1], top, h]
    nw = 0.014
    off_out, off_in = {}, {}
    order_out = sorted(range(len(flows)), key=lambda i: (-pos[flows[i][1]][1]))
    order_in = sorted(range(len(flows)), key=lambda i: (-pos[flows[i][0]][1]))
    acc = {}
    for i in order_out:
        a = flows[i][0]
        off_out[i] = acc.get(a, 0.0)
        acc[a] = off_out[i] + flows[i][2] * k
    acc = {}
    for i in order_in:
        b = flows[i][1]
        off_in[i] = acc.get(b, 0.0)
        acc[b] = off_in[i] + flows[i][2] * k
    col_of = {n: ci for ci, c in enumerate(cols) for n in c}
    for i, (a, b, v) in enumerate(flows):
        x0 = pos[a][0] + nw / 2
        x1 = pos[b][0] - nw / 2
        h = v * k
        y0t = pos[a][1] - off_out[i]
        y1t = pos[b][1] - off_in[i]
        xm = (x0 + x1) / 2
        verts = [(x0, y0t), (xm, y0t), (xm, y1t), (x1, y1t), (x1, y1t - h), (xm, y1t - h), (xm, y0t - h),
                 (x0, y0t - h), (x0, y0t)]
        codes = [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.LINETO, MPath.CURVE4,
                 MPath.CURVE4, MPath.CURVE4, MPath.CLOSEPOLY]
        src_col = col_of.get(a) == 0
        colr = NODE_COLORS.get(a, CARRIERS.get(a, {}).get("color", "#9aa5b1")) if src_col else \
            NODE_COLORS.get(b, "#9aa5b1")
        ax.add_patch(PathPatch(MPath(verts, codes), facecolor=colr, alpha=0.38, lw=0))
    tot_in = sum(nv[n] for n in cols[0])
    # Beschriftungen kollisionsfrei setzen (Mindestabstand je Spalte)
    lab_y = {}
    for ci, c in enumerate(cols):
        last = None
        for n in c:
            if n not in pos:
                continue
            yc = pos[n][1] - pos[n][2] / 2
            if last is not None and yc > last - 0.062:
                yc = last - 0.062
            lab_y[n] = yc
            last = yc
    for n, (x, yt, h) in pos.items():
        colr = NODE_COLORS.get(n, CARRIERS.get(n, {}).get("color", "#9aa5b1"))
        ax.add_patch(Rectangle((x - nw / 2, yt - h), nw, h, facecolor=colr, edgecolor="white", lw=0.5))
        ci = col_of[n]
        lbl = f"{n}\n{fnum(nv[n])} kWh"
        if ci == 0:
            lbl += f" ({fnum(nv[n] / tot_in * 100)} %)" if tot_in else ""
            ax.text(x - nw / 2 - 0.008, lab_y.get(n, yt - h / 2), lbl, ha="right", va="center", fontsize=7.3, color=TXT)
        elif n in ("Stromverteilung", "Wärmeerzeugung"):
            ax.text(x, yt + 0.006, lbl.replace("\n", "  "), ha="center", va="bottom", fontsize=7.3, color=TXT,
                    fontweight="bold", bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))
        else:
            ax.text(x + nw / 2 + 0.008, lab_y.get(n, yt - h / 2), lbl, ha="left", va="center", fontsize=7.3, color=TXT)


def chart_huelle(fig, R, M=None):
    A = R["A"]
    q_loss = sum(m["Q_l"] for m in R["months"])
    Htot = R["H_T"] + R["H_V"]
    items = [(c["name"], c["H"]) for c in R["comp"]] + [("Lüftung / Infiltration", R["H_V"])]
    items.sort(key=lambda x: -x[1])
    ax = fig.add_axes([0.20, 0.14, 0.22, 0.72])
    vals = [h / Htot * q_loss / A if Htot else 0 for _, h in items]
    _barh(ax, [n for n, _ in items], vals, COMP_COLORS[:len(items)], "Wärmeverluste nach Bauteil",
          "kWh/(m²a)", annot=[f"  ({fnum(h / Htot * 100)} %)" for _, h in items])
    ax2 = fig.add_axes([0.53, 0.14, 0.45, 0.72])
    x = list(range(12))
    mo = R["months"]
    sc, un = _scale([m["Q_l"] for m in mo])
    qt = [m["Q_T"] / sc for m in mo]
    qv = [m["Q_V"] / sc for m in mo]
    gs = [-m["eta"] * m["Q_s"] / sc for m in mo]
    gi = [-m["eta"] * m["Q_i"] / sc for m in mo]
    ax2.bar(x, qt, color="#ef6f6c", width=0.7, label="Transmission")
    ax2.bar(x, qv, bottom=qt, color="#f4a259", width=0.7, label="Lüftung")
    ax2.bar(x, gs, color="#ffd54f", width=0.7, label="solare Gewinne (genutzt)")
    ax2.bar(x, gi, bottom=gs, color="#9fd8cb", width=0.7, label="innere Gewinne (genutzt)")
    ax2.plot(x, [m["Q_h"] / sc for m in mo], color=DARK, marker="o", ms=3.5, lw=1.6, label="Heizwärmebedarf")
    ax2.axhline(0, color=MUTED, lw=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(MONTHS, fontsize=7.5)
    lo = min(gs[i] + gi[i] for i in x)
    hi = max(qt[i] + qv[i] for i in x)
    ax2.set_ylim(lo * 1.15, hi * 1.6)
    ax2.legend(frameon=False, fontsize=7, ncol=3, loc="upper center")
    _title(ax2, f"Monatsbilanz Verluste / Gewinne (DIN V 4108-6) [{un}]")
    _style_ax(ax2, "y")


def chart_strom(fig, R, M=None):
    A = R["A"]
    items = [(k, v, EL_COLORS[k]) for k, v in R["el"].items() if v > 0]
    sc, un = _scale([R["E_el"]])
    ax = fig.add_axes([0.0, 0.05, 0.27, 0.9])
    _donut(ax, items, fnum(R["E_el"] / sc, 1 if sc > 1 else 0), f"{un}/a Strom")
    items.sort(key=lambda x: -x[1])
    ax2 = fig.add_axes([0.76, 0.14, 0.21, 0.74])
    _barh(ax2, [k for k, _, _ in items], [v / A for _, v, _ in items], [c for _, _, c in items],
          "Spezifischer Stromverbrauch", "kWh/(m²a)")


def chart_waerme(fig, R, M=None):
    items = [(c["name"], c["kwh"], c["color"]) for c in R["carriers"]] + \
            [(r["name"], r["kwh"], r["color"]) for r in R["renew"]]
    total = sum(v for _, v, _ in items)
    sc, un = _scale([total])
    ax = fig.add_axes([0.0, 0.05, 0.27, 0.9])
    _donut(ax, items, fnum(total / sc, 1 if sc > 1 else 0), f"{un}/a Wärme")
    ax2 = fig.add_axes([0.62, 0.15, 0.36, 0.70])
    curve = R["dl_curve"]
    if not curve or not R["gens"]:
        ax2.axis("off")
        return
    x = [i * 24 for i in range(len(curve))]
    prev = [0.0] * len(curve)
    cum = 0.0
    layers, labels, colors = [], [], []
    for g in R["gens"]:
        cum += g["kw_tot"]
        cur = [min(v, cum) for v in curve]
        layers.append([c - p for c, p in zip(cur, prev)])
        labels.append(f"{g['idx']} {g['typ'][:26]}")
        colors.append(_gen_color(g))
        prev = cur
    rest = [max(0.0, v - p) for v, p in zip(curve, prev)]
    if max(rest) > 0.01:
        layers.append(rest)
        labels.append("nicht gedeckt (Heizstab/Überlast)")
        colors.append("#e53935")
    ax2.stackplot(x, *layers, colors=colors, labels=labels, alpha=0.9)
    ax2.plot(x, curve, color=DARK, lw=1.0)
    ax2.axhline(R["phi_rec"], color=MUTED, ls="--", lw=1)
    ax2.text(8760, R["phi_rec"], f"Heizlast {fnum(R['phi_rec'], 1)} kW ", ha="right", va="bottom",
             fontsize=7, color=MUTED)
    ax2.set_xlim(0, 8760)
    ax2.set_ylim(0, max(max(curve), R["phi_rec"]) * 1.35)
    ax2.set_xlabel(f"Stunden im Jahr ({fnum(R['heat_hours'])} Heizstunden)", fontsize=7.5, color=MUTED)
    ax2.set_ylabel("kW", fontsize=7.5, color=MUTED)
    ax2.legend(frameon=False, fontsize=7, loc="upper right")
    _title(ax2, "Geordnete Jahresdauerlinie & Erzeugerdeckung")
    _style_ax(ax2, "y")


def chart_wasser(fig, R, M=None):
    rows = [(n, m3, WATER_COLORS[i % len(WATER_COLORS)]) for i, (n, m3, _, _) in enumerate(R["w_rows"])]
    ax = fig.add_axes([0.0, 0.05, 0.27, 0.9])
    _donut(ax, rows, fnum(R["water_m3"]), "m³/a Trinkwasser")
    div = max(R["persons"], 1) * (365 if R["res"] else R["days"])
    rs = sorted(rows, key=lambda x: -x[1])
    ax2 = fig.add_axes([0.76, 0.14, 0.21, 0.74])
    _barh(ax2, [n for n, _, _ in rs], [m3 * 1000 / div for _, m3, _ in rs], [c for _, _, c in rs],
          "Liter je Person und Nutzungstag", "L/(P·d)")


COST_COLORS = {"Strom": "#26a69a", "Strom Wärmepumpe (WP-Tarif)": "#80cbc4", "Wasser/Abwasser": "#3aa0d8",
               "Grundpreise / Zähler": "#b0bec5"}


def chart_kosten(fig, R, M=None):
    items = []
    for k, v in R["cost"].items():
        if v > 0.5:
            items.append((k, v, COST_COLORS.get(k, CARRIERS.get(k, {}).get("color", "#9aa5b1"))))
    ax = fig.add_axes([0.0, 0.05, 0.25, 0.85])
    _donut(ax, items, f"{fnum(R['cost_total'])} €", "pro Jahr (netto Gutschrift)")
    _title(ax, "Kosten")
    co2 = [(k, v, CARRIERS.get(k.split(" (")[0], {}).get("color", "#26a69a")) for k, v in R["co2"].items() if v > 0]
    ax2 = fig.add_axes([0.50, 0.05, 0.25, 0.85])
    _donut(ax2, co2, f"{fnum(R['co2_total'], 1)} t", "CO₂ pro Jahr")
    _title(ax2, "CO₂-Emissionen")


def chart_monat(fig, R, M=None):
    x = list(range(12))
    hz, ww = R["heat_heiz_m"], R["ww_m"]
    sc, un = _scale([hz[i] + ww[i] for i in x])
    ax = fig.add_axes([0.06, 0.14, 0.38, 0.72])
    ax.bar(x, [v / sc for v in hz], color="#ef6f6c", label="Heizung", width=0.7)
    ax.bar(x, [v / sc for v in ww], bottom=[v / sc for v in hz], color="#f7c873", label="Warmwasser", width=0.7)
    _title(ax, f"Wärmebedarf am Erzeuger [{un}]")
    axt = ax.twinx()
    te = [m["th_e"] for m in R["months"]]
    axt.plot(x, te, color="#3aa0d8", marker="o", ms=3, lw=1.5, label="Außentemp.")
    axt.set_ylim(min(te + [0]) - 2, max(te) * 1.5)
    axt.set_ylabel("°C", fontsize=7.5, color="#3aa0d8")
    axt.tick_params(labelsize=7.5, colors="#3aa0d8")
    for sp in ("top",):
        axt.spines[sp].set_visible(False)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = axt.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, frameon=False, fontsize=7, loc="upper center", ncol=3)
    ax2 = fig.add_axes([0.55, 0.14, 0.42, 0.72])
    tot_m = [sum(v[i] for v in R["el_m"].values()) for i in x]
    sc2, un2 = _scale(tot_m + R["pv_m"])
    bottom = [0.0] * 12
    for k in GROUP_ORDER:
        vv = [v / sc2 for v in R["el_m"].get(k, [0] * 12)]
        if sum(vv) <= 0:
            continue
        ax2.bar(x, vv, bottom=bottom, color=GROUP_COLORS[k], label=k, width=0.7)
        bottom = [b + v for b, v in zip(bottom, vv)]
    if R["pv_gen"] > 0:
        ax2.plot(x, [v / sc2 for v in R["pv_m"]], color="#f9a825", marker="o", ms=3.5, lw=1.8, label="PV-Erzeugung")
    _title(ax2, f"Stromverbrauch [{un2}]")
    ax2.legend(frameon=False, fontsize=7, loc="upper center", ncol=3)
    for a in (ax, ax2):
        a.set_xticks(x)
        a.set_xticklabels(MONTHS, fontsize=7.5)
        a.set_ylim(0, a.get_ylim()[1] * 1.32)
        _style_ax(a, "y")


def chart_massnahmen(fig, R, M=None):
    if not M:
        fig.text(0.5, 0.5, "Keine sinnvollen Maßnahmen gefunden bzw. noch nicht berechnet.", ha="center",
                 va="center", color=MUTED, fontsize=10)
        return
    names = [m["name"] for m in M]
    ax = fig.add_axes([0.24, 0.12, 0.30, 0.76])
    _barh(ax, names, [m["d_co2"] for m in M],
          ["#2e7d32" if m["d_co2"] >= 0 else "#e53935" for m in M], "CO₂-Einsparung", "t CO₂/a")
    ax2 = fig.add_axes([0.62, 0.12, 0.34, 0.76])
    ann = [f"  ({fnum(m['amort'], 1 if m['amort'] < 10 else 0)} J.)" if m["amort"] and m["amort"] < 100 else ""
           for m in M]
    _barh(ax2, names, [m["d_cost"] for m in M], ["#1f6feb" if m["d_cost"] >= 0 else "#e53935" for m in M],
          "Kostenersparnis (statische Amortisation)", "€/a", annot=ann)
    ax2.set_yticklabels([])


CHARTS = {"overview": chart_overview, "flow": chart_flow, "huelle": chart_huelle, "strom": chart_strom,
          "waerme": chart_waerme, "wasser": chart_wasser, "kosten": chart_kosten, "monat": chart_monat,
          "massnahmen": chart_massnahmen}


# ----------------------------------------------------------------------------- Tabellen
def _row(level, cells, style=None):
    return dict(level=level, cells=cells, style=style)


def build_tables(key, R, M=None):
    """Tabellen je Auswertung. Spaltendefinition: (Titel, Breite px, 'w'/'e')."""
    A = R["A"]
    T = []
    if key == "overview":
        cols = [("Bereich", 250, "w"), ("Jahreswert", 110, "e"), ("Einheit", 70, "w"), ("je m²", 90, "e"),
                ("Herleitung / Hinweis", 430, "w")]
        rows = [_row(0, ["Strom gesamt", fnum(R["E_el"]), "kWh/a", fnum(R["E_el"] / A, 1),
                         "Summe aller Stromverbraucher"], "total")]
        grp = {}
        for k, val in R["el"].items():
            grp[EL_GROUP[k]] = grp.get(EL_GROUP[k], 0) + val
        hints = {"Beleuchtung": f"{fnum(R['P_light'] / A, 1)} W/m² installiert, {R['lm']}",
                 "Nutzung": "Geräte, Arbeitshilfen, Küche, IT, Sonderverbraucher",
                 "Wärmepumpe": "Wärmepumpenstrom (Heizung + WW)",
                 "Heizung & Technik": "Direktheizung, el. WW, Lüftung, Kühlung, Aufzüge, Pumpen",
                 "E-Mobilität": f"{fnum(R['ev_n'])} Fahrzeuge"}
        for name in GROUP_ORDER:
            if grp.get(name, 0) > 0:
                rows.append(_row(1, [name, fnum(grp[name]), "kWh/a", fnum(grp[name] / A, 1), hints[name]]))
        if R["pv_gen"] or R["bhkw_el"]:
            rows.append(_row(1, ["Netzbezug (nach Eigenerzeugung)", fnum(R["grid"]), "kWh/a", fnum(R["grid"] / A, 1),
                                 f"PV {fnum(R['pv_gen'])} kWh, Eigenverbrauch {fnum(R['pv_ev'] + R['bhkw_ev'])} kWh"],
                             "muted"))
        rows.append(_row(0, ["Wärme – Endenergie", fnum(R["heat_endenergy"]), "kWh/a",
                             fnum(R["heat_endenergy"] / A, 1),
                             f"Heizwärme {fnum(R['Q_h'] / A, 1)} + WW {fnum(R['Q_ww_use'] / A, 1)} kWh/(m²a) Nutzenergie"],
                         "total"))
        for cr in R["carriers"]:
            amt = "" if cr["unit"] == "kWh" else f"≈ {fnum(cr['amount'])} {cr['unit']}"
            rows.append(_row(1, [cr["name"], fnum(cr["kwh"]), "kWh/a", fnum(cr["kwh"] / A, 1), amt]))
        for rn in R["renew"]:
            rows.append(_row(1, [rn["name"], fnum(rn["kwh"]), "kWh/a", fnum(rn["kwh"] / A, 1),
                                 "Umgebungsenergie, nicht eingekauft"], "muted"))
        rows.append(_row(0, ["Wasser", fnum(R["water_m3"], 1), "m³/a", fnum(R["water_m3"] * 1000 / A),
                             f"Liter je m²  ·  {fnum(R['l_pd'])} L/(Person·Tag)"], "total"))
        rows.append(_row(1, ["davon erwärmt", fnum(R["hot_m3"], 1), "m³/a", "", f"{fnum(R['T_ww'])} °C-Äquivalent"]))
        rows.append(_row(1, ["Abwasser", fnum(R["abwasser"], 1), "m³/a", "", ""]))
        rows.append(_row(0, ["Kosten (inkl. Grundpreise)", fnum(R["cost_total"]), "€/a", fnum(R["cost_total"] / A, 2),
                             f"CO₂ {fnum(R['co2_total'], 1)} t/a · Primärenergie {fnum(R['pe'] / A)} kWh/(m²a)"],
                         "group"))
        cls = f"Klasse {R['ea_class']} (Energieausweis-Skala)" if R["res"] else \
            (f"Vergleichswert NWG: Wärme {R['bench'][0]}, Strom {R['bench'][1]} kWh/(m²a)" if R["bench"] else "")
        rows.append(_row(0, ["Endenergiekennwert", "", "", fnum(R["ea_value"], 1), cls], "group"))
        for n, v, m, u in _ist_rows(R):
            rows.append(_row(0, [f"Soll-Ist {n}", fnum(m, 1 if u == "m³" else 0), u, "",
                                 f"gemessen; berechnet {fnum(v)} {u} → Abweichung {fnum((v - m) / m * 100, 1)} %"],
                             "muted"))
        T.append(dict(title="Jahresbilanz", cols=cols, rows=rows))
    elif key == "flow":
        cols = [("Energiefluss", 320, "w"), ("kWh/a", 110, "e"), ("Anteil am Bezug", 120, "e")]
        tot = sum(v for a, b, v in R["flows"] if a in R["heat_end"] or a in
                  ("Netzstrom", "PV-Eigenverbrauch", "BHKW-Strom", "Umweltwärme", "Solarthermie"))
        rows = [_row(0, [f"{a}  →  {b}", fnum(v), f"{fnum(v / tot * 100, 1)} %" if tot else ""]) for a, b, v in R["flows"]]
        T.append(dict(title="Energieflüsse", cols=cols, rows=rows))
    elif key == "huelle":
        q_loss = sum(m["Q_l"] for m in R["months"])
        Htot = R["H_T"] + R["H_V"]
        cols = [("Bauteil", 250, "w"), ("Fläche m²", 85, "e"), ("U W/(m²K)", 85, "e"), ("Fx", 55, "e"),
                ("H W/K", 80, "e"), ("Verlust kWh/a", 105, "e"), ("Anteil", 70, "e")]
        rows = []
        for cp in R["comp"]:
            rows.append(_row(0, [cp["name"], fnum(cp["A"]), fnum(cp["U"], 2), fnum(cp["Fx"], 2), fnum(cp["H"]),
                                 fnum(cp["H"] / Htot * q_loss), f"{fnum(cp['H'] / Htot * 100, 1)} %"]))
        rows.append(_row(0, [f"Lüftung / Infiltration (n = {fnum(R['n_eff'], 2)} 1/h)", "", "", "", fnum(R["H_V"]),
                             fnum(R["H_V"] / Htot * q_loss), f"{fnum(R['H_V'] / Htot * 100, 1)} %"]))
        rows.append(_row(0, [f"Summe  (H'T = {fnum(R['ht'], 2)} W/(m²K), A/V = {fnum(R['AV'], 2)})", fnum(R["A_env"]),
                             "", "", fnum(Htot), fnum(q_loss), "100 %"], "total"))
        T.append(dict(title="Bauteile und Wärmeverluste", cols=cols, rows=rows))
        cols = [("kWh", 200, "w")] + [(m, 56, "e") for m in MONTHS] + [("Jahr", 76, "e")]

        def mrow(name, vals, style=None, d=0):
            return _row(0, [name] + [fnum(v, d) for v in vals] + [fnum(sum(vals), d) if d == 0 else ""], style)
        mo = R["months"]
        rows = [_row(0, ["Außentemperatur °C"] + [fnum(m["th_e"], 1) for m in mo] + [""], "muted"),
                mrow("Transmissionsverluste", [m["Q_T"] for m in mo]),
                mrow("Lüftungsverluste", [m["Q_V"] for m in mo]),
                mrow("solare Gewinne", [m["Q_s"] for m in mo]),
                mrow("innere Gewinne", [m["Q_i"] for m in mo]),
                _row(0, ["Ausnutzungsgrad η"] + [fnum(m["eta"], 2) for m in mo] + [""], "muted"),
                mrow("Heizwärmebedarf", [m["Q_h"] for m in mo], "total")]
        T.append(dict(title="Monatsbilanz", cols=cols, rows=rows))
    elif key == "strom":
        cols = [("Kategorie", 250, "w"), ("kWh/a", 100, "e"), ("kWh/(m²a)", 85, "e"), ("Anteil", 65, "e"),
                ("Grundlage / Annahme", 520, "w")]
        tot = R["E_el"] or 1
        groups = {}
        for k, val in R["el"].items():
            groups.setdefault(EL_GROUP[k], []).append((k, val))
        rows = []
        for gname in GROUP_ORDER:
            items = groups.get(gname, [])
            gs = sum(v for _, v in items)
            if gs <= 0 and gname not in ("Wärmepumpe",):
                continue
            rows.append(_row(0, [gname, fnum(gs), fnum(gs / A, 1), f"{fnum(gs / tot * 100, 1)} %", ""], "group"))
            for k, val in items:
                if val > 0:
                    rows.append(_row(1, [k, fnum(val), fnum(val / A, 1), f"{fnum(val / tot * 100, 1)} %",
                                         R["el_basis"][k]]))
                    if k == "Beleuchtung":
                        for z in R["zones"]:
                            rows.append(_row(2, [z["name"], fnum(z["E"]), fnum(z["E"] / A, 2), "",
                                                 f"{fnum(z['area'])} m², {fnum(z['lux'])} lx, "
                                                 f"{fnum(z['P'] / max(z['area'], 1), 1)} W/m², {fnum(z['h'])} h → "
                                                 f"eff. {fnum(z['h_eff'])} h"], "muted"))
                    if k == "Kleingeräte (Liste)":
                        for d in R["dev_rows"]:
                            rows.append(_row(2, [d[0], fnum(d[4]), "", "",
                                                 f"{fnum(d[2])} × {fnum(d[1], 2)} kW × {fnum(d[3])} h"], "muted"))
        rows.append(_row(0, ["Summe Strom", fnum(R["E_el"]), fnum(R["E_el"] / A, 1), "100 %",
                             f"Netzbezug {fnum(R['grid'])} kWh" +
                             (f" · PV {fnum(R['pv_gen'])} kWh (Eigenverbrauch {fnum(R['pv_ev'])} kWh, "
                              f"Einspeisung {fnum(R['feed_in'])} kWh)" if R["pv_gen"] else "")], "total"))
        T.append(dict(title="Stromverbrauch", cols=cols, rows=rows))
    elif key == "waerme":
        cols = [("Wärmeerzeuger", 250, "w"), ("installiert", 95, "e"), ("Wärme kWh/a", 95, "e"), ("Anteil", 55, "e"),
                ("Endenergie", 90, "e"), ("Träger", 75, "w"), ("Volllast h/a", 85, "e"), ("Effizienz", 150, "w")]
        rows = [_row(0, [f"{g['idx']}  {g['typ']}", f"{g['n']} × {fnum(g['kw'], 1)} kW", fnum(g["Q"]),
                         f"{fnum(g['share'])} %", fnum(g["E"]), g["carrier"], fnum(g["vbh"]), g["eff"]])
                for g in R["gens"]]
        rows.append(_row(0, ["Heizlast inkl. WW-Zuschlag", f"{fnum(R['phi_rec'], 1)} kW", fnum(R["Q_total"]), "", "",
                             "", "", f"{fnum(R['phi_hl'] * 1000 / A)} W/m² (Raum)"], "total"))
        T.append(dict(title="Wärmeerzeuger", cols=cols, rows=rows))
        cols = [("Energieträger / Bilanzposten", 300, "w"), ("kWh/a", 105, "e"), ("Menge", 120, "e"),
                ("kWh/(m²a)", 85, "e"), ("Kosten €/a", 95, "e"), ("CO₂ t/a", 75, "e")]
        rows = []
        for cr in R["carriers"]:
            base = cr["name"].split(" (")[0]
            if cr["name"] == "Strom (Wärmepumpe)":
                price = R["p_wp"]
            else:
                price = R["prices"].get(base, R["prices"].get("Strom", 0))
            co2f = CARRIERS.get(base, CARRIERS["Strom"])["co2"]
            amt = f"{fnum(cr['amount'])} {cr['unit']}" if cr["unit"] != "kWh" else "–"
            rows.append(_row(0, [cr["name"], fnum(cr["kwh"]), amt, fnum(cr["kwh"] / A, 1), fnum(cr["kwh"] * price),
                                 fnum(cr["kwh"] * co2f / 1e6, 2)]))
        for rn in R["renew"]:
            rows.append(_row(0, [rn["name"], fnum(rn["kwh"]), "kostenlos", fnum(rn["kwh"] / A, 1), "0", "0"], "muted"))
        rows.append(_row(0, ["Summe Endenergie Wärme", fnum(R["heat_endenergy"]), "", fnum(R["heat_endenergy"] / A, 1),
                             "", ""], "total"))
        for n, v in R["heat_balance"]:
            rows.append(_row(1, [f"Bilanz: {n}", fnum(v), "", fnum(v / A, 1), "", ""], "muted"))
        T.append(dict(title="Energieträger und Wärmebilanz", cols=cols, rows=rows))
    elif key == "wasser":
        cols = [("Verwendung", 230, "w"), ("m³/a", 85, "e"), ("L/(P·d)", 75, "e"), ("Anteil", 65, "e"),
                ("Warmwasser kWh/a", 115, "e"), ("Grundlage", 430, "w")]
        tot = R["water_m3"] or 1
        div = max(R["persons"], 1) * (365 if R["res"] else R["days"])
        rows = [_row(0, [n, fnum(m3, 1), fnum(m3 * 1000 / div, 1), f"{fnum(m3 / tot * 100, 1)} %", fnum(q), b])
                for n, m3, q, b in R["w_rows"]]
        if R["rain_m3"]:
            rows.append(_row(0, ["Regenwasser (WC)", fnum(R["rain_m3"], 1), "", "", "", "nicht im Trinkwasser enthalten"],
                             "muted"))
        rows.append(_row(0, ["Summe Trinkwasser", fnum(R["water_m3"], 1), fnum(R["l_pd"], 1), "100 %", fnum(R["Q_ww_use"]),
                             f"{R['n_wc']} WC · {R['n_sink']} Waschbecken · {R['n_shower']} Duschen · "
                             f"{fnum(R['shower_events'], 1)} Duschgänge/Tag"], "total"))
        T.append(dict(title="Wasserverbrauch", cols=cols, rows=rows))
    elif key == "kosten":
        cols = [("Position", 280, "w"), ("€/a", 95, "e"), ("€/(m²a)", 80, "e"), ("Anteil", 65, "e"),
                ("CO₂ t/a", 75, "e"), ("Primärenergie kWh/a", 135, "e"), ("Hinweis", 260, "w")]
        rows = []
        tot = R["cost_total"] or 1
        for k, v in R["cost"].items():
            if abs(v) < 0.5:
                continue
            co2 = R["co2"].get(k)
            pe = ""
            hint = ""
            if k in R["heat_end"]:
                pe = fnum(R["heat_end"][k] * CARRIERS[k]["fp"])
                hint = f"{fnum(R['heat_end'][k])} kWh × {fnum(R['prices'][k], 3)} €/kWh, fP = {fnum(CARRIERS[k]['fp'], 1)}"
            elif k == "Strom":
                co2 = R["co2"].get("Strom (Netzbezug)")
                pe = fnum(R["grid"] * CARRIERS["Strom"]["fp"])
                hint = f"{fnum(R['grid'] - R['wp_grid'])} kWh × {fnum(R['prices']['Strom'], 3)} €/kWh"
            elif k.startswith("Strom Wärmepumpe"):
                hint = f"{fnum(R['wp_grid'])} kWh × {fnum(R['p_wp'], 3)} €/kWh"
            elif k.startswith("Wasser"):
                hint = f"{fnum(R['water_m3'], 1)} m³"
            elif k.startswith("Einspeisung"):
                hint = f"{fnum(R['feed_in'])} kWh"
            rows.append(_row(0, [k, fnum(v), fnum(v / A, 2), f"{fnum(v / tot * 100, 1)} %",
                                 fnum(co2, 2) if co2 is not None else "", pe, hint]))
        rows.append(_row(0, ["Summe", fnum(R["cost_total"]), fnum(R["cost_total"] / A, 2), "100 %",
                             fnum(R["co2_total"], 2), fnum(R["pe"]),
                             f"{fnum(R['co2_total'] * 1000 / A, 1)} kg CO₂/(m²a)"], "total"))
        if R["co2_levy"] > 1:
            rows.append(_row(1, ["darin CO₂-Abgabe (BEHG)", fnum(R["co2_levy"]), "", "", "", "",
                                 f"{fnum(R['co2_price'])} €/t auf fossile Brennstoffe"], "muted"))
        T.append(dict(title="Kosten, CO₂ und Primärenergie", cols=cols, rows=rows))
    elif key == "monat":
        cols = [("kWh", 200, "w")] + [(m, 56, "e") for m in MONTHS] + [("Jahr", 76, "e")]

        def mrow(name, vals, style=None):
            return _row(0, [name] + [fnum(v) for v in vals] + [fnum(sum(vals))], style)
        rows = [_row(0, ["Außentemperatur °C"] + [fnum(m["th_e"], 1) for m in R["months"]] + [""], "muted"),
                mrow("Heizwärme ab Erzeuger", R["heat_heiz_m"]),
                mrow("Warmwasser" + ("" if R["ww_mode"] == "zentral" else " (dezentral)"), R["ww_m"])]
        for k in GROUP_ORDER:
            vals = R["el_m"].get(k)
            if vals and sum(vals) > 0:
                rows.append(mrow(f"Strom: {k}", vals))
        tot = [sum(v[i] for v in R["el_m"].values()) for i in range(12)]
        rows.append(mrow("Strom gesamt", tot, "total"))
        if R["pv_gen"] > 0:
            rows.append(mrow("PV-Erzeugung", R["pv_m"], "muted"))
        T.append(dict(title="Monatswerte", cols=cols, rows=rows))
    elif key == "massnahmen":
        cols = [("Maßnahme", 240, "w"), ("Δ Endenergie kWh/a", 115, "e"), ("Δ Netzstrom kWh/a", 110, "e"),
                ("Δ Kosten €/a", 90, "e"), ("Δ CO₂ t/a", 70, "e"), ("Invest € *", 95, "e"), ("Amort. J.", 70, "e"),
                ("Beschreibung", 380, "w")]
        rows = []
        for m in M or []:
            rows.append(_row(0, [m["name"], fnum(m["d_end"]), fnum(m["d_el"]), fnum(m["d_cost"]), fnum(m["d_co2"], 1),
                                 fnum(round(m["invest"], -2)), fnum(m["amort"], 1) if m["amort"] else "–", m["desc"]],
                             "total" if m.get("paket") else None))
        T.append(dict(title="Maßnahmen (Einsparung gegenüber Ist-Zustand)", cols=cols, rows=rows,
                      note="* Investitionen: Richtwerte brutto (Preisstand 2025/26) ohne Förderung (BEG/KfW). Bei ohnehin "
                           "fälligen Arbeiten nur die energiebedingten Mehrkosten ansetzen (ca. 30–50 %). Amortisation "
                           "statisch ohne Preissteigerung. Jede Maßnahme einzeln gegenüber dem Ist-Zustand gerechnet."))
    return T


# =============================================================================
#  PDF-Export (matplotlib, keine Zusatzbibliothek nötig)
# =============================================================================

A4L = (11.69, 8.27)


def _pdf_page(title, R, subtitle=None):
    fig = Figure(figsize=A4L, dpi=100, facecolor="white")
    fig.patches.append(Rectangle((0, 0.935), 1, 0.065, transform=fig.transFigure, facecolor=DARK, zorder=0))
    fig.text(0.03, 0.967, title, color="white", fontsize=14, fontweight="bold", va="center")
    pj = R.get("projekt", {})
    right = " · ".join(x for x in (pj.get("projekt_name"), pj.get("projekt_adresse")) if x) or \
        f"{R['profil']}, {fnum(R['A'])} m²"
    fig.text(0.97, 0.967, right, color="#c9d3de", fontsize=9, va="center", ha="right")
    if subtitle:
        fig.text(0.03, 0.912, subtitle, color=MUTED, fontsize=8.5, va="center")
    return fig


def _pdf_footer(fig, n, total, R):
    fig.text(0.03, 0.018, "Gebäude-Energierechner · Grobabschätzung nach DIN V 4108-6 / DIN V 18599 (vereinfacht) – "
             "ersetzt keinen Energieausweis nach GEG", fontsize=6.8, color=MUTED)
    fig.text(0.97, 0.018, f"Seite {n} von {total}", fontsize=7, color=MUTED, ha="right")


def _fit(text, width_in, fs):
    text = str(text)
    maxc = max(3, int(width_in * 72 / (fs * 0.55)))
    return text if len(text) <= maxc else text[:maxc - 1] + "…"


def draw_table(fig, rect, spec, start=0, fs=7.2, row_h_in=0.205, title=True):
    """Zeichnet eine Tabelle in rect (Figurkoordinaten). Gibt den Index der ersten nicht gezeichneten Zeile zurück."""
    x0, y0, w, h = rect
    W_in, H_in = fig.get_size_inches()
    ax = fig.add_axes(rect)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    rh = row_h_in / (h * H_in)
    y = 1.0
    if title:
        ax.text(0, y - rh * 0.5, spec["title"], fontsize=fs + 1.6, fontweight="bold", color=ACCENT, va="center")
        y -= rh * 1.25
    widths = [c[1] for c in spec["cols"]]
    tw = float(sum(widths))
    fr = [wd / tw for wd in widths]
    xs = [sum(fr[:i]) for i in range(len(fr))]
    ax.add_patch(Rectangle((0, y - rh), 1, rh, facecolor="#e6ebf1", edgecolor="none"))
    for (name, _, al), xx, ff in zip(spec["cols"], xs, fr):
        tx = xx + 0.004 if al == "w" else xx + ff - 0.004
        ax.text(tx, y - rh / 2, _fit(name, ff * w * W_in, fs), fontsize=fs, fontweight="bold", color=TXT,
                va="center", ha="left" if al == "w" else "right")
    y -= rh
    i = start
    rows = spec["rows"]
    while i < len(rows) and y - rh >= -1e-9:
        r = rows[i]
        st = r.get("style")
        if st == "total":
            ax.add_patch(Rectangle((0, y - rh), 1, rh, facecolor="#eaf1fb", edgecolor="none"))
        elif (i - start) % 2 == 1:
            ax.add_patch(Rectangle((0, y - rh), 1, rh, facecolor="#f8fafc", edgecolor="none"))
        ax.plot([0, 1], [y - rh, y - rh], color="#edf0f4", lw=0.5)
        bold = st in ("total", "group")
        colr = MUTED if st == "muted" else TXT
        for j, (cell, (name, _, al), xx, ff) in enumerate(zip(r["cells"], spec["cols"], xs, fr)):
            txt = ("    " * r.get("level", 0) + str(cell)) if j == 0 else str(cell)
            tx = xx + 0.004 if al == "w" else xx + ff - 0.004
            ax.text(tx, y - rh / 2, _fit(txt, ff * w * W_in, fs), fontsize=fs, color=colr, va="center",
                    ha="left" if al == "w" else "right", fontweight="bold" if bold else "normal")
        y -= rh
        i += 1
    if i >= len(rows) and spec.get("note") and y - rh >= -1e-9:
        import textwrap
        ax.text(0, y - rh * 0.3, "\n".join(textwrap.wrap(spec["note"], int(w * W_in * 72 / (fs * 0.5)))),
                fontsize=fs - 0.6, color=MUTED, va="top", style="italic")
    return i


def _table_height(spec, n_rows, row_h_in=0.205):
    return (n_rows + 2.3 + (1.6 if spec.get("note") else 0)) * row_h_in


def _kpi_tile(fig, x, y, w, h, title, value, sub, color):
    fig.patches.append(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.004,rounding_size=0.008",
                                      transform=fig.transFigure, facecolor="#f7f9fb", edgecolor=BORDER, lw=0.8))
    fig.patches.append(Rectangle((x, y), 0.006, h, transform=fig.transFigure, facecolor=color, edgecolor="none"))
    fig.text(x + 0.018, y + h - 0.025, title.upper(), fontsize=7.5, color=MUTED, fontweight="bold", va="top")
    fig.text(x + 0.018, y + h * 0.48, value, fontsize=16, color=TXT, fontweight="bold", va="center")
    fig.text(x + 0.018, y + 0.018, sub, fontsize=7.5, color=MUTED, va="bottom")


def kpi_values(R):
    A = R["A"]
    g = {}
    for k, val in R["el"].items():
        g[EL_GROUP[k]] = g.get(EL_GROUP[k], 0) + val
    return [
        ("Strom", f"{fnum(R['E_el'])} kWh", f"{fnum(R['E_el'] / A, 1)} kWh/(m²a)  ·  Licht {fnum(g.get('Beleuchtung', 0) / A, 1)} · "
                                             f"Nutzung {fnum(g.get('Nutzung', 0) / A, 1)} · WP {fnum(g.get('Wärmepumpe', 0) / A, 1)}",
         "#26a69a"),
        ("Wärme · Endenergie", f"{fnum(R['heat_endenergy'])} kWh",
         f"{fnum(R['heat_endenergy'] / A, 1)} kWh/(m²a)  ·  Nutzwärme {fnum((R['Q_h'] + R['Q_ww_use']) / A, 1)}", "#ef6f6c"),
        ("Wasser", f"{fnum(R['water_m3'])} m³", f"{fnum(R['l_pd'])} L/(Person·Tag) · davon warm {fnum(R['hot_m3'])} m³",
         "#3aa0d8"),
        ("Heizlast", f"{fnum(R['phi_rec'], 1)} kW",
         f"{fnum(R['phi_hl'] * 1000 / A)} W/m² (Raum) · installiert {fnum(R['kw_inst'], 1)} kW", "#f4a259"),
        ("Kosten", f"{fnum(R['cost_total'])} €", f"{fnum(R['cost_total'] / A, 2)} €/(m²a) inkl. Wasser & Grundpreise",
         "#7a6ff0"),
        ("CO₂", f"{fnum(R['co2_total'], 1)} t", f"{fnum(R['co2_total'] * 1000 / A, 1)} kg/(m²a) · PE {fnum(R['pe'] / A)} kWh/(m²a)",
         "#6b7785"),
    ]


def export_pdf(path, R, M=None, inputs=None, opts=None):
    """Erstellt einen mehrseitigen PDF-Bericht mit allen Auswertungen und Grafiken."""
    from matplotlib.backends.backend_pdf import PdfPages
    opts = opts or {}
    pages = []
    A = R["A"]

    # ---------- Deckblatt
    if opts.get("deckblatt", True):
        fig = Figure(figsize=A4L, dpi=100, facecolor="white")
        fig.patches.append(Rectangle((0, 0.80), 1, 0.20, transform=fig.transFigure, facecolor=DARK))
        fig.text(0.04, 0.925, "Energiebilanz Gebäude", color="white", fontsize=24, fontweight="bold", va="center")
        fig.text(0.04, 0.862, "Jahresverbrauch Strom · Wärme · Wasser – Grobabschätzung", color="#c9d3de", fontsize=12,
                 va="center")
        pj = R.get("projekt", {})
        info = [pj.get("projekt_name") or f"{R['profil']}", pj.get("projekt_adresse") or "",
                (f"Bearbeitung: {pj['projekt_bearbeiter']} · " if pj.get("projekt_bearbeiter") else "") +
                f"Stand: {datetime.date.today().strftime('%d.%m.%Y')}"]
        fig.text(0.96, 0.93, info[0], color="white", fontsize=11, ha="right", va="center", fontweight="bold")
        fig.text(0.96, 0.89, info[1], color="#c9d3de", fontsize=9.5, ha="right", va="center")
        fig.text(0.96, 0.855, info[2], color="#c9d3de", fontsize=9, ha="right", va="center")
        tiles = kpi_values(R)
        tw, th = 0.295, 0.13
        for i, (t, v, sub, col) in enumerate(tiles):
            cx = 0.04 + (i % 3) * (tw + 0.0175)
            cy = 0.62 - (i // 3) * (th + 0.02)
            _kpi_tile(fig, cx, cy, tw, th, t, v, sub, col)
        gens = "; ".join(f"{g['n']}× {g['typ']} {fnum(g['kw'], 1)} kW ({fnum(g['share'])} %)" for g in R["gens"]) or "–"
        facts = [
            ("Nutzung", f"{R['profil']} · {fnum(A)} m² Nutzfläche · {R['floors']} Geschosse · "
                        f"{fnum(R['persons'])} Personen" + (f" · {R['we']} WE" if R["res"] else "")),
            ("Baualter / Zustand", f"{R['baujahr']} · {R['sanierung']} · {R['std_label']}"),
            ("Gebäudehülle", f"H'T = {fnum(R['ht'], 2)} W/(m²K) · A/V = {fnum(R['AV'], 2)} 1/m · {R['fenster']} · "
                             f"{R['vent']}"),
            ("Wärmeerzeugung", gens),
            ("Warmwasser", f"{R['ww_label']} · Zirkulation: {R['circ']}"),
            ("Eigenerzeugung", (f"PV {fnum(R['pv_kwp'], 1)} kWp, {fnum(R['pv_gen'])} kWh/a" if R["pv_gen"] else "keine PV")
             + (f" · BHKW-Strom {fnum(R['bhkw_el'])} kWh/a" if R["bhkw_el"] else "")),
            ("Klima / Rechenmodus", f"{R['region']} · {R['witterung']} · {R['modus']}"),
            ("Kennwert", f"{fnum(R['ea_value'], 1)} kWh/(m²a) Endenergie" +
             (f" → Effizienzklasse {R['ea_class']}" if R["res"] else "")),
        ]
        y = 0.305
        fig.text(0.04, y + 0.03, "Eckdaten", fontsize=11, fontweight="bold", color=ACCENT)
        for k, v in facts:
            fig.text(0.04, y, k, fontsize=8.5, color=MUTED, va="top")
            fig.text(0.20, y, _fit(v, 7.4, 8.5), fontsize=8.5, color=TXT, va="top")
            y -= 0.028
        if R["warn"]:
            fig.text(0.04, y - 0.01, "Hinweise: " + _fit(" · ".join(R["warn"]), 10.4, 7.5), fontsize=7.5,
                     color="#8a4b00", va="top")
        pages.append(fig)

    # ---------- Eingabeparameter
    if opts.get("eingaben", True) and inputs:
        spec = dict(title="Eingabeparameter (Ø = angenommener Durchschnittswert)",
                    cols=[("Parameter", 230, "w"), ("Wert", 210, "w"), ("Einheit", 60, "w"), ("Quelle", 70, "w")],
                    rows=[])
        last = None
        for sec, lbl, val, unit, src in inputs:
            if sec != last:
                spec["rows"].append(_row(0, [sec, "", "", ""], "group"))
                last = sec
            spec["rows"].append(_row(1, [lbl, val, unit, src], "muted" if src.startswith("Ø") else None))
        i = 0
        while i < len(spec["rows"]):
            fig = _pdf_page("Eingabeparameter", R, "Alle Eingaben der Berechnung – grün/„Ø“ in der Software = "
                                                   "typischer Durchschnittswert bei unbekannter Angabe")
            for col in range(2):
                if i >= len(spec["rows"]):
                    break
                i = draw_table(fig, [0.03 + col * 0.48, 0.05, 0.46, 0.83], spec, i, fs=6.6, row_h_in=0.178,
                               title=(col == 0))
            pages.append(fig)

    # ---------- Auswertungsseiten
    keys = [k for k, _ in TABS if k != "massnahmen" or (opts.get("massnahmen", True) and M)]
    for key in keys:
        tables = build_tables(key, R, M)
        fig = _pdf_page(TAB_TITLES[key], R)
        chart_h = 0.50 if key not in ("flow",) else 0.80
        if key == "monat":
            chart_h = 0.52
        gs = fig.add_gridspec(3, 1, height_ratios=[0.10, chart_h, 0.90 - chart_h], hspace=0, wspace=0,
                              left=0.02, right=0.98, top=1.0, bottom=0.0)
        sub = fig.add_subfigure(gs[1])
        sub.set_facecolor("none")
        CHARTS[key](sub, R, M)
        y_top = 0.88 - chart_h
        avail_in = (y_top - 0.045) * A4L[1]
        for spec in tables:
            j = 0
            while j < len(spec["rows"]):
                n_fit = int(avail_in / 0.205 - 2.3 - (1.6 if spec.get("note") else 0))
                if n_fit < 3:
                    pages.append(fig)
                    fig = _pdf_page(TAB_TITLES[key] + " (Fortsetzung)", R)
                    y_top = 0.89
                    avail_in = (y_top - 0.045) * A4L[1]
                    continue
                n_rows = min(n_fit, len(spec["rows"]) - j)
                h_in = _table_height(spec, n_rows)
                h = h_in / A4L[1]
                j = draw_table(fig, [0.03, y_top - h, 0.94, h], spec, j, title=(j == 0))
                y_top -= h + 0.015
                avail_in = (y_top - 0.045) * A4L[1]
        pages.append(fig)

    # ---------- Methodik
    if opts.get("methodik", True):
        fig = _pdf_page("Methodik und Annahmen", R)
        txt = [
            ("Wärme", "Monatsbilanzverfahren nach DIN V 4108-6 / DIN EN ISO 13790: Transmission über Bauteile mit "
                      "U-Werten und Temperatur-Korrekturfaktoren Fx, Wärmebrückenzuschlag ΔU_WB, Lüftung über "
                      "wirksamen Luftwechsel; solare und innere Gewinne mit Ausnutzungsgrad η (Zeitkonstante τ). "
                      "Klimadaten Referenzklima Potsdam (DIN V 18599-10) mit regionaler Korrektur."),
            ("Realitätsnähe", "Im Modus „realitätsnaher Verbrauch“ wird der Prebound-/Rebound-Effekt "
                              "berücksichtigt (IWU/TABULA, Sunikka-Blank & Galvin): Bestandsgebäude mit hohem "
                              "rechnerischem Bedarf verbrauchen real deutlich weniger (Teilbeheizung, Nutzerverhalten)."),
            ("Heizlast", "vereinfacht nach DIN EN 12831 mit Norm-Außentemperatur der Region; Erzeugeraufteilung über "
                         "eine stündliche, geordnete Jahresdauerlinie (synthetisches Temperaturjahr)."),
            ("Erzeuger", "Wirkungsgrade bzw. Jahresarbeitszahlen nach Vorlauftemperatur und Wärmequelle "
                         "(VDI 4650-1, Feldtests Fraunhofer ISE), Verteil- und Speicherverluste nach DIN V 18599-5/-8."),
            ("Warmwasser", "Zapfmengen je Nutzung (Duschen, Bäder, Händewaschen, Küche, Reinigung), Speicher- und "
                           "Zirkulationsverluste (DIN EN 12897, DVGW W 551)."),
            ("Strom", "Beleuchtung nach DIN V 18599-4 (vereinfacht: Wartungswert × spezifische Leistung × "
                      "Betriebszeit, Tageslicht- und Präsenzfaktoren), Haushaltsstrom nach Stromspiegel, "
                      "Arbeitshilfen nach VDI 3807, Aufzüge nach VDI 4707, Gebäudeautomation nach DIN EN ISO 52120-1."),
            ("Wasser", "Personenbezogene Verbrauchsansätze je Nutzungsprofil, Armaturen und Spülmengen."),
            ("Bewertung", "CO₂-Faktoren und Primärenergiefaktoren nach GEG Anlage 4/9 (Strom: aktueller Strommix), "
                          "Kennwerte NWG nach VDI 3807 / Bekanntmachung Vergleichswerte, Effizienzklassen nach GEG."),
            ("Grenzen", "Ergebnis ist eine Grobabschätzung für Planung, Budget und Plausibilisierung. Es ersetzt "
                        "weder Energieausweis noch Heizlastberechnung oder Fachplanung. Alle Durchschnittswerte "
                        "sind im Programm einsehbar und änderbar."),
        ]
        import textwrap
        y = 0.86
        for k, v in txt:
            fig.text(0.04, y, k, fontsize=9.5, fontweight="bold", color=ACCENT, va="top")
            lines = textwrap.wrap(v, 150)
            fig.text(0.17, y, "\n".join(lines), fontsize=8.5, color=TXT, va="top", linespacing=1.45)
            y -= 0.035 + 0.026 * len(lines)
        pages.append(fig)

    with PdfPages(path) as pdf:
        info = pdf.infodict()
        info["Title"] = "Energiebilanz Gebäude"
        info["Creator"] = "Gebäude-Energierechner"
        for n, fig in enumerate(pages, 1):
            _pdf_footer(fig, n, len(pages), R)
            pdf.savefig(fig)
    return len(pages)


# =============================================================================
#  GUI
# =============================================================================

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:          # pragma: no cover
    tk = ttk = filedialog = messagebox = None

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_TKAGG = HAS_MPL
except Exception:            # pragma: no cover
    HAS_TKAGG = False

UI_FONT = "Segoe UI" if sys.platform.startswith("win") else ("Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")
MONO_FONT = "Consolas" if sys.platform.startswith("win") else ("Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono")


class Tooltip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _=None):
        if self.tip:
            return
        x = self.w.winfo_rootx() + 16
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#fffbe6", fg=TXT, relief="solid", bd=1, wraplength=400,
                 justify="left", font=(UI_FONT, 9), padx=7, pady=5).pack()

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class AvgField:
    """Zahlenfeld mit Ø-Schalter. Ist Ø aktiv, zeigt das Feld den angenommenen Wert (grün)."""

    def __init__(self, parent, on_change, width=10, allow_avg=True, default=None):
        self.allow_avg = allow_avg
        self.on_change = on_change
        self._mute = False
        self.frame = ttk.Frame(parent, style="Card.TFrame")
        self.sv = tk.StringVar(value="" if default is None else fplain(default))
        self.avg = tk.BooleanVar(value=allow_avg and default is None)
        self.entry = ttk.Entry(self.frame, textvariable=self.sv, width=width, justify="right")
        self.entry.pack(side="left")
        if allow_avg:
            ttk.Checkbutton(self.frame, text="Ø", variable=self.avg, command=self._toggle,
                            style="Card.TCheckbutton").pack(side="left", padx=(4, 0))
        self.sv.trace_add("write", self._changed)
        self._apply()

    def _apply(self):
        self.entry.configure(state="disabled" if self.avg.get() else "normal")

    def _toggle(self):
        self._apply()
        if not self.avg.get():
            self.entry.focus_set()
            self.entry.select_range(0, "end")
        self.on_change()

    def _changed(self, *_):
        if self._mute:
            return
        self.entry.configure(style="TEntry")
        self.on_change()

    def get(self):
        if self.allow_avg and self.avg.get():
            return None
        if not self.sv.get().strip():
            return None
        try:
            return parse_num(self.sv.get())
        except ValueError:
            self.entry.configure(style="Bad.TEntry")
            return None

    def is_avg(self):
        return self.allow_avg and self.avg.get()

    def show_default(self, v):
        if self.is_avg():
            self._mute = True
            self.sv.set("" if v is None else fplain(v))
            self._mute = False

    def to_json(self):
        return {"avg": bool(self.avg.get()), "v": self.sv.get()}

    def from_json(self, d):
        self._mute = True
        if isinstance(d, dict):
            self.sv.set(d.get("v", ""))
            self.avg.set(bool(d.get("avg")) and self.allow_avg)
        self._mute = False
        self._apply()


class ChoiceField:
    def __init__(self, parent, options, on_change, allow_avg=True, default=None, width=40):
        self.allow_avg = allow_avg
        self.options = ([AVG_LABEL] if allow_avg else []) + list(options)
        self.frame = ttk.Frame(parent, style="Card.TFrame")
        self.var = tk.StringVar(value=default if default in self.options else self.options[0])
        self.combo = ttk.Combobox(self.frame, values=self.options, textvariable=self.var, state="readonly", width=width)
        self.combo.pack(side="top", anchor="w")
        self.hint = ttk.Label(self.frame, style="Hint.TLabel", text="")
        self.var.trace_add("write", lambda *_: on_change())

    def get(self):
        v = self.var.get()
        return None if v == AVG_LABEL else v

    def is_avg(self):
        return self.get() is None

    def show_default(self, v):
        if self.get() is None and v not in (None, ""):
            self.hint.configure(text="→ angenommen: " + (fplain(v) if isinstance(v, (int, float)) else str(v)))
            if not self.hint.winfo_ismapped():
                self.hint.pack(side="top", anchor="w")
        elif self.hint.winfo_ismapped():
            self.hint.pack_forget()

    def to_json(self):
        return self.var.get()

    def from_json(self, v):
        if v in self.options:
            self.var.set(v)


class TextField:
    allow_avg = False

    def __init__(self, parent, on_change, width=44):
        self.frame = ttk.Frame(parent, style="Card.TFrame")
        self.sv = tk.StringVar()
        ttk.Entry(self.frame, textvariable=self.sv, width=width).pack(side="left")
        self.sv.trace_add("write", lambda *_: on_change())

    def get(self):
        return self.sv.get().strip() or None

    def is_avg(self):
        return False

    def show_default(self, v):
        pass

    def to_json(self):
        return self.sv.get()

    def from_json(self, v):
        self.sv.set(v or "")


class ScrollFrame(ttk.Frame if ttk else object):
    def __init__(self, parent):
        super().__init__(parent, style="Card.TFrame")
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=CARD, bd=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="Card.TFrame", padding=(12, 10))
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.bind("<Enter>", self._wheel_on)
        self.bind("<Leave>", self._wheel_off)

    def _wheel_on(self, _):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", self._wheel)
        self.canvas.bind_all("<Button-5>", self._wheel)

    def _wheel_off(self, _):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(ev)

    def _wheel(self, e):
        if getattr(e, "num", None) == 4:
            d = -1
        elif getattr(e, "num", None) == 5:
            d = 1
        else:
            d = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(d, "units")


class App(tk.Tk if tk else object):
    def __init__(self):
        super().__init__()
        self._building = True
        self.title(APP_TITLE)
        self.geometry("1620x980")
        self.minsize(1200, 740)
        self.configure(bg=BG)
        self.fields = {}
        self.meta = {}
        self.devices = []
        self._job = None
        self._sec = ""
        self.R = None
        self.M = None
        self.dirty = set()
        self.auto = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Bereit")
        self._style()
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_class("TCombobox", ev)
        self._toolbar()
        ttk.Label(self, textvariable=self.status, style="Status.TLabel", anchor="w").pack(side="bottom", fill="x")
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(8, 4))
        left = ttk.Frame(paned, width=650)
        left.pack_propagate(False)
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)
        self._build_inputs(left)
        self._build_results(right)
        self._snapshot = self.get_state()
        self._building = False
        self.after(150, self.recalc)

    # ------------------------------------------------------------------ Stil
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TXT, font=(UI_FONT, 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("Bar.TFrame", background=DARK)
        st.configure("Title.TLabel", background=DARK, foreground="white", font=(UI_FONT, 15, "bold"))
        st.configure("Sub.TLabel", background=DARK, foreground="#aab6c4", font=(UI_FONT, 10))
        st.configure("Bar.TCheckbutton", background=DARK, foreground="white")
        st.map("Bar.TCheckbutton", background=[("active", DARK)], foreground=[("active", "white")])
        st.configure("Card.TLabelframe", background=CARD, bordercolor=BORDER, relief="solid", borderwidth=1)
        st.configure("Card.TLabelframe.Label", background=CARD, foreground=ACCENT, font=(UI_FONT, 10, "bold"))
        st.configure("Card.TLabel", background=CARD, foreground=TXT)
        st.configure("Head.TLabel", background=CARD, foreground=MUTED, font=(UI_FONT, 9, "bold"))
        st.configure("Unit.TLabel", background=CARD, foreground=MUTED, font=(UI_FONT, 9))
        st.configure("Hint.TLabel", background=CARD, foreground=AVG_FG, font=(UI_FONT, 9, "italic"))
        st.configure("Note.TLabel", background=CARD, foreground=MUTED, font=(UI_FONT, 9))
        st.configure("TblTitle.TLabel", background=CARD, foreground=ACCENT, font=(UI_FONT, 9, "bold"))
        st.configure("Warn.TLabel", background="#fff4e5", foreground="#8a4b00", font=(UI_FONT, 9), padding=8)
        st.configure("Status.TLabel", background=DARK, foreground="#d5dde6", font=(UI_FONT, 9), padding=(10, 4))
        st.configure("Card.TCheckbutton", background=CARD)
        st.map("Card.TCheckbutton", background=[("active", CARD)])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(9, 5), font=(UI_FONT, 9))
        st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        st.configure("Treeview", rowheight=21, fieldbackground=CARD, background=CARD, font=(UI_FONT, 9))
        st.configure("Treeview.Heading", font=(UI_FONT, 9, "bold"), background="#e6ebf1")
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=(UI_FONT, 10, "bold"),
                     padding=(14, 5))
        st.map("Accent.TButton", background=[("active", "#1858c4")])
        st.configure("Tool.TMenubutton", padding=(10, 4))
        st.configure("TEntry", fieldbackground="white")
        st.map("TEntry", fieldbackground=[("disabled", "#eef6ee")], foreground=[("disabled", AVG_FG)])
        st.configure("Bad.TEntry", fieldbackground="#fde2e1")

    def _toolbar(self):
        bar = ttk.Frame(self, style="Bar.TFrame", padding=(14, 9))
        bar.pack(fill="x", side="top")
        ttk.Label(bar, text="⚡ Gebäude-Energierechner", style="Title.TLabel").pack(side="left")
        ttk.Label(bar, text="Jahresverbrauch Strom · Wärme · Wasser  (DIN V 4108-6 / DIN V 18599 vereinfacht)",
                  style="Sub.TLabel").pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Berechnen", style="Accent.TButton", command=self.recalc).pack(side="right")
        ttk.Checkbutton(bar, text="automatisch", variable=self.auto, style="Bar.TCheckbutton").pack(side="right", padx=10)
        ttk.Button(bar, text="PDF-Bericht", command=self.export_pdf_dialog).pack(side="right", padx=4)
        mb = ttk.Menubutton(bar, text="Datei ▾", style="Tool.TMenubutton")
        menu = tk.Menu(mb, tearoff=0)
        menu.add_command(label="Parameter speichern …", command=self.save_params)
        menu.add_command(label="Parameter laden …", command=self.load_params)
        menu.add_separator()
        menu.add_command(label="PDF-Bericht mit allen Grafiken …", command=self.export_pdf_dialog)
        menu.add_command(label="Bericht exportieren (.txt) …", command=self.export_report)
        menu.add_command(label="Ergebnisse exportieren (.csv) …", command=self.export_csv)
        menu.add_command(label="Aktuelles Diagramm speichern (.png) …", command=self.save_figure)
        menu.add_separator()
        menu.add_command(label="Alle Eingaben zurücksetzen", command=self.reset)
        mb["menu"] = menu
        mb.pack(side="right", padx=4)

    # ------------------------------------------------------------------ Eingabe-Helfer
    def schedule(self, *_):
        if self._building or not self.auto.get():
            return
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(450, self.recalc)

    def section(self, parent, title, note=None):
        self._sec = title
        lf = ttk.LabelFrame(parent, text=title, style="Card.TLabelframe", padding=(10, 6))
        lf.pack(fill="x", pady=(0, 10))
        lf.columnconfigure(1, weight=1)
        lf._row = 0
        if note:
            ttk.Label(lf, text=note, style="Note.TLabel", wraplength=570, justify="left").grid(
                row=0, column=0, columnspan=8, sticky="w", pady=(0, 4))
            lf._row = 1
        return lf

    def _label(self, sec, r, text, tip):
        lbl = ttk.Label(sec, text=text + (" ⓘ" if tip else ""), style="Card.TLabel")
        lbl.grid(row=r, column=0, sticky="nw", pady=3, padx=(0, 10))
        if tip:
            Tooltip(lbl, tip)

    def num(self, sec, key, label, unit="", avg=True, default=None, width=10, tip=None):
        r = sec._row
        sec._row += 1
        self._label(sec, r, label, tip)
        f = AvgField(sec, self.schedule, width, avg, default)
        f.frame.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(sec, text=unit, style="Unit.TLabel").grid(row=r, column=2, sticky="w", padx=(6, 0))
        self.fields[key] = f
        self.meta[key] = (self._sec, label, unit)
        return f

    def choice(self, sec, key, label, options, avg=True, default=None, width=44, tip=None):
        r = sec._row
        sec._row += 1
        self._label(sec, r, label, tip)
        f = ChoiceField(sec, options, self.schedule, avg, default, width)
        f.frame.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        self.fields[key] = f
        self.meta[key] = (self._sec, label, "")
        return f

    def text(self, sec, key, label):
        r = sec._row
        sec._row += 1
        self._label(sec, r, label, None)
        f = TextField(sec, self.schedule)
        f.frame.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
        self.fields[key] = f
        self.meta[key] = (self._sec, label, "")
        return f

    # ------------------------------------------------------------------ Eingabe-Tabs
    def _build_inputs(self, left):
        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)

        def tab(title):
            sf = ScrollFrame(nb)
            nb.add(sf, text=title)
            return sf.inner

        # --- Gebäude
        t = tab("Gebäude")
        s = self.section(t, "Projekt")
        self.text(s, "projekt_name", "Bezeichnung")
        self.text(s, "projekt_adresse", "Adresse / Standort")
        self.text(s, "projekt_bearbeiter", "Bearbeitung")
        s = self.section(t, "Nutzung & Größe",
                         "Ø = Wert unbekannt → ein typischer Durchschnittswert wird angenommen und grün angezeigt. "
                         "Häkchen entfernen, um einen eigenen Wert einzugeben.")
        self.choice(s, "profil", "Gebäudenutzung", list(PROFILES), avg=False, default="Mehrfamilienhaus",
                    tip="Legt das Nutzungsprofil fest (DIN V 18599-10): Nutzungszeiten, Personendichte, Beleuchtung, "
                        "Wasser- und Gerätebedarf, Vergleichswerte.")
        self.num(s, "flaeche", "Nutzfläche / Wohnfläche (beheizt)", "m²", avg=False, default=1000,
                 tip="Beheizte Netto-Raumfläche bzw. Wohnfläche. Bezugsgröße aller Kennwerte (kWh/m²a).")
        self.num(s, "geschosse", "Anzahl Vollgeschosse", "")
        self.num(s, "raumhoehe", "lichte Raumhöhe", "m", tip="Bestimmt das beheizte Luftvolumen (Lüftungsverluste) "
                                                            "und das Bruttovolumen (Hüllfläche).")
        self.num(s, "raeume", "Anzahl Räume", "", tip="Über die mittlere Raumgröße wird der Raumwirkungsgrad der "
                                                      "Beleuchtung berücksichtigt (kleine Räume → mehr W/m²).")
        self.num(s, "personen", "Anzahl Personen / Nutzer", "",
                 tip="Wohngebäude: Bewohner. NWG: Beschäftigte bzw. Schüler, Betten, Sitzplätze, tägliche Nutzer.")
        self.num(s, "wohneinheiten", "Wohneinheiten (nur Wohngebäude)", "")
        self.choice(s, "region", "Klimaregion", list(REGIONS))
        s = self.section(t, "Nutzungszeiten & Heizbetrieb")
        self.num(s, "nutzungstage", "Nutzungstage pro Jahr", "d/a")
        self.num(s, "nutzungsstunden", "Nutzungsstunden pro Tag", "h/d")
        self.num(s, "raumtemp", "Raumsolltemperatur", "°C", tip="Je Kelvin ändert sich der Heizwärmebedarf um ca. 6 %.")
        self.choice(s, "absenkung", "Absenkbetrieb Heizung", ABSENK_OPTS)
        self.num(s, "beheizt", "beheizter Flächenanteil", "%")
        s = self.section(t, "Berechnungsgrundlagen")
        self.choice(s, "rechenmodus", "Rechenmodus", MODI,
                    tip="Realitätsnaher Verbrauch: Bestandsgebäude mit hohem rechnerischem Bedarf werden real "
                        "sparsamer genutzt (Teilbeheizung, Nutzerverhalten) – Prebound-Effekt nach IWU/TABULA. "
                        "Sehr effiziente Gebäude verbrauchen eher etwas mehr (Rebound). "
                        "Normbedarf: Standard-Randbedingungen wie im Bedarfsausweis.")
        self.choice(s, "witterung", "Witterung des Bezugsjahres", list(WITTERUNG),
                    tip="Für den Vergleich mit gemessenen Verbräuchen eines bestimmten Jahres.")

        # --- Hülle & Lüftung
        t = tab("Hülle")
        s = self.section(t, "Baualter, Sanierung, Standard",
                         "Ist der genaue Standard unbekannt, reicht das Baujahr. Ein bekannter Energiestandard "
                         "(KfW, Passivhaus, Nullemission) bzw. ein bekannter H'T-Wert skaliert die Bauteile.")
        self.choice(s, "baujahr", "Baualtersklasse", list(BAUJAHR))
        self.choice(s, "sanierung", "Sanierungsstand", SAN_OPTS)
        self.choice(s, "standard", "Energiestandard (falls bekannt)", list(STANDARDS))
        self.num(s, "ht_wert", "H'T (falls bekannt)", "W/(m²K)",
                 tip="Spezifischer Transmissionswärmeverlust je m² Hüllfläche inkl. Wärmebrücken. Nur ändern, "
                     "wenn aus Energieausweis/GEG-Nachweis bekannt.")
        s = self.section(t, "Bauteile (U-Werte nach Baualter, IWU-Typologie)",
                         "Ø-Werte ergeben sich aus Baualter und Sanierung. Bekannte U-Werte (z. B. aus "
                         "Sanierungsunterlagen) einfach eintragen.")
        self.choice(s, "dach", "oberer Abschluss", list(DACH_OPTS))
        self.choice(s, "unten", "unterer Abschluss", list(UNTEN_OPTS))
        self.num(s, "u_wand", "U-Wert Außenwand", "W/(m²K)")
        self.num(s, "u_dach", "U-Wert Dach / oberste Decke", "W/(m²K)")
        self.num(s, "u_boden", "U-Wert Boden / Kellerdecke", "W/(m²K)")
        self.choice(s, "fenster", "Verglasung", list(FENSTER))
        self.num(s, "u_fenster", "U-Wert Fenster (U_w)", "W/(m²K)")
        self.num(s, "fensteranteil", "Fensterfläche bezogen auf Nutzfläche", "%")
        s = self.section(t, "Geometrie & solare Gewinne")
        self.choice(s, "gebaeudeform", "Gebäudeform", list(FORMS))
        self.choice(s, "bauweise", "Bauweise (Speichermasse)", list(MASS))
        self.choice(s, "verschattung", "Verschattung", list(VERSCHATTUNG))
        self.choice(s, "ausrichtung", "Hauptausrichtung der Fenster", list(AUSRICHTUNG))
        s = self.section(t, "Lüftung")
        self.choice(s, "lueftung", "Lüftungsart", VENT_OPTS)
        self.num(s, "wrg", "Wärmerückgewinnungsgrad", "%")
        self.num(s, "sfp", "spez. Ventilatorleistung", "W/(m³/h)", tip="Ca. 0,25–0,35 für Wohnungslüftung, "
                                                                     "0,4–0,6 für RLT-Anlagen in NWG.")
        self.num(s, "lueft_h", "Betriebsstunden Lüftungsanlage", "h/a")
        s = self.section(t, "Gebäudeautomation")
        self.choice(s, "gebaeudeautomation", "Automationsklasse (DIN EN ISO 52120-1)", list(BACS),
                    tip="Klasse A–D nach DIN EN ISO 52120-1 (ehem. DIN EN 15232). Faktoren auf Heizwärme/Kälte "
                        "sowie Beleuchtung, Lüftung und Hilfsenergie. C = Referenz.")

        # --- Heizung & WW
        t = tab("Heizung")
        s = self.section(t, "Wärmeverteilung")
        self.choice(s, "heizflaechen", "Heizflächen / Vorlauftemperatur", list(HEIZFL))
        self.choice(s, "verteilung", "Zustand Verteilung", DIST_OPTS)
        self.choice(s, "pumpe", "Heizungspumpe", list(PUMP_OPTS))
        s = self.section(t, "Wärmeerzeuger (Reihenfolge = Priorität, 1 = Grundlast)",
                         "Leistung je Gerät × Anzahl. Ohne Vorgabe wird die Wärme über die stündliche Jahresdauerlinie "
                         "verteilt: Erzeuger 1 deckt die Grundlast, die folgenden die Spitzenlast. Optional: fester "
                         "Deckungsanteil oder bekannte Volllaststunden vorgeben.")
        s.columnconfigure(1, weight=0)
        s.columnconfigure(6, weight=1)
        r0 = s._row
        for j, txt in enumerate(["#", "Erzeuger", "kW je Gerät", "Anzahl", "Anteil %", "Volllast h/a"]):
            ttk.Label(s, text=txt, style="Head.TLabel").grid(row=r0, column=j, sticky="w", padx=(0, 6))
        gen_opts = list(GEN_TYPES)
        for i in range(1, 5):
            r = r0 + i
            ttk.Label(s, text=str(i), style="Card.TLabel").grid(row=r, column=0, sticky="nw", pady=3)
            if i == 1:
                f = ChoiceField(s, gen_opts, self.schedule, True, None, 30)
            else:
                f = ChoiceField(s, [NO_GEN] + gen_opts, self.schedule, False, NO_GEN, 30)
            f.frame.grid(row=r, column=1, sticky="nw", pady=2, padx=(0, 6))
            self.fields[f"gen{i}_typ"] = f
            self.meta[f"gen{i}_typ"] = (self._sec, f"Erzeuger {i}: Typ", "")
            for col, (k, w, lbl, unit) in enumerate([("kw", 6, "Leistung je Gerät", "kW"), ("n", 4, "Anzahl", ""),
                                                     ("anteil", 5, "Deckungsanteil", "%"),
                                                     ("h", 6, "Volllaststunden", "h/a")], start=2):
                af = AvgField(s, self.schedule, w, True, None)
                af.frame.grid(row=r, column=col, sticky="nw", pady=2, padx=(0, 6))
                self.fields[f"gen{i}_{k}"] = af
                self.meta[f"gen{i}_{k}"] = (self._sec, f"Erzeuger {i}: {lbl}", unit)
        s._row = r0 + 5
        s = self.section(t, "Wärmepumpe")
        self.num(s, "jaz_wp", "Jahresarbeitszahl (falls gemessen)", "",
                 tip="Ø = aus Wärmequelle und Vorlauftemperatur (VDI 4650-1 / Feldtests). Bei bekannter JAZ "
                     "(z. B. aus Wärmemengen- und Stromzähler) hier eintragen.")
        s = self.section(t, "Warmwasser")
        self.choice(s, "ww_system", "Warmwasserbereitung", list(WW_OPTS))
        self.num(s, "ww_temp", "Speicher-/Auslauftemperatur", "°C")
        self.num(s, "speicher", "Speichervolumen", "Liter")
        self.choice(s, "zirkulation", "Zirkulation", CIRC_OPTS)
        self.num(s, "solarthermie", "Solarthermie Kollektorfläche", "m²")

        # --- Sanitär & Wasser
        t = tab("Wasser")
        s = self.section(t, "Sanitärausstattung")
        self.num(s, "anz_wc", "Toiletten (WC)", "Stk")
        self.num(s, "anz_waschbecken", "Waschbecken", "Stk")
        self.num(s, "anz_duschen", "Duschen", "Stk")
        self.num(s, "anz_waschraeume", "Waschräume / Bäder", "Stk")
        self.choice(s, "waschbecken_ww", "Warmwasser an Waschbecken", ["ja", "nein (nur Kaltwasser)"])
        s = self.section(t, "Nutzung & Technik")
        self.num(s, "duschgaenge", "Duschgänge je Dusche und Tag", "1/d")
        self.num(s, "duschdauer", "Duschdauer", "min")
        self.num(s, "baeder", "Wannenbäder je Person und Woche", "1/Wo", tip="Ein Wannenbad ≈ 140 L bei 40 °C.")
        self.choice(s, "armaturen", "Armaturen / Duschköpfe", list(ARM_OPTS))
        self.choice(s, "wc_typ", "WC-Spülung", list(WC_OPTS))
        self.choice(s, "regenwasser", "Regenwassernutzung", ["nein", "ja (WC-Spülung)"])
        s = self.section(t, "Küche / Kantine")
        self.choice(s, "kueche", "Küchenart", list(KITCHEN))
        self.num(s, "essen", "Essen pro Tag", "Stk/d")
        s = self.section(t, "Sonstiges")
        self.num(s, "garten", "bewässerte Außenanlagen", "m²")
        self.num(s, "wasser_sonst", "sonstiger Wasserverbrauch", "m³/a")

        # --- Beleuchtung
        t = tab("Licht")
        s = self.section(t, "Beleuchtungstechnik (DIN V 18599-4 vereinfacht)")
        self.choice(s, "leuchtmittel", "Leuchtmittel", list(LEUCHTMITTEL))
        self.choice(s, "lichtsteuerung", "Steuerung", list(STEUERUNG))
        s = self.section(t, "Zonen: wo und wie lange wird beleuchtet?",
                         "Flächenanteile werden auf 100 % normiert. Betriebsstunden vor Abzug von Tageslicht und "
                         "Präsenzerfassung.")
        s.columnconfigure(1, weight=0)
        s.columnconfigure(4, weight=1)
        r0 = s._row
        for j, txt in enumerate(["Zone", "Anteil %", "Lux", "Betrieb h/a"]):
            ttk.Label(s, text=txt, style="Head.TLabel").grid(row=r0, column=j, sticky="w", padx=(0, 6))
        for i, (k, name) in enumerate([("haupt", "Hauptnutzung"), ("verkehr", "Flure / Treppen"),
                                       ("sanitaer", "Sanitär / Waschräume"), ("neben", "Neben-/Technik/Keller")], 1):
            ttk.Label(s, text=name, style="Card.TLabel").grid(row=r0 + i, column=0, sticky="w", pady=2)
            for col, (sub, w, unit) in enumerate([("anteil", 5, "%"), ("lux", 5, "lx"), ("h", 6, "h/a")], start=1):
                af = AvgField(s, self.schedule, w, True, None)
                af.frame.grid(row=r0 + i, column=col, sticky="w", pady=2, padx=(0, 6))
                self.fields[f"lz_{k}_{sub}"] = af
                self.meta[f"lz_{k}_{sub}"] = (self._sec, f"{name}: {sub}", unit)
        s._row = r0 + 5
        s = self.section(t, "Außen- und Sonderbeleuchtung")
        self.num(s, "aussen_w", "Außenbeleuchtung installiert", "W")
        self.num(s, "aussen_h", "Außenbeleuchtung Betrieb", "h/a", tip="Dämmerung bis Dämmerung ≈ 4.100 h/a, "
                                                                       "mit Nachtabschaltung ≈ 2.000 h/a.")
        self.num(s, "garage", "Tiefgarage / Parkhaus", "m²")
        self.choice(s, "garage_licht", "Garagenbeleuchtung", ["Bewegungsmelder", "Dauerlicht"], avg=False)
        self.num(s, "rettungszeichen", "Rettungszeichenleuchten (Dauerlicht)", "Stk")

        # --- Geräte & Technik
        t = tab("Technik")
        s = self.section(t, "Geräte- und Nutzerstrom")
        self.choice(s, "geraete_modus", "Berechnung", GERAETE_MODI, avg=False, default=GERAETE_MODI[0],
                    tip="Pauschale nach Nutzungsprofil (Haushaltsstrom nach Stromspiegel bzw. Arbeitshilfen je "
                        "Person nach VDI 3807). Geräteliste für zusätzliche oder bekannte Verbraucher.")
        self.num(s, "geraete_pauschal", "Pauschale Gerätestrom", "kWh/a")
        self._build_devices(t)
        s = self.section(t, "IT & Sonderverbraucher")
        self.num(s, "it_kw", "Serverraum IT-Last (Dauerbetrieb)", "kW")
        self.num(s, "pue", "PUE (Kühlung/USV Serverraum)", "")
        self.num(s, "sonder_kw", "Sonderverbraucher (Maschinen, Druckluft …)", "kW")
        self.num(s, "sonder_h", "Sonderverbraucher Volllaststunden", "h/a")
        s = self.section(t, "E-Mobilität (Laden am Gebäude)")
        self.num(s, "emob_n", "Anzahl E-Fahrzeuge / Ladepunkte", "Stk")
        self.num(s, "emob_km", "Fahrleistung je Fahrzeug", "km/a")
        self.num(s, "emob_verbrauch", "Verbrauch", "kWh/100 km")
        self.num(s, "emob_anteil", "Anteil am Gebäude geladen", "%")
        s = self.section(t, "Aufzüge & Fahrtreppen (VDI 4707)")
        self.num(s, "aufzuege", "Anzahl Aufzüge", "Stk")
        self.choice(s, "aufzug_kat", "Nutzungskategorie", list(LIFT_CAT))
        self.num(s, "aufzug_last", "Nennlast", "kg")
        self.choice(s, "aufzug_technik", "Antriebstechnik", list(LIFT_TECH))
        self.num(s, "fahrtreppen", "Fahrtreppen", "Stk")
        self.num(s, "fahrtreppen_h", "Betriebsstunden Fahrtreppen", "h/a")
        s = self.section(t, "Kühlung / Klimatisierung")
        self.choice(s, "kuehlung", "Kühlsystem", list(COOL_OPTS))
        self.num(s, "kuehl_anteil", "gekühlter Flächenanteil", "%")
        self.num(s, "eer", "Jahresarbeitszahl Kälte (EER/SEER)", "")
        s = self.section(t, "Photovoltaik")
        self.num(s, "pv_kwp", "PV-Leistung", "kWp")
        self.num(s, "pv_ertrag", "spezifischer Ertrag", "kWh/kWp")
        self.num(s, "speicher_kwh", "Batteriespeicher", "kWh")

        # --- Preise & Ist
        t = tab("Preise & Ist")
        s = self.section(t, "Arbeitspreise (brutto)")
        for k in CARRIERS:
            self.num(s, f"preis_{k.lower()}", k, "€/kWh")
        self.num(s, "preis_wp", "Wärmepumpenstrom (WP-Tarif)", "€/kWh")
        self.num(s, "preis_wasser", "Trink- + Abwasser", "€/m³")
        self.num(s, "preis_einspeisung", "Einspeisevergütung", "€/kWh")
        self.num(s, "grundpreise", "Grundpreise / Zählergebühren gesamt", "€/a")
        s = self.section(t, "Emissionen",
                         "Brennstoffe nach GEG Anlage 9. Strom: Ø = aktueller deutscher Strommix (ca. 380 g/kWh); "
                         "GEG-Wert wäre 560 g/kWh.")
        self.num(s, "co2_strom", "CO₂-Faktor Strom", "g/kWh")
        self.num(s, "co2_preis", "CO₂-Preis (BEHG/ETS2)", "€/t")
        s = self.section(t, "Gemessener Verbrauch (Soll-Ist-Vergleich)",
                         "Optional: Werte aus Abrechnungen eintragen. Die Abweichung wird in der Übersicht gezeigt. "
                         "Tipp: Witterung des Bezugsjahres und Rechenmodus passend wählen.")
        self.num(s, "ist_strom", "Strom (gesamt)", "kWh/a", avg=False)
        self.num(s, "ist_waerme", "Wärme (Endenergie, z. B. Gas in kWh)", "kWh/a", avg=False)
        self.num(s, "ist_wasser", "Wasser", "m³/a", avg=False)

    def _build_devices(self, t):
        s = self.section(t, "Kleingeräte bis 3 kW (Leistung × Anzahl × Volllaststunden)")
        cols = [("name", "Gerät", 200), ("kw", "kW", 60), ("n", "Anzahl", 60), ("h", "h/a", 70), ("kwh", "kWh/a", 80)]
        self.dev_tree = ttk.Treeview(s, columns=[c[0] for c in cols], show="headings", height=5)
        for cid, txt, w in cols:
            self.dev_tree.heading(cid, text=txt)
            self.dev_tree.column(cid, width=w, anchor="w" if cid == "name" else "e", stretch=cid == "name")
        self.dev_tree.grid(row=s._row, column=0, columnspan=3, sticky="ew")
        s._row += 1
        row = ttk.Frame(s, style="Card.TFrame")
        row.grid(row=s._row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        s._row += 1
        self.dev_preset = tk.StringVar(value="Vorlage …")
        cb = ttk.Combobox(row, values=list(DEVICE_PRESETS), textvariable=self.dev_preset, state="readonly", width=24)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self._preset)
        self.dev_name = tk.StringVar()
        self.dev_kw = tk.StringVar()
        self.dev_n = tk.StringVar(value="1")
        self.dev_h = tk.StringVar()
        for lbl, var, w in [("Name", self.dev_name, 14), ("kW", self.dev_kw, 5), ("Anz.", self.dev_n, 4),
                            ("h/a", self.dev_h, 6)]:
            ttk.Label(row, text=lbl, style="Unit.TLabel").pack(side="left", padx=(8, 2))
            ttk.Entry(row, textvariable=var, width=w).pack(side="left")
        row2 = ttk.Frame(s, style="Card.TFrame")
        row2.grid(row=s._row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        s._row += 1
        ttk.Button(row2, text="+ Hinzufügen", command=self._add_device).pack(side="left")
        ttk.Button(row2, text="– Ausgewählte entfernen", command=self._del_device).pack(side="left", padx=6)

    def _preset(self, _=None):
        name = self.dev_preset.get()
        if name in DEVICE_PRESETS:
            kw, h = DEVICE_PRESETS[name]
            self.dev_name.set(name)
            self.dev_kw.set(fplain(kw))
            self.dev_h.set(fplain(h))

    def _add_device(self):
        try:
            d = dict(name=self.dev_name.get().strip() or "Gerät", kw=parse_num(self.dev_kw.get()),
                     n=parse_num(self.dev_n.get()), h=parse_num(self.dev_h.get()))
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Bitte Leistung (kW), Anzahl und Stunden/Jahr als Zahl angeben.")
            return
        if d["kw"] > 3 and not messagebox.askyesno(APP_TITLE, "Das Gerät hat mehr als 3 kW. Trotzdem als Kleingerät "
                                                              "erfassen?\n(Alternativ: Sonderverbraucher)"):
            return
        self.devices.append(d)
        self._refresh_devices()
        self.schedule()

    def _del_device(self):
        sel = [int(i) for i in self.dev_tree.selection()]
        self.devices = [d for j, d in enumerate(self.devices) if j not in sel]
        self._refresh_devices()
        self.schedule()

    def _refresh_devices(self):
        self.dev_tree.delete(*self.dev_tree.get_children())
        for j, d in enumerate(self.devices):
            self.dev_tree.insert("", "end", iid=str(j), values=(d["name"], fnum(d["kw"], 2), fnum(d["n"]),
                                                                  fnum(d["h"]), fnum(d["kw"] * d["n"] * d["h"])))

    # ------------------------------------------------------------------ Ergebnisbereich
    def _build_results(self, parent):
        kpi = ttk.Frame(parent)
        kpi.pack(fill="x", pady=(0, 8))
        self.tiles = []
        colors = ["#26a69a", "#ef6f6c", "#3aa0d8", "#f4a259", "#7a6ff0", "#6b7785"]
        for i, col in enumerate(colors):
            f = tk.Frame(kpi, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            f.grid(row=i // 3, column=i % 3, sticky="nsew", padx=(0 if i % 3 == 0 else 6, 0), pady=(0 if i < 3 else 6, 0))
            kpi.columnconfigure(i % 3, weight=1, uniform="kpi")
            tk.Frame(f, bg=col, width=5).pack(side="left", fill="y")
            body = tk.Frame(f, bg=CARD)
            body.pack(side="left", fill="both", expand=True, padx=10, pady=6)
            t = tk.Label(body, text="", bg=CARD, fg=MUTED, font=(UI_FONT, 8, "bold"))
            t.pack(anchor="w")
            v = tk.Label(body, text="–", bg=CARD, fg=TXT, font=(UI_FONT, 15, "bold"))
            v.pack(anchor="w")
            sub = tk.Label(body, text="", bg=CARD, fg=MUTED, font=(UI_FONT, 8), justify="left", anchor="w")
            sub.pack(anchor="w", fill="x")
            self.tiles.append((t, v, sub))

        self.out_nb = ttk.Notebook(parent)
        self.out_nb.pack(fill="both", expand=True)
        self.figs = {}
        self.tbl_frames = {}
        self.trees = {}
        for key, title in TABS:
            frame = ttk.Frame(self.out_nb, style="Card.TFrame", padding=6)
            self.out_nb.add(frame, text=title)
            pw = ttk.PanedWindow(frame, orient="vertical")
            pw.pack(fill="both", expand=True)
            top = ttk.Frame(pw, style="Card.TFrame")
            bottom = ScrollFrame(pw)
            bottom.inner.configure(padding=(2, 2))
            pw.add(top, weight=3 if key != "flow" else 5)
            pw.add(bottom, weight=2 if key != "flow" else 1)
            if HAS_TKAGG:
                fig = Figure(figsize=(8, 3.6), dpi=100, facecolor=CARD)
                canvas = FigureCanvasTkAgg(fig, master=top)
                canvas.get_tk_widget().pack(fill="both", expand=True)
                self.figs[key] = (fig, canvas)
            else:
                ttk.Label(top, text="Für Diagramme bitte matplotlib installieren:  pip install matplotlib",
                          style="Note.TLabel").pack(pady=30)
            self.tbl_frames[key] = bottom.inner
        self.warn_lbl = ttk.Label(self.tbl_frames["overview"], text="", style="Warn.TLabel", wraplength=900,
                                  justify="left")
        mframe = self.tbl_frames["massnahmen"]
        bar = ttk.Frame(mframe, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text="Maßnahmen neu berechnen", command=self._calc_measures).pack(side="left")
        ttk.Label(bar, text="  Jede Maßnahme wird als eigene Variante komplett durchgerechnet "
                            "(Einsparung gegenüber dem aktuellen Ist-Zustand).", style="Note.TLabel").pack(side="left")

        frame = ttk.Frame(self.out_nb, style="Card.TFrame", padding=8)
        self.out_nb.add(frame, text="Bericht")
        bar = ttk.Frame(frame, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="📄  PDF-Bericht mit allen Grafiken …", style="Accent.TButton",
                   command=self.export_pdf_dialog).pack(side="left")
        ttk.Button(bar, text="Text (.txt)", command=self.export_report).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Tabelle (.csv)", command=self.export_csv).pack(side="left", padx=6)
        ttk.Label(bar, text="PDF: Deckblatt, Eingaben, Übersicht bis Monatsverlauf, Maßnahmen, Methodik",
                  style="Note.TLabel").pack(side="left", padx=8)
        self.report = tk.Text(frame, wrap="none", font=(MONO_FONT, 9), bg="#fbfcfd", fg=TXT, relief="flat",
                              padx=10, pady=8)
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.report.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.report.xview)
        self.report.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        xs.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        self.report.pack(fill="both", expand=True)
        self.out_nb.bind("<<NotebookTabChanged>>", self._tab_changed)

    def _current_key(self):
        idx = self.out_nb.index(self.out_nb.select())
        return TABS[idx][0] if idx < len(TABS) else None

    def _tab_changed(self, _=None):
        key = self._current_key()
        if key and key in self.dirty:
            self.render(key)

    # ------------------------------------------------------------------ Rechnen
    def params(self):
        return {k: f.get() for k, f in self.fields.items()}

    def recalc(self):
        self._job = None
        t0 = time.perf_counter()
        try:
            R = compute(self.params(), self.devices)
        except Exception as ex:  # noqa
            traceback.print_exc()
            self.status.set(f"Fehler in der Berechnung: {ex}")
            return
        self.R = R
        self.M = None
        for k, f in self.fields.items():
            f.show_default(R["used"].get(k))
        try:
            self.update_tiles(R)
            self.dirty = {k for k, _ in TABS}
            key = self._current_key()
            if key:
                self.render(key)
            self.report.configure(state="normal")
            y = self.report.yview()[0]
            self.report.delete("1.0", "end")
            self.report.insert("1.0", R["report"])
            self.report.configure(state="disabled")
            self.report.yview_moveto(y)
        except Exception as ex:  # noqa
            traceback.print_exc()
            self.status.set(f"Fehler in der Darstellung: {ex}")
            return
        n_avg = sum(1 for f in self.fields.values() if f.is_avg())
        self.status.set(f"Berechnet in {(time.perf_counter() - t0) * 1000:.0f} ms  ·  {R['profil']}, "
                        f"{fnum(R['A'])} m²  ·  {n_avg} Eingaben auf Ø (Durchschnittswerte)  ·  "
                        f"{len(R['warn'])} Hinweis(e)")

    def _calc_measures(self):
        if not self.R:
            return
        self.status.set("Maßnahmen werden berechnet …")
        self.update_idletasks()
        try:
            self.M = measures(self.params(), self.devices, self.R)
        except Exception as ex:  # noqa
            traceback.print_exc()
            self.M = []
            self.status.set(f"Fehler bei Maßnahmen: {ex}")
            return
        self.dirty.add("massnahmen")
        if self._current_key() == "massnahmen":
            self.render("massnahmen")
        self.status.set(f"{len(self.M)} Maßnahmen berechnet.")

    # ------------------------------------------------------------------ Darstellung
    def update_tiles(self, R):
        for (t, v, sub), (title, val, txt, _col) in zip(self.tiles, kpi_values(R)):
            t.configure(text=title.upper())
            v.configure(text=val)
            sub.configure(text=txt.replace("  ·  ", "\n", 1))

    def render(self, key):
        R = self.R
        if R is None:
            return
        if key == "massnahmen" and self.M is None:
            self._calc_measures()
            return
        self.dirty.discard(key)
        if HAS_TKAGG:
            fig, canvas = self.figs[key]
            fig.clear()
            CHARTS[key](fig, R, self.M)
            canvas.draw_idle()
        specs = build_tables(key, R, self.M)
        parent = self.tbl_frames[key]
        for i, spec in enumerate(specs):
            tv, lbl, note = self._get_tree(key, i, spec, parent)
            lbl.configure(text=spec["title"])
            note.configure(text=spec.get("note", ""))
            tv.delete(*tv.get_children())
            parents = {-1: ""}
            for r in spec["rows"]:
                lev = r.get("level", 0)
                par = parents.get(lev - 1, "")
                tags = (r["style"],) if r.get("style") else ()
                iid = tv.insert(par, "end", text=r["cells"][0], values=r["cells"][1:], tags=tags, open=True)
                parents[lev] = iid
            tv.configure(height=min(max(len(spec["rows"]), 3), 14))
        if key == "overview":
            if R["warn"]:
                self.warn_lbl.configure(text="⚠  " + "\n⚠  ".join(R["warn"]))
                self.warn_lbl.pack(fill="x", pady=(6, 0))
            else:
                self.warn_lbl.pack_forget()

    def _get_tree(self, key, i, spec, parent):
        k = (key, i)
        if k in self.trees:
            return self.trees[k]
        lbl = ttk.Label(parent, text=spec["title"], style="TblTitle.TLabel")
        lbl.pack(anchor="w", pady=(6 if i else 2, 2))
        frm = ttk.Frame(parent, style="Card.TFrame")
        frm.pack(fill="x")
        cols = spec["cols"]
        tv = ttk.Treeview(frm, columns=[f"c{j}" for j in range(1, len(cols))], show="tree headings", height=6)
        tv.heading("#0", text=cols[0][0], anchor="w")
        tv.column("#0", width=cols[0][1], stretch=False)
        for j, (txt, w, al) in enumerate(cols[1:], 1):
            anc = "e" if al == "e" else "w"
            tv.heading(f"c{j}", text=txt, anchor=anc)
            tv.column(f"c{j}", width=w, anchor=anc, stretch=(j == len(cols) - 1))
        tv.tag_configure("total", font=(UI_FONT, 9, "bold"), background="#eaf1fb")
        tv.tag_configure("group", font=(UI_FONT, 9, "bold"))
        tv.tag_configure("muted", foreground=MUTED)
        tv.pack(side="left", fill="x", expand=True)
        note = ttk.Label(parent, text="", style="Note.TLabel", wraplength=900, justify="left")
        note.pack(anchor="w")
        self.trees[k] = (tv, lbl, note)
        return self.trees[k]

    # ------------------------------------------------------------------ Datei-Funktionen
    def get_state(self):
        return {"programm": APP_TITLE, "version": 2,
                "fields": {k: f.to_json() for k, f in self.fields.items()},
                "devices": [dict(d) for d in self.devices]}

    def set_state(self, st):
        self._building = True
        try:
            for k, v in st.get("fields", {}).items():
                if k in self.fields:
                    self.fields[k].from_json(v)
            self.devices = [d for d in st.get("devices", []) if {"name", "kw", "n", "h"} <= set(d)]
            self._refresh_devices()
        finally:
            self._building = False
        self.recalc()

    def save_params(self):
        fn = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Parameter", "*.json")],
                                          initialfile="gebaeude_parameter.json")
        if fn:
            with open(fn, "w", encoding="utf-8") as fh:
                json.dump(self.get_state(), fh, ensure_ascii=False, indent=2)
            self.status.set(f"Parameter gespeichert: {fn}")

    def load_params(self):
        fn = filedialog.askopenfilename(filetypes=[("Parameter", "*.json"), ("Alle Dateien", "*.*")])
        if fn:
            try:
                with open(fn, encoding="utf-8") as fh:
                    self.set_state(json.load(fh))
            except (OSError, ValueError) as ex:
                messagebox.showerror(APP_TITLE, f"Datei konnte nicht geladen werden:\n{ex}")

    def reset(self):
        if messagebox.askyesno(APP_TITLE, "Alle Eingaben auf Standard (Ø) zurücksetzen?"):
            self.set_state(self._snapshot)

    def input_list(self):
        """(Abschnitt, Parameter, Wert, Einheit, Quelle) für den PDF-Bericht."""
        out = []
        used = self.R["used"] if self.R else {}
        for k, f in self.fields.items():
            sec, lbl, unit = self.meta.get(k, ("", k, ""))
            if isinstance(f, TextField):
                if f.get():
                    out.append((sec, lbl, f.get(), unit, "Eingabe"))
                continue
            if k.startswith("gen") and not k.endswith("_typ"):
                i = k[3]
                t = self.fields[f"gen{i}_typ"].get()
                if i != "1" and (t is None or t == NO_GEN):
                    continue
            if k.startswith("gen") and k.endswith("_typ") and k != "gen1_typ" and f.get() in (None, NO_GEN):
                continue
            if k.startswith("ist_") and f.get() is None:
                continue
            if f.is_avg():
                v = used.get(k)
                if v is None or v == "":
                    continue
                val = fplain(v) if isinstance(v, (int, float)) else str(v)
                out.append((sec, lbl, val, unit, "Ø-Annahme"))
            else:
                v = f.get()
                if v is None:
                    continue
                out.append((sec, lbl, fplain(v) if isinstance(v, (int, float)) else str(v), unit, "Eingabe"))
        for d in self.devices:
            out.append(("Kleingeräte", d["name"], f"{fplain(d['n'])} × {fplain(d['kw'])} kW × {fplain(d['h'])} h",
                        "", "Eingabe"))
        return out

    def export_pdf_dialog(self):
        if not self.R:
            return
        if not HAS_MPL:
            messagebox.showerror(APP_TITLE, "Für den PDF-Export wird matplotlib benötigt (pip install matplotlib).")
            return
        dlg = tk.Toplevel(self)
        dlg.title("PDF-Bericht")
        dlg.configure(bg=CARD)
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, style="Card.TFrame", padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Inhalt des PDF-Berichts", style="TblTitle.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(frm, text="Immer enthalten: Übersicht, Energiefluss, Hülle, Strom, Wärme, Wasser,\n"
                            "Kosten & CO₂ und Monatsverlauf – jeweils mit Grafik und Tabelle.",
                  style="Note.TLabel", justify="left").pack(anchor="w", pady=(0, 8))
        opts = {k: tk.BooleanVar(value=True) for k in ("deckblatt", "eingaben", "massnahmen", "methodik")}
        for k, txt in (("deckblatt", "Deckblatt mit Kennzahlen und Eckdaten"),
                       ("eingaben", "Liste aller Eingabeparameter (Eingabe / Ø-Annahme)"),
                       ("massnahmen", "Maßnahmen und Einsparpotenziale"),
                       ("methodik", "Methodik und Annahmen")):
            ttk.Checkbutton(frm, text=txt, variable=opts[k], style="Card.TCheckbutton").pack(anchor="w", pady=1)
        res = {}

        def ok():
            res.update({k: v.get() for k, v in opts.items()})
            dlg.destroy()
        btns = ttk.Frame(frm, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="PDF erstellen …", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(btns, text="Abbrechen", command=dlg.destroy).pack(side="right", padx=6)
        dlg.grab_set()
        self.wait_window(dlg)
        if not res:
            return
        pj = self.R["projekt"].get("projekt_name") or "energiebericht"
        safe = re.sub(r"[^\w\-]+", "_", pj).strip("_") or "energiebericht"
        fn = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
                                          initialfile=f"{safe}_{datetime.date.today():%Y-%m-%d}.pdf")
        if not fn:
            return
        self.export_pdf(fn, res)

    def export_pdf(self, fn, opts=None):
        opts = opts or {}
        self.status.set("PDF wird erstellt …")
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            if opts.get("massnahmen", True) and self.M is None:
                self.M = measures(self.params(), self.devices, self.R)
            n = export_pdf(fn, self.R, self.M, self.input_list(), opts)
        except Exception as ex:  # noqa
            traceback.print_exc()
            messagebox.showerror(APP_TITLE, f"PDF konnte nicht erstellt werden:\n{ex}")
            self.status.set("PDF-Export fehlgeschlagen")
            return
        finally:
            self.config(cursor="")
        self.status.set(f"PDF gespeichert ({n} Seiten): {fn}")
        if messagebox.askyesno(APP_TITLE, f"PDF-Bericht mit {n} Seiten gespeichert.\n\nJetzt öffnen?"):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(fn)          # noqa
                else:
                    import webbrowser
                    webbrowser.open("file://" + os.path.abspath(fn))
            except Exception:  # noqa
                pass

    def export_report(self):
        if not self.R:
            return
        fn = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")],
                                          initialfile="energiebericht.txt")
        if fn:
            with open(fn, "w", encoding="utf-8") as fh:
                fh.write(self.R["report"])
            self.status.set(f"Bericht gespeichert: {fn}")

    def export_csv(self):
        if not self.R:
            return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")],
                                          initialfile="energiebilanz.csv")
        if not fn:
            return
        rows = [("Auswertung", "Tabelle", "Ebene") + tuple(f"Spalte {i}" for i in range(1, 15))]
        for key, title in TABS:
            if key == "massnahmen" and not self.M:
                continue
            for spec in build_tables(key, self.R, self.M):
                rows.append((title, spec["title"], "Kopf") + tuple(c[0] for c in spec["cols"]))
                for r in spec["rows"]:
                    rows.append((title, spec["title"], str(r.get("level", 0))) + tuple(str(c) for c in r["cells"]))
        with open(fn, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)
        self.status.set(f"CSV gespeichert: {fn}")

    def save_figure(self):
        if not HAS_TKAGG:
            return
        key = self._current_key()
        if not key:
            messagebox.showinfo(APP_TITLE, "Bitte zuerst einen Diagramm-Reiter auswählen.")
            return
        fn = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")],
                                          initialfile=f"diagramm_{key}.png")
        if fn:
            self.figs[key][0].savefig(fn, dpi=160, facecolor=CARD)
            self.status.set(f"Diagramm gespeichert: {fn}")


# =============================================================================
#  Start
# =============================================================================

def main():
    args = sys.argv[1:]
    if "--pdf" in args:                       # python energierechner.py --pdf bericht.pdf [parameter.json]
        i = args.index("--pdf")
        out = args[i + 1] if i + 1 < len(args) else "energiebericht.pdf"
        p, devs = {"profil": "Mehrfamilienhaus", "flaeche": 1000}, []
        if i + 2 < len(args):
            with open(args[i + 2], encoding="utf-8") as fh:
                st = json.load(fh)
            p = {}
            for k, v in st.get("fields", {}).items():
                if isinstance(v, dict):
                    if v.get("avg") or not str(v.get("v", "")).strip():
                        continue
                    try:
                        p[k] = parse_num(v["v"])
                    except ValueError:
                        pass
                elif v and v != AVG_LABEL:
                    p[k] = v
            devs = st.get("devices", [])
        R = compute(p, devs)
        n = export_pdf(out, R, measures(p, devs, R))
        print(f"{out}: {n} Seiten")
        return
    if "--bericht" in args or tk is None:
        if tk is None:
            print("tkinter ist nicht verfügbar – es wird ein Beispielbericht ausgegeben.\n")
        print(compute({"profil": "Mehrfamilienhaus", "flaeche": 1000})["report"])
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
