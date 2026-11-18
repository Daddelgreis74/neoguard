import logging
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import cv2
import httpx
import numpy as np

logger = logging.getLogger("tapo_room_guard.camera")


class TapoCameraService:
    """Manages RTSP frame capture, PTZ/Privacy control via pytapo, and go2rtc Two-Way Audio config."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.go2rtc_config_path = data_dir / "go2rtc.yaml"
        self._frame_lock = threading.Lock()
        self._last_frame: Optional[np.ndarray] = None
        self._last_frame_ts: float = 0.0
        self._privacy_enabled: bool = False

    @staticmethod
    def build_rtsp_url(camera_cfg: Dict[str, Any], stream: Optional[str] = None) -> Optional[str]:
        host = (camera_cfg.get("host") or "").strip()
        user = (camera_cfg.get("rtsp_user") or "").strip()
        pw = camera_cfg.get("rtsp_pass") or ""
        port = int(camera_cfg.get("rtsp_port") or 554)
        stream_path = stream or camera_cfg.get("stream_quality") or "stream1"
        if not host:
            return None
        if user and pw:
            safe_user = urllib.parse.quote(user, safe="")
            safe_pw = urllib.parse.quote(pw, safe="")
            return f"rtsp://{safe_user}:{safe_pw}@{host}:{port}/{stream_path}"
        return f"rtsp://{host}:{port}/{stream_path}"

    def sync_go2rtc_config(self, camera_cfg: Dict[str, Any], go2rtc_url: str = "http://127.0.0.1:1984") -> None:
        """Write go2rtc.yaml with RTSP + Tapo Two-Way Audio Backchannel streams."""
        host = (camera_cfg.get("host") or "").strip()
        user = (camera_cfg.get("rtsp_user") or "admin").strip()
        rtsp_pass = camera_cfg.get("rtsp_pass") or ""
        cloud_pass = camera_cfg.get("cloud_password") or rtsp_pass
        rtsp_url = self.build_rtsp_url(camera_cfg, "stream1")

        lines = [
            "api:",
            "  listen: ':1984'",
            "rtsp:",
            "  listen: ':8554'",
            "streams:",
        ]
        if host and rtsp_url:
            safe_cloud = urllib.parse.quote(cloud_pass, safe="")
            tapo_url = f"tapo://admin:{safe_cloud}@{host}"
            lines.extend(
                [
                    "  tapo_c200:",
                    f"    - {rtsp_url}",
                    f"    - {tapo_url}",
                ]
            )
        else:
            lines.append("  tapo_c200: []")

        self.go2rtc_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Dynamically register stream via go2rtc API if running
        if host and rtsp_url:
            try:
                safe_cloud = urllib.parse.quote(cloud_pass, safe="")
                tapo_url = f"tapo://admin:{safe_cloud}@{host}"
                with httpx.Client(timeout=2.5) as client:
                    client.put(
                        f"{go2rtc_url.rstrip('/')}/api/streams",
                        params={"name": "tapo_c200", "src": [rtsp_url, tapo_url]},
                    )
            except Exception:
                pass

    def capture_frame(self, camera_cfg: Dict[str, Any], stream: Optional[str] = None) -> Optional[np.ndarray]:
        """Capture a single BGR frame from the Tapo C200 RTSP stream."""
        if self._privacy_enabled:
            return None

        rtsp_url = self.build_rtsp_url(camera_cfg, stream=stream)
        if not rtsp_url or not (camera_cfg.get("rtsp_user") and camera_cfg.get("rtsp_pass")):
            return None

        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                with self._frame_lock:
                    self._last_frame = frame.copy()
                    self._last_frame_ts = time.time()
                return frame
        except Exception as exc:
            logger.debug("RTSP frame capture error: %s", exc)
        finally:
            cap.release()
        return None

    def get_snapshot_or_placeholder(self, camera_cfg: Dict[str, Any]) -> np.ndarray:
        """Return recent cached frame, fresh RTSP frame, or an informative status placeholder image."""
        with self._frame_lock:
            if self._last_frame is not None and (time.time() - self._last_frame_ts) < 3.0:
                return self._last_frame.copy()

        frame = self.capture_frame(camera_cfg, stream=camera_cfg.get("preview_stream", "stream2"))
        if frame is not None:
            return frame

        return self.render_placeholder_frame(camera_cfg)

    def render_placeholder_frame(self, camera_cfg: Dict[str, Any]) -> np.ndarray:
        """Render a clean 16:9 dark Aurora status frame when camera stream is offline or in privacy mode."""
        img = np.zeros((360, 640, 3), dtype=np.uint8)
        # Subtle dark navy gradient background
        for y in range(360):
            img[y, :] = (28 + y // 18, 18 + y // 24, 10 + y // 30)

        host = camera_cfg.get("host") or "Nicht konfiguriert"
        if self._privacy_enabled:
            title = "PRIVATSPHAERE-MODUS AKTIV"
            sub = f"Tapo C200 ({host}) — Linse verdeckt"
            color = (128, 220, 56)
        elif not camera_cfg.get("rtsp_user"):
            title = "TAPO C200 — KAMERA-KONTO EINRICHTEN"
            sub = f"IP: {host} (RTSP Port 554 / ONVIF 2020)"
            color = (248, 189, 56)
        else:
            title = "WARTE AUF RTSP-VERBINDUNG..."
            sub = f"rtsp://{camera_cfg.get('rtsp_user')}@{host}:554"
            color = (180, 160, 255)

        cv2.putText(img, title, (40, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.72, color, 2, cv2.LINE_AA)
        cv2.putText(img, sub, (40, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 210, 225), 1, cv2.LINE_AA)
        ts = time.strftime("%H:%M:%S")
        cv2.putText(img, f"Tapo Room Guard  |  {ts}", (40, 325), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (130, 145, 165), 1, cv2.LINE_AA)
        return img

    def mjpeg_generator(self, camera_cfg_getter) -> Generator[bytes, None, None]:
        """Yield MJPEG frames for live browser preview."""
        while True:
            cfg = camera_cfg_getter()
            frame = self.get_snapshot_or_placeholder(cfg)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                jpg_bytes = buf.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + jpg_bytes + b"\r\n"
                )
            time.sleep(0.5)

    def _get_pytapo_client(self, camera_cfg: Dict[str, Any]):
        from pytapo import Tapo

        host = (camera_cfg.get("host") or "").strip()
        user = (camera_cfg.get("rtsp_user") or "admin").strip()
        pw = camera_cfg.get("cloud_password") or camera_cfg.get("rtsp_pass") or ""
        if not host or not pw:
            raise ValueError("Kamera-IP und Passwort (Kamera-Konto oder TP-Link Cloud-Passwort) erforderlich.")
        return Tapo(host, user, pw)

    def move_ptz(self, camera_cfg: Dict[str, Any], direction: str, step: Optional[int] = None) -> Dict[str, Any]:
        """Pan/Tilt the Tapo C200 camera via pytapo."""
        deg = int(step or camera_cfg.get("ptz_step") or 15)
        dx, dy = 0, 0
        if direction == "left":
            dx = -deg
        elif direction == "right":
            dx = deg
        elif direction == "up":
            dy = deg
        elif direction == "down":
            dy = -deg
        else:
            return {"ok": False, "error": f"Unbekannte Richtung: {direction}"}

        try:
            tapo = self._get_pytapo_client(camera_cfg)
            tapo.moveMotor(dx, dy)
            return {"ok": True, "direction": direction, "dx": dx, "dy": dy}
        except Exception as exc:
            logger.warning("PTZ move failed: %s", exc)
            return {"ok": False, "error": str(exc), "direction": direction}

    def set_privacy_mode(self, camera_cfg: Dict[str, Any], enabled: bool) -> Dict[str, Any]:
        """Toggle physical lens privacy mode on the Tapo C200."""
        self._privacy_enabled = bool(enabled)
        try:
            tapo = self._get_pytapo_client(camera_cfg)
            tapo.setPrivacyMode(bool(enabled))
            return {"ok": True, "privacy_mode": self._privacy_enabled}
        except Exception as exc:
            logger.warning("Setting privacy mode via pytapo failed: %s", exc)
            return {"ok": False, "privacy_mode": self._privacy_enabled, "error": str(exc)}

    def goto_preset(self, camera_cfg: Dict[str, Any], preset_id: str) -> Dict[str, Any]:
        """Move camera to a saved PTZ preset."""
        try:
            tapo = self._get_pytapo_client(camera_cfg)
            tapo.setPreset(preset_id)
            return {"ok": True, "preset_id": preset_id}
        except Exception as exc:
            logger.warning("Goto preset failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def get_camera_status(self, camera_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Check connectivity and device info of the Tapo C200."""
        host = (camera_cfg.get("host") or "").strip()
        if not host:
            return {"online": False, "model": "TP-Link Tapo C200", "privacy_mode": self._privacy_enabled}
        try:
            tapo = self._get_pytapo_client(camera_cfg)
            info = tapo.getBasicInfo()
            dev_info = info.get("device_info", {}).get("basic_info", {})
            return {
                "online": True,
                "model": dev_info.get("device_model", "Tapo C200"),
                "alias": dev_info.get("device_alias", "Tapo C200"),
                "sw_version": dev_info.get("sw_version", ""),
                "hw_version": dev_info.get("hw_version", ""),
                "mac": dev_info.get("mac", ""),
                "privacy_mode": self._privacy_enabled,
            }
        except Exception as exc:
            return {
                "online": False,
                "model": "TP-Link Tapo C200",
                "privacy_mode": self._privacy_enabled,
                "detail": str(exc),
            }
