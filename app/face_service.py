import logging
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("neoguard.face")

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)
YOLOV8N_URL = "https://huggingface.co/Kalray/yolov8/resolve/main/yolov8n.onnx"


class LocalFaceService:
    """100% Local AI Person (YOLOv8n) + Face Detection & Recognition (YuNet + SFace ONNX)."""

    def __init__(self, models_dir: Path) -> None:
        self.vision_dir = models_dir / "vision"
        self.vision_dir.mkdir(parents=True, exist_ok=True)
        self.yunet_path = self.vision_dir / "face_detection_yunet_2023mar.onnx"
        self.sface_path = self.vision_dir / "face_recognition_sface_2021dec.onnx"
        self.yolo_path = self.vision_dir / "yolov8n.onnx"
        self._detector = None
        self._recognizer = None
        self._yolo_session = None
        self._yolo_input_name = "images"
        self._haar_cascade = None
        self._infer_lock = threading.Lock()
        self._yolo_lock = threading.Lock()
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
            if not self.yolo_path.exists():
                logger.info("Downloading YOLOv8n ONNX person detector (~13 MB)...")
                urllib.request.urlretrieve(YOLOV8N_URL, str(self.yolo_path))
            self._init_engines()
            return self.engine_name == "yunet+sface-onnx"
        except Exception as exc:
            logger.warning("Could not download ONNX models (%s), using fallback engine.", exc)
            return False

    def _init_engines(self) -> None:
        if self.yolo_path.exists():
            try:
                import onnxruntime as ort

                self._yolo_session = ort.InferenceSession(
                    str(self.yolo_path), providers=["CPUExecutionProvider"]
                )
                self._yolo_input_name = self._yolo_session.get_inputs()[0].name
                logger.info("Initialized local YOLOv8n ONNX person detector.")
            except Exception as exc:
                logger.warning("Failed to initialize YOLOv8n ONNX: %s", exc)

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
                self.engine_name = (
                    "yolov8n+yunet+sface-onnx"
                    if self._yolo_session is not None
                    else "yunet+sface-onnx"
                )
                logger.info("Initialized local %s engine.", self.engine_name)
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
            with self._infer_lock:
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
                        landmarks = [
                            [int(face[4]), int(face[5])],
                            [int(face[6]), int(face[7])],
                            [int(face[8]), int(face[9])],
                            [int(face[10]), int(face[11])],
                            [int(face[12]), int(face[13])],
                        ]
                        results.append(
                            {
                                "bbox": [max(0, bx), max(0, by), max(1, bw), max(1, bh)],
                                "landmarks": landmarks,
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
        Pure motion-contour tracker (replaces HOG so static baskets/jackets are never tracked).
        Finds the largest genuinely moving object between consecutive frames.
        Returns (bbox_or_None, current_small_gray).
        """
        if image_bgr is None or image_bgr.size == 0:
            return None, prev_gray

        h, w = image_bgr.shape[:2]
        target_w = 480
        scale = target_w / float(w)
        small_h = max(1, int(h * scale))
        small = cv2.resize(image_bgr, (target_w, small_h), interpolation=cv2.INTER_AREA)
        cur_gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (11, 11), 0)

        if prev_gray is None or prev_gray.shape != cur_gray.shape:
            return None, cur_gray

        diff = cv2.absdiff(prev_gray, cur_gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        total_motion_ratio = float(np.count_nonzero(thresh)) / float(thresh.size)

        # Ignore static scenes (<0.8% changed pixels) and full-frame camera shake (>40%)
        if total_motion_ratio < 0.008 or total_motion_ratio > 0.40:
            return None, cur_gray

        # Merge nearby motion blobs into a single clean person silhouette
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 25))
        dilated = cv2.dilate(thresh, kernel, iterations=2)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, cur_gray

        min_area = (target_w * small_h) * 0.015  # at least 1.5% of frame area
        best_rect = None
        best_area = 0.0
        for cnt in contours:
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            area = float(rw * rh)
            if area >= min_area and area > best_area:
                best_area = area
                best_rect = (rx, ry, rw, rh)

        if best_rect is None:
            return None, cur_gray

        rx, ry, rw, rh = best_rect
        inv = 1.0 / scale
        return [int(rx * inv), int(ry * inv), int(rw * inv), int(rh * inv)], cur_gray

    @staticmethod
    def compute_motion_mask(
        image_bgr: np.ndarray,
        prev_gray: Optional[np.ndarray] = None,
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], float]:
        """Compute downscaled binary motion mask and overall motion ratio between frames."""
        if image_bgr is None or image_bgr.size == 0:
            return None, prev_gray, 0.0
        h, w = image_bgr.shape[:2]
        target_w = 480
        scale = target_w / float(w)
        small_h = max(1, int(h * scale))
        small = cv2.resize(image_bgr, (target_w, small_h), interpolation=cv2.INTER_AREA)
        cur_gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (11, 11), 0)
        if prev_gray is None or prev_gray.shape != cur_gray.shape:
            return None, cur_gray, 0.0
        diff = cv2.absdiff(prev_gray, cur_gray)
        _, thresh = cv2.threshold(diff, 22, 255, cv2.THRESH_BINARY)
        ratio = float(np.count_nonzero(thresh)) / float(thresh.size)
        return thresh, cur_gray, ratio

    @staticmethod
    def has_motion_near_bbox(
        thresh_mask: Optional[np.ndarray],
        image_shape: tuple,
        bbox: List[int],
        min_roi_ratio: float = 0.015,
    ) -> bool:
        """Check whether real pixel motion occurred inside/around a full-frame bbox."""
        if thresh_mask is None or len(bbox) < 4:
            return False
        fh, fw = image_shape[:2]
        mh, mw = thresh_mask.shape[:2]
        if fw <= 0 or fh <= 0:
            return False
        sx = mw / float(fw)
        sy = mh / float(fh)
        bx, by, bw, bh = bbox[:4]
        pad_x = int(bw * 0.4)
        pad_y = int(bh * 0.4)
        x1 = max(0, int((bx - pad_x) * sx))
        y1 = max(0, int((by - pad_y) * sy))
        x2 = min(mw, int((bx + bw + pad_x) * sx))
        y2 = min(mh, int((by + bh + pad_y) * sy))
        if x2 <= x1 or y2 <= y1:
            return False
        roi = thresh_mask[y1:y2, x1:x2]
        return (float(np.count_nonzero(roi)) / float(roi.size)) >= min_roi_ratio

    def detect_people_yolo(
        self,
        image_bgr: np.ndarray,
        conf_threshold: float = 0.45,
        nms_threshold: float = 0.45,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Stage 1 neural person detector using local YOLOv8n ONNX (COCO class 0 = person).
        Returns list of {"bbox": [x, y, w, h], "score": float} sorted by area desc,
        or None if YOLOv8n is not loaded.
        """
        if self._yolo_session is None:
            return None
        if image_bgr is None or image_bgr.size == 0:
            return []

        ih, iw = image_bgr.shape[:2]
        input_size = 640
        scale = min(input_size / float(iw), input_size / float(ih))
        nw, nh = int(round(iw * scale)), int(round(ih * scale))
        resized = cv2.resize(image_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)

        canvas = np.full((input_size, input_size, 3), 114, dtype=np.uint8)
        pad_x = (input_size - nw) // 2
        pad_y = (input_size - nh) // 2
        canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = resized

        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(rgb, (2, 0, 1))[np.newaxis, ...]

        with self._yolo_lock:
            outputs = self._yolo_session.run(None, {self._yolo_input_name: blob})

        # Output shape: (1, 84, 8400) -> transpose to (8400, 84)
        preds = np.squeeze(outputs[0]).T
        person_scores = preds[:, 4]  # class 0 = person
        mask = person_scores >= conf_threshold
        filtered = preds[mask]
        scores = person_scores[mask]

        if len(filtered) == 0:
            return []

        boxes = []
        confidences = []
        for row, sc in zip(filtered, scores):
            cx, cy, bw, bh = row[0], row[1], row[2], row[3]
            x1 = (cx - bw / 2.0 - pad_x) / scale
            y1 = (cy - bh / 2.0 - pad_y) / scale
            w_orig = bw / scale
            h_orig = bh / scale
            x1_c = max(0, min(iw - 1, int(x1)))
            y1_c = max(0, min(ih - 1, int(y1)))
            w_c = max(1, min(iw - x1_c, int(w_orig)))
            h_c = max(1, min(ih - y1_c, int(h_orig)))
            boxes.append([x1_c, y1_c, w_c, h_c])
            confidences.append(float(sc))

        indices = cv2.dnn.NMSBoxes(boxes, confidences, conf_threshold, nms_threshold)
        results: List[Dict[str, Any]] = []
        if len(indices) > 0:
            for idx in np.array(indices).flatten():
                results.append(
                    {
                        "bbox": boxes[int(idx)],
                        "score": round(confidences[int(idx)], 3),
                    }
                )
        results.sort(key=lambda r: r["bbox"][2] * r["bbox"][3], reverse=True)
        return results

    def detect_faces_in_crop(
        self,
        image_bgr: np.ndarray,
        roi_bbox: List[int],
        upscale: float = 2.2,
        upper_body_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Digital AI-Zoom: crop the target region (e.g. upper body of a distant moving person
        or an uncertain small face), upscale with cubic interpolation, and run YuNet + SFace.
        Maps detected bounding boxes and landmarks back to full-frame coordinates.
        """
        if image_bgr is None or image_bgr.size == 0 or len(roi_bbox) < 4:
            return []

        fh, fw = image_bgr.shape[:2]
        rx, ry, rw, rh = [int(v) for v in roi_bbox[:4]]
        if upper_body_only:
            rh = max(40, int(rh * 0.62))

        pad_x = int(rw * 0.25)
        pad_y = int(rh * 0.25)
        x1 = max(0, rx - pad_x)
        y1 = max(0, ry - pad_y)
        x2 = min(fw, rx + rw + pad_x)
        y2 = min(fh, ry + rh + pad_y)
        if (x2 - x1) < 32 or (y2 - y1) < 32:
            return []

        crop = image_bgr[y1:y2, x1:x2]
        zoomed = cv2.resize(
            crop,
            (max(1, int((x2 - x1) * upscale)), max(1, int((y2 - y1) * upscale))),
            interpolation=cv2.INTER_CUBIC,
        )
        dets = self.detect_and_embed(zoomed)
        mapped: List[Dict[str, Any]] = []
        for d in dets:
            bx, by, bw, bh = d["bbox"]
            new_bbox = [
                int(x1 + bx / upscale),
                int(y1 + by / upscale),
                max(1, int(bw / upscale)),
                max(1, int(bh / upscale)),
            ]
            new_lms = [
                [int(x1 + p[0] / upscale), int(y1 + p[1] / upscale)]
                for p in d.get("landmarks", [])
            ]
            mapped.append(
                {
                    "bbox": new_bbox,
                    "landmarks": new_lms,
                    "score": d["score"],
                    "embedding": d["embedding"],
                    "zoomed": True,
                }
            )
        return mapped
