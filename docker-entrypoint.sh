#!/usr/bin/env bash
set -e

mkdir -p /app/data/models/vision /app/data/models/tts /app/data/faces /app/data/snapshots

# Copy pre-bundled offline AI & TTS models into persistent volume if not present yet
if [ -d "/opt/models/vision" ]; then
  cp -n /opt/models/vision/* /app/data/models/vision/ 2>/dev/null || true
fi
if [ -d "/opt/models/tts" ]; then
  cp -n /opt/models/tts/* /app/data/models/tts/ 2>/dev/null || true
fi

# Create initial go2rtc.yaml if missing
if [ ! -f "/app/data/go2rtc.yaml" ]; then
  cat <<EOF > /app/data/go2rtc.yaml
api:
  listen: ":1984"
rtsp:
  listen: ":8554"
streams:
  tapo_c200: []
EOF
fi

# Start go2rtc in background for Tapo C200 Two-Way Audio Backchannel
if command -v go2rtc >/dev/null 2>&1; then
  go2rtc -config /app/data/go2rtc.yaml &
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8095}"
