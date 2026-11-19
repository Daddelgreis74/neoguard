import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from app.camera_service import TapoCameraService
from app.config import ConfigStore
from app.face_service import LocalFaceService
from app.tts_service import LocalTTSService

logger = logging.getLogger("neoguard.worker")


def get_german_time_greeting(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now()
    hour = dt.hour
    if 5 <= hour < 11:
        return "Guten Morgen"
    if 11 <= hour < 18:
        return "Guten Tag"
    if 18 <= hour < 22:
        return "Guten Abend"
    return "Hallo"


def format_greeting_template(template: str, name: str = "Gast", now: Optional[datetime] = None) -> str:
    dt = now or datetime.now()
    greeting_time = get_german_time_greeting(dt)
    time_str = dt.strftime("%H:%M")
    try:
        return template.format(
            name=name,
            greeting_time=greeting_time,
            time=time_str,
        )
    except Exception:
        return f"{greeting_time}, {name}!"


def is_within_active_hours(start_str: str, end_str: str, now: Optional[datetime] = None) -> bool:
    dt = now or datetime.now()
    try:
        sh, sm = [int(x) for x in start_str.split(":")]
        eh, em = [int(x) for x in end_str.split(":")]
        cur_mins = dt.hour * 60 + dt.minute
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
        if start_mins <= end_mins:
            return start_mins <= cur_mins <= end_mins
        # Overnight window (e.g. 22:00 -> 06:00)
        return cur_mins >= start_mins or cur_mins <= end_mins
    except Exception:
        return True


class RoomGuardWorker:
    """Background worker and frame analyzer for person recognition and speaker greeting."""

    def __init__(
        self,
        store: ConfigStore,
        camera_service: Any,
        face_service: LocalFaceService,
        tts_service: LocalTTSService,
    ) -> None:
        self.store = store
        self.camera = camera_service
        self.face = face_service
        self.tts = tts_service
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_unknown_greet_ts: float = 0.0
        self._last_person_seen_by_cam: Dict[str, float] = {}
        self._last_known_seen_ts: float = 0.0
        self._at_home_by_cam: Dict[str, bool] = {}
        self._prev_motion_gray: Optional[np.ndarray] = None
        self._unknown_streak: int = 0

    def _cam_service(self, camera_id: Optional[str]) -> TapoCameraService:
        if hasattr(self.camera, "get_service"):
            return self.camera.get_service(camera_id)
        return self.camera

    def check_cooldown_expired(self, last_greeted_iso: Optional[str], cooldown_minutes: int) -> bool:
        if not last_greeted_iso or cooldown_minutes <= 0:
            return True
        try:
            last_dt = datetime.fromisoformat(last_greeted_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
            return elapsed_min >= cooldown_minutes
        except Exception:
            return True

    def analyze_and_greet_frame(
        self,
        frame_bgr: np.ndarray,
        force_greet: bool = False,
        camera_cfg: Optional[Dict[str, Any]] = None,
        cam_service: Optional[TapoCameraService] = None,
    ) -> List[Dict[str, Any]]:
        """
        Detect faces in frame_bgr, match against enrolled persons, and log recognition events.
        Returns list of recognition results for all detected faces.
        """
        raw_cfg = self.store.get_raw_config()
        camera_cfg = camera_cfg or raw_cfg["camera"]
        cam_id = str(camera_cfg.get("id") or "cam_1")
        cam_name = str(camera_cfg.get("name") or "Kamera 1")
        cam_svc = cam_service or self._cam_service(cam_id)
        guard_cfg = raw_cfg["guard"]
        persons = raw_cfg["persons"]

        cam_svc.set_ai_overlay_enabled(bool(guard_cfg.get("ai_overlay", True)))
        auto_track = bool(camera_cfg.get("auto_tracking", guard_cfg.get("auto_tracking", True)))
        track_speed = float(camera_cfg.get("tracking_speed", guard_cfg.get("tracking_speed", 1.4)))

        # Stage 1: Neural Person Detection via local YOLOv8n ONNX
        yolo_people = self.face.detect_people_yolo(frame_bgr, conf_threshold=0.45)
        detections: List[Dict[str, Any]] = []
        body_bbox: Optional[List[int]] = None

        if yolo_people is not None:
            if not yolo_people and not force_greet:
                self._unknown_streak = 0
                cam_svc.update_ai_overlay(frame_bgr.shape, [], body_bbox=None)
                return []

            if yolo_people:
                self._last_person_seen_by_cam[cam_id] = datetime.now(timezone.utc).timestamp()
                body_bbox = yolo_people[0]["bbox"]

                # Stage 2: Search for faces ONLY inside detected YOLOv8n persons (with 2.0x AI Zoom!)
                for person_det in yolo_people[:3]:
                    p_dets = self.face.detect_faces_in_crop(
                        frame_bgr, person_det["bbox"], upscale=2.0, upper_body_only=True
                    )
                    if not p_dets:
                        p_dets = self.face.detect_faces_in_crop(
                            frame_bgr, person_det["bbox"], upscale=1.0, upper_body_only=True
                        )
                    detections.extend(p_dets)

                if not detections:
                    # Person detected by YOLOv8n (e.g. sideways/back-turned): track body smoothly!
                    if auto_track:
                        moved = cam_svc.track_face_in_frame(
                            camera_cfg, body_bbox, frame_bgr.shape, speed_multiplier=track_speed
                        )
                        if moved:
                            self._at_home_by_cam[cam_id] = False
                    cam_svc.update_ai_overlay(frame_bgr.shape, [], body_bbox=body_bbox)
                    return []
            else:
                # force_greet=True fallback when no person box triggered
                detections = self.face.detect_and_embed(frame_bgr)
                if not detections:
                    cam_svc.update_ai_overlay(frame_bgr.shape, [], body_bbox=None)
                    return []
        else:
            # Fallback if YOLOv8n model is missing
            thresh_mask, cur_gray, motion_ratio = self.face.compute_motion_mask(
                frame_bgr, prev_gray=self._prev_motion_gray
            )
            self._prev_motion_gray = cur_gray
            detections = self.face.detect_and_embed(frame_bgr)
            if not detections:
                cam_svc.update_ai_overlay(frame_bgr.shape, [], body_bbox=None)
                return []

        self._last_person_seen_by_cam[cam_id] = datetime.now(timezone.utc).timestamp()

        # Auto-Tracking: follow the largest detected face smoothly via ONVIF PTZ
        if auto_track and detections:
            primary_det = max(detections, key=lambda d: d["bbox"][2] * d["bbox"][3])
            moved = cam_svc.track_face_in_frame(
                camera_cfg, primary_det["bbox"], frame_bgr.shape, speed_multiplier=track_speed
            )
            if moved:
                self._at_home_by_cam[cam_id] = False

        threshold = float(guard_cfg.get("similarity_threshold", 0.38))
        default_cooldown = int(guard_cfg.get("cooldown_minutes", 120))
        voice_name = guard_cfg.get("tts_voice", "de_DE-thorsten-medium")
        volume_pct = int(guard_cfg.get("tts_volume", 100))
        go2rtc_url = guard_cfg.get("go2rtc_url", "http://127.0.0.1:1984")
        now_iso = datetime.now(timezone.utc).isoformat()

        outcomes: List[Dict[str, Any]] = []
        overlay_faces: List[Dict[str, Any]] = []
        annotated = frame_bgr.copy()

        for det in detections:
            bx, by, bw, bh = det["bbox"]
            matched_person, sim_score = self.face.match_person(
                det["embedding"], persons, threshold=threshold
            )

            # If a small/distant face wasn't recognized yet, zoom 2.2x on it and re-check
            if matched_person is None and not det.get("zoomed") and bw < 85:
                zoomed_dets = self.face.detect_faces_in_crop(
                    frame_bgr, det["bbox"], upscale=2.2, upper_body_only=False
                )
                if zoomed_dets:
                    z_best = max(zoomed_dets, key=lambda d: d["bbox"][2] * d["bbox"][3])
                    z_person, z_score = self.face.match_person(
                        z_best["embedding"], persons, threshold=threshold
                    )
                    if z_score > sim_score:
                        matched_person, sim_score = z_person, z_score
                        det = z_best
                        bx, by, bw, bh = det["bbox"]

            if matched_person is not None:
                self._unknown_streak = 0
                self._last_known_seen_ts = datetime.now(timezone.utc).timestamp()
                p_id = matched_person["id"]
                p_name = matched_person["name"]
                p_cooldown = matched_person.get("cooldown_minutes")
                effective_cooldown = int(p_cooldown if p_cooldown is not None else default_cooldown)
                can_greet = force_greet or self.check_cooldown_expired(
                    matched_person.get("last_greeted"), effective_cooldown
                )

                template = (
                    matched_person.get("greeting")
                    or guard_cfg.get("default_greeting")
                    or "{greeting_time}, {name}!"
                )
                greeting_text = format_greeting_template(template, name=p_name)

                # Draw bounding box on snapshot
                cv2.rectangle(annotated, (bx, by), (bx + bw, by + bh), (153, 211, 52), 2)
                cv2.putText(
                    annotated,
                    f"{p_name} ({int(sim_score * 100)}%)",
                    (bx, max(24, by - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (153, 211, 52),
                    2,
                    cv2.LINE_AA,
                )

                audio_sent = False
                if can_greet:
                    # Bekannte Personen sind OK: nur last_seen aktualisieren, kein Snapshot/Event speichern
                    self.store.update_person(p_id, {"last_seen": now_iso})
                    if guard_cfg.get("auto_privacy_after_greeting"):
                        cam_svc.set_privacy_mode(camera_cfg, True)
                else:
                    self.store.update_person(p_id, {"last_seen": now_iso})

                overlay_faces.append(
                    {
                        "bbox": det["bbox"],
                        "landmarks": det.get("landmarks", []),
                        "recognized": True,
                        "label": f"{p_name} ({int(sim_score * 100)}%)",
                    }
                )
                outcomes.append(
                    {
                        "recognized": True,
                        "person_id": p_id,
                        "person_name": p_name,
                        "similarity": sim_score,
                        "greeted": can_greet,
                        "audio_sent": audio_sent,
                        "greeting_text": greeting_text if can_greet else None,
                        "bbox": det["bbox"],
                    }
                )
            else:
                # Unknown person
                cv2.rectangle(annotated, (bx, by), (bx + bw, by + bh), (56, 189, 248), 2)
                cv2.putText(
                    annotated,
                    "Unbekannt",
                    (bx, max(24, by - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (56, 189, 248),
                    2,
                    cv2.LINE_AA,
                )

                self._unknown_streak += 1
                greet_unknown = bool(guard_cfg.get("greet_unknown", False))
                unknown_cooldown = int(guard_cfg.get("unknown_cooldown_minutes", 2))
                now_ts = datetime.now(timezone.utc).timestamp()
                recently_known = (now_ts - self._last_known_seen_ts) < 15.0
                can_greet_unknown = force_greet or (
                    greet_unknown
                    and not recently_known
                    and self._unknown_streak >= 2
                    and (now_ts - self._last_unknown_greet_ts) >= (unknown_cooldown * 60)
                )
                audio_sent = False
                greeting_text = format_greeting_template(
                    guard_cfg.get("unknown_greeting") or "Hallo!", name="Gast"
                )

                if can_greet_unknown:
                    self._last_unknown_greet_ts = now_ts
                    snap_name = f"event_{uuid.uuid4().hex[:8]}.jpg"
                    snap_path = self.store.snapshots_dir / snap_name
                    cv2.imwrite(str(snap_path), annotated)

                    self.store.add_event(
                        person_name="Unbekannt",
                        person_id=None,
                        confidence=sim_score,
                        greeting_text="",
                        audio_sent=False,
                        snapshot_filename=snap_name,
                        event_type="unknown_person",
                        camera_id=cam_id,
                        camera_name=cam_name,
                    )

                overlay_faces.append(
                    {
                        "bbox": det["bbox"],
                        "landmarks": det.get("landmarks", []),
                        "recognized": False,
                        "label": f"Unbekannt ({int(sim_score * 100)}%)",
                    }
                )
                outcomes.append(
                    {
                        "recognized": False,
                        "person_id": None,
                        "person_name": "Unbekannt",
                        "similarity": sim_score,
                        "greeted": can_greet_unknown,
                        "audio_sent": audio_sent,
                        "greeting_text": greeting_text if can_greet_unknown else None,
                        "bbox": det["bbox"],
                    }
                )

        cam_svc.update_ai_overlay(frame_bgr.shape, overlay_faces, body_bbox=None)
        return outcomes

    async def _loop(self) -> None:
        logger.info("NeoGuard multi-camera monitoring loop started.")
        while self._running:
            interval = 2.0
            try:
                raw_cfg = self.store.get_raw_config()
                guard_cfg = raw_cfg["guard"]
                cameras = [c for c in raw_cfg.get("cameras", []) if c.get("enabled", True)]
                interval = max(1.0, float(guard_cfg.get("detection_interval_sec", 2.0)))

                if guard_cfg.get("enabled", True) and is_within_active_hours(
                    guard_cfg.get("active_hours_start", "06:00"),
                    guard_cfg.get("active_hours_end", "23:00"),
                ):
                    if any(c.get("auto_tracking", True) for c in cameras):
                        interval = min(interval, 0.12)

                    for cam_cfg in cameras:
                        cam_id = str(cam_cfg.get("id") or "cam_1")
                        cam_svc = self._cam_service(cam_id)
                        frame = await asyncio.to_thread(
                            cam_svc.capture_frame,
                            cam_cfg,
                            cam_cfg.get("stream_quality", "stream1"),
                        )
                        if frame is not None:
                            await asyncio.to_thread(
                                self.analyze_and_greet_frame, frame, False, cam_cfg, cam_svc
                            )

                        # Return to Preset 1 (home/door) after no person seen for configured delay
                        last_seen = self._last_person_seen_by_cam.get(cam_id, 0.0)
                        at_home = self._at_home_by_cam.get(cam_id, True)
                        if (
                            cam_cfg.get("auto_tracking", True)
                            and cam_cfg.get("tracking_return_home", True)
                            and not at_home
                            and last_seen > 0
                        ):
                            idle_sec = datetime.now(timezone.utc).timestamp() - last_seen
                            home_delay = float(cam_cfg.get("tracking_home_delay_sec", 20))
                            if idle_sec >= home_delay:
                                await asyncio.to_thread(cam_svc.goto_preset, cam_cfg, "1")
                                self._at_home_by_cam[cam_id] = True
            except Exception as exc:
                logger.debug("Guard loop iteration warning: %s", exc)

            await asyncio.sleep(interval)

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
