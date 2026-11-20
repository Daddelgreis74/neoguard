import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["DISABLE_BACKGROUND_WORKER"] = "1"

from app.config import WEEKLY_DAYS, get_schedule_mode
from app.main import app, store
from app.recording_service import RecordingService


@pytest.fixture()
def client(tmp_path: Path):
    store.data_dir = tmp_path
    store.config_file = tmp_path / "config.json"
    store.events_file = tmp_path / "events.json"
    store.recordings_file = tmp_path / "recordings.json"
    store.faces_dir = tmp_path / "faces"
    store.snapshots_dir = tmp_path / "snapshots"
    store.recordings_dir = tmp_path / "recordings"
    store.models_dir = tmp_path / "models"
    store._ensure_dirs()
    store._config = store._load_config()
    store._events = []
    store._recordings = []
    with TestClient(app) as c:
        login_res = c.post("/api/auth/login", json={"username": "admin", "password": "tapo1234"})
        assert login_res.status_code == 200
        yield c


def test_get_schedule_mode_7x24():
    weekly = {d: ["off"] * 24 for d in WEEKLY_DAYS}
    weekly["mon"][9] = "events"
    weekly["sat"][23] = "continuous"
    cfg = {"enabled": True, "weekly_schedule": weekly}

    # 2026-11-16 is a Monday
    assert get_schedule_mode(cfg, datetime(2026, 11, 16, 9, 30)) == "events"
    assert get_schedule_mode(cfg, datetime(2026, 11, 16, 10, 0)) == "off"
    # 2026-11-21 is a Saturday
    assert get_schedule_mode(cfg, datetime(2026, 11, 21, 23, 45)) == "continuous"
    assert get_schedule_mode({"enabled": False, "weekly_schedule": weekly}, datetime(2026, 11, 16, 9, 30)) == "off"


def test_schedule_api_roundtrip(client: TestClient):
    res = client.get("/api/schedule")
    assert res.status_code == 200
    data = res.json()
    assert set(data["weekly_schedule"].keys()) == set(WEEKLY_DAYS)
    assert len(data["weekly_schedule"]["mon"]) == 24

    new_sched = data["weekly_schedule"]
    new_sched["sun"][2] = "continuous"
    put_res = client.put(
        "/api/schedule",
        json={
            "weekly_schedule": new_sched,
            "recording": {"retention_days": 7, "max_storage_gb": 50, "trigger_on": "unknown"},
        },
    )
    assert put_res.status_code == 200
    updated = put_res.json()
    assert updated["weekly_schedule"]["sun"][2] == "continuous"
    assert updated["recording"]["retention_days"] == 7
    assert updated["recording"]["max_storage_gb"] == 50
    assert updated["recording"]["trigger_on"] == "unknown"


def test_recordings_api_and_retention(client: TestClient):
    rec_dir = store.recordings_dir / "2026-11-20" / "cam_1"
    rec_dir_old = store.recordings_dir / "2026-11-01" / "cam_1"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec_dir_old.mkdir(parents=True, exist_ok=True)
    mp4_new = rec_dir / "clip_new.mp4"
    mp4_old = rec_dir_old / "clip_old.mp4"
    mp4_new.write_bytes(b"ftypmp42" + b"0" * 5000)
    mp4_old.write_bytes(b"ftypmp42" + b"1" * 5000)

    now_utc = datetime.now(timezone.utc)
    store.add_recording(
        {
            "id": "rec_new",
            "camera_id": "cam_1",
            "camera_name": "Flur",
            "mode": "events",
            "event_type": "unknown_person",
            "person_name": "Unbekannt",
            "date": "2026-11-20",
            "start_ts": now_utc.isoformat(),
            "end_ts": now_utc.isoformat(),
            "local_hour": 14,
            "local_minute": 30,
            "duration_sec": 25,
            "rel_path": "2026-11-20/cam_1/clip_new.mp4",
            "size_bytes": mp4_new.stat().st_size,
        }
    )
    store.add_recording(
        {
            "id": "rec_old",
            "camera_id": "cam_1",
            "camera_name": "Flur",
            "mode": "events",
            "event_type": "known_person",
            "person_name": "Steffen",
            "date": "2026-11-01",
            "start_ts": (now_utc - timedelta(days=20)).isoformat(),
            "end_ts": (now_utc - timedelta(days=20)).isoformat(),
            "local_hour": 8,
            "local_minute": 0,
            "duration_sec": 25,
            "rel_path": "2026-11-01/cam_1/clip_old.mp4",
            "size_bytes": mp4_old.stat().st_size,
        }
    )

    list_res = client.get("/api/recordings?date=2026-11-20")
    assert list_res.status_code == 200
    assert len(list_res.json()["recordings"]) == 1

    vid_res = client.get("/api/recordings/rec_new/video")
    assert vid_res.status_code == 200
    assert vid_res.content.startswith(b"ftypmp42")

    svc = RecordingService(store=store)
    removed = svc.enforce_retention(retention_days=14, max_storage_gb=100)
    assert removed == 1
    assert store.get_recording("rec_old") is None
    assert not mp4_old.exists()

    del_res = client.delete("/api/recordings/rec_new")
    assert del_res.status_code == 200
    assert not mp4_new.exists()
