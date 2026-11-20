import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
CONFIG_FILE = DATA_DIR / "config.json"
EVENTS_FILE = DATA_DIR / "events.json"
FACES_DIR = DATA_DIR / "faces"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
MODELS_DIR = DATA_DIR / "models"

DEFAULT_CAMERA: Dict[str, Any] = {
    "id": "cam_1",
    "name": "Kamera 1",
    "enabled": True,
    "brand_preset": "tapo",
    "host": os.environ.get("TAPO_HOST", "192.168.178.50"),
    "rtsp_user": os.environ.get("TAPO_RTSP_USER", ""),
    "rtsp_pass": os.environ.get("TAPO_RTSP_PASS", ""),
    "cloud_password": os.environ.get("TAPO_CLOUD_PASS", ""),
    "rtsp_port": int(os.environ.get("TAPO_RTSP_PORT", "554")),
    "onvif_port": int(os.environ.get("TAPO_ONVIF_PORT", "2020")),
    "onvif_profile": "profile_1",
    "stream_quality": "stream1",
    "preview_stream": "stream2",
    "ptz_step": 15,
    "auto_tracking": True,
    "tracking_speed": 1.4,
    "tracking_return_home": True,
    "tracking_home_delay_sec": 20,
    "preset_names": {
        "1": "Tür / Eingang",
        "2": "Raum-Mitte",
        "3": "Schreibtisch",
    },
}

WEEKLY_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VALID_SCHEDULE_MODES = ("off", "events", "continuous")


def default_weekly_schedule(start_hour: int = 6, end_hour: int = 23) -> Dict[str, List[str]]:
    """Build default 7x24h schedule matching legacy active hours."""
    return {
        day: ["events" if start_hour <= h < end_hour else "off" for h in range(24)]
        for day in WEEKLY_DAYS
    }


def normalize_weekly_schedule(raw: Any) -> Dict[str, List[str]]:
    """Normalize arbitrary schedule payload into strict 7x24h mode lists."""
    base = default_weekly_schedule()
    if not isinstance(raw, dict):
        return base
    for day in WEEKLY_DAYS:
        slots = raw.get(day)
        if isinstance(slots, list) and len(slots) == 24:
            base[day] = [
                s if s in VALID_SCHEDULE_MODES else "off"
                for s in slots
            ]
    return base


def get_schedule_mode(guard_cfg: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """Resolve current schedule mode ('off', 'events', 'continuous') for local time."""
    if not guard_cfg.get("enabled", True):
        return "off"
    dt = now or datetime.now()
    weekly = guard_cfg.get("weekly_schedule")
    if isinstance(weekly, dict):
        day_key = WEEKLY_DAYS[dt.weekday()]
        slots = weekly.get(day_key)
        if isinstance(slots, list) and len(slots) == 24:
            mode = slots[dt.hour]
            if mode in VALID_SCHEDULE_MODES:
                return mode
    # Legacy fallback
    try:
        sh, sm = [int(x) for x in str(guard_cfg.get("active_hours_start", "06:00")).split(":")]
        eh, em = [int(x) for x in str(guard_cfg.get("active_hours_end", "23:00")).split(":")]
        cur_m = dt.hour * 60 + dt.minute
        start_m, end_m = sh * 60 + sm, eh * 60 + em
        active = (start_m <= cur_m <= end_m) if start_m <= end_m else (cur_m >= start_m or cur_m <= end_m)
        return "events" if active else "off"
    except Exception:
        return "events"


DEFAULT_CONFIG: Dict[str, Any] = {
    "cameras": [json.loads(json.dumps(DEFAULT_CAMERA))],
    "guard": {
        "enabled": True,
        "detection_interval_sec": 2.0,
        "cooldown_minutes": 120,
        "unknown_cooldown_minutes": 2,
        "similarity_threshold": 0.38,
        "default_greeting": "{greeting_time}, {name}! Willkommen zurück.",
        "unknown_greeting": "{greeting_time}! Bitte beachte, dass dieser Raum überwacht wird.",
        "greet_unknown": True,
        "event_retention_hours": 48,
        "active_hours_start": "06:00",
        "active_hours_end": "23:00",
        "auto_privacy_after_greeting": False,
        "auto_tracking": True,
        "ai_overlay": True,
        "tracking_speed": 1.4,
        "tracking_return_home": True,
        "tracking_home_delay_sec": 20,
        "preset_names": {
            "1": "Tür / Eingang",
            "2": "Raum-Mitte",
            "3": "Schreibtisch",
        },
        "tts_voice": "de_DE-thorsten-medium",
        "tts_volume": 100,
        "go2rtc_url": os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984"),
        "weekly_schedule": default_weekly_schedule(),
        "recording": {
            "enabled": True,
            "trigger_on": "all",
            "pre_roll_sec": 5,
            "post_roll_sec": 20,
            "retention_days": 14,
            "max_storage_gb": 100,
        },
    },
    "persons": [],
    "auth": {
        "enabled": True,
        "username": os.environ.get("AUTH_USER", "admin"),
        "password_hash": "",
        "session_secret": "",
    },
}


class ConfigStore:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self.data_dir = data_dir or DATA_DIR
        self.config_file = self.data_dir / "config.json"
        self.events_file = self.data_dir / "events.json"
        self.recordings_file = self.data_dir / "recordings.json"
        self.faces_dir = self.data_dir / "faces"
        self.snapshots_dir = self.data_dir / "snapshots"
        self.recordings_dir = self.data_dir / "recordings"
        self.models_dir = self.data_dir / "models"
        self._ensure_dirs()
        self._config = self._load_config()
        self._events = self._load_events()
        self._recordings = self._load_recordings()

    def _ensure_dirs(self) -> None:
        for folder in (
            self.data_dir,
            self.faces_dir,
            self.snapshots_dir,
            self.recordings_dir,
            self.models_dir,
            self.models_dir / "vision",
            self.models_dir / "tts",
        ):
            folder.mkdir(parents=True, exist_ok=True)

    def _merge_camera(self, raw_cam: Dict[str, Any], idx: int = 1) -> Dict[str, Any]:
        cam = json.loads(json.dumps(DEFAULT_CAMERA))
        cam["id"] = str(raw_cam.get("id") or f"cam_{idx}")
        cam["name"] = str(raw_cam.get("name") or f"Kamera {idx}")
        for k, v in raw_cam.items():
            if k == "preset_names" and isinstance(v, dict):
                cam["preset_names"].update({str(pk): str(pv) for pk, pv in v.items()})
            elif k in cam and v is not None:
                cam[k] = v
        return cam

    def _load_config(self) -> Dict[str, Any]:
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                merged = json.loads(json.dumps(DEFAULT_CONFIG))
                merged["guard"].update(loaded.get("guard", {}))
                raw_cams = loaded.get("cameras")
                if isinstance(raw_cams, list) and raw_cams:
                    merged["cameras"] = [
                        self._merge_camera(c, idx=i + 1)
                        for i, c in enumerate(raw_cams)
                        if isinstance(c, dict)
                    ]
                elif isinstance(loaded.get("camera"), dict):
                    # Migrate legacy single-camera config cleanly
                    legacy = dict(loaded["camera"])
                    legacy_guard = loaded.get("guard", {})
                    for gk in (
                        "auto_tracking",
                        "tracking_speed",
                        "tracking_return_home",
                        "tracking_home_delay_sec",
                        "preset_names",
                    ):
                        if gk in legacy_guard and gk not in legacy:
                            legacy[gk] = legacy_guard[gk]
                    merged["cameras"] = [self._merge_camera(legacy, idx=1)]
                merged["persons"] = loaded.get("persons", [])
                merged["auth"].update(loaded.get("auth", {}))
                self._ensure_auth_initialized(merged)
                return merged
            except Exception:
                pass
        fresh = json.loads(json.dumps(DEFAULT_CONFIG))
        self._ensure_auth_initialized(fresh)
        self._save_config_unlocked(fresh)
        return fresh

    @staticmethod
    def _hash_password(password: str, salt: Optional[str] = None) -> str:
        s = salt or secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), s.encode("utf-8"), 120_000).hex()
        return f"pbkdf2_sha256${s}${dk}"

    def _ensure_auth_initialized(self, cfg: Dict[str, Any]) -> None:
        auth = cfg.setdefault("auth", {})
        changed = False
        if not auth.get("session_secret"):
            auth["session_secret"] = secrets.token_hex(32)
            changed = True
        if not auth.get("username"):
            auth["username"] = os.environ.get("AUTH_USER", "admin")
            changed = True
        if not auth.get("password_hash"):
            default_pw = os.environ.get("AUTH_PASS", "tapo1234")
            auth["password_hash"] = self._hash_password(default_pw)
            changed = True
        if changed:
            self._save_config_unlocked(cfg)

    def verify_login(self, username: str, password: str) -> bool:
        with self._lock:
            auth = self._config.get("auth", {})
            if not auth.get("enabled", True):
                return True
            expected_user = (auth.get("username") or "admin").strip()
            if username.strip() != expected_user:
                return False
            stored = auth.get("password_hash") or ""
            parts = stored.split("$")
            if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
                return False
            calc = self._hash_password(password, salt=parts[1])
            return hmac.compare_digest(calc, stored)

    def get_session_secret(self) -> str:
        with self._lock:
            return self._config.get("auth", {}).get("session_secret", "")

    def get_auth_info(self) -> Dict[str, Any]:
        with self._lock:
            auth = self._config.get("auth", {})
            return {
                "enabled": bool(auth.get("enabled", True)),
                "username": auth.get("username", "admin"),
            }

    def update_credentials(self, new_username: Optional[str], new_password: str) -> Dict[str, Any]:
        with self._lock:
            auth = self._config.setdefault("auth", {})
            if new_username and new_username.strip():
                auth["username"] = new_username.strip()
            if new_password:
                auth["password_hash"] = self._hash_password(new_password)
                # Rotate session secret to invalidate old sessions on password change
                auth["session_secret"] = secrets.token_hex(32)
            self._save_config_unlocked(self._config)
            return {"username": auth["username"], "enabled": bool(auth.get("enabled", True))}

    def _save_config_unlocked(self, cfg: Dict[str, Any]) -> None:
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    def _load_events(self) -> List[Dict[str, Any]]:
        if self.events_file.exists():
            try:
                with open(self.events_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    def _save_events_unlocked(self) -> None:
        with open(self.events_file, "w", encoding="utf-8") as f:
            json.dump(self._events[:200], f, indent=2, ensure_ascii=False)

    def get_config(self, mask_secrets: bool = False) -> Dict[str, Any]:
        with self._lock:
            cfg = json.loads(json.dumps(self._config))
            # Never expose password hashes or session secrets in config API
            if "auth" in cfg:
                cfg["auth"] = {
                    "enabled": bool(cfg["auth"].get("enabled", True)),
                    "username": cfg["auth"].get("username", "admin"),
                }
            # Strip heavy numpy embedding arrays in UI payloads if requested
            for p in cfg.get("persons", []):
                p["photo_count"] = len(p.get("photos", []))
                p.pop("embeddings", None)
            if mask_secrets:
                for cam in cfg.get("cameras", []):
                    if cam.get("rtsp_pass"):
                        cam["rtsp_pass_set"] = True
                    if cam.get("cloud_password"):
                        cam["cloud_password_set"] = True
            cfg["camera"] = cfg["cameras"][0] if cfg.get("cameras") else json.loads(json.dumps(DEFAULT_CAMERA))
            return cfg

    def get_raw_config(self) -> Dict[str, Any]:
        with self._lock:
            cfg = json.loads(json.dumps(self._config))
            cfg["camera"] = cfg["cameras"][0] if cfg.get("cameras") else json.loads(json.dumps(DEFAULT_CAMERA))
            return cfg

    def list_cameras(self) -> List[Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._config.get("cameras", [])))

    def get_camera(self, camera_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            cams = self._config.get("cameras", [])
            if camera_id:
                for c in cams:
                    if c.get("id") == camera_id:
                        return json.loads(json.dumps(c))
            if cams:
                return json.loads(json.dumps(cams[0]))
            return json.loads(json.dumps(DEFAULT_CAMERA))

    def add_camera(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cams = self._config.setdefault("cameras", [])
            cam_id = f"cam_{str(uuid.uuid4())[:6]}"
            payload = dict(data)
            payload["id"] = cam_id
            if not payload.get("name"):
                payload["name"] = f"Kamera {len(cams) + 1}"
            cam = self._merge_camera(payload, idx=len(cams) + 1)
            cams.append(cam)
            self._save_config_unlocked(self._config)
            return json.loads(json.dumps(cam))

    def update_camera(self, updates: Dict[str, Any], camera_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            cams = self._config.setdefault("cameras", [])
            if not cams:
                cams.append(json.loads(json.dumps(DEFAULT_CAMERA)))
            target = None
            if camera_id:
                for c in cams:
                    if c.get("id") == camera_id:
                        target = c
                        break
            if target is None:
                target = cams[0]
            for key, val in updates.items():
                if key == "preset_names" and isinstance(val, dict):
                    target.setdefault("preset_names", {}).update({str(k): str(v) for k, v in val.items()})
                elif key in target and val is not None and key != "id":
                    # Keep existing password if masked placeholder is sent
                    if key in ("rtsp_pass", "cloud_password") and val == "••••••••":
                        continue
                    target[key] = val
            self._save_config_unlocked(self._config)
            return json.loads(json.dumps(target))

    def delete_camera(self, camera_id: str) -> bool:
        with self._lock:
            cams = self._config.get("cameras", [])
            if len(cams) <= 1:
                return False
            new_cams = [c for c in cams if c.get("id") != camera_id]
            if len(new_cams) == len(cams):
                return False
            self._config["cameras"] = new_cams
            self._save_config_unlocked(self._config)
            return True

    def update_guard(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key, val in updates.items():
                if key in self._config["guard"] and val is not None:
                    self._config["guard"][key] = val
            self._save_config_unlocked(self._config)
            return dict(self._config["guard"])

    def list_persons(self, include_embeddings: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            persons = json.loads(json.dumps(self._config.get("persons", [])))
            if not include_embeddings:
                for p in persons:
                    p["photo_count"] = len(p.get("photos", []))
                    p.pop("embeddings", None)
            return persons

    def get_person(self, person_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for p in self._config.get("persons", []):
                if p["id"] == person_id:
                    return json.loads(json.dumps(p))
            return None

    def add_person(
        self,
        name: str,
        greeting: str = "",
        cooldown_minutes: Optional[int] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            person = {
                "id": str(uuid.uuid4())[:8],
                "name": name.strip(),
                "greeting": greeting.strip(),
                "cooldown_minutes": cooldown_minutes,
                "enabled": enabled,
                "photos": [],
                "embeddings": [],
                "last_seen": None,
                "last_greeted": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._config["persons"].append(person)
            self._save_config_unlocked(self._config)
            result = dict(person)
            result["photo_count"] = 0
            result.pop("embeddings", None)
            return result

    def update_person(self, person_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            for p in self._config["persons"]:
                if p["id"] == person_id:
                    for key in ("name", "greeting", "cooldown_minutes", "enabled", "last_seen", "last_greeted"):
                        if key in updates:
                            p[key] = updates[key]
                    self._save_config_unlocked(self._config)
                    res = json.loads(json.dumps(p))
                    res["photo_count"] = len(res.get("photos", []))
                    res.pop("embeddings", None)
                    return res
            return None

    def add_person_photo(
        self, person_id: str, filename: str, embedding: List[float]
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            for p in self._config["persons"]:
                if p["id"] == person_id:
                    p.setdefault("photos", []).append(filename)
                    p.setdefault("embeddings", []).append(embedding)
                    self._save_config_unlocked(self._config)
                    res = json.loads(json.dumps(p))
                    res["photo_count"] = len(res.get("photos", []))
                    res.pop("embeddings", None)
                    return res
            return None

    def delete_person_photo(self, person_id: str, filename: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            safe_name = Path(filename).name
            for p in self._config["persons"]:
                if p["id"] == person_id:
                    photos = p.get("photos", [])
                    embeddings = p.get("embeddings", [])
                    if safe_name not in photos:
                        return None
                    idx = photos.index(safe_name)
                    photos.pop(idx)
                    if idx < len(embeddings):
                        embeddings.pop(idx)
                    photo_path = self.faces_dir / safe_name
                    if photo_path.exists():
                        try:
                            photo_path.unlink()
                        except OSError:
                            pass
                    self._save_config_unlocked(self._config)
                    res = json.loads(json.dumps(p))
                    res["photo_count"] = len(res.get("photos", []))
                    res.pop("embeddings", None)
                    return res
            return None

    def delete_person(self, person_id: str) -> bool:
        with self._lock:
            before = len(self._config["persons"])
            target = next((p for p in self._config["persons"] if p["id"] == person_id), None)
            if not target:
                return False
            for photo in target.get("photos", []):
                photo_path = self.faces_dir / photo
                if photo_path.exists():
                    try:
                        photo_path.unlink()
                    except OSError:
                        pass
            self._config["persons"] = [p for p in self._config["persons"] if p["id"] != person_id]
            self._save_config_unlocked(self._config)
            return len(self._config["persons"]) < before

    def _prune_expired_events_unlocked(self, retention_hours: int = 48) -> bool:
        if retention_hours <= 0 or not self._events:
            return False
        cutoff = datetime.now(timezone.utc).timestamp() - (retention_hours * 3600)
        kept: List[Dict[str, Any]] = []
        changed = False
        for ev in self._events:
            keep = True
            ts_str = ev.get("timestamp")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.timestamp() < cutoff:
                        keep = False
                except Exception:
                    pass
            if keep:
                kept.append(ev)
            else:
                changed = True
                snap_url = ev.get("snapshot_url") or ""
                if snap_url.startswith("/api/snapshots/"):
                    snap_file = self.snapshots_dir / Path(snap_url).name
                    if snap_file.exists():
                        try:
                            snap_file.unlink()
                        except OSError:
                            pass
        if changed:
            self._events = kept
        return changed

    def add_event(
        self,
        person_name: str,
        person_id: Optional[str],
        confidence: float,
        greeting_text: str,
        audio_sent: bool,
        snapshot_filename: Optional[str] = None,
        event_type: str = "greeting",
        camera_id: Optional[str] = None,
        camera_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            event = {
                "id": str(uuid.uuid4())[:8],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "camera_id": camera_id,
                "camera_name": camera_name,
                "person_id": person_id,
                "person_name": person_name,
                "confidence": round(float(confidence), 3),
                "greeting_text": greeting_text,
                "audio_sent": audio_sent,
                "snapshot_url": f"/api/snapshots/{snapshot_filename}" if snapshot_filename else None,
            }
            self._events.insert(0, event)
            if len(self._events) > 200:
                for evicted in self._events[200:]:
                    snap_url = evicted.get("snapshot_url") or ""
                    if snap_url.startswith("/api/snapshots/"):
                        snap_file = self.snapshots_dir / Path(snap_url).name
                        if snap_file.exists():
                            try:
                                snap_file.unlink()
                            except OSError:
                                pass
                self._events = self._events[:200]
            retention = int(self._config.get("guard", {}).get("event_retention_hours", 48))
            self._prune_expired_events_unlocked(retention)
            self._save_events_unlocked()
            return event

    def list_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            retention = int(self._config.get("guard", {}).get("event_retention_hours", 48))
            if self._prune_expired_events_unlocked(retention):
                self._save_events_unlocked()
            return list(self._events[:limit])

    def clear_events(self) -> None:
        with self._lock:
            self._events = []
            self._save_events_unlocked()
            for snap in self.snapshots_dir.glob("*.jpg"):
                try:
                    snap.unlink()
                except OSError:
                    pass

    # -------------------- Weekly Schedule & Recordings --------------------

    def _load_recordings(self) -> List[Dict[str, Any]]:
        if self.recordings_file.exists():
            try:
                with open(self.recordings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save_recordings_unlocked(self) -> None:
        with open(self.recordings_file, "w", encoding="utf-8") as f:
            json.dump(self._recordings, f, indent=2, ensure_ascii=False)

    def get_schedule(self) -> Dict[str, Any]:
        with self._lock:
            guard = self._config.get("guard", {})
            rec_defaults = DEFAULT_CONFIG["guard"]["recording"]
            rec = dict(rec_defaults)
            rec.update(guard.get("recording") or {})
            return {
                "weekly_schedule": normalize_weekly_schedule(guard.get("weekly_schedule")),
                "recording": rec,
                "current_mode": get_schedule_mode(guard),
            }

    def update_schedule(
        self,
        weekly_schedule: Optional[Dict[str, Any]] = None,
        recording: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            guard = self._config.setdefault("guard", {})
            if weekly_schedule is not None:
                guard["weekly_schedule"] = normalize_weekly_schedule(weekly_schedule)
            if recording is not None:
                rec = dict(DEFAULT_CONFIG["guard"]["recording"])
                rec.update(guard.get("recording") or {})
                for k in ("enabled", "trigger_on", "pre_roll_sec", "post_roll_sec", "retention_days", "max_storage_gb"):
                    if k in recording and recording[k] is not None:
                        rec[k] = recording[k]
                rec["pre_roll_sec"] = max(0, min(20, int(rec["pre_roll_sec"])))
                rec["post_roll_sec"] = max(5, min(120, int(rec["post_roll_sec"])))
                rec["retention_days"] = max(1, min(365, int(rec["retention_days"])))
                rec["max_storage_gb"] = max(1, min(2000, int(rec["max_storage_gb"])))
                if rec.get("trigger_on") not in ("all", "unknown"):
                    rec["trigger_on"] = "all"
                guard["recording"] = rec
            self._save_config_unlocked(self._config)
        return self.get_schedule()

    def add_recording(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._recordings.insert(0, entry)
            self._save_recordings_unlocked()
            return entry

    def list_recordings(
        self,
        date_str: Optional[str] = None,
        camera_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._recordings)
        if date_str:
            items = [r for r in items if r.get("date") == date_str]
        if camera_id:
            items = [r for r in items if r.get("camera_id") == camera_id]
        return items

    def get_recording(self, rec_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for r in self._recordings:
                if r.get("id") == rec_id:
                    return dict(r)
        return None

    def delete_recording(self, rec_id: str) -> bool:
        with self._lock:
            target = None
            remaining = []
            for r in self._recordings:
                if r.get("id") == rec_id:
                    target = r
                else:
                    remaining.append(r)
            if target is None:
                return False
            self._recordings = remaining
            self._save_recordings_unlocked()
        rel_path = target.get("rel_path")
        if rel_path:
            fpath = self.recordings_dir / rel_path
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError:
                    pass
        return True

    def get_recording_storage_stats(self) -> Dict[str, Any]:
        with self._lock:
            total_bytes = sum(int(r.get("size_bytes") or 0) for r in self._recordings)
            count = len(self._recordings)
        return {
            "count": count,
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 1),
        }
