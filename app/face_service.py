import logging
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("tapo_room_guard.face")

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)


class LocalFaceService:
    """100% Local Face Detection and Recognition using OpenCV YuNet + SFace ONNX (with Haar fallback)."""

    def __init__(self, models_dir: Path) -> None:
        self.vision_dir = models_dir / "vision"
        self.vision_dir.mkdir(parents=True, exist_ok=True)
        self.yunet_path = self.vision_dir / "face_detection_yunet_2023mar.onnx"
        self.sface_path = self.vision_dir / "face_recognition_sface_2021dec.onnx"
        self._detector = None
        self._recognizer = None
        self._haar_cascade = None
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.engine_name = "opencv-fallback"
        self._init_engines()

    def ensure_models_downloaded(self) -> bool:
        """Download YuNet and SFace ONNX models if not present yet."""
        try:
            if not self.yunet_path.exists():
                logger.info("Downloading YuNet ONNX face detector (~230 KB)...")
                urllib.request.urlretrieve(YUNET_URL, str(self.yunet_path))
            if not self.sface_path.exists():
                logger.info("Downloading SFace ONNX face recognizer (~37 MB)...")
                urllib.request.urlretrieve(SFACE_URL, str(self.sface_path))
            self._init_engines()
            return self.engine_name == "yunet+sface-onnx"
        except Exception as exc:
            logger.warning("Could not download ONNX models (%s), using fallback engine.", exc)
            return False

    def _init_engines(self) -> None:
        if self.yunet_path.exists() and self.sface_path.exists():
            try:
                self._detector = cv2.FaceDetectorYN.create(
                    str(self.yunet_path),
                    "",
                    (320, 320),
                    score_threshold=0.65,
                    nms_threshold=0.3,
                    top_k=5000,
                )
                self._recognizer = cv2.FaceRecognizerSF.create(
                    str(self.sface_path), ""
                )
                self.engine_name = "yunet+sface-onnx"
                logger.info("Initialized local YuNet + SFace ONNX engine.")
                return
            except Exception as exc:
                logger.warning("Failed to initialize ONNX face models: %s", exc)

        # Fallback: Built-in OpenCV Haar Cascade
        haar_file = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._haar_cascade = cv2.CascadeClassifier(haar_file)
        self.engine_name = "opencv-haar-fallback"

    def detect_and_embed(self, image_bgr: np.ndarray) -> List[Dict[str, Any]]:
        """
        Detect faces in a BGR image and compute a 128-D normalized embedding for each face.
        Returns list of dicts: {"bbox": [x, y, w, h], "score": float, "embedding": List[float]}.
        """
        if image_bgr is None or image_bgr.size == 0:
            return []

        h, w = image_bgr.shape[:2]
        results: List[Dict[str, Any]] = []

        if self._detector is not None and self._recognizer is not None:
            self._detector.setInputSize((w, h))
            _, faces = self._detector.detect(image_bgr)
            if faces is not None:
                for face in faces:
                    bx, by, bw, bh = int(face[0]), int(face[1]), int(face[2]), int(face[3])
                    score = float(face[-1])
                    aligned = self._recognizer.alignCrop(image_bgr, face)
                    feat = self._recognizer.feature(aligned)
                    feat_norm = feat.flatten()
                    norm_val = float(np.linalg.norm(feat_norm))
                    if norm_val > 1e-8:
                        feat_norm = feat_norm / norm_val
                    results.append(
                        {
                            "bbox": [max(0, bx), max(0, by), max(1, bw), max(1, bh)],
                            "score": round(score, 3),
                            "embedding": [round(float(x), 6) for x in feat_norm.tolist()],
                        }
                    )
            return results

        # Fallback Haar Cascade + 128-D normalized spatial-gradient/intensity descriptor
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        rects = self._haar_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(48, 48)
        )
        for (bx, by, bw, bh) in rects:
            crop = gray[by : by + bh, bx : bx + bw]
            embedding = self._compute_fallback_embedding(crop)
            results.append(
                {
                    "bbox": [int(bx), int(by), int(bw), int(bh)],
                    "score": 0.85,
                    "embedding": embedding,
                }
            )
        return results

    def compute_embedding_for_enrollment(self, image_bgr: np.ndarray) -> Tuple[List[float], List[int]]:
        """
        Extract primary face embedding from an enrollment photo.
        If no face detector box triggers (e.g. tight crop), computes embedding on center crop.
        """
        detections = self.detect_and_embed(image_bgr)
        if detections:
            # Pick largest face
            best = max(detections, key=lambda d: d["bbox"][2] * d["bbox"][3])
            return best["embedding"], best["bbox"]

        # Center crop fallback so enrollment of pre-cropped portraits always succeeds
        h, w = image_bgr.shape[:2]
        if self._recognizer is not None:
            resized = cv2.resize(image_bgr, (112, 112))
            feat = self._recognizer.feature(resized).flatten()
            norm_val = float(np.linalg.norm(feat))
            if norm_val > 1e-8:
                feat = feat / norm_val
            return [round(float(x), 6) for x in feat.tolist()], [0, 0, w, h]

        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        return self._compute_fallback_embedding(gray), [0, 0, w, h]

    @staticmethod
    def _compute_fallback_embedding(gray_crop: np.ndarray) -> List[float]:
        resized = cv2.resize(gray_crop, (16, 8), interpolation=cv2.INTER_AREA)
        eq = cv2.equalizeHist(resized).astype(np.float32).flatten()
        eq -= float(np.mean(eq))
        norm = float(np.linalg.norm(eq))
        if norm > 1e-8:
            eq /= norm
        return [round(float(x), 6) for x in eq.tolist()]

    @staticmethod
    def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        a = np.asarray(vec_a, dtype=np.float32)
        b = np.asarray(vec_b, dtype=np.float32)
        if a.shape != b.shape or a.size == 0:
            return 0.0
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom < 1e-8:
            return 0.0
        return float(np.dot(a, b) / denom)

    def match_person(
        self,
        face_embedding: List[float],
        persons: List[Dict[str, Any]],
        threshold: float = 0.38,
    ) -> Tuple[Optional[Dict[str, Any]], float]:
        """
        Compare a detected face embedding against all enrolled persons.
        Returns (matched_person_or_None, best_similarity_score).
        """
        best_person: Optional[Dict[str, Any]] = None
        best_score: float = -1.0

        for person in persons:
            if not person.get("enabled", True):
                continue
            for ref_emb in person.get("embeddings", []):
                sim = self.cosine_similarity(face_embedding, ref_emb)
                if sim > best_score:
                    best_score = sim
                    best_person = person

        if best_person is not None and best_score >= threshold:
            return best_person, round(best_score, 3)
        return None, round(max(0.0, best_score), 3)

    def detect_largest_person_bbox(
        self,
        image_bgr: np.ndarray,
        prev_gray: Optional[np.ndarray] = None,
    ) -> Tuple[Optional[List[int]], Optional[np.ndarray]]:
        """
        Motion-gated full-body person detector using OpenCV HOG.
        Requires both high HOG confidence (>= 0.85) AND real pixel motion inside the bbox
        so static objects (e.g. hanging coats/jackets) never trigger false tracking.
        Returns (bbox_or_None, current_small_gray).
        """
        if image_bgr is None or image_bgr.size == 0:
            return None, prev_gray

        h, w = image_bgr.shape[:2]
        target_w = 480
        scale = target_w / float(w)
        small = cv2.resize(image_bgr, (target_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        cur_gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (9, 9), 0)

        # If we have a previous frame, require real motion before running HOG
        if prev_gray is None or prev_gray.shape != cur_gray.shape:
            return None, cur_gray

        diff = cv2.absdiff(prev_gray, cur_gray)
        _, thresh = cv2.threshold(diff, 22, 255, cv2.THRESH_BINARY)
        total_motion_ratio = float(np.count_nonzero(thresh)) / float(thresh.size)
        # Ignore completely static scenes (<0.4% changed pixels) and full-frame camera shake (>45%)
        if total_motion_ratio < 0.004 or total_motion_ratio > 0.45:
            return None, cur_gray

        rects, weights = self._hog.detectMultiScale(
            small,
            winStride=(8, 8),
            padding=(8, 8),
            scale=1.08,
        )
        if len(rects) == 0:
            return None, cur_gray

        best_idx = -1
        best_val = -1.0
        for idx, (rx, ry, rw, rh) in enumerate(rects):
            wt = float(weights[idx]) if idx < len(weights) else 0.0
            if wt < 0.85:
                continue
            # Require at least 3% moving pixels inside this specific person bounding box
            roi = thresh[max(0, ry) : ry + rh, max(0, rx) : rx + rw]
            if roi.size == 0:
                continue
            roi_motion = float(np.count_nonzero(roi)) / float(roi.size)
            if roi_motion < 0.03:
                continue
            val = (rw * rh) * wt
            if val > best_val:
                best_val = val
                best_idx = idx

        if best_idx < 0:
            return None, cur_gray
        rx, ry, rw, rh = rects[best_idx]
        inv = 1.0 / scale
        return [int(rx * inv), int(ry * inv), int(rw * inv), int(rh * inv)], cur_gray
