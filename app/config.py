import json
import os
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

DEFAULT_CONFIG: Dict[str, Any] = {
    "camera": {
        "host": os.environ.get("TAPO_HOST", "192.168.178.50"),
        "rtsp_user": os.environ.get("TAPO_RTSP_USER", ""),
        "rtsp_pass": os.environ.get("TAPO_RTSP_PASS", ""),
        "cloud_password": os.environ.get("TAPO_CLOUD_PASS", ""),
        "rtsp_port": int(os.environ.get("TAPO_RTSP_PORT", "554")),
        "onvif_port": int(os.environ.get("TAPO_ONVIF_PORT", "2020")),
        "stream_quality": "stream1",
        "preview_stream": "stream2",
        "ptz_step": 15,
    },
    "guard": {
        "enabled": True,
        "detection_interval_sec": 2.0,
        "cooldown_minutes": 120,
        "similarity_threshold": 0.38,
        "default_greeting": "{greeting_time}, {name}! Willkommen zurück.",
        "unknown_greeting": "{greeting_time}! Bitte beachte, dass dieser Raum überwacht wird.",
        "greet_unknown": False,
        "active_hours_start": "06:00",
        "active_hours_end": "23:00",
        "auto_privacy_after_greeting": False,
        "auto_tracking": True,
        "tracking_speed": 1.4,
        "tracking_return_home": True,
        "tracking_home_delay_sec": 20,
        "tts_voice": "de_DE-thorsten-medium",
        "go2rtc_url": os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984"),
    },
    "persons": [],
}


class ConfigStore:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self.data_dir = data_dir or DATA_DIR
        self.config_file = self.data_dir / "config.json"
        self.events_file = self.data_dir / "events.json"
        self.faces_dir = self.data_dir / "faces"
        self.snapshots_dir = self.data_dir / "snapshots"
        self.models_dir = self.data_dir / "models"
        self._ensure_dirs()
        self._config = self._load_config()
        self._events = self._load_events()

    def _ensure_dirs(self) -> None:
        for folder in (
            self.data_dir,
            self.faces_dir,
            self.snapshots_dir,
            self.models_dir,
            self.models_dir / "vision",
            self.models_dir / "tts",
        ):
            folder.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> Dict[str, Any]:
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                merged = json.loads(json.dumps(DEFAULT_CONFIG))
                merged["camera"].update(loaded.get("camera", {}))
                merged["guard"].update(loaded.get("guard", {}))
                merged["persons"] = loaded.get("persons", [])
                return merged
            except Exception:
                pass
        fresh = json.loads(json.dumps(DEFAULT_CONFIG))
        self._save_config_unlocked(fresh)
        return fresh

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
            # Strip heavy numpy embedding arrays in UI payloads if requested
            for p in cfg.get("persons", []):
                p["photo_count"] = len(p.get("photos", []))
                p.pop("embeddings", None)
            if mask_secrets:
                if cfg["camera"].get("rtsp_pass"):
                    cfg["camera"]["rtsp_pass_set"] = True
                if cfg["camera"].get("cloud_password"):
                    cfg["camera"]["cloud_password_set"] = True
            return cfg

    def get_raw_config(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._config))

    def update_camera(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key, val in updates.items():
                if key in self._config["camera"] and val is not None:
                    # Keep existing password if masked placeholder is sent
                    if key in ("rtsp_pass", "cloud_password") and val == "••••••••":
                        continue
                    self._config["camera"][key] = val
            self._save_config_unlocked(self._config)
            return dict(self._config["camera"])

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

    def add_event(
        self,
        person_name: str,
        person_id: Optional[str],
        confidence: float,
        greeting_text: str,
        audio_sent: bool,
        snapshot_filename: Optional[str] = None,
        event_type: str = "greeting",
    ) -> Dict[str, Any]:
        with self._lock:
            event = {
                "id": str(uuid.uuid4())[:8],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "person_id": person_id,
                "person_name": person_name,
                "confidence": round(float(confidence), 3),
                "greeting_text": greeting_text,
                "audio_sent": audio_sent,
                "snapshot_url": f"/api/snapshots/{snapshot_filename}" if snapshot_filename else None,
            }
            self._events.insert(0, event)
            self._events = self._events[:200]
            self._save_events_unlocked()
            return event

    def list_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
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
