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

logger = logging.getLogger("tapo_room_guard.worker")


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
        camera_service: TapoCameraService,
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
        self._last_person_seen_ts: float = 0.0
        self._at_home_preset: bool = True
        self._prev_motion_gray: Optional[np.ndarray] = None

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
    ) -> List[Dict[str, Any]]:
        """
        Detect faces in frame_bgr, match against enrolled persons, and trigger greeting if cooldown allows.
        Returns list of recognition results for all detected faces.
        """
        raw_cfg = self.store.get_raw_config()
        camera_cfg = raw_cfg["camera"]
        guard_cfg = raw_cfg["guard"]
        persons = raw_cfg["persons"]

        track_speed = float(guard_cfg.get("tracking_speed", 1.4))
        detections = self.face.detect_and_embed(frame_bgr)
        if not detections:
            # Fallback: track walking/sideways/back-turned person via motion-gated HOG body detector
            if guard_cfg.get("auto_tracking", True):
                person_bbox, cur_gray = self.face.detect_largest_person_bbox(
                    frame_bgr, prev_gray=self._prev_motion_gray
                )
                self._prev_motion_gray = cur_gray
                if person_bbox is not None:
                    self._last_person_seen_ts = datetime.now(timezone.utc).timestamp()
                    moved = self.camera.track_face_in_frame(
                        camera_cfg, person_bbox, frame_bgr.shape, speed_multiplier=track_speed
                    )
                    if moved:
                        self._at_home_preset = False
                        # Reset motion baseline after motor movement so camera pan doesn't self-trigger
                        self._prev_motion_gray = None
            return []

        self._prev_motion_gray = None
        self._last_person_seen_ts = datetime.now(timezone.utc).timestamp()

        # Auto-Tracking: follow the largest detected face smoothly via ONVIF PTZ
        if guard_cfg.get("auto_tracking", True):
            primary_det = max(detections, key=lambda d: d["bbox"][2] * d["bbox"][3])
            moved = self.camera.track_face_in_frame(
                camera_cfg, primary_det["bbox"], frame_bgr.shape, speed_multiplier=track_speed
            )
            if moved:
                self._at_home_preset = False

        threshold = float(guard_cfg.get("similarity_threshold", 0.38))
        default_cooldown = int(guard_cfg.get("cooldown_minutes", 120))
        voice_name = guard_cfg.get("tts_voice", "de_DE-thorsten-medium")
        go2rtc_url = guard_cfg.get("go2rtc_url", "http://127.0.0.1:1984")
        now_iso = datetime.now(timezone.utc).isoformat()

        outcomes: List[Dict[str, Any]] = []
        annotated = frame_bgr.copy()

        for det in detections:
            bx, by, bw, bh = det["bbox"]
            matched_person, sim_score = self.face.match_person(
                det["embedding"], persons, threshold=threshold
            )

            if matched_person is not None:
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
                    snap_name = f"event_{uuid.uuid4().hex[:8]}.jpg"
                    snap_path = self.store.snapshots_dir / snap_name
                    cv2.imwrite(str(snap_path), annotated)

                    tts_res = self.tts.speak_on_camera(
                        text=greeting_text,
                        camera_cfg=camera_cfg,
                        voice_name=voice_name,
                        go2rtc_url=go2rtc_url,
                    )
                    audio_sent = bool(tts_res.get("ok"))

                    self.store.update_person(
                        p_id, {"last_seen": now_iso, "last_greeted": now_iso}
                    )
                    self.store.add_event(
                        person_name=p_name,
                        person_id=p_id,
                        confidence=sim_score,
                        greeting_text=greeting_text,
                        audio_sent=audio_sent,
                        snapshot_filename=snap_name,
                        event_type="greeting",
                    )

                    if guard_cfg.get("auto_privacy_after_greeting"):
                        self.camera.set_privacy_mode(camera_cfg, True)
                else:
                    self.store.update_person(p_id, {"last_seen": now_iso})

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

                greet_unknown = bool(guard_cfg.get("greet_unknown", False))
                now_ts = datetime.now(timezone.utc).timestamp()
                can_greet_unknown = force_greet or (
                    greet_unknown and (now_ts - self._last_unknown_greet_ts) >= (default_cooldown * 60)
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

                    tts_res = self.tts.speak_on_camera(
                        text=greeting_text,
                        camera_cfg=camera_cfg,
                        voice_name=voice_name,
                        go2rtc_url=go2rtc_url,
                    )
                    audio_sent = bool(tts_res.get("ok"))
                    self.store.add_event(
                        person_name="Unbekannt",
                        person_id=None,
                        confidence=sim_score,
                        greeting_text=greeting_text,
                        audio_sent=audio_sent,
                        snapshot_filename=snap_name,
                        event_type="unknown_person",
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

        return outcomes

    async def _loop(self) -> None:
        logger.info("RoomGuard background monitoring loop started.")
        while self._running:
            try:
                raw_cfg = self.store.get_raw_config()
                guard_cfg = raw_cfg["guard"]
                camera_cfg = raw_cfg["camera"]
                interval = max(1.0, float(guard_cfg.get("detection_interval_sec", 2.0)))

                if guard_cfg.get("enabled", True) and is_within_active_hours(
                    guard_cfg.get("active_hours_start", "06:00"),
                    guard_cfg.get("active_hours_end", "23:00"),
                ):
                    if guard_cfg.get("auto_tracking", True):
                        interval = min(interval, 0.4)

                    frame = await asyncio.to_thread(
                        self.camera.capture_frame,
                        camera_cfg,
                        camera_cfg.get("stream_quality", "stream1"),
                    )
                    if frame is not None:
                        await asyncio.to_thread(self.analyze_and_greet_frame, frame, False)

                    # Return to Preset 1 (home/door) after no person seen for configured delay
                    if (
                        guard_cfg.get("auto_tracking", True)
                        and guard_cfg.get("tracking_return_home", True)
                        and not self._at_home_preset
                        and self._last_person_seen_ts > 0
                    ):
                        idle_sec = datetime.now(timezone.utc).timestamp() - self._last_person_seen_ts
                        home_delay = float(guard_cfg.get("tracking_home_delay_sec", 20))
                        if idle_sec >= home_delay:
                            await asyncio.to_thread(self.camera.goto_preset, camera_cfg, "1")
                            self._at_home_preset = True
            except Exception as exc:
                logger.debug("Guard loop iteration warning: %s", exc)

            await asyncio.sleep(interval if "interval" in locals() else 2.0)

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
