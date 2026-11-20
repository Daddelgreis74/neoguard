import asyncio
import hashlib
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import __version__
from app.camera_service import CameraManager, TapoCameraService
from app.config import ConfigStore
from app.face_service import LocalFaceService
from app.guard_worker import RoomGuardWorker, format_greeting_template
from app.recording_service import RecordingService
from app.tts_service import PIPER_VOICES, LocalTTSService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("neoguard")

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

store = ConfigStore(data_dir=DATA_DIR)
camera_manager = CameraManager(data_dir=store.data_dir)
camera_service = camera_manager.get_service("cam_1")
face_service = LocalFaceService(models_dir=store.models_dir)
tts_service = LocalTTSService(models_dir=store.models_dir)
recording_service = RecordingService(store=store)
guard_worker = RoomGuardWorker(
    store=store,
    camera_service=camera_manager,
    face_service=face_service,
    tts_service=tts_service,
    recording_service=recording_service,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    raw_cfg = store.get_raw_config()
    go2rtc_url = raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984")
    for cam in raw_cfg.get("cameras", []):
        camera_manager.get_service(cam.get("id")).sync_go2rtc_config(cam, go2rtc_url=go2rtc_url)
    if os.environ.get("DISABLE_BACKGROUND_WORKER") != "1":
        # Non-blocking background check for ONNX models
        asyncio.create_task(asyncio.to_thread(face_service.ensure_models_downloaded))
        guard_worker.start()
    yield
    await guard_worker.stop()
    recording_service.stop_all()


app = FastAPI(
    title="NeoGuard",
    description="100% Local AI Room Monitoring, Face Recognition & Speaker Greeting for TP-Link Tapo C200",
    version=__version__,
    lifespan=lifespan,
)


# -------------------- Pydantic Schemas --------------------

class CameraSettingsUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    brand_preset: Optional[str] = None
    host: Optional[str] = None
    rtsp_user: Optional[str] = None
    rtsp_pass: Optional[str] = None
    cloud_password: Optional[str] = None
    rtsp_port: Optional[int] = None
    onvif_port: Optional[int] = None
    onvif_profile: Optional[str] = None
    stream_quality: Optional[str] = None
    preview_stream: Optional[str] = None
    ptz_step: Optional[int] = None
    auto_tracking: Optional[bool] = None
    tracking_speed: Optional[float] = None
    tracking_return_home: Optional[bool] = None
    tracking_home_delay_sec: Optional[int] = None
    preset_names: Optional[Dict[str, str]] = None


class GuardSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    detection_interval_sec: Optional[float] = None
    cooldown_minutes: Optional[int] = None
    unknown_cooldown_minutes: Optional[int] = None
    similarity_threshold: Optional[float] = None
    default_greeting: Optional[str] = None
    unknown_greeting: Optional[str] = None
    greet_unknown: Optional[bool] = None
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    auto_privacy_after_greeting: Optional[bool] = None
    auto_tracking: Optional[bool] = None
    ai_overlay: Optional[bool] = None
    tracking_speed: Optional[float] = None
    tracking_return_home: Optional[bool] = None
    tracking_home_delay_sec: Optional[int] = None
    preset_names: Optional[Dict[str, str]] = None
    tts_voice: Optional[str] = None
    tts_volume: Optional[int] = None


class PersonCreate(BaseModel):
    name: str
    greeting: Optional[str] = ""
    cooldown_minutes: Optional[int] = None
    enabled: bool = True


class PersonUpdate(BaseModel):
    name: Optional[str] = None
    greeting: Optional[str] = None
    cooldown_minutes: Optional[int] = None
    enabled: Optional[bool] = None


class PTZRequest(BaseModel):
    direction: str
    step: Optional[int] = None
    camera_id: Optional[str] = None


class PrivacyRequest(BaseModel):
    enabled: bool
    camera_id: Optional[str] = None


class HardwareControlRequest(BaseModel):
    control: str
    value: Any = None
    camera_id: Optional[str] = None


class PresetRequest(BaseModel):
    preset_id: str
    camera_id: Optional[str] = None


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_username: Optional[str] = None
    new_password: str


# -------------------- Auth Middleware & Helpers --------------------

SESSION_COOKIE_NAME = "neoguard_session"
SESSION_TTL_SEC = 30 * 24 * 3600  # 30 days
PUBLIC_API_PATHS = frozenset({"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"})


def _sign_session_token(username: str) -> str:
    exp = int(time.time()) + SESSION_TTL_SEC
    payload = f"{username}:{exp}"
    secret = store.get_session_secret().encode("utf-8")
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_session_token(token: Optional[str]) -> Optional[str]:
    if not token or not store.get_auth_info().get("enabled", True):
        return store.get_auth_info().get("username", "admin") if not store.get_auth_info().get("enabled", True) else None
    parts = token.rsplit(":", 2)
    if len(parts) != 3:
        return None
    username, exp_str, sig = parts
    try:
        if int(exp_str) < int(time.time()):
            return None
    except ValueError:
        return None
    payload = f"{username}:{exp_str}"
    secret = store.get_session_secret().encode("utf-8")
    expected_sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected_sig):
        return username
    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
        if store.get_auth_info().get("enabled", True):
            token = request.cookies.get(SESSION_COOKIE_NAME)
            if not _verify_session_token(token):
                return JSONResponse(status_code=401, content={"detail": "Nicht angemeldet."})
    return await call_next(request)


# -------------------- Auth Endpoints --------------------

@app.get("/api/auth/status")
def get_auth_status(request: Request) -> Dict[str, Any]:
    auth_info = store.get_auth_info()
    if not auth_info.get("enabled", True):
        return {"authenticated": True, "auth_enabled": False, "username": auth_info.get("username", "admin")}
    user = _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    return {
        "authenticated": user is not None,
        "auth_enabled": True,
        "username": user or "",
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response) -> Dict[str, Any]:
    if not store.verify_login(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Benutzername oder Passwort falsch.")
    token = _sign_session_token(payload.username.strip())
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SEC,
        path="/",
    )
    return {"ok": True, "username": payload.username.strip()}


@app.post("/api/auth/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(payload: PasswordChangeRequest, request: Request, response: Response) -> Dict[str, Any]:
    current_user = _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME)) or store.get_auth_info().get("username", "admin")
    if not store.verify_login(current_user, payload.current_password):
        raise HTTPException(status_code=401, detail="Aktuelles Passwort ist falsch.")
    if len(payload.new_password) < 4:
        raise HTTPException(status_code=400, detail="Neues Passwort muss mindestens 4 Zeichen haben.")
    info = store.update_credentials(payload.new_username, payload.new_password)
    new_token = _sign_session_token(info["username"])
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SEC,
        path="/",
    )
    return {"ok": True, "username": info["username"]}


# -------------------- API Endpoints --------------------

@app.get("/api/health")
def get_health() -> Dict[str, Any]:
    cfg = store.get_config(mask_secrets=True)
    return {
        "status": "ok",
        "version": __version__,
        "face_engine": face_service.engine_name,
        "available_voices": list(PIPER_VOICES.keys()),
        "guard_enabled": cfg["guard"]["enabled"],
        "enrolled_persons": len(cfg["persons"]),
    }


def _mask_camera(cam: Dict[str, Any]) -> Dict[str, Any]:
    masked = dict(cam)
    masked["rtsp_pass"] = "••••••••" if masked.get("rtsp_pass") else ""
    masked["cloud_password"] = "••••••••" if masked.get("cloud_password") else ""
    return masked


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    cfg = store.get_config(mask_secrets=True)
    cfg["cameras"] = [_mask_camera(c) for c in cfg.get("cameras", [])]
    cfg["camera"] = cfg["cameras"][0] if cfg["cameras"] else _mask_camera(cfg["camera"])
    cfg["system"] = {
        "version": __version__,
        "face_engine": face_service.engine_name,
        "available_voices": list(PIPER_VOICES.keys()),
    }
    return cfg


@app.get("/api/cameras")
def list_cameras() -> Dict[str, Any]:
    return {"cameras": [_mask_camera(c) for c in store.list_cameras()]}


@app.post("/api/cameras")
def create_camera(payload: CameraSettingsUpdate) -> Dict[str, Any]:
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    created = store.add_camera(data)
    raw_cfg = store.get_raw_config()
    camera_manager.get_service(created["id"]).sync_go2rtc_config(
        created, go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984")
    )
    return {"ok": True, "camera": _mask_camera(created)}


@app.put("/api/cameras/{camera_id}")
def update_camera_by_id(camera_id: str, payload: CameraSettingsUpdate) -> Dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = store.update_camera(updates, camera_id=camera_id)
    raw_cfg = store.get_raw_config()
    svc = camera_manager.get_service(updated["id"])
    if not updated.get("enabled", True):
        svc.stop_reader()
    svc.sync_go2rtc_config(
        updated, go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984")
    )
    return {"ok": True, "camera": _mask_camera(updated)}


@app.delete("/api/cameras/{camera_id}")
def delete_camera_by_id(camera_id: str) -> Dict[str, Any]:
    if not store.delete_camera(camera_id):
        raise HTTPException(status_code=400, detail="Mindestens eine Kamera muss erhalten bleiben.")
    camera_manager.remove_service(camera_id)
    return {"ok": True}


@app.put("/api/config/camera")
def update_camera_config(payload: CameraSettingsUpdate, camera_id: Optional[str] = None) -> Dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = store.update_camera(updates, camera_id=camera_id)
    raw_cfg = store.get_raw_config()
    camera_manager.get_service(updated["id"]).sync_go2rtc_config(
        updated, go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984")
    )
    return {"ok": True, "camera": _mask_camera(updated)}


@app.put("/api/config/guard")
def update_guard_config(payload: GuardSettingsUpdate) -> Dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = store.update_guard(updates)
    if "ai_overlay" in updated:
        for cam in store.list_cameras():
            camera_manager.get_service(cam.get("id")).set_ai_overlay_enabled(bool(updated["ai_overlay"]))
    return {"ok": True, "guard": updated}


@app.get("/api/camera/status")
def get_camera_status(camera_id: Optional[str] = None) -> Dict[str, Any]:
    cam = store.get_camera(camera_id)
    return camera_manager.get_service(cam.get("id")).get_camera_status(cam)


@app.get("/api/camera/snapshot.jpg")
def get_camera_snapshot(camera_id: Optional[str] = None) -> Response:
    cam = store.get_camera(camera_id)
    frame = camera_manager.get_service(cam.get("id")).get_snapshot_or_placeholder(cam)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(status_code=500, detail="Snapshot encoding failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/camera/stream.mjpg")
def get_camera_mjpeg_stream(camera_id: Optional[str] = None) -> StreamingResponse:
    cam = store.get_camera(camera_id)
    cid = cam.get("id")
    return StreamingResponse(
        camera_manager.get_service(cid).mjpeg_generator(lambda: store.get_camera(cid)),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/api/camera/ptz")
def move_camera_ptz(payload: PTZRequest) -> Dict[str, Any]:
    cam = store.get_camera(payload.camera_id)
    return camera_manager.get_service(cam.get("id")).move_ptz(cam, payload.direction, payload.step)


@app.post("/api/camera/ptz/start")
def start_camera_ptz(payload: PTZRequest) -> Dict[str, Any]:
    cam = store.get_camera(payload.camera_id)
    return camera_manager.get_service(cam.get("id")).start_ptz_continuous(cam, payload.direction)


@app.post("/api/camera/ptz/stop")
def stop_camera_ptz(camera_id: Optional[str] = None) -> Dict[str, Any]:
    cam = store.get_camera(camera_id)
    return camera_manager.get_service(cam.get("id")).stop_ptz(cam)


@app.post("/api/camera/privacy")
def toggle_camera_privacy(payload: PrivacyRequest) -> Dict[str, Any]:
    cam = store.get_camera(payload.camera_id)
    return camera_manager.get_service(cam.get("id")).set_privacy_mode(cam, payload.enabled)


@app.get("/api/camera/hardware")
def get_camera_hardware_state(camera_id: Optional[str] = None) -> Dict[str, Any]:
    cam = store.get_camera(camera_id)
    return camera_manager.get_service(cam.get("id")).get_hardware_state(cam)


@app.post("/api/camera/hardware")
def set_camera_hardware(payload: HardwareControlRequest) -> Dict[str, Any]:
    cam = store.get_camera(payload.camera_id)
    raw_cfg = store.get_raw_config()
    if payload.control == "siren":
        return tts_service.play_siren_on_camera(
            camera_cfg=cam,
            go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984"),
        )
    return camera_manager.get_service(cam.get("id")).set_hardware_control(cam, payload.control, payload.value)


@app.post("/api/camera/preset")
def goto_camera_preset(payload: PresetRequest) -> Dict[str, Any]:
    cam = store.get_camera(payload.camera_id)
    return camera_manager.get_service(cam.get("id")).goto_preset(cam, payload.preset_id)


@app.post("/api/camera/preset/save")
def save_camera_preset(payload: PresetRequest) -> Dict[str, Any]:
    cam = store.get_camera(payload.camera_id)
    return camera_manager.get_service(cam.get("id")).save_preset(cam, payload.preset_id)


# -------------------- Persons & Face Enrollment --------------------

@app.get("/api/persons")
def list_persons() -> Dict[str, Any]:
    return {"persons": store.list_persons(include_embeddings=False)}


@app.post("/api/persons")
def create_person(payload: PersonCreate) -> Dict[str, Any]:
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Name darf nicht leer sein.")
    person = store.add_person(
        name=payload.name,
        greeting=payload.greeting or "",
        cooldown_minutes=payload.cooldown_minutes,
        enabled=payload.enabled,
    )
    return {"ok": True, "person": person}


@app.put("/api/persons/{person_id}")
def update_person(person_id: str, payload: PersonUpdate) -> Dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = store.update_person(person_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Person nicht gefunden.")
    return {"ok": True, "person": updated}


@app.delete("/api/persons/{person_id}")
def delete_person(person_id: str) -> Dict[str, Any]:
    deleted = store.delete_person(person_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Person nicht gefunden.")
    return {"ok": True}


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB limit for face photo uploads


@app.post("/api/persons/{person_id}/photo")
async def upload_person_photo(person_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    person = store.get_person(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person nicht gefunden.")

    raw_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Bilddatei zu groß (max. 10 MB).")
    np_arr = np.frombuffer(raw_bytes, np.uint8)
    img_bgr = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if img_bgr is None or img_bgr.size == 0:
        raise HTTPException(status_code=400, detail="Ungültige Bilddatei.")

    embedding, bbox = face_service.compute_embedding_for_enrollment(img_bgr)
    filename = f"{person_id}_{uuid.uuid4().hex[:6]}.jpg"
    save_path = store.faces_dir / filename
    cv2.imwrite(str(save_path), img_bgr)

    updated = store.add_person_photo(person_id, filename, embedding)
    return {"ok": True, "person": updated, "filename": filename, "bbox": bbox}


@app.post("/api/persons/{person_id}/capture")
def capture_person_photo_from_camera(person_id: str, camera_id: Optional[str] = None) -> Dict[str, Any]:
    person = store.get_person(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person nicht gefunden.")

    cam = store.get_camera(camera_id)
    frame = camera_manager.get_service(cam.get("id")).capture_frame(cam, stream=cam.get("stream_quality", "stream1"))
    if frame is None:
        raise HTTPException(
            status_code=503,
            detail="Kein Live-Kamerabild verfügbar (Kamera offline oder Privacy-Mode aktiv).",
        )

    embedding, bbox = face_service.compute_embedding_for_enrollment(frame)
    filename = f"{person_id}_{uuid.uuid4().hex[:6]}.jpg"
    save_path = store.faces_dir / filename
    cv2.imwrite(str(save_path), frame)

    updated = store.add_person_photo(person_id, filename, embedding)
    return {"ok": True, "person": updated, "filename": filename, "bbox": bbox}


@app.delete("/api/persons/{person_id}/photo/{filename}")
def delete_person_photo(person_id: str, filename: str) -> Dict[str, Any]:
    updated = store.delete_person_photo(person_id, filename)
    if not updated:
        raise HTTPException(status_code=404, detail="Foto oder Person nicht gefunden.")
    return {"ok": True, "person": updated}


@app.get("/api/faces/{filename}")
def get_face_photo(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = store.faces_dir / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="Foto nicht gefunden.")
    return FileResponse(target, media_type="image/jpeg")


# -------------------- TTS & Speaker Greeting --------------------

@app.post("/api/tts/speak")
def speak_text_on_camera(payload: SpeakRequest) -> Dict[str, Any]:
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Text darf nicht leer sein.")
    raw_cfg = store.get_raw_config()
    voice_name = payload.voice or raw_cfg["guard"].get("tts_voice", "de_DE-thorsten-medium")
    volume_pct = int(raw_cfg["guard"].get("tts_volume", 100))
    formatted_text = format_greeting_template(payload.text.strip(), name="Daniel")
    res = tts_service.speak_on_camera(
        text=formatted_text,
        camera_cfg=raw_cfg["camera"],
        voice_name=voice_name,
        go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984"),
        volume_percent=volume_pct,
    )
    store.add_event(
        person_name="Manuelle Durchsage",
        person_id=None,
        confidence=1.0,
        greeting_text=formatted_text,
        audio_sent=bool(res.get("ok")),
        event_type="manual_tts",
    )
    return res


@app.get("/api/tts/last.wav")
def get_last_tts_wav() -> FileResponse:
    if not tts_service.last_wav_path.exists():
        tts_service.synthesize_to_wav("Hallo! NeoGuard ist bereit.")
    return FileResponse(tts_service.last_wav_path, media_type="audio/wav")


# -------------------- Manual Scan & Events --------------------

@app.post("/api/guard/scan-now")
def trigger_manual_scan(camera_id: Optional[str] = None) -> Dict[str, Any]:
    cam = store.get_camera(camera_id)
    svc = camera_manager.get_service(cam.get("id"))
    frame = svc.capture_frame(cam, stream=cam.get("stream_quality", "stream1"))
    if frame is None:
        return {
            "ok": False,
            "error": "Kein Live-RTSP-Frame von der Tapo C200 empfangen. Prüfe IP & Kamera-Konto.",
            "detections": [],
        }
    outcomes = guard_worker.analyze_and_greet_frame(frame, force_greet=True, camera_cfg=cam, cam_service=svc)
    return {"ok": True, "detections": outcomes}


@app.get("/api/events")
def list_events(limit: int = 50) -> Dict[str, Any]:
    return {"events": store.list_events(limit=limit)}


@app.delete("/api/events")
def clear_events() -> Dict[str, Any]:
    store.clear_events()
    return {"ok": True}


@app.get("/api/snapshots/{filename}")
def get_event_snapshot(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = store.snapshots_dir / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="Snapshot nicht gefunden.")
    return FileResponse(target, media_type="image/jpeg")


# -------------------- Weekly Schedule & Recordings --------------------


class ScheduleUpdatePayload(BaseModel):
    weekly_schedule: Optional[Dict[str, Any]] = None
    recording: Optional[Dict[str, Any]] = None


@app.get("/api/schedule")
def get_schedule() -> Dict[str, Any]:
    data = store.get_schedule()
    data["storage"] = store.get_recording_storage_stats()
    return data


@app.put("/api/schedule")
def update_schedule(payload: ScheduleUpdatePayload) -> Dict[str, Any]:
    data = store.update_schedule(
        weekly_schedule=payload.weekly_schedule,
        recording=payload.recording,
    )
    data["storage"] = store.get_recording_storage_stats()
    return {"ok": True, **data}


@app.get("/api/recordings")
def list_recordings(date: Optional[str] = None, camera_id: Optional[str] = None) -> Dict[str, Any]:
    items = store.list_recordings(date_str=date, camera_id=camera_id)
    return {
        "recordings": items,
        "storage": store.get_recording_storage_stats(),
    }


@app.get("/api/recordings/{rec_id}/video")
def stream_recording_video(rec_id: str) -> FileResponse:
    rec = store.get_recording(rec_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Aufnahme nicht gefunden.")
    rel_path = rec.get("rel_path") or ""
    target = (store.recordings_dir / rel_path).resolve()
    if not str(target).startswith(str(store.recordings_dir.resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="Videodatei nicht gefunden.")
    return FileResponse(target, media_type="video/mp4")


@app.delete("/api/recordings/{rec_id}")
def delete_recording(rec_id: str) -> Dict[str, Any]:
    if not store.delete_recording(rec_id):
        raise HTTPException(status_code=404, detail="Aufnahme nicht gefunden.")
    return {"ok": True, "storage": store.get_recording_storage_stats()}


# -------------------- Static Web-UI --------------------

if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
