# 🚦 BÜ Steingleis Monitor

Ein intelligentes Streamlit-Dashboard zur Echtzeit-Überwachung und Prognose von Schrankenschließzeiten am Bahnübergang „Steingleis“ in Neustadt an der Weinstraße (KBS 670 / Pfälzerwald-Korridor).

## 📌 Über das Projekt

Der **BÜ Steingleis Monitor** greift auf Live-Fahrplandaten der Deutschen Bahn (via [DBF / finalrewind.org](https://dbf.finalrewind.org/)) zu und berechnet anhand eines einstellbaren Stellwerksmodells (Vor- und Nachlaufzeiten) auf die Sekunde genau, wann und wie lange die Schranken am Bahnübergang geschlossen bleiben.

### ✨ Neue Features in v0.2.0

* **🚉 Authentische DB-Bahnhofstafel:** Ein komplett neues Dashboard im Design der digitalen Fahrgastinformationsanlagen (FIA) der DB. Inklusive Filter für Züge, die über den BÜ Steingleis fahren, Gleisanzeigen und gelben Störungsmeldungen.

* **🛡️ API-Schutz & Entkopplung:** UI-Rendering (alle 5s lokal) und Netzwerkabrufe (alle 60-120s aus dem Cache) sind nun vollständig getrennt, um die API-Server massiv zu entlasten.

* **🔔 Entprellter Audio-Alarm:** Der akustische Schrankengong ertönt nun zuverlässig exakt **einmal** pro Schließphase, selbst wenn sich Züge kurzfristig verspäten.

* **🕒 Native Live-Uhr:** Eine flüssige, systemnative Sekunden-Uhr in der Sidebar (ohne Python-Reruns).

## 🚀 Funktionen

1. **🔴 Live-Echtzeit & 📅 Plan-Analyse**

   * Verfolge den Bahnübergang live inklusive aller aktuellen Verspätungen.

   * Modus *„Fahrplan-Analyse“*: Wähle ein beliebiges Datum/Uhrzeit, um den reinen Soll-Fahrplan (ohne Live-Abweichungen) zu simulieren.

2. **🚄 Fernverkehrs-Erfassung**

   * Durchfahrende ICE/TGV-Züge halten nicht in Neustadt. Die App überwacht automatisch die Nachbarknoten **Mannheim Hbf** und **Kaiserslautern Hbf** und errechnet die Durchfahrtszeiten am Steingleis.

3. **⚙️ Konfigurierbares Stellwerksmodell**

   * Alle Vor- und Nachlaufzeiten (Lead/Lag) für Abfahrten und Ankünfte lassen sich per Schieberegler justieren.

   * Einstellbare „Mindestöffnungslücke“: Bleibt die Schranke bei dichten Zugfolgen unten?

4. **📊 60-Minuten-Zeitstrahl**

   * Visuelle Altair-Grafik zur schnellen Erkennung von „Freie Fahrt“-Fenstern in der nächsten Stunde.

## ⚠️ Wichtige Einschränkungen (Disclaimer)

* **Keine Güterzüge:** Güterzüge und unplanmäßige Sonderfahrten sind in den öffentlichen Schnittstellen der Bahn **nicht erfasst**. Die Schranke kann und wird sich auch für Züge schließen, die nicht auf dem Monitor stehen.

* **Ohne Gewähr:** Dieses Tool ist ein Hobby-Projekt zur Prognose. Vertrauen Sie vor Ort immer den physischen Signalanlagen (Rotlicht, Halbschranken, Akustik).

## 🛠️ Installation & Start

1. **Repository klonen:**

   ```
   git clone https://github.com/heis2201/Steingleis.git
   cd Steingleis
   
   ```

2. **Abhängigkeiten installieren:**
   Stellen Sie sicher, dass Python installiert ist.

   ```
   pip install -r requirements.txt
   
   ```

   *(Benötigt u.a. `streamlit`, `pandas`, `altair`, `requests`)*

3. **App starten:**

   ```
   streamlit run app.py
   
   ```

## 📁 Dateistruktur

* `app.py`: Die Hauptanwendung (Streamlit Monitor & Bahnhofstafel).

* `train-training.py`: Ein kleines Helfer-Skript zur lokalen Aufzeichnung echter Schließzeiten am Bahnübergang zur Kalibrierung der Parameter.

* `config.json`: Wird automatisch von der App generiert. Hier werden Ihre Einstellungen (Vorwarnzeiten, Caching-Intervalle) atomar und typsicher gespeichert.

## 📜 Lizenz

Dieses Projekt steht unter der **MIT-Lizenz**. Details finden Sie in der `LICENSE`-Datei.

Ein großer Dank geht an [finalrewind.org](https://finalrewind.org/) für die Bereitstellung der großartigen DBF-API!
