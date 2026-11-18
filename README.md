# 🛡️ Tapo Room Guard

**100 % Lokale KI-Raumüberwachung, Personen-Tracking, Gesichtserkennung & Sprachbegrüßung über den eingebauten Lautsprecher der TP-Link Tapo C200.**

Keine Cloud-API, keine monatlichen Kosten: Alle Video-Analysen (OpenCV YuNet + SFace ONNX + HOG Ganzkörper-Fallback) und die deutsche Sprachsynthese (**Piper TTS**) laufen vollständig offline im Docker-Container und geben die Begrüßung direkt über den **integrierten Lautsprecher der Tapo C200** (`pcm_alaw` 8000 Hz Backchannel via `go2rtc` auf Port `8800`) aus.

---

## ✨ Hauptfunktionen

### 1. Live-Ansicht, Digital-Zoom & PTZ-Steuerung
- **Flüssiger Live-Stream (~15 FPS):** Ein dauerhafter RTSP-Reader-Thread hält die Verbindung zur Kamera offen und liefert verzögerungsfreie Live-Bilder in voller Auflösung ans Web-Dashboard.
- **Digital-Zoom (`1.0x – 4.0x`):** Stufenloses Rein- und Rauszoomen per Regler, `-` / `+`-Buttons oder **Mausrad** direkt auf dem Videobild – inklusive **Drag-to-Pan** (Bildausschnitt mit gedrückter Maustaste verschieben).
- **Stufenlose ONVIF-PTZ-Steuerung (`Port 2020`):** Über das Steuerkreuz (`ContinuousMove` + `Stop`) schwenkt und neigt die Kamera flüssig, solange die Taste gedrückt gehalten wird.
- **Schnell-Positionen (Presets 1–3):** Gespeicherte Blickwinkel (z. B. *Tür / Eingang*, *Raum-Mitte*, *Schreibtisch*) lassen sich per 1-Klick anfahren oder direkt über das **Speichern-Symbol (💾)** fest in der Tapo C200 hinterlegen.

### 2. KI-Auto-Tracking & Automatische Rückkehr
- **Personenverfolgung beim Laufen:** Erkennt die KI eine Person außerhalb der Bildmitte, schwenkt die Tapo C200 automatisch hinterher:
  - **Primär:** OpenCV **YuNet** (präzise Gesichtszentrierung).
  - **Fallback:** OpenCV **HOG Ganzkörper-Erkennung**, damit die Kamera auch dann weiter folgt, wenn man seitlich läuft oder ihr den Rücken zukehrt.
- **Tracking-Tempo-Skala (`0.3x – 2.5x`):** Über den Schieberegler (`-` / `+`) unter den Presets lässt sich die Reaktionsgeschwindigkeit der Motor-Nachführung live mit der Maus anpassen.
- **Automatische Rückkehr zu Preset 1:** Sobald 20 Sekunden lang keine Person mehr im Bild ist, fährt die Kamera automatisch auf ihre Ausgangsposition (**Preset 1**) zurück.

### 3. Lokale Gesichtserkennung & Sprachausgabe (Piper TTS)
- **100 % Offline-Gesichtserkennung:** OpenCV **YuNet** (Detektion) + **SFace** (128-D Cosine-Similarity Embeddings). Referenzfotos lassen sich hochladen oder per **1-Klick Kamera-Snap** direkt aufnehmen.
- **100 % Offline deutsche Sprachausgabe:** Vorinstalliertes natives C++-Binary von **Piper TTS** (`de_DE-thorsten-medium` & `de_DE-kerstin-low`) mit Platzhaltern `{name}`, `{greeting_time}` (*Guten Morgen / Guten Tag / Guten Abend*) und `{time}`.
- **Tapo C200 V5 / Firmware 1.5+ Lautsprecher-Ausgabe:** `go2rtc` nutzt automatisch den erforderlichen **Großbuchstaben-SHA256-Hash** des TP-Link Cloud-Passworts für den 2-Wege-Audio-Backchannel (`tapo://admin:<SHA256>@<ip>`).
- **Cooldown & Aktivzeiten:** Verhindert wiederholte Begrüßungen pro Person (z. B. 120 Minuten Cooldown) und respektiert konfigurierbare Uhrzeiten (z. B. `06:00–23:00`).

### 4. Neo Deck Aurora & Nordic Glassmorphism Web-UI
- Dark- und Light-Mode, Live-Durchsage-Tester (`Sprechen`), Personen-Karten, Schnellschalter für `Tracking: AN/AUS` und `Privacy` sowie ein Erkennungs-Log mit Kamera-Snapshots.

---

## 🚀 Schnellstart (Docker Compose / TrueNAS SCALE)

```yaml
services:
  tapo-room-guard:
    image: daddelgreis74/tapo-room-guard:latest
    container_name: tapo-room-guard
    restart: unless-stopped
    ports:
      - "8095:8095"
    environment:
      - TZ=Europe/Berlin
      - PORT=8095
      - TAPO_HOST=192.168.178.101
      - TAPO_RTSP_USER=dein_kamera_user
      - TAPO_RTSP_PASS=dein_kamera_passwort
      - TAPO_CLOUD_PASS=dein_tplink_cloud_passwort
    volumes:
      - ./data:/app/data
```

Nach dem Start ist das Web-Dashboard erreichbar unter:
👉 **`http://<server-ip>:8095`**

### TrueNAS SCALE (`ix-app` Custom App)
In TrueNAS SCALE lässt sich der Container sauber über **Apps → Discover Apps → Custom App** einrichten:
- **Repository:** `daddelgreis74/tapo-room-guard` · **Tag:** `latest`
- **Port:** Host `8095` → Container `8095` (`TCP`, `published`)
- **Storage:** `ixVolume` (`data`) gemountet auf `/app/data`
- **Portal:** HTTP auf Node-IP, Port `8095`, Pfad `/` (für den direkten *Web UI*-Button)

---

## 📷 Vorbereitung in der TP-Link Tapo App

1. Öffne die **Tapo App** auf dem Smartphone und wähle deine **Tapo C200**.
2. Gehe auf **Kamera-Einstellungen ⚙️ → Erweiterte Einstellungen → Kamera-Konto** und lege einen lokalen Benutzernamen + Passwort für **RTSP (Port `554`) & ONVIF (Port `2020`)** an.
3. Aktiviere unter **Ich → Tapo Lab** die Option **Kompatibilität mit Drittanbietern** (falls vorhanden).
4. Trage im **Tapo Room Guard Web-UI** unter **Kamera & KI-Setup** die IP-Adresse der C200, das Kamera-Konto sowie für die Lautsprecher-Ausgabe (Port `8800`) dein **TP-Link Cloud-Passwort** ein.
