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
from app.camera_service import TapoCameraService
from app.config import ConfigStore
from app.face_service import LocalFaceService
from app.guard_worker import RoomGuardWorker, format_greeting_template
from app.tts_service import PIPER_VOICES, LocalTTSService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tapo_room_guard")

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

store = ConfigStore(data_dir=DATA_DIR)
camera_service = TapoCameraService(data_dir=store.data_dir)
face_service = LocalFaceService(models_dir=store.models_dir)
tts_service = LocalTTSService(models_dir=store.models_dir)
guard_worker = RoomGuardWorker(
    store=store,
    camera_service=camera_service,
    face_service=face_service,
    tts_service=tts_service,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    raw_cfg = store.get_raw_config()
    camera_service.sync_go2rtc_config(
        raw_cfg["camera"], go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984")
    )
    if os.environ.get("DISABLE_BACKGROUND_WORKER") != "1":
        # Non-blocking background check for ONNX models
        asyncio.create_task(asyncio.to_thread(face_service.ensure_models_downloaded))
        guard_worker.start()
    yield
    await guard_worker.stop()


app = FastAPI(
    title="Tapo Room Guard",
    description="100% Local AI Room Monitoring, Face Recognition & Speaker Greeting for TP-Link Tapo C200",
    version=__version__,
    lifespan=lifespan,
)


# -------------------- Pydantic Schemas --------------------

class CameraSettingsUpdate(BaseModel):
    host: Optional[str] = None
    rtsp_user: Optional[str] = None
    rtsp_pass: Optional[str] = None
    cloud_password: Optional[str] = None
    rtsp_port: Optional[int] = None
    onvif_port: Optional[int] = None
    stream_quality: Optional[str] = None
    preview_stream: Optional[str] = None
    ptz_step: Optional[int] = None


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


class PrivacyRequest(BaseModel):
    enabled: bool


class HardwareControlRequest(BaseModel):
    control: str
    value: Any = None


class PresetRequest(BaseModel):
    preset_id: str


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

SESSION_COOKIE_NAME = "tapo_guard_session"
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


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    cfg = store.get_config(mask_secrets=True)
    cfg["camera"]["rtsp_pass"] = "••••••••" if cfg["camera"].get("rtsp_pass") else ""
    cfg["camera"]["cloud_password"] = "••••••••" if cfg["camera"].get("cloud_password") else ""
    cfg["system"] = {
        "version": __version__,
        "face_engine": face_service.engine_name,
        "available_voices": list(PIPER_VOICES.keys()),
    }
    return cfg


@app.put("/api/config/camera")
def update_camera_config(payload: CameraSettingsUpdate) -> Dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = store.update_camera(updates)
    raw_cfg = store.get_raw_config()
    camera_service.sync_go2rtc_config(
        raw_cfg["camera"], go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984")
    )
    masked = dict(updated)
    masked["rtsp_pass"] = "••••••••" if masked.get("rtsp_pass") else ""
    masked["cloud_password"] = "••••••••" if masked.get("cloud_password") else ""
    return {"ok": True, "camera": masked}


@app.put("/api/config/guard")
def update_guard_config(payload: GuardSettingsUpdate) -> Dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = store.update_guard(updates)
    if "ai_overlay" in updated:
        camera_service.set_ai_overlay_enabled(bool(updated["ai_overlay"]))
    return {"ok": True, "guard": updated}


@app.get("/api/camera/status")
def get_camera_status() -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.get_camera_status(raw_cfg["camera"])


@app.get("/api/camera/snapshot.jpg")
def get_camera_snapshot() -> Response:
    raw_cfg = store.get_raw_config()
    frame = camera_service.get_snapshot_or_placeholder(raw_cfg["camera"])
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(status_code=500, detail="Snapshot encoding failed")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/camera/stream.mjpg")
def get_camera_mjpeg_stream() -> StreamingResponse:
    return StreamingResponse(
        camera_service.mjpeg_generator(lambda: store.get_raw_config()["camera"]),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/api/camera/ptz")
def move_camera_ptz(payload: PTZRequest) -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.move_ptz(raw_cfg["camera"], payload.direction, payload.step)


@app.post("/api/camera/ptz/start")
def start_camera_ptz(payload: PTZRequest) -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.start_ptz_continuous(raw_cfg["camera"], payload.direction)


@app.post("/api/camera/ptz/stop")
def stop_camera_ptz() -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.stop_ptz(raw_cfg["camera"])


@app.post("/api/camera/privacy")
def toggle_camera_privacy(payload: PrivacyRequest) -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.set_privacy_mode(raw_cfg["camera"], payload.enabled)


@app.get("/api/camera/hardware")
def get_camera_hardware_state() -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.get_hardware_state(raw_cfg["camera"])


@app.post("/api/camera/hardware")
def set_camera_hardware(payload: HardwareControlRequest) -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    if payload.control == "siren":
        return tts_service.play_siren_on_camera(
            camera_cfg=raw_cfg["camera"],
            go2rtc_url=raw_cfg["guard"].get("go2rtc_url", "http://127.0.0.1:1984"),
        )
    return camera_service.set_hardware_control(raw_cfg["camera"], payload.control, payload.value)


@app.post("/api/camera/preset")
def goto_camera_preset(payload: PresetRequest) -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.goto_preset(raw_cfg["camera"], payload.preset_id)


@app.post("/api/camera/preset/save")
def save_camera_preset(payload: PresetRequest) -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    return camera_service.save_preset(raw_cfg["camera"], payload.preset_id)


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
def capture_person_photo_from_camera(person_id: str) -> Dict[str, Any]:
    person = store.get_person(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person nicht gefunden.")

    raw_cfg = store.get_raw_config()
    frame = camera_service.capture_frame(raw_cfg["camera"], stream="stream1")
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
        tts_service.synthesize_to_wav("Hallo! Tapo Room Guard ist bereit.")
    return FileResponse(tts_service.last_wav_path, media_type="audio/wav")


# -------------------- Manual Scan & Events --------------------

@app.post("/api/guard/scan-now")
def trigger_manual_scan() -> Dict[str, Any]:
    raw_cfg = store.get_raw_config()
    frame = camera_service.capture_frame(raw_cfg["camera"], stream="stream1")
    if frame is None:
        return {
            "ok": False,
            "error": "Kein Live-RTSP-Frame von der Tapo C200 empfangen. Prüfe IP & Kamera-Konto.",
            "detections": [],
        }
    outcomes = guard_worker.analyze_and_greet_frame(frame, force_greet=True)
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


# -------------------- Static Web-UI --------------------

if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
