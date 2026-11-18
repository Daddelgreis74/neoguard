# 🛡️ Tapo Room Guard

**100 % Lokale KI-Raumüberwachung, Gesichtserkennung & Sprachbegrüßung über den eingebauten Lautsprecher der TP-Link Tapo C200.**

Keine Cloud-API, keine monatlichen Kosten: Alle Video-Analysen (OpenCV YuNet + SFace ONNX) und die deutsche Sprachsynthese (**Piper TTS**) laufen vollständig offline im Docker-Container und geben die Begrüßung direkt über den **integrierten Lautsprecher der Tapo C200** (`pcm_alaw` 8000 Hz Backchannel via `go2rtc` + `ffmpeg`) aus.

---

## ✨ Hauptfunktionen

- **100 % Lokale Gesichtserkennung (Offline Vision):**
  - Nutzt OpenCV **YuNet** (Gesichtsdetektion) + **SFace** (128-D Cosine-Similarity Embeddings) als vorinstallierte ONNX-Modelle.
  - Mehrere Referenzfotos pro Person (per Datei-Upload oder direkt per 1-Klick **Kamera-Snapshot** von der Tapo C200).
- **100 % Lokale deutsche Sprachausgabe (Piper TTS):**
  - Vorinstalliertes deutsches Sprachmodell (`de_DE-thorsten-medium`) erzeugt natürlich klingende Begrüßungen in unter 200 ms auf der CPU.
  - Dynamische Platzhalter: `{name}`, `{greeting_time}` (*Guten Morgen / Guten Tag / Guten Abend*), `{time}`.
- **Direkte Ausgabe über den Tapo C200 Lautsprecher:**
  - Integrierte `go2rtc`- und `ffmpeg`-Bridge streamt das erzeugte Sprach-Audio über das Tapo-Two-Way-Audio-Protokoll direkt auf den Lautsprecher der Kamera.
- **PTZ- & Privatsphäre-Steuerung (`pytapo`):**
  - Schwenken & Neigen (360° horizontal / 114° vertikal), Schnell-Positionen (Presets 1–3) und physischer **Privacy-Mode** (optional automatisch nach erfolgter Begrüßung).
- **Neo Deck Aurora & Nordic Glassmorphism Web-UI:**
  - Modernes Dashboard mit Dark/Light-Mode, Live-MJPEG-Stream, Durchsage-Tester, Personen-Verwaltung und Event-Historie inklusive Kamera-Snapshots.

---

## 🚀 Schnellstart (Docker Compose)

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
      - TAPO_HOST=192.168.178.50
      - TAPO_RTSP_USER=dein_kamera_user
      - TAPO_RTSP_PASS=dein_kamera_passwort
      - TAPO_CLOUD_PASS=dein_tplink_cloud_passwort
    volumes:
      - ./data:/app/data
```

Nach dem Start ist das Web-Dashboard erreichbar unter:
👉 **`http://<server-ip>:8095`**

---

## 📷 Vorbereitung in der TP-Link Tapo App

1. Öffne die **Tapo App** auf dem Smartphone und wähle deine **Tapo C200**.
2. Gehe auf **Kamera-Einstellungen ⚙️ → Erweiterte Einstellungen → Kamera-Konto**.
3. Lege dort einen lokalen Benutzernamen und ein Passwort für RTSP/ONVIF an.
4. Trage im **Tapo Room Guard Web-UI** unter **Kamera & KI-Setup** die IP-Adresse der C200, das Kamera-Konto sowie (für den 2-Wege-Lautsprecher & PTZ) dein TP-Link Cloud-Passwort ein.
