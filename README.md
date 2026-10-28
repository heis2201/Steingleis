# 🚦 BÜ Steingleis Monitor (v0.1)

Eine in Python und [Streamlit](https://streamlit.io/) geschriebene Web-Applikation zur Echtzeit-Überwachung und Prognose von Schrankenschließzeiten am Bahnübergang "Steingleis" in Neustadt an der Weinstraße (KBS 670).

Das Tool ruft über die [DBF-API](https://dbf.finalrewind.org/) Live-Fahrplandaten ab, berechnet anhand konfigurierbarer Vor- und Nachlaufzeiten die Schließphasen des Bahnübergangs und visualisiert diese in einem interaktiven Dashboard inklusive 60-Minuten-Zeitstrahl.

## ✨ Features

* **Echtzeit-Dashboard & Fahrplan-Modus:** Anzeige des aktuellen BÜ-Status (Offen / Geschlossen) oder Analyse eines beliebigen Zeitfensters in der Zukunft/Vergangenheit.
* **Intelligente Fernverkehrs-Erkennung:** Da durchfahrende ICE- und TGV-Züge nicht im Neustadter Fahrplan auftauchen, überwacht die App parallel die Abfahrten in **Mannheim Hbf** und **Kaiserslautern Hbf**. Durch Laufzeit-Interpolation (z. B. +16 Min ab Mannheim) werden Schließungen für Durchfahrten exakt prognostiziert.
* **Visueller Zeitstrahl (Gantt-Diagramm):** Eine grafische Übersicht der nächsten 60 Minuten mit 5-Minuten-Taktraster (erstellt mit Altair).
* **Akustische Vorwarnung:** Ein im Browser autark laufender Schrankengong (Web Audio API) warnt konfigurierbare X Sekunden vor einer bevorstehenden Schließung.
* **Dynamische Stellwerks-Parameter:** Alle Vorlauf- und Nachlaufzeiten für Ankünfte, Abfahrten und Durchfahrten können live über das Einstellungs-Menü angepasst und in der `config.json` dauerhaft gesichert werden.
* **Zeitzonen-sicher:** Automatischer Fallback auf deutsche Ortszeit (`Europe/Berlin`), wodurch die App problemlos auf internationalen Cloud-Servern (z. B. Streamlit Community Cloud) betrieben werden kann.

---

## 🚀 Installation & Betrieb

Die Anwendung kann auf drei verschiedene Arten betrieben werden:

### 1. Lokal (Windows / macOS / Linux)

Voraussetzung: Python 3.9 oder höher.

```bash
# Repository klonen
git clone https://github.com/heis2201/Steingleis.git
cd Steingleis

# Abhängigkeiten installieren
pip install -r requirements.txt

# App starten
python -m streamlit run app.py
