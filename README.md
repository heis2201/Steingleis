# 🚦 BÜ Steingleis Monitor

Eine in Python und [Streamlit](https://streamlit.io/) geschriebene Web-Applikation zur Echtzeit-Überwachung und Prognose von Schrankenschließzeiten am Bahnübergang "Steingleis" in Neustadt an der Weinstraße (KBS 670).

Das Tool ruft über die [DBF-API](https://dbf.finalrewind.org/) Live-Fahrplandaten ab, berechnet anhand konfigurierbarer Vor- und Nachlaufzeiten die Schließphasen des Bahnübergangs und visualisiert diese in einem interaktiven Dashboard inklusive 60-Minuten-Zeitstrahl.

## 📑 Inhaltsverzeichnis
- [✨ Features](#-features)
- [🚀 Installation & Betrieb](#-installation--betrieb)
  - [1. Lokal (Windows / macOS / Linux)](#1-lokal-windows--macos--linux)
  - [2. Docker / Raspberry Pi](#2-docker--raspberry-pi)
  - [3. Streamlit Community Cloud](#3-streamlit-community-cloud)
- [⚙️ Konfiguration (`config.json`)](#️-konfiguration-configjson)
- [🛠️ Verwendete Technologien](#️-verwendete-technologien)
- [📄 Lizenz & Haftungsausschluss](#-lizenz--haftungsausschluss)

## ✨ Features

* **Echtzeit-Dashboard & Fahrplan-Modus:** Anzeige des aktuellen BÜ-Status (Offen / Geschlossen) oder Analyse eines beliebigen Zeitfensters in der Zukunft/Vergangenheit.
* **Intelligente Fernverkehrs-Erkennung:** Da durchfahrende ICE- und TGV-Züge nicht im Neustadter Fahrplan auftauchen, überwacht die App parallel die Abfahrten in **Mannheim Hbf** und **Kaiserslautern Hbf**. Durch Laufzeit-Interpolation (z. B. +16 Min ab Mannheim) werden Schließungen für Durchfahrten prognostiziert.
* **Visueller Zeitstrahl:** Eine grafische Übersicht der nächsten 60 Minuten mit 5-Minuten-Taktraster.
* **Akustische Vorwarnung:** Ein im Browser autark laufender Schrankengong warnt konfigurierbare X Sekunden vor einer bevorstehenden Schließung.
* **Dynamische Stellwerks-Parameter:** Alle Vorlauf- und Nachlaufzeiten für Ankünfte, Abfahrten und Durchfahrten können live über das Einstellungs-Menü angepasst und in der `config.json` dauerhaft gesichert werden.
* 🚉 **Authentische DB-Bahnhofstafel**: Ein komplett neues Dashboard im Design der digitalen Fahrgastinformationsanlagen (FIA) der DB. Inklusive Filter für Züge, die über den BÜ Steingleis fahren, Gleisanzeigen und gelben Störungsmeldungen.

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
```
Die App ist danach unter `http://localhost:8501` im Browser erreichbar.

### 2. Docker / Raspberry Pi
Ideal für den Betrieb auf einem lokalen Homeserver oder Raspberry Pi.
```bash

# Klonen und in das Verzeichnis wechseln
git clone https://github.com/heis2201/Steingleis.git
cd Steingleis

# Container bauen und im Hintergrund starten
docker compose up -d --build
```
Die App läuft dauerhaft und ist im gesamten Heimnetzwerk unter `http://<SERVER-IP>:8501` erreichbar.

### 3. Streamlit Community Cloud
Die App ist "Cloud-Ready". 
1. Loggen Sie sich auf [share.streamlit.io](https://share.streamlit.io/) mit Ihrem GitHub-Account ein.
2. Klicken Sie auf **"Create App"**.
3. Wählen Sie Ihr Repository (`heis2201/Steingleis`) und die Datei `app.py` aus.
4. Klicken Sie auf Deploy. 

---

## ⚙️ Konfiguration (`config.json`)

Die App speichert ihre Grundeinstellungen in der Datei `config.json`. Diese wird automatisch erstellt/aktualisiert, wenn Sie in der Web-Oberfläche auf der Seite **"⚙️ Einstellungen"** auf "Speichern" klicken.

Wichtige Parameter:

* `lead_dep` / `lag_dep`: Vorlaufzeit (Sekunden) vor der Planabfahrt am Hbf / Nachlaufzeit nach Abfahrt.
* `lead_arr` / `lag_arr`: Schließzeit vor Ankunft am Hbf / Öffnungszeit vor Halt am Bahnsteig.
* `track_through_trains`: Aktiviert die Nachbarbahnhof-Erkennung für Fernzüge.
* `transit_ma_min` / `transit_kl_min`: Fahrzeit der Fernzüge von Mannheim bzw. Kaiserslautern bis zum BÜ Steingleis (in Minuten).
* `sound_warn_sec`: Wie viele Sekunden vor einer Schließung der Browser-Gong ertönen soll.
---

## 🛠️ Verwendete Technologien

* **Python 3.11+**
* **Streamlit** (Web-Framework)
* **Pandas** (Datenverarbeitung der Intervalle)
* **Altair** (Visualisierung des Zeitstrahls)
* **Web Audio API** (Browser-nativer Sound-Generator, keine `.mp3` Dateien nötig)

## 📄 Lizenz & Haftungsausschluss

Dieses Projekt steht unter der **[MIT Lizenz](LICENSE)**.

Dieses Projekt ist für private und analytische Zwecke konzipiert. Es handelt sich **nicht** um eine offizielle oder sicherheitsrelevante Anwendung der Deutschen Bahn. 
Die Schließzeiten basieren ausschließlich auf mathematischen Prognosen des Personenverkehrs – Güterzüge und Sonderfahrten lösen unangekündigte Schließungen aus.

**Betreten Sie niemals geschlossene Bahnanlagen!**
