FROM golang:1.23-alpine AS tapoctl-build
WORKDIR /src
COPY tapoctl/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /tapoctl .

FROM python:3.12-slim AS yolo-export
WORKDIR /export
RUN pip install --no-cache-dir ultralytics onnx onnxslim && \
    python3 -c "from ultralytics import YOLO; YOLO('yolo26n.pt').export(format='onnx', imgsz=640)"

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8095 \
    DATA_DIR=/app/data

WORKDIR /app

COPY --from=tapoctl-build /tapoctl /usr/local/bin/tapoctl

# Install system dependencies (FFmpeg for 8kHz pcm_alaw audio streaming + OpenCV runtime libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    libglib2.0-0 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

# Install go2rtc for native TP-Link Tapo C200 Two-Way Audio Backchannel
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then GO2RTC_BIN="go2rtc_linux_arm64"; else GO2RTC_BIN="go2rtc_linux_amd64"; fi && \
    curl -fsSL "https://github.com/AlexxIT/go2rtc/releases/latest/download/${GO2RTC_BIN}" -o /usr/local/bin/go2rtc && \
    chmod +x /usr/local/bin/go2rtc

# Install official standalone Piper TTS C++ binary & pre-bundle 100% offline Vision + German Voice models
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then PIPER_TAR="piper_linux_aarch64.tar.gz"; else PIPER_TAR="piper_linux_x86_64.tar.gz"; fi && \
    curl -fsSL "https://github.com/rhasspy/piper/releases/latest/download/${PIPER_TAR}" | tar -xz -C /opt && \
    ln -sf /opt/piper/piper /usr/local/bin/piper && \
    mkdir -p /opt/models/vision /opt/models/tts && \
    curl -fsSL "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
      -o /opt/models/vision/face_detection_yunet_2023mar.onnx && \
    curl -fsSL "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
      -o /opt/models/vision/face_recognition_sface_2021dec.onnx && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json" \
      -o /opt/models/tts/de_DE-thorsten-medium.onnx.json && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx" \
      -o /opt/models/tts/de_DE-thorsten-medium.onnx

COPY --from=yolo-export /export/yolo26n.onnx /opt/models/vision/yolo26n.onnx

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY public ./public
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh && chmod -R a+rX /app /opt/models

VOLUME ["/app/data"]
EXPOSE 30504

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-30504}/api/health" || exit 1'

ENTRYPOINT ["/app/docker-entrypoint.sh"]
