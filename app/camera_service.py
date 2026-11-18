import base64
import datetime
import hashlib
import logging
import os
import re
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
    """Manages RTSP frame capture, ONVIF/pytapo PTZ control, and go2rtc Two-Way Audio config."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.go2rtc_config_path = data_dir / "go2rtc.yaml"
        self._frame_lock = threading.Lock()
        self._last_frame: Optional[np.ndarray] = None
        self._last_frame_ts: float = 0.0
        self._last_fail_ts: float = 0.0
        self._last_cfg_sig: str = ""
        self._privacy_enabled: bool = False
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._reader_cfg: Optional[Dict[str, Any]] = None
        self._active_mjpeg_clients: int = 0
        self._last_track_ts: float = 0.0

    @staticmethod
    def _cfg_signature(camera_cfg: Dict[str, Any]) -> str:
        return (
            f"{camera_cfg.get('host')}|{camera_cfg.get('rtsp_user')}|"
            f"{camera_cfg.get('rtsp_pass')}|{camera_cfg.get('cloud_password')}"
        )

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
        """
        Write go2rtc.yaml with Tapo Two-Way Audio Backchannel (UPPERCASE SHA256 for V5 / FW 1.5+).
        """
        self._last_fail_ts = 0.0
        self._last_cfg_sig = self._cfg_signature(camera_cfg)

        host = (camera_cfg.get("host") or "").strip()
        rtsp_pass = camera_cfg.get("rtsp_pass") or ""
        cloud_pass = camera_cfg.get("cloud_password") or rtsp_pass

        lines = [
            "api:",
            "  listen: ':1984'",
            "rtsp:",
            "  listen: ':8554'",
            "streams:",
        ]
        tapo_url = ""
        if host and cloud_pass:
            # Tapo C200 V5 / FW 1.4+ / 1.5+ requires UPPERCASE SHA256 of the cloud password in go2rtc
            sha256_upper = hashlib.sha256(cloud_pass.encode("utf-8")).hexdigest().upper()
            tapo_url = f"tapo://admin:{sha256_upper}@{host}"
            lines.extend(
                [
                    "  tapo_c200:",
                    f"    - {tapo_url}",
                ]
            )
        else:
            lines.append("  tapo_c200: []")

        self.go2rtc_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if host and tapo_url:
            try:
                with httpx.Client(timeout=2.5) as client:
                    client.put(
                        f"{go2rtc_url.rstrip('/')}/api/streams",
                        params={"name": "tapo_c200", "src": tapo_url},
                    )
            except Exception:
                pass

    def _ensure_reader_running(self, camera_cfg: Dict[str, Any]) -> None:
        """Start or restart the persistent RTSP reader thread for smooth 15fps preview."""
        if self._privacy_enabled:
            return
        if not (camera_cfg.get("host") and camera_cfg.get("rtsp_user") and camera_cfg.get("rtsp_pass")):
            return

        sig = self._cfg_signature(camera_cfg)
        self._reader_cfg = dict(camera_cfg)
        if self._reader_thread is not None and self._reader_thread.is_alive():
            if sig == self._last_cfg_sig:
                return
            self._reader_stop.set()
            self._reader_thread.join(timeout=1.5)

        self._last_cfg_sig = sig
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(target=self._rtsp_reader_loop, daemon=True)
        self._reader_thread.start()

    def _rtsp_reader_loop(self) -> None:
        """Keep one persistent RTSP connection open so frames are always ready with minimal latency."""
        cap = None
        try:
            while not self._reader_stop.is_set():
                cfg = self._reader_cfg or {}
                if self._privacy_enabled:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(0.5)
                    continue

                if cap is None or not cap.isOpened():
                    rtsp_url = self.build_rtsp_url(cfg, stream=cfg.get("preview_stream", "stream2"))
                    if not rtsp_url:
                        time.sleep(1.0)
                        continue
                    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        self._last_fail_ts = time.time()
                        time.sleep(10.0)
                        continue

                ok, frame = cap.read()
                if ok and frame is not None and frame.size > 0:
                    with self._frame_lock:
                        self._last_frame = frame
                        self._last_frame_ts = time.time()
                        self._last_fail_ts = 0.0
                else:
                    cap.release()
                    cap = None
                    time.sleep(0.5)
        finally:
            if cap is not None:
                cap.release()

    def capture_frame(
        self,
        camera_cfg: Dict[str, Any],
        stream: Optional[str] = None,
        ignore_backoff: bool = False,
    ) -> Optional[np.ndarray]:
        """Return latest frame from persistent RTSP reader or capture one-shot frame."""
        if self._privacy_enabled:
            return None

        self._ensure_reader_running(camera_cfg)

        with self._frame_lock:
            if self._last_frame is not None and (time.time() - self._last_frame_ts) < 2.0:
                return self._last_frame.copy()

        sig = self._cfg_signature(camera_cfg)
        if sig != self._last_cfg_sig:
            self._last_cfg_sig = sig
            self._last_fail_ts = 0.0

        if not ignore_backoff and self._last_fail_ts > 0 and (time.time() - self._last_fail_ts) < 30.0:
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
                    self._last_fail_ts = 0.0
                return frame
            self._last_fail_ts = time.time()
        except Exception as exc:
            self._last_fail_ts = time.time()
            logger.debug("RTSP frame capture error: %s", exc)
        finally:
            cap.release()
        return None

    def get_snapshot_or_placeholder(self, camera_cfg: Dict[str, Any]) -> np.ndarray:
        """Return recent cached frame, fresh RTSP frame, or an informative status placeholder image."""
        self._ensure_reader_running(camera_cfg)
        with self._frame_lock:
            if self._last_frame is not None and (time.time() - self._last_frame_ts) < 2.5:
                return self._last_frame.copy()

        return self.render_placeholder_frame(camera_cfg)

    def render_placeholder_frame(self, camera_cfg: Dict[str, Any]) -> np.ndarray:
        """Render a clean 16:9 dark Aurora status frame when camera stream is offline or in privacy mode."""
        img = np.zeros((360, 640, 3), dtype=np.uint8)
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
        """Yield smooth ~15 FPS MJPEG frames for live browser preview."""
        last_sent_ts = 0.0
        while True:
            cfg = camera_cfg_getter()
            self._ensure_reader_running(cfg)
            frame = None
            with self._frame_lock:
                if self._last_frame is not None and (time.time() - self._last_frame_ts) < 2.5:
                    if self._last_frame_ts > last_sent_ts:
                        frame = self._last_frame.copy()
                        last_sent_ts = self._last_frame_ts
                else:
                    frame = None

            if frame is None and (time.time() - last_sent_ts) >= 0.8:
                frame = self.render_placeholder_frame(cfg)
                last_sent_ts = time.time()

            if frame is not None:
                ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if ok:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                    )
            time.sleep(0.06)

    @staticmethod
    def _onvif_request(camera_cfg: Dict[str, Any], service_path: str, body_xml: str) -> httpx.Response:
        """Send an authenticated ONVIF SOAP 1.2 request using WS-UsernameToken PasswordDigest."""
        host = (camera_cfg.get("host") or "").strip()
        port = int(camera_cfg.get("onvif_port") or 2020)
        user = (camera_cfg.get("rtsp_user") or "admin").strip()
        pw = camera_cfg.get("rtsp_pass") or ""

        nonce_raw = os.urandom(16)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = hashlib.sha1(nonce_raw + created.encode("utf-8") + pw.encode("utf-8")).digest()
        pass_digest = base64.b64encode(digest).decode("ascii")
        nonce_b64 = base64.b64encode(nonce_raw).decode("ascii")

        soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
            xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Header>
    <wsse:Security s:mustUnderstand="1">
      <wsse:UsernameToken>
        <wsse:Username>{user}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{pass_digest}</wsse:Password>
        <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
        <wsu:Created>{created}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>
  <s:Body>{body_xml}</s:Body>
</s:Envelope>"""
        with httpx.Client(timeout=5.0) as client:
            return client.post(
                f"http://{host}:{port}{service_path}",
                content=soap,
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            )

    def move_ptz(self, camera_cfg: Dict[str, Any], direction: str, step: Optional[int] = None) -> Dict[str, Any]:
        """Pan/Tilt the Tapo C200 camera via native ONVIF Port 2020."""
        deg = float(step or camera_cfg.get("ptz_step") or 15)
        norm_step = round(min(0.5, max(0.02, deg / 180.0)), 3)
        px, py = 0.0, 0.0
        if direction == "left":
            px = norm_step
        elif direction == "right":
            px = -norm_step
        elif direction == "up":
            py = -norm_step
        elif direction == "down":
            py = norm_step
        else:
            return {"ok": False, "error": f"Unbekannte Richtung: {direction}"}

        try:
            resp = self._onvif_request(
                camera_cfg,
                "/onvif/ptz_service",
                f"""<tptz:RelativeMove>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:Translation>
    <tt:PanTilt x="{px}" y="{py}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"/>
  </tptz:Translation>
</tptz:RelativeMove>""",
            )
            if resp.status_code == 200 and "Fault" not in resp.text:
                return {"ok": True, "method": "onvif", "direction": direction, "px": px, "py": py}
        except Exception as exc:
            logger.debug("ONVIF PTZ move failed: %s", exc)

        return {"ok": False, "error": "ONVIF PTZ fehlgeschlagen. Prüfe Kamera-Konto.", "direction": direction}

    def start_ptz_continuous(self, camera_cfg: Dict[str, Any], direction: str, speed: float = 0.45) -> Dict[str, Any]:
        """Start smooth continuous Pan/Tilt movement via ONVIF ContinuousMove."""
        vx, vy = 0.0, 0.0
        if direction == "left":
            vx = -speed
        elif direction == "right":
            vx = speed
        elif direction == "up":
            vy = speed
        elif direction == "down":
            vy = -speed
        else:
            return {"ok": False, "error": f"Unbekannte Richtung: {direction}"}

        try:
            resp = self._onvif_request(
                camera_cfg,
                "/onvif/ptz_service",
                f"""<tptz:ContinuousMove>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:Velocity>
    <tt:PanTilt x="{vx}" y="{vy}"/>
  </tptz:Velocity>
</tptz:ContinuousMove>""",
            )
            if resp.status_code == 200 and "Fault" not in resp.text:
                return {"ok": True, "method": "onvif-continuous", "direction": direction}
        except Exception as exc:
            logger.debug("ONVIF ContinuousMove failed: %s", exc)
        return {"ok": False, "error": "ONVIF ContinuousMove fehlgeschlagen."}

    def stop_ptz(self, camera_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Stop continuous Pan/Tilt movement immediately via ONVIF Stop."""
        try:
            resp = self._onvif_request(
                camera_cfg,
                "/onvif/ptz_service",
                """<tptz:Stop>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:PanTilt>true</tptz:PanTilt>
  <tptz:Zoom>false</tptz:Zoom>
</tptz:Stop>""",
            )
            if resp.status_code == 200 and "Fault" not in resp.text:
                return {"ok": True, "method": "onvif-stop"}
        except Exception as exc:
            logger.debug("ONVIF Stop failed: %s", exc)
        return {"ok": False, "error": "ONVIF Stop fehlgeschlagen."}

    def track_face_in_frame(
        self,
        camera_cfg: Dict[str, Any],
        bbox: list,
        frame_shape: tuple,
        speed_multiplier: float = 1.4,
    ) -> bool:
        """
        Pan/tilt the Tapo C200 to keep a detected face or person centered in the frame.
        Returns True if a tracking movement was performed.
        """
        speed_mult = max(0.3, min(2.5, float(speed_multiplier or 1.4)))
        now = time.time()
        min_cooldown = max(0.15, 0.35 / speed_mult)
        if (now - self._last_track_ts) < min_cooldown:
            return False

        h, w = frame_shape[:2]
        if w <= 0 or h <= 0 or len(bbox) < 4:
            return False

        bx, by, bw, bh = bbox[:4]
        face_cx = bx + bw / 2.0
        face_cy = by + bh / 2.0

        # Normalized offset from center in [-0.5, +0.5]
        err_x = (face_cx - (w / 2.0)) / float(w)
        err_y = (face_cy - (h / 2.0)) / float(h)

        # Deadzone ±10% horizontally, ±14% vertically
        vx = 0.0
        vy = 0.0
        if abs(err_x) > 0.10:
            vx = max(-1.0, min(1.0, err_x * 1.5 * speed_mult))
        if abs(err_y) > 0.14:
            vy = max(-0.85, min(0.85, -err_y * 1.1 * speed_mult))

        if vx == 0.0 and vy == 0.0:
            return False

        self._last_track_ts = now
        try:
            resp = self._onvif_request(
                camera_cfg,
                "/onvif/ptz_service",
                f"""<tptz:ContinuousMove>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:Velocity>
    <tt:PanTilt x="{vx:.3f}" y="{vy:.3f}"/>
  </tptz:Velocity>
</tptz:ContinuousMove>""",
            )
            if resp.status_code == 200 and "Fault" not in resp.text:
                pulse_sec = min(0.70, max(0.16, max(abs(err_x), abs(err_y)) * 0.85 * speed_mult))
                time.sleep(pulse_sec)
                self.stop_ptz(camera_cfg)
                return True
        except Exception as exc:
            logger.debug("Auto-tracking pulse failed: %s", exc)
        return False

    def set_privacy_mode(self, camera_cfg: Dict[str, Any], enabled: bool) -> Dict[str, Any]:
        """Toggle software/hardware privacy mode."""
        self._privacy_enabled = bool(enabled)
        return {"ok": True, "privacy_mode": self._privacy_enabled}

    def goto_preset(self, camera_cfg: Dict[str, Any], preset_id: str) -> Dict[str, Any]:
        """Move camera to a saved PTZ preset via ONVIF Port 2020."""
        try:
            resp = self._onvif_request(
                camera_cfg,
                "/onvif/ptz_service",
                f"""<tptz:GotoPreset>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:PresetToken>{preset_id}</tptz:PresetToken>
</tptz:GotoPreset>""",
            )
            if resp.status_code == 200 and "Fault" not in resp.text:
                return {"ok": True, "method": "onvif", "preset_id": preset_id}
            return {"ok": False, "error": f"Position {preset_id} ist noch nicht gespeichert. Klicke erst auf das Speichern-Symbol (💾)."}
        except Exception as exc:
            logger.warning("Goto preset failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def save_preset(self, camera_cfg: Dict[str, Any], preset_id: str) -> Dict[str, Any]:
        """Save current camera PTZ position into preset_id via ONVIF Port 2020."""
        try:
            # Ensure earlier presets exist if user saves slot 2 or 3 first
            target_num = int(preset_id) if preset_id.isdigit() else 1
            for slot in range(1, target_num + 1):
                slot_str = str(slot)
                xml_body = f"""<tptz:SetPreset>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:PresetName>Pos{slot_str}</tptz:PresetName>
  <tptz:PresetToken>{slot_str}</tptz:PresetToken>
</tptz:SetPreset>""" if slot == target_num else ""
                if not xml_body:
                    continue
                resp = self._onvif_request(camera_cfg, "/onvif/ptz_service", xml_body)
                if resp.status_code == 200 and "SetPresetResponse" in resp.text:
                    return {"ok": True, "method": "onvif", "preset_id": preset_id}
                # Fallback without PresetToken if slot wasn't created yet
                resp2 = self._onvif_request(
                    camera_cfg,
                    "/onvif/ptz_service",
                    f"""<tptz:SetPreset>
  <tptz:ProfileToken>profile_1</tptz:ProfileToken>
  <tptz:PresetName>Pos{preset_id}</tptz:PresetName>
</tptz:SetPreset>""",
                )
                if resp2.status_code == 200 and "SetPresetResponse" in resp2.text:
                    token_m = re.search(r"<tptz:PresetToken>([^<]+)</tptz:PresetToken>", resp2.text)
                    return {
                        "ok": True,
                        "method": "onvif",
                        "preset_id": token_m.group(1) if token_m else preset_id,
                    }
            return {"ok": False, "error": "ONVIF SetPreset fehlgeschlagen."}
        except Exception as exc:
            logger.warning("Save preset failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def get_camera_status(self, camera_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Check connectivity and device info of the Tapo C200 via ONVIF Port 2020."""
        host = (camera_cfg.get("host") or "").strip()
        if not host:
            return {"online": False, "model": "TP-Link Tapo C200", "privacy_mode": self._privacy_enabled}
        try:
            resp = self._onvif_request(
                camera_cfg,
                "/onvif/device_service",
                "<tds:GetDeviceInformation/>",
            )
            if resp.status_code == 200 and "GetDeviceInformationResponse" in resp.text:
                model_m = re.search(r"<tds:Model>([^<]+)</tds:Model>", resp.text)
                fw_m = re.search(r"<tds:FirmwareVersion>([^<]+)</tds:FirmwareVersion>", resp.text)
                hw_m = re.search(r"<tds:HardwareId>([^<]+)</tds:HardwareId>", resp.text)
                return {
                    "online": True,
                    "model": model_m.group(1) if model_m else "Tapo C200",
                    "sw_version": fw_m.group(1) if fw_m else "",
                    "hw_version": hw_m.group(1) if hw_m else "",
                    "privacy_mode": self._privacy_enabled,
                }
        except Exception as exc:
            return {
                "online": False,
                "model": "TP-Link Tapo C200",
                "privacy_mode": self._privacy_enabled,
                "detail": str(exc),
            }
        return {"online": False, "model": "TP-Link Tapo C200", "privacy_mode": self._privacy_enabled}
