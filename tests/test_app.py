import io
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

os.environ["DISABLE_BACKGROUND_WORKER"] = "1"

from app.main import app, face_service, store, tts_service


@pytest.fixture()
def client(tmp_path: Path):
    store.data_dir = tmp_path
    store.config_file = tmp_path / "config.json"
    store.events_file = tmp_path / "events.json"
    store.faces_dir = tmp_path / "faces"
    store.snapshots_dir = tmp_path / "snapshots"
    store.models_dir = tmp_path / "models"
    store._ensure_dirs()
    store._config = store._load_config()
    store._events = []
    with TestClient(app) as c:
        # Authenticate test client with default credentials
        login_res = c.post("/api/auth/login", json={"username": "admin", "password": "tapo1234"})
        assert login_res.status_code == 200
        yield c


def test_auth_protection(tmp_path: Path):
    store.data_dir = tmp_path
    store.config_file = tmp_path / "config.json"
    store.events_file = tmp_path / "events.json"
    store.faces_dir = tmp_path / "faces"
    store.snapshots_dir = tmp_path / "snapshots"
    store.models_dir = tmp_path / "models"
    store._ensure_dirs()
    store._config = store._load_config()
    store._events = []
    with TestClient(app) as anon:
        assert anon.get("/api/health").status_code == 200
        assert anon.get("/api/config").status_code == 401
        assert anon.post("/api/tts/speak", json={"text": "test"}).status_code == 401


def test_health_endpoint(client: TestClient):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "face_engine" in body
    assert "de_DE-thorsten-medium" in body["available_voices"]


def test_camera_and_guard_config_update(client: TestClient):
    res = client.put(
        "/api/config/camera",
        json={
            "host": "192.168.178.77",
            "rtsp_user": "cam_user",
            "rtsp_pass": "secret123",
            "rtsp_port": 554,
        },
    )
    assert res.status_code == 200
    assert res.json()["camera"]["host"] == "192.168.178.77"
    assert res.json()["camera"]["rtsp_pass"] == "••••••••"

    # Masked password should not overwrite real password
    res2 = client.put("/api/config/camera", json={"rtsp_pass": "••••••••"})
    assert res2.status_code == 200
    assert store.get_raw_config()["camera"]["rtsp_pass"] == "secret123"

    res_guard = client.put(
        "/api/config/guard",
        json={"cooldown_minutes": 60, "greet_unknown": True},
    )
    assert res_guard.status_code == 200
    assert res_guard.json()["guard"]["cooldown_minutes"] == 60
    assert res_guard.json()["guard"]["greet_unknown"] is True


def test_person_enrollment_and_face_matching(client: TestClient):
    # Create person
    res = client.post(
        "/api/persons",
        json={
            "name": "Daniel",
            "greeting": "Hallo {name}, willkommen im Büro!",
            "cooldown_minutes": 90,
        },
    )
    assert res.status_code == 200
    person = res.json()["person"]
    p_id = person["id"]
    assert person["name"] == "Daniel"

    # Create synthetic portrait image and upload
    img = np.zeros((200, 200, 3), dtype=np.uint8)
    cv2.circle(img, (100, 100), 60, (200, 180, 160), -1)
    cv2.circle(img, (80, 85), 8, (20, 20, 20), -1)
    cv2.circle(img, (120, 85), 8, (20, 20, 20), -1)
    cv2.ellipse(img, (100, 120), (22, 10), 0, 0, 180, (40, 40, 150), 3)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok

    upload_res = client.post(
        f"/api/persons/{p_id}/photo",
        files={"file": ("daniel.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")},
    )
    assert upload_res.status_code == 200
    updated_person = upload_res.json()["person"]
    assert updated_person["photo_count"] == 1

    # Verify cosine similarity matching works against stored embedding
    raw_persons = store.list_persons(include_embeddings=True)
    emb = raw_persons[0]["embeddings"][0]
    matched, score = face_service.match_person(emb, raw_persons, threshold=0.38)
    assert matched is not None
    assert matched["name"] == "Daniel"
    assert score > 0.99


def test_tts_synthesis_and_speak_endpoint(client: TestClient):
    res = client.post(
        "/api/tts/speak",
        json={"text": "{greeting_time}, Daniel! Systemtest erfolgreich."},
    )
    assert res.status_code == 200
    data = res.json()
    assert "Daniel" in data["text"]
    assert Path(data["wav_path"]).exists()

    wav_res = client.get("/api/tts/last.wav")
    assert wav_res.status_code == 200
    assert wav_res.headers["content-type"].startswith("audio/")

    events = client.get("/api/events").json()["events"]
    assert len(events) >= 1
    assert events[0]["event_type"] == "manual_tts"


def test_snapshot_endpoint(client: TestClient):
    res = client.get("/api/camera/snapshot.jpg")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert len(res.content) > 1000


def test_multi_camera_crud_and_48h_retention(client: TestClient):
    # Initially 1 camera exists; deleting the last camera must fail
    cams = client.get("/api/cameras").json()["cameras"]
    assert len(cams) == 1
    assert client.delete(f"/api/cameras/{cams[0]['id']}").status_code == 400

    # Add second camera
    add_res = client.post(
        "/api/cameras",
        json={"name": "Wohnzimmer", "host": "192.168.178.102", "tracking_speed": 2.2},
    )
    assert add_res.status_code == 200
    cam2 = add_res.json()["camera"]
    assert cam2["name"] == "Wohnzimmer"

    # Update second camera preset names
    upd_res = client.put(
        f"/api/cameras/{cam2['id']}",
        json={"preset_names": {"1": "Sofa", "2": "Terrasse", "3": "Esstisch"}},
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["camera"]["preset_names"]["1"] == "Sofa"

    # 48h retention pruning removes expired events & snapshots
    from datetime import datetime, timedelta, timezone

    old_snap = store.snapshots_dir / "old_test.jpg"
    old_snap.write_bytes(b"old")
    old_ev = store.add_event(
        person_name="Unbekannt",
        person_id=None,
        confidence=0.2,
        greeting_text="",
        audio_sent=False,
        snapshot_filename="old_test.jpg",
        event_type="unknown_person",
        camera_id=cam2["id"],
        camera_name="Wohnzimmer",
    )
    old_ev["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    fresh_ev = store.add_event(
        person_name="Unbekannt",
        person_id=None,
        confidence=0.3,
        greeting_text="",
        audio_sent=False,
        event_type="unknown_person",
        camera_id=cam2["id"],
        camera_name="Wohnzimmer",
    )
    events = client.get("/api/events").json()["events"]
    assert len(events) == 1
    assert events[0]["id"] == fresh_ev["id"]
    assert not old_snap.exists()

    # Delete second camera succeeds
    assert client.delete(f"/api/cameras/{cam2['id']}").status_code == 200
