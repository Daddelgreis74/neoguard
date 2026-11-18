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
        yield c


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
