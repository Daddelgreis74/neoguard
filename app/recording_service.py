import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.camera_service import TapoCameraService
from app.config import ConfigStore

logger = logging.getLogger("neoguard.recording")


class RecordingService:
    """CPU-lightweight RTSP stream copy recorder (RAM ring buffer + event clips + continuous segments)."""

    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._procs: Dict[str, Dict[str, Any]] = {}
        self._last_clip_ts: Dict[str, float] = {}
        self._active_clip_jobs: set[str] = set()
        self._known_cont_files: set[str] = set()
        shm_root = Path("/dev/shm")
        if shm_root.exists() and os.access(shm_root, os.W_OK):
            self._ring_root = shm_root / "neoguard_rec"
        else:
            self._ring_root = self.store.data_dir / "tmp_rec"
        self._ring_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_cam_id(cam_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", str(cam_id or "cam_1"))

    def _get_ring_dir(self, cam_id: str) -> Path:
        p = self._ring_root / self._safe_cam_id(cam_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _stop_proc_unlocked(self, cam_id: str) -> None:
        info = self._procs.pop(cam_id, None)
        if not info:
            return
        proc: subprocess.Popen = info["proc"]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self) -> None:
        with self._lock:
            for cam_id in list(self._procs.keys()):
                self._stop_proc_unlocked(cam_id)

    def sync_camera_mode(self, camera_cfg: Dict[str, Any], mode: str, rec_cfg: Dict[str, Any]) -> None:
        """Ensure background ffmpeg recorder matches current schedule mode ('off', 'events', 'continuous')."""
        cam_id = str(camera_cfg.get("id") or "cam_1")
        enabled = bool(rec_cfg.get("enabled", True)) and bool(camera_cfg.get("enabled", True))
        target_mode = mode if (enabled and mode in ("events", "continuous")) else "off"

        rtsp_url = TapoCameraService.build_rtsp_url(
            camera_cfg, stream=camera_cfg.get("stream_quality", "stream1")
        )
        if not rtsp_url or not (camera_cfg.get("rtsp_user") and camera_cfg.get("rtsp_pass")):
            target_mode = "off"
        else:
            # Prefer local go2rtc RTSP restream so camera only serves one Wi-Fi stream
            rtsp_url = f"rtsp://127.0.0.1:8554/cam_{self._safe_cam_id(cam_id)}"

        with self._lock:
            cur = self._procs.get(cam_id)
            if target_mode == "off":
                if cur:
                    self._stop_proc_unlocked(cam_id)
                return

            if cur and cur["mode"] == target_mode and cur["rtsp_url"] == rtsp_url:
                if cur["proc"].poll() is None:
                    if target_mode == "events":
                        self._prune_ring_dir(cam_id)
                    elif target_mode == "continuous":
                        self._scan_continuous_files(camera_cfg)
                    return
                # Process exited unexpectedly -> restart
                self._procs.pop(cam_id, None)
            elif cur:
                self._stop_proc_unlocked(cam_id)

            if not shutil.which("ffmpeg"):
                return

            if target_mode == "events":
                ring_dir = self._get_ring_dir(cam_id)
                for old in ring_dir.glob("seg_*.ts"):
                    try:
                        old.unlink()
                    except OSError:
                        pass
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    rtsp_url,
                    "-c",
                    "copy",
                    "-f",
                    "segment",
                    "-segment_time",
                    "4",
                    "-reset_timestamps",
                    "1",
                    str(ring_dir / "seg_%06d.ts"),
                ]
            else:
                today = datetime.now().strftime("%Y-%m-%d")
                out_dir = self.store.recordings_dir / today / self._safe_cam_id(cam_id)
                out_dir.mkdir(parents=True, exist_ok=True)
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    rtsp_url,
                    "-c",
                    "copy",
                    "-f",
                    "segment",
                    "-segment_time",
                    "300",
                    "-strftime",
                    "1",
                    "-reset_timestamps",
                    "1",
                    "-segment_format_options",
                    "movflags=+faststart",
                    str(out_dir / "cont_%H-%M-%S.mp4"),
                ]

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                self._procs[cam_id] = {
                    "mode": target_mode,
                    "rtsp_url": rtsp_url,
                    "proc": proc,
                }
                logger.info("Started %s ffmpeg recorder for %s.", target_mode, cam_id)
            except Exception as exc:
                logger.warning("Failed to start ffmpeg recorder for %s: %s", cam_id, exc)

    def _prune_ring_dir(self, cam_id: str, max_age_sec: float = 45.0) -> None:
        ring_dir = self._get_ring_dir(cam_id)
        cutoff = time.time() - max_age_sec
        segs = sorted(ring_dir.glob("seg_*.ts"))
        for seg in segs[:-2]:
            try:
                if seg.stat().st_mtime < cutoff:
                    seg.unlink()
            except OSError:
                pass

    def _scan_continuous_files(self, camera_cfg: Dict[str, Any]) -> None:
        cam_id = str(camera_cfg.get("id") or "cam_1")
        cam_name = str(camera_cfg.get("name") or cam_id)
        safe_id = self._safe_cam_id(cam_id)
        now_ts = time.time()
        existing_paths = {r.get("rel_path") for r in self.store.list_recordings(camera_id=cam_id)}
        for mp4 in self.store.recordings_dir.glob(f"*/{safe_id}/cont_*.mp4"):
            rel = str(mp4.relative_to(self.store.recordings_dir)).replace("\\", "/")
            if rel in existing_paths or rel in self._known_cont_files:
                continue
            try:
                st = mp4.stat()
                # Only register finished segments (not modified in last 10s) and > 10 KB
                if (now_ts - st.st_mtime) < 10.0 or st.st_size < 10_240:
                    continue
                date_str = mp4.parent.parent.name
                start_dt = datetime.fromtimestamp(st.st_mtime - 300, tz=timezone.utc)
                end_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                entry = {
                    "id": str(uuid.uuid4())[:10],
                    "camera_id": cam_id,
                    "camera_name": cam_name,
                    "mode": "continuous",
                    "event_type": "continuous",
                    "person_name": "",
                    "date": date_str,
                    "start_ts": start_dt.isoformat(),
                    "end_ts": end_dt.isoformat(),
                    "local_hour": datetime.fromtimestamp(st.st_mtime).hour,
                    "local_minute": datetime.fromtimestamp(st.st_mtime).minute,
                    "duration_sec": 300,
                    "rel_path": rel,
                    "size_bytes": st.st_size,
                    "snapshot_url": None,
                }
                self.store.add_recording(entry)
                self._known_cont_files.add(rel)
            except OSError:
                pass

    def trigger_event_clip(
        self,
        camera_cfg: Dict[str, Any],
        event_type: str,
        person_name: str = "",
        snapshot_filename: Optional[str] = None,
        rec_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Assemble pre-roll + post-roll .ts segments from RAM ring buffer into an MP4 clip."""
        rec_cfg = rec_cfg or self.store.get_schedule()["recording"]
        if not rec_cfg.get("enabled", True):
            return
        if not shutil.which("ffmpeg"):
            return

        cam_id = str(camera_cfg.get("id") or "cam_1")
        post_roll = int(rec_cfg.get("post_roll_sec", 20))
        pre_roll = int(rec_cfg.get("pre_roll_sec", 5))
        now = time.time()

        with self._lock:
            if cam_id in self._active_clip_jobs:
                return
            if (now - self._last_clip_ts.get(cam_id, 0.0)) < max(10, post_roll):
                return
            self._last_clip_ts[cam_id] = now
            self._active_clip_jobs.add(cam_id)

        t = threading.Thread(
            target=self._build_event_clip_worker,
            args=(dict(camera_cfg), event_type, person_name, snapshot_filename, pre_roll, post_roll, rec_cfg),
            daemon=True,
        )
        t.start()

    def _build_event_clip_worker(
        self,
        camera_cfg: Dict[str, Any],
        event_type: str,
        person_name: str,
        snapshot_filename: Optional[str],
        pre_roll: int,
        post_roll: int,
        rec_cfg: Dict[str, Any],
    ) -> None:
        cam_id = str(camera_cfg.get("id") or "cam_1")
        cam_name = str(camera_cfg.get("name") or cam_id)
        ring_dir = self._get_ring_dir(cam_id)
        trigger_dt = datetime.now()
        trigger_utc = datetime.now(timezone.utc)
        trigger_ts = time.time()

        try:
            # Wait for post-roll, then collect each finalized segment ONCE in exact sequence order
            staging = ring_dir / f"job_{uuid.uuid4().hex[:8]}"
            staging.mkdir(parents=True, exist_ok=True)
            copied_names: set[str] = set()

            def snapshot_finalized_segments(include_latest: bool = False) -> None:
                cutoff = trigger_ts - max(4, pre_roll + 4)
                segs = sorted(ring_dir.glob("seg_*.ts"))
                candidates = segs if include_latest else segs[:-1]
                for seg in candidates:
                    if seg.name in copied_names:
                        continue
                    try:
                        st = seg.stat()
                        if st.st_mtime < cutoff or st.st_size < 1024:
                            continue
                        shutil.copy2(seg, staging / seg.name)
                        copied_names.add(seg.name)
                    except OSError:
                        pass

            snapshot_finalized_segments(include_latest=False)
            deadline = time.time() + post_roll
            while time.time() < deadline:
                time.sleep(2.0)
                snapshot_finalized_segments(include_latest=False)
            # Wait briefly for the currently writing segment to finalize
            time.sleep(1.5)
            snapshot_finalized_segments(include_latest=False)

            staged_files = sorted(staging.glob("seg_*.ts"))
            if not staged_files:
                shutil.rmtree(staging, ignore_errors=True)
                return

            date_str = trigger_dt.strftime("%Y-%m-%d")
            safe_id = self._safe_cam_id(cam_id)
            out_dir = self.store.recordings_dir / date_str / safe_id
            out_dir.mkdir(parents=True, exist_ok=True)

            rec_id = uuid.uuid4().hex[:10]
            stamp = trigger_dt.strftime("%H-%M-%S")
            out_mp4 = out_dir / f"event_{stamp}_{rec_id}.mp4"
            concat_file = staging / "concat.txt"
            concat_file.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in staged_files) + "\n",
                encoding="utf-8",
            )

            res = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-fflags",
                    "+genpts",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(out_mp4),
                ],
                capture_output=True,
                timeout=30,
            )
            shutil.rmtree(staging, ignore_errors=True)

            if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size < 4096:
                if out_mp4.exists():
                    out_mp4.unlink(missing_ok=True)
                return

            rel_path = str(out_mp4.relative_to(self.store.recordings_dir)).replace("\\", "/")
            duration = pre_roll + post_roll
            entry = {
                "id": rec_id,
                "camera_id": cam_id,
                "camera_name": cam_name,
                "mode": "events",
                "event_type": event_type,
                "person_name": person_name,
                "date": date_str,
                "start_ts": (trigger_utc - timedelta(seconds=pre_roll)).isoformat(),
                "end_ts": (trigger_utc + timedelta(seconds=post_roll)).isoformat(),
                "local_hour": trigger_dt.hour,
                "local_minute": trigger_dt.minute,
                "duration_sec": duration,
                "rel_path": rel_path,
                "size_bytes": out_mp4.stat().st_size,
                "snapshot_url": f"/api/snapshots/{snapshot_filename}" if snapshot_filename else None,
            }
            self.store.add_recording(entry)
            self.enforce_retention(
                retention_days=int(rec_cfg.get("retention_days", 14)),
                max_storage_gb=int(rec_cfg.get("max_storage_gb", 100)),
            )
            logger.info("Saved event clip %s (%s, %.1f MB)", rel_path, event_type, entry["size_bytes"] / (1024 * 1024))
        except Exception as exc:
            logger.warning("Event clip assembly failed for %s: %s", cam_id, exc)
        finally:
            with self._lock:
                self._active_clip_jobs.discard(cam_id)

    def enforce_retention(self, retention_days: int = 14, max_storage_gb: int = 100) -> int:
        """Delete recordings older than retention_days or exceeding max_storage_gb."""
        deleted = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        items = self.store.list_recordings()

        # 1. Age-based pruning
        for item in list(items):
            try:
                dt = datetime.fromisoformat(item.get("start_ts", ""))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    if self.store.delete_recording(item["id"]):
                        deleted += 1
            except Exception:
                pass

        # 2. Size-based pruning (oldest last in list -> delete from tail)
        max_bytes = max(1, max_storage_gb) * 1024 * 1024 * 1024
        items = self.store.list_recordings()
        total_bytes = sum(int(r.get("size_bytes") or 0) for r in items)
        for item in reversed(items):
            if total_bytes <= max_bytes:
                break
            sz = int(item.get("size_bytes") or 0)
            if self.store.delete_recording(item["id"]):
                total_bytes -= sz
                deleted += 1
        return deleted
