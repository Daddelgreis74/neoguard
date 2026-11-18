import json
import logging
import math
import shutil
import struct
import subprocess
import threading
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("tapo_room_guard.tts")

PIPER_VOICES: Dict[str, Dict[str, str]] = {
    "de_DE-thorsten-medium": {
        "onnx": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"
        ),
        "json": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json"
        ),
    },
    "de_DE-kerstin-low": {
        "onnx": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "de/de_DE/kerstin/low/de_DE-kerstin-low.onnx"
        ),
        "json": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "de/de_DE/kerstin/low/de_DE-kerstin-low.onnx.json"
        ),
    },
}


class LocalTTSService:
    """100% Local Offline Text-to-Speech (Piper TTS) + Tapo C200 Backchannel Speaker Output."""

    def __init__(self, models_dir: Path) -> None:
        self.tts_dir = models_dir / "tts"
        self.tts_dir.mkdir(parents=True, exist_ok=True)
        self.last_wav_path = self.tts_dir / "last_greeting.wav"
        self._speak_lock = threading.Lock()

    def ensure_voice_model(self, voice_name: str = "de_DE-thorsten-medium") -> Tuple[Path, Path]:
        if voice_name not in PIPER_VOICES:
            voice_name = "de_DE-thorsten-medium"
        onnx_path = self.tts_dir / f"{voice_name}.onnx"
        json_path = self.tts_dir / f"{voice_name}.onnx.json"

        if not onnx_path.exists() or not json_path.exists():
            urls = PIPER_VOICES[voice_name]
            try:
                if not json_path.exists():
                    logger.info("Downloading Piper voice config %s...", json_path.name)
                    urllib.request.urlretrieve(urls["json"], str(json_path))
                if not onnx_path.exists():
                    logger.info("Downloading Piper voice model %s (~60 MB)...", onnx_path.name)
                    urllib.request.urlretrieve(urls["onnx"], str(onnx_path))
            except Exception as exc:
                logger.warning("Failed to download Piper voice model (%s): %s", voice_name, exc)

        # Sanitize multi-codepoint keys (e.g. "aɪ") in phoneme_id_map for standalone C++ piper binary
        if json_path.exists():
            try:
                cfg_data = json.loads(json_path.read_text(encoding="utf-8"))
                pmap = cfg_data.get("phoneme_id_map", {})
                if any(len(k) > 1 for k in pmap):
                    cfg_data["phoneme_id_map"] = {k: v for k, v in pmap.items() if len(k) <= 1}
                    json_path.write_text(
                        json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            except Exception:
                pass

        return onnx_path, json_path

    def synthesize_to_wav(
        self,
        text: str,
        voice_name: str = "de_DE-thorsten-medium",
        output_path: Optional[Path] = None,
        volume_percent: int = 100,
    ) -> Path:
        """
        Synthesize German text into a WAV file completely offline using Piper TTS.
        Applies optional software gain (10% - 200%) with soft clipping protection.
        """
        target_path = output_path or self.last_wav_path
        clean_text = text.strip() or "Hallo!"
        onnx_path, json_path = self.ensure_voice_model(voice_name)

        synthesized = False
        piper_bin = shutil.which("piper") or (
            "/usr/local/bin/piper" if Path("/usr/local/bin/piper").exists() else None
        )
        if piper_bin and onnx_path.exists() and json_path.exists():
            try:
                subprocess.run(
                    [
                        piper_bin,
                        "--model",
                        str(onnx_path),
                        "--config",
                        str(json_path),
                        "--output_file",
                        str(target_path),
                    ],
                    input=clean_text.encode("utf-8"),
                    check=True,
                    timeout=15,
                )
                synthesized = True
            except Exception as cli_exc:
                logger.warning("Piper CLI failed: %s", cli_exc)

        if not synthesized:
            self._write_chime_wav(target_path, duration_sec=1.2)

        if int(volume_percent) != 100:
            self._apply_wav_volume(target_path, volume_percent)
        return target_path

    @staticmethod
    def _apply_wav_volume(wav_path: Path, volume_percent: int) -> None:
        """Scale 16-bit PCM WAV amplitude cleanly using numpy with clipping protection."""
        import numpy as np

        gain = max(0.1, min(2.0, float(volume_percent) / 100.0))
        try:
            with wave.open(str(wav_path), "rb") as wf:
                params = wf.getparams()
                raw = wf.readframes(wf.getnframes())
            if params.sampwidth != 2 or not raw:
                return
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) * gain
            samples = np.clip(samples, -32767.0, 32767.0).astype(np.int16)
            with wave.open(str(wav_path), "wb") as wf:
                wf.setparams(params)
                wf.writeframes(samples.tobytes())
        except Exception as exc:
            logger.debug("Volume scaling skipped: %s", exc)

    @staticmethod
    def _write_chime_wav(target_path: Path, sample_rate: int = 16000, duration_sec: float = 1.0) -> None:
        num_samples = int(sample_rate * duration_sec)
        with wave.open(str(target_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            frames = bytearray()
            for i in range(num_samples):
                t = i / sample_rate
                freq = 523.25 if t < (duration_sec / 2) else 659.25
                env = math.exp(-3.0 * (t % (duration_sec / 2)))
                sample = int(12000 * env * math.sin(2.0 * math.pi * freq * t))
                frames.extend(struct.pack("<h", max(-32768, min(32767, sample))))
            wf.writeframes(bytes(frames))

    def send_wav_to_camera_speaker(
        self,
        wav_path: Path,
        camera_cfg: Dict[str, Any],
        go2rtc_url: str = "http://127.0.0.1:1984",
    ) -> Dict[str, Any]:
        """
        Stream a WAV file directly to the TP-Link Tapo C200 built-in speaker.
        Uses go2rtc's Tapo two-way backchannel (pcm_alaw / 8000 Hz / 1ch) on Port 8800.
        """
        host = (camera_cfg.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "Keine Kamera-IP konfiguriert."}

        # 1. Push via go2rtc HTTP API to tapo_c200 (tapo://admin:<SHA256_UPPER>@<host>)
        try:
            src_arg = f"ffmpeg:{wav_path.resolve()}#audio=pcma"
            query = urllib.parse.urlencode({"dst": "tapo_c200", "src": src_arg})
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(f"{go2rtc_url.rstrip('/')}/api/streams?{query}")
                if resp.status_code in (200, 201, 204):
                    return {
                        "ok": True,
                        "method": "go2rtc-tapo-backchannel",
                        "message": f"Audio direkt an Tapo C200 ({host}:8800) Lautsprecher gesendet.",
                    }
                logger.warning("go2rtc backchannel status %s: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("go2rtc HTTP API backchannel error: %s", exc)

        return {
            "ok": False,
            "method": "offline-synthesized-only",
            "error": (
                f"Sprachdatei erzeugt ({wav_path.name}), aber Kamera-Backchannel ({host}) "
                "war nicht erreichbar."
            ),
        }

    def speak_on_camera(
        self,
        text: str,
        camera_cfg: Dict[str, Any],
        voice_name: str = "de_DE-thorsten-medium",
        go2rtc_url: str = "http://127.0.0.1:1984",
        volume_percent: int = 100,
    ) -> Dict[str, Any]:
        """Synthesize text offline and immediately play it on the Tapo C200 speaker (thread-safe)."""
        with self._speak_lock:
            wav_path = self.synthesize_to_wav(
                text, voice_name=voice_name, volume_percent=volume_percent
            )
            delivery = self.send_wav_to_camera_speaker(
                wav_path=wav_path,
                camera_cfg=camera_cfg,
                go2rtc_url=go2rtc_url,
            )
            delivery["text"] = text
            delivery["voice"] = voice_name
            delivery["wav_path"] = str(wav_path)
            return delivery
