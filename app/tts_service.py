import logging
import math
import shutil
import struct
import subprocess
import tempfile
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
        self._loaded_voice_name: Optional[str] = None
        self._piper_voice: Any = None

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

        return onnx_path, json_path

    def synthesize_to_wav(
        self,
        text: str,
        voice_name: str = "de_DE-thorsten-medium",
        output_path: Optional[Path] = None,
    ) -> Path:
        """
        Synthesize German text into a WAV file completely offline using Piper TTS.
        Falls back to a pleasant chime + PCM audio file if Piper model is unavailable.
        """
        target_path = output_path or self.last_wav_path
        clean_text = text.strip() or "Hallo!"
        onnx_path, json_path = self.ensure_voice_model(voice_name)

        # Method 1: Python piper.PiperVoice API
        if onnx_path.exists() and json_path.exists():
            try:
                from piper.voice import PiperVoice

                if self._piper_voice is None or self._loaded_voice_name != voice_name:
                    self._piper_voice = PiperVoice.load(str(onnx_path), config_path=str(json_path))
                    self._loaded_voice_name = voice_name

                with wave.open(str(target_path), "wb") as wav_file:
                    self._piper_voice.synthesize(clean_text, wav_file)
                return target_path
            except Exception as exc:
                logger.warning("Python PiperVoice synthesis failed (%s), trying piper CLI...", exc)

            # Method 2: piper CLI binary
            piper_bin = shutil.which("piper")
            if piper_bin:
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
                    return target_path
                except Exception as cli_exc:
                    logger.warning("Piper CLI failed: %s", cli_exc)

        # Fallback: Generate a clean two-tone greeting chime WAV (16kHz 16-bit Mono)
        self._write_chime_wav(target_path, duration_sec=1.2)
        return target_path

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
                freq = 523.25 if t < (duration_sec / 2) else 659.25  # C5 -> E5 pleasant chime
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
        Uses go2rtc's Tapo two-way backchannel (pcm_alaw / 8000 Hz / 1ch).
        """
        host = (camera_cfg.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "Keine Kamera-IP konfiguriert."}

        ffmpeg_bin = shutil.which("ffmpeg")

        # 1. Try pushing via go2rtc HTTP API (ffmpeg source -> tapo_c200 backchannel)
        try:
            src_arg = f"ffmpeg:{wav_path.resolve()}#audio=pcma"
            query = urllib.parse.urlencode({"dst": "tapo_c200", "src": src_arg})
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(f"{go2rtc_url.rstrip('/')}/api/streams?{query}")
                if resp.status_code in (200, 201, 204):
                    return {
                        "ok": True,
                        "method": "go2rtc-api-backchannel",
                        "message": f"Audio an Tapo C200 ({host}) Lautsprecher gesendet.",
                    }
        except Exception as exc:
            logger.debug("go2rtc HTTP API backchannel push not reachable (%s), trying RTSP push...", exc)

        # 2. Try pushing via ffmpeg RTSP backchannel to local go2rtc RTSP server (port 8554)
        if ffmpeg_bin:
            try:
                cmd = [
                    ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-i",
                    str(wav_path.resolve()),
                    "-c:a",
                    "pcm_alaw",
                    "-ar",
                    "8000",
                    "-ac",
                    "1",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    "rtsp://127.0.0.1:8554/tapo_c200?backchannel=1",
                ]
                subprocess.run(cmd, check=True, timeout=20)
                return {
                    "ok": True,
                    "method": "ffmpeg-rtsp-backchannel",
                    "message": f"Audio via RTSP-Backchannel an Tapo C200 ({host}) ausgegeben.",
                }
            except Exception as exc:
                logger.warning("FFmpeg RTSP backchannel failed: %s", exc)

        return {
            "ok": False,
            "method": "offline-synthesized-only",
            "error": (
                f"Sprachdatei erzeugt ({wav_path.name}), aber Kamera-Backchannel ({host}) "
                "war nicht erreichbar. Prüfe Kamera-IP, Passwort und go2rtc."
            ),
        }

    def speak_on_camera(
        self,
        text: str,
        camera_cfg: Dict[str, Any],
        voice_name: str = "de_DE-thorsten-medium",
        go2rtc_url: str = "http://127.0.0.1:1984",
    ) -> Dict[str, Any]:
        """Synthesize text offline and immediately play it on the Tapo C200 speaker."""
        wav_path = self.synthesize_to_wav(text, voice_name=voice_name)
        delivery = self.send_wav_to_camera_speaker(
            wav_path=wav_path,
            camera_cfg=camera_cfg,
            go2rtc_url=go2rtc_url,
        )
        delivery["text"] = text
        delivery["voice"] = voice_name
        delivery["wav_path"] = str(wav_path)
        return delivery
