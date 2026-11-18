(() => {
  let currentConfig = null;
  let privacyEnabled = false;

  let statusTimer = null;
  const showStatus = (msg, type = "info") => {
    const el = document.getElementById("statusAlert");
    if (!el) return;
    el.className = `alert alert-${type} py-2 px-3 small rounded-3 floating-status-toast`;
    el.innerHTML = `<i class="fa-solid fa-circle-info me-2"></i>${msg}`;
    el.classList.remove("d-none");
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(() => el.classList.add("d-none"), 4000);
  };

  const escapeHtml = (str) =>
    String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const formatTime = (isoStr) => {
    if (!isoStr) return "Noch nie";
    try {
      const dt = new Date(isoStr);
      return dt.toLocaleString("de-DE", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    } catch {
      return isoStr;
    }
  };

  // -------------------- Theme Toggle --------------------
  const initTheme = () => {
    const saved = localStorage.getItem("tapo_guard_theme") || "dark";
    document.documentElement.setAttribute("data-bs-theme", saved);
    const icon = document.getElementById("themeIcon");
    if (icon) icon.className = saved === "dark" ? "fa-solid fa-sun" : "fa-solid fa-moon";
  };

  document.getElementById("btnThemeToggle")?.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-bs-theme") || "dark";
    const next = cur === "dark" ? "light" : "dark";
    localStorage.setItem("tapo_guard_theme", next);
    initTheme();
  });

  // -------------------- Load Config, Persons & Events --------------------
  const loadConfig = async () => {
    try {
      const res = await fetch("/api/config");
      const data = await res.json();
      currentConfig = data;

      const cam = data.camera || {};
      const guard = data.guard || {};
      const sys = data.system || {};

      document.getElementById("cameraIpSubtitle").textContent =
        `IP: ${cam.host || "Nicht gesetzt"} · RTSP Port ${cam.rtsp_port || 554} · ONVIF Port ${cam.onvif_port || 2020}`;
      document.getElementById("engineBadge").textContent =
        `100% Lokal (${sys.face_engine || "OpenCV ONNX"} & Piper TTS)`;

      const badge = document.getElementById("guardActiveBadge");
      if (guard.enabled) {
        badge.className = "badge rounded-pill text-bg-success px-3 py-2";
        badge.innerHTML = `<i class="fa-solid fa-shield-halved me-1"></i>Überwachung Aktiv (${guard.active_hours_start}–${guard.active_hours_end})`;
      } else {
        badge.className = "badge rounded-pill text-bg-secondary px-3 py-2";
        badge.innerHTML = `<i class="fa-solid fa-pause me-1"></i>Überwachung Pausiert`;
      }

      // Populate Settings Modal
      document.getElementById("cfgHost").value = cam.host || "";
      document.getElementById("cfgRtspPort").value = cam.rtsp_port || 554;
      document.getElementById("cfgOnvifPort").value = cam.onvif_port || 2020;
      document.getElementById("cfgRtspUser").value = cam.rtsp_user || "";
      document.getElementById("cfgRtspPass").value = cam.rtsp_pass || "";
      document.getElementById("cfgCloudPass").value = cam.cloud_password || "";

      document.getElementById("cfgGuardEnabled").value = String(Boolean(guard.enabled));
      document.getElementById("cfgInterval").value = guard.detection_interval_sec ?? 2.0;
      document.getElementById("cfgCooldown").value = guard.cooldown_minutes ?? 120;
      document.getElementById("cfgThreshold").value = guard.similarity_threshold ?? 0.38;
      document.getElementById("cfgHoursStart").value = guard.active_hours_start || "06:00";
      document.getElementById("cfgHoursEnd").value = guard.active_hours_end || "23:00";
      document.getElementById("cfgVoice").value = guard.tts_voice || "de_DE-thorsten-medium";
      document.getElementById("cfgDefaultGreeting").value = guard.default_greeting || "";
      document.getElementById("cfgUnknownGreeting").value = guard.unknown_greeting || "";
      document.getElementById("cfgAutoPrivacy").checked = Boolean(guard.auto_privacy_after_greeting);
      document.getElementById("cfgGreetUnknown").checked = Boolean(guard.greet_unknown);
      const autoTrack = guard.auto_tracking !== false;
      document.getElementById("cfgAutoTracking").checked = autoTrack;
      document.getElementById("cfgTrackingReturnHome").checked = guard.tracking_return_home !== false;
      const trackSpan = document.getElementById("autoTrackState");
      if (trackSpan) {
        trackSpan.textContent = autoTrack ? "AN" : "AUS";
        trackSpan.className = autoTrack ? "text-success fw-bold" : "text-secondary";
      }
      const aiOverlay = guard.ai_overlay !== false;
      const overlaySpan = document.getElementById("aiOverlayState");
      if (overlaySpan) {
        overlaySpan.textContent = aiOverlay ? "AN" : "AUS";
        overlaySpan.className = aiOverlay ? "text-success fw-bold" : "text-secondary";
      }
      const speedVal = guard.tracking_speed ?? 1.4;
      const slider = document.getElementById("trackingSpeedSlider");
      const speedBadge = document.getElementById("trackingSpeedVal");
      if (slider) slider.value = speedVal;
      if (speedBadge) speedBadge.textContent = `${Number(speedVal).toFixed(1)}x`;
      const volVal = guard.tts_volume ?? 100;
      const volSlider = document.getElementById("ttsVolumeSlider");
      const volBadge = document.getElementById("ttsVolumeVal");
      if (volSlider) volSlider.value = volVal;
      if (volBadge) volBadge.textContent = `${volVal}%`;

      renderPersons(data.persons || []);
    } catch (err) {
      console.error("Failed to load config:", err);
    }
  };

  const renderPersons = (persons) => {
    const container = document.getElementById("personsList");
    if (!container) return;

    if (!persons.length) {
      container.innerHTML = `
        <div class="text-center text-secondary py-4 small">
          <i class="fa-solid fa-user-astronaut fa-2x mb-2 opacity-50 d-block"></i>
          Noch keine Personen hinterlegt. Lege eine Person an und nimm ein Referenzfoto über die Tapo C200 auf!
        </div>
      `;
      return;
    }

    container.innerHTML = persons
      .map((p) => {
        const photos = (p.photos || [])
          .slice(-3)
          .map(
            (fn) =>
              `<img src="/api/faces/${encodeURIComponent(fn)}" class="face-thumb" alt="${escapeHtml(p.name)}">`
          )
          .join("");

        return `
          <div class="person-card">
            <div class="d-flex justify-content-between align-items-start gap-2 mb-2">
              <div>
                <span class="fw-bold">${escapeHtml(p.name)}</span>
                <span class="badge bg-info bg-opacity-25 text-info ms-2">${p.photo_count || 0} Fotos</span>
                ${p.enabled === false ? '<span class="badge bg-secondary ms-1">Pausiert</span>' : ""}
                <div class="small text-secondary mt-1">
                  <i class="fa-solid fa-comment-dots me-1"></i>${escapeHtml(p.greeting || "Standard-Begrüßung")}
                </div>
                <div class="small text-secondary">
                  Zuletzt begrüßt: ${formatTime(p.last_greeted)} · Cooldown: ${p.cooldown_minutes ?? 120} Min.
                </div>
              </div>
              <div class="d-flex gap-1">
                <button class="btn btn-glass btn-sm text-info" data-edit-person="${escapeHtml(p.id)}" title="Person bearbeiten">
                  <i class="fa-solid fa-pen"></i>
                </button>
                <button class="btn btn-glass btn-sm text-danger" data-delete-person="${escapeHtml(p.id)}" title="Person löschen">
                  <i class="fa-solid fa-trash"></i>
                </button>
              </div>
            </div>
            <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mt-2 pt-2 border-top border-secondary border-opacity-25">
              <div class="d-flex gap-2 align-items-center">
                ${photos || '<span class="small text-warning"><i class="fa-solid fa-triangle-exclamation me-1"></i>Noch kein Foto</span>'}
              </div>
              <div class="d-flex gap-2">
                <button class="btn btn-glass btn-sm" data-capture-person="${escapeHtml(p.id)}" title="Jetzt Foto von Tapo C200 aufnehmen">
                  <i class="fa-solid fa-camera me-1"></i>Kamera-Snap
                </button>
                <label class="btn btn-glass btn-sm mb-0" title="Foto-Datei hochladen">
                  <i class="fa-solid fa-upload me-1"></i>Upload
                  <input type="file" accept="image/*" class="d-none" data-upload-person="${escapeHtml(p.id)}">
                </label>
              </div>
            </div>
          </div>
        `;
      })
      .join("");
  };

  const loadEvents = async () => {
    try {
      const res = await fetch("/api/events?limit=30");
      const data = await res.json();
      const events = data.events || [];
      const container = document.getElementById("eventsList");
      if (!container) return;

      if (!events.length) {
        container.innerHTML = `<div class="text-center text-secondary py-3 small">Noch keine Ereignisse protokolliert.</div>`;
        return;
      }

      container.innerHTML = events
        .map((ev) => {
          const snapHtml = ev.snapshot_url
            ? `<a href="${escapeHtml(ev.snapshot_url)}" target="_blank"><img src="${escapeHtml(ev.snapshot_url)}" class="event-snap" alt="Snapshot"></a>`
            : `<div class="event-snap d-flex align-items-center justify-content-center bg-dark text-info"><i class="fa-solid fa-volume-high"></i></div>`;

          const statusBadge = ev.audio_sent
            ? `<span class="badge text-bg-success">Lautsprecher OK</span>`
            : `<span class="badge text-bg-secondary">Lokal / Offline</span>`;

          return `
            <div class="event-item d-flex align-items-center justify-content-between gap-3">
              <div class="d-flex align-items-center gap-3">
                ${snapHtml}
                <div>
                  <div class="fw-semibold small">
                    ${escapeHtml(ev.person_name)}
                    <span class="text-secondary fw-normal ms-1">(${Math.round((ev.confidence || 0) * 100)}%)</span>
                  </div>
                  <div class="small text-info">"${escapeHtml(ev.greeting_text)}"</div>
                  <div class="text-secondary" style="font-size: 0.72rem;">${formatTime(ev.timestamp)}</div>
                </div>
              </div>
              <div>${statusBadge}</div>
            </div>
          `;
        })
        .join("");
    } catch (err) {
      console.error("Failed to load events:", err);
    }
  };

  // -------------------- Event Listeners --------------------

  // PTZ controls (smooth hold-to-move via ContinuousMove + Stop)
  let ptzActive = false;
  const stopPtz = async () => {
    if (!ptzActive) return;
    ptzActive = false;
    try {
      await fetch("/api/camera/ptz/stop", { method: "POST" });
    } catch {}
  };

  document.querySelectorAll("[data-ptz]").forEach((btn) => {
    const direction = btn.getAttribute("data-ptz");
    btn.addEventListener("pointerdown", async (e) => {
      e.preventDefault();
      ptzActive = true;
      await fetch("/api/camera/ptz/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction }),
      });
    });
    btn.addEventListener("pointerup", stopPtz);
    btn.addEventListener("pointerleave", stopPtz);
    btn.addEventListener("pointercancel", stopPtz);
  });
  window.addEventListener("pointerup", stopPtz);

  // Presets (Goto)
  document.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const preset_id = btn.getAttribute("data-preset");
      const res = await fetch("/api/camera/preset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset_id }),
      });
      const data = await res.json();
      if (data.ok) {
        showStatus(`Position ${preset_id} angefahren.`, "success");
      } else {
        showStatus(`Preset-Hinweis: ${data.error}`, "warning");
      }
    });
  });

  // Presets (Save current position)
  document.querySelectorAll("[data-save-preset]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const preset_id = btn.getAttribute("data-save-preset");
      const res = await fetch("/api/camera/preset/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset_id }),
      });
      const data = await res.json();
      if (data.ok) {
        showStatus(`Aktuelle Kameraposition fest auf Platz ${preset_id} gespeichert!`, "success");
      } else {
        showStatus(`Speichern fehlgeschlagen: ${data.error}`, "warning");
      }
    });
  });

  // AI Overlay Grid Quick Toggle
  document.getElementById("btnAiOverlayToggle")?.addEventListener("click", async () => {
    const currentlyOn = currentConfig?.guard?.ai_overlay !== false;
    const nextVal = !currentlyOn;
    const res = await fetch("/api/config/guard", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ai_overlay: nextVal }),
    });
    if (res.ok) {
      showStatus(
        nextVal ? "KI-Gesichts-Raster im Live-Bild aktiviert." : "KI-Gesichts-Raster ausgeblendet.",
        "info"
      );
      loadConfig();
    }
  });

  // Auto-Tracking Quick Toggle
  document.getElementById("btnAutoTrackToggle")?.addEventListener("click", async () => {
    const currentlyOn = currentConfig?.guard?.auto_tracking !== false;
    const nextVal = !currentlyOn;
    const res = await fetch("/api/config/guard", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_tracking: nextVal }),
    });
    if (res.ok) {
      showStatus(
        nextVal ? "KI-Personen-Verfolgung (Auto-Tracking) aktiviert." : "Auto-Tracking pausiert.",
        "info"
      );
      loadConfig();
    }
  });

  // Tracking Speed Scale (- ||||||||||||| +)
  const saveTrackingSpeed = async (val) => {
    const clamped = Math.max(0.3, Math.min(2.5, Math.round(val * 10) / 10));
    const slider = document.getElementById("trackingSpeedSlider");
    const badge = document.getElementById("trackingSpeedVal");
    if (slider) slider.value = clamped;
    if (badge) badge.textContent = `${clamped.toFixed(1)}x`;
    await fetch("/api/config/guard", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracking_speed: clamped }),
    });
  };

  document.getElementById("trackingSpeedSlider")?.addEventListener("input", (e) => {
    const badge = document.getElementById("trackingSpeedVal");
    if (badge) badge.textContent = `${Number(e.target.value).toFixed(1)}x`;
  });
  document.getElementById("trackingSpeedSlider")?.addEventListener("change", (e) => {
    saveTrackingSpeed(parseFloat(e.target.value));
  });
  document.getElementById("btnSpeedMinus")?.addEventListener("click", () => {
    const cur = parseFloat(document.getElementById("trackingSpeedSlider")?.value || "1.4");
    saveTrackingSpeed(cur - 0.2);
  });
  document.getElementById("btnSpeedPlus")?.addEventListener("click", () => {
    const cur = parseFloat(document.getElementById("trackingSpeedSlider")?.value || "1.4");
    saveTrackingSpeed(cur + 0.2);
  });

  // Privacy Toggle
  document.getElementById("btnPrivacyToggle")?.addEventListener("click", async () => {
    privacyEnabled = !privacyEnabled;
    const res = await fetch("/api/camera/privacy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: privacyEnabled }),
    });
    const data = await res.json();
    showStatus(
      privacyEnabled
        ? "Privatsphäre-Modus aktiviert (Linse verdeckt)."
        : "Privatsphäre-Modus beendet (Kamera aktiv).",
      data.ok ? "info" : "warning"
    );
  });

  // Refresh snapshot
  document.getElementById("btnRefreshSnap")?.addEventListener("click", () => {
    const img = document.getElementById("liveStreamImg");
    if (img) img.src = `/api/camera/stream.mjpg?t=${Date.now()}`;
  });

  // Digital Zoom (1.0x - 4.0x) + Mouse Wheel + Drag-to-Pan
  let zoomLevel = 1.0;
  let panX = 0;
  let panY = 0;
  let isDraggingZoom = false;
  let dragStartX = 0;
  let dragStartY = 0;

  const streamViewport = document.getElementById("streamViewport");
  const liveStreamImg = document.getElementById("liveStreamImg");
  const zoomSlider = document.getElementById("digitalZoomSlider");
  const zoomBadge = document.getElementById("digitalZoomVal");

  const applyDigitalZoom = (nextZoom, resetPan = false) => {
    zoomLevel = Math.max(1.0, Math.min(4.0, Math.round(nextZoom * 10) / 10));
    if (zoomLevel <= 1.0 || resetPan) {
      panX = 0;
      panY = 0;
    }
    if (streamViewport) {
      const maxPanX = (streamViewport.clientWidth * (zoomLevel - 1)) / (2 * zoomLevel);
      const maxPanY = (streamViewport.clientHeight * (zoomLevel - 1)) / (2 * zoomLevel);
      panX = Math.max(-maxPanX, Math.min(maxPanX, panX));
      panY = Math.max(-maxPanY, Math.min(maxPanY, panY));
      streamViewport.classList.toggle("zoomed", zoomLevel > 1.0);
    }
    if (liveStreamImg) {
      liveStreamImg.style.transform = `scale(${zoomLevel}) translate(${panX}px, ${panY}px)`;
    }
    if (zoomSlider) zoomSlider.value = zoomLevel;
    if (zoomBadge) zoomBadge.textContent = `${zoomLevel.toFixed(1)}x`;
  };

  zoomSlider?.addEventListener("input", (e) => applyDigitalZoom(parseFloat(e.target.value)));
  document.getElementById("btnZoomMinus")?.addEventListener("click", () => applyDigitalZoom(zoomLevel - 0.3));
  document.getElementById("btnZoomPlus")?.addEventListener("click", () => applyDigitalZoom(zoomLevel + 0.3));
  document.getElementById("btnZoomReset")?.addEventListener("click", () => applyDigitalZoom(1.0, true));

  streamViewport?.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 0.2 : -0.2;
      applyDigitalZoom(zoomLevel + delta);
    },
    { passive: false }
  );

  streamViewport?.addEventListener("pointerdown", (e) => {
    if (zoomLevel <= 1.0) return;
    isDraggingZoom = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    streamViewport.setPointerCapture(e.pointerId);
  });

  streamViewport?.addEventListener("pointermove", (e) => {
    if (!isDraggingZoom || zoomLevel <= 1.0) return;
    const dx = (e.clientX - dragStartX) / zoomLevel;
    const dy = (e.clientY - dragStartY) / zoomLevel;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    panX += dx;
    panY += dy;
    applyDigitalZoom(zoomLevel);
  });

  const stopZoomDrag = () => {
    isDraggingZoom = false;
  };
  streamViewport?.addEventListener("pointerup", stopZoomDrag);
  streamViewport?.addEventListener("pointercancel", stopZoomDrag);

  // Scan Now
  document.getElementById("btnScanNow")?.addEventListener("click", async () => {
    showStatus("Prüfe 1080p-Frame der Tapo C200 auf bekannte Gesichter...", "info");
    const res = await fetch("/api/guard/scan-now", { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      showStatus(data.error || "Kein Frame verfügbar.", "warning");
    } else if (!data.detections.length) {
      showStatus("Keine Person im aktuellen Kamerabild erkannt.", "info");
    } else {
      const names = data.detections.map((d) => d.person_name).join(", ");
      showStatus(`Erkannt: ${names}`, "success");
    }
    loadConfig();
    loadEvents();
  });

  // Speak Form
  document.getElementById("speakForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("speakTextInput");
    const text = input.value.trim();
    if (!text) return;

    const btn = document.getElementById("btnSpeakNow");
    btn.disabled = true;
    try {
      const res = await fetch("/api/tts/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const data = await res.json();
      if (data.ok) {
        showStatus(`Ausgabe auf Tapo C200 Lautsprecher: "${data.text}"`, "success");
      } else {
        showStatus(data.error || "WAV lokal generiert (Kamera noch offline).", "warning");
      }
      loadEvents();
    } finally {
      btn.disabled = false;
    }
  });

  document.querySelectorAll(".quick-tts-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.getElementById("speakTextInput").value = chip.getAttribute("data-text");
    });
  });

  // TTS Volume Slider (10% - 200%)
  const saveTtsVolume = async (val) => {
    const clamped = Math.max(10, Math.min(200, Math.round(val / 10) * 10));
    const slider = document.getElementById("ttsVolumeSlider");
    const badge = document.getElementById("ttsVolumeVal");
    if (slider) slider.value = clamped;
    if (badge) badge.textContent = `${clamped}%`;
    await fetch("/api/config/guard", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tts_volume: clamped }),
    });
  };
  document.getElementById("ttsVolumeSlider")?.addEventListener("input", (e) => {
    const badge = document.getElementById("ttsVolumeVal");
    if (badge) badge.textContent = `${e.target.value}%`;
  });
  document.getElementById("ttsVolumeSlider")?.addEventListener("change", (e) => {
    saveTtsVolume(parseInt(e.target.value, 10));
  });
  document.getElementById("btnVolMinus")?.addEventListener("click", () => {
    const cur = parseInt(document.getElementById("ttsVolumeSlider")?.value || "100", 10);
    saveTtsVolume(cur - 10);
  });
  document.getElementById("btnVolPlus")?.addEventListener("click", () => {
    const cur = parseInt(document.getElementById("ttsVolumeSlider")?.value || "100", 10);
    saveTtsVolume(cur + 10);
  });

  // Add Person Form
  document.getElementById("addPersonForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = document.getElementById("personNameInput").value.trim();
    const greeting = document.getElementById("personGreetingInput").value.trim();
    const cooldown_minutes = parseInt(document.getElementById("personCooldownInput").value, 10) || 120;

    const res = await fetch("/api/persons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, greeting, cooldown_minutes, enabled: true }),
    });
    if (res.ok) {
      document.getElementById("addPersonForm").reset();
      bootstrap.Modal.getInstance(document.getElementById("addPersonModal"))?.hide();
      showStatus(`Person "${name}" angelegt. Füge jetzt 1–3 Referenzfotos hinzu!`, "success");
      loadConfig();
    }
  });

  // -------------------- Photo Crop Editor --------------------
  let cropTargetPersonId = null;
  let cropImage = null;
  let cropScale = 1.0;
  let cropOffsetX = 0;
  let cropOffsetY = 0;
  let cropRotation = 0;
  let isCropDragging = false;
  let cropDragStartX = 0;
  let cropDragStartY = 0;

  const cropCanvas = document.getElementById("cropCanvas");
  const cropCtx = cropCanvas?.getContext("2d");
  const cropZoomSlider = document.getElementById("cropZoomSlider");

  const renderCropCanvas = () => {
    if (!cropCtx || !cropCanvas || !cropImage) return;
    const cw = cropCanvas.width;
    const ch = cropCanvas.height;
    cropCtx.clearRect(0, 0, cw, ch);
    cropCtx.fillStyle = "#050811";
    cropCtx.fillRect(0, 0, cw, ch);

    cropCtx.save();
    cropCtx.translate(cw / 2 + cropOffsetX, ch / 2 + cropOffsetY);
    cropCtx.rotate((cropRotation * Math.PI) / 180);
    cropCtx.scale(cropScale, cropScale);
    cropCtx.drawImage(cropImage, -cropImage.width / 2, -cropImage.height / 2);
    cropCtx.restore();
  };

  const openCropEditorWithSrc = (personId, imgSrc) => {
    cropTargetPersonId = personId;
    cropRotation = 0;
    cropOffsetX = 0;
    cropOffsetY = 0;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      cropImage = img;
      // Fit image to 320x320 initially
      const baseScale = Math.max(320 / img.width, 320 / img.height);
      cropScale = baseScale;
      if (cropZoomSlider) {
        cropZoomSlider.min = (baseScale * 0.4).toFixed(2);
        cropZoomSlider.max = (baseScale * 4.5).toFixed(2);
        cropZoomSlider.step = (baseScale * 0.05).toFixed(3);
        cropZoomSlider.value = baseScale;
      }
      renderCropCanvas();
      new bootstrap.Modal(document.getElementById("cropPhotoModal")).show();
    };
    img.src = imgSrc;
  };

  cropZoomSlider?.addEventListener("input", (e) => {
    cropScale = parseFloat(e.target.value) || 1.0;
    renderCropCanvas();
  });

  document.getElementById("btnCropRotate")?.addEventListener("click", () => {
    cropRotation = (cropRotation + 90) % 360;
    renderCropCanvas();
  });

  cropCanvas?.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      if (!cropZoomSlider) return;
      const min = parseFloat(cropZoomSlider.min);
      const max = parseFloat(cropZoomSlider.max);
      const step = (max - min) * 0.04;
      cropScale = Math.max(min, Math.min(max, cropScale + (e.deltaY < 0 ? step : -step)));
      cropZoomSlider.value = cropScale;
      renderCropCanvas();
    },
    { passive: false }
  );

  cropCanvas?.addEventListener("pointerdown", (e) => {
    isCropDragging = true;
    cropDragStartX = e.clientX;
    cropDragStartY = e.clientY;
    cropCanvas.setPointerCapture(e.pointerId);
  });
  cropCanvas?.addEventListener("pointermove", (e) => {
    if (!isCropDragging) return;
    cropOffsetX += e.clientX - cropDragStartX;
    cropOffsetY += e.clientY - cropDragStartY;
    cropDragStartX = e.clientX;
    cropDragStartY = e.clientY;
    renderCropCanvas();
  });
  const stopCropDrag = () => {
    isCropDragging = false;
  };
  cropCanvas?.addEventListener("pointerup", stopCropDrag);
  cropCanvas?.addEventListener("pointercancel", stopCropDrag);

  document.getElementById("btnCropSave")?.addEventListener("click", async () => {
    if (!cropCanvas || !cropTargetPersonId) return;
    const btn = document.getElementById("btnCropSave");
    btn.disabled = true;
    try {
      // Export crisp 400x400 portrait JPEG
      const outCanvas = document.createElement("canvas");
      outCanvas.width = 400;
      outCanvas.height = 400;
      const outCtx = outCanvas.getContext("2d");
      outCtx.drawImage(cropCanvas, 0, 0, 400, 400);

      const blob = await new Promise((resolve) => outCanvas.toBlob(resolve, "image/jpeg", 0.92));
      const formData = new FormData();
      formData.append("file", blob, "cropped_face.jpg");

      const res = await fetch(`/api/persons/${encodeURIComponent(cropTargetPersonId)}/photo`, {
        method: "POST",
        body: formData,
      });
      if (res.ok) {
        const data = await res.json();
        bootstrap.Modal.getInstance(document.getElementById("cropPhotoModal"))?.hide();
        if (document.getElementById("editPersonId")?.value === cropTargetPersonId) {
          renderEditPersonPhotos(data.person);
        }
        showStatus("Zugeschnittenes Referenzfoto gespeichert & Embedding aktualisiert!", "success");
        loadConfig();
      } else {
        showStatus("Fehler beim Speichern des zugeschnittenen Fotos.", "danger");
      }
    } finally {
      btn.disabled = false;
    }
  });

  // Render photos inside Edit Person Modal
  const renderEditPersonPhotos = (person) => {
    const box = document.getElementById("editPersonPhotos");
    if (!box) return;
    const photos = person?.photos || [];
    if (!photos.length) {
      box.innerHTML = `<span class="small text-secondary">Noch keine Referenzfotos hinterlegt.</span>`;
      return;
    }
    box.innerHTML = photos
      .map(
        (fn) => `
          <div class="position-relative d-inline-block">
            <img src="/api/faces/${encodeURIComponent(fn)}" class="face-thumb" style="width: 64px; height: 64px;" alt="Foto">
            <button type="button" class="btn btn-danger btn-sm position-absolute top-0 end-0 p-0 d-flex align-items-center justify-content-center"
              style="width: 20px; height: 20px; border-radius: 50%; transform: translate(30%, -30%); font-size: 0.7rem;"
              data-delete-photo="${escapeHtml(fn)}" data-person-id="${escapeHtml(person.id)}" title="Foto entfernen">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </div>
        `
      )
      .join("");
  };

  document.getElementById("editPersonPhotos")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-delete-photo]");
    if (!btn) return;
    const fn = btn.getAttribute("data-delete-photo");
    const pid = btn.getAttribute("data-person-id");
    const res = await fetch(`/api/persons/${encodeURIComponent(pid)}/photo/${encodeURIComponent(fn)}`, {
      method: "DELETE",
    });
    if (res.ok) {
      const data = await res.json();
      renderEditPersonPhotos(data.person);
      showStatus("Foto entfernt.", "info");
      loadConfig();
    }
  });

  document.getElementById("btnEditCapturePhoto")?.addEventListener("click", () => {
    const pid = document.getElementById("editPersonId").value;
    if (!pid) return;
    openCropEditorWithSrc(pid, `/api/camera/snapshot.jpg?t=${Date.now()}`);
  });

  document.getElementById("inputEditUploadPhoto")?.addEventListener("change", (e) => {
    const pid = document.getElementById("editPersonId").value;
    if (!pid || !e.target.files.length) return;
    const fileUrl = URL.createObjectURL(e.target.files[0]);
    openCropEditorWithSrc(pid, fileUrl);
    e.target.value = "";
  });

  // Edit Person Form Submit
  document.getElementById("editPersonForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = document.getElementById("editPersonId").value;
    const name = document.getElementById("editPersonName").value.trim();
    const greeting = document.getElementById("editPersonGreeting").value.trim();
    const cooldownVal = document.getElementById("editPersonCooldown").value;
    const cooldown_minutes = cooldownVal === "" ? 120 : parseInt(cooldownVal, 10);
    const enabled = document.getElementById("editPersonEnabled").checked;

    const res = await fetch(`/api/persons/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, greeting, cooldown_minutes, enabled }),
    });
    if (res.ok) {
      bootstrap.Modal.getInstance(document.getElementById("editPersonModal"))?.hide();
      showStatus(`Änderungen für "${name}" gespeichert!`, "success");
      loadConfig();
    } else {
      showStatus("Fehler beim Speichern der Person.", "danger");
    }
  });

  // Delegated Person actions (Edit, Delete, Capture, Upload)
  document.getElementById("personsList")?.addEventListener("click", async (e) => {
    const editBtn = e.target.closest("[data-edit-person]");
    if (editBtn) {
      const id = editBtn.getAttribute("data-edit-person");
      const person = (currentConfig?.persons || []).find((p) => p.id === id);
      if (person) {
        document.getElementById("editPersonId").value = person.id;
        document.getElementById("editPersonName").value = person.name || "";
        document.getElementById("editPersonGreeting").value = person.greeting || "";
        document.getElementById("editPersonCooldown").value = person.cooldown_minutes ?? 120;
        document.getElementById("editPersonEnabled").checked = person.enabled !== false;
        renderEditPersonPhotos(person);
        new bootstrap.Modal(document.getElementById("editPersonModal")).show();
      }
      return;
    }

    const delBtn = e.target.closest("[data-delete-person]");
    if (delBtn) {
      const id = delBtn.getAttribute("data-delete-person");
      await fetch(`/api/persons/${encodeURIComponent(id)}`, { method: "DELETE" });
      showStatus("Person gelöscht.", "info");
      loadConfig();
      return;
    }

    const capBtn = e.target.closest("[data-capture-person]");
    if (capBtn) {
      const id = capBtn.getAttribute("data-capture-person");
      openCropEditorWithSrc(id, `/api/camera/snapshot.jpg?t=${Date.now()}`);
    }
  });

  document.getElementById("personsList")?.addEventListener("change", (e) => {
    const fileInput = e.target.closest("[data-upload-person]");
    if (fileInput && fileInput.files.length > 0) {
      const id = fileInput.getAttribute("data-upload-person");
      const fileUrl = URL.createObjectURL(fileInput.files[0]);
      openCropEditorWithSrc(id, fileUrl);
      fileInput.value = "";
    }
  });

  // Save Settings Form
  document.getElementById("settingsForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const camPayload = {
      host: document.getElementById("cfgHost").value.trim(),
      rtsp_port: parseInt(document.getElementById("cfgRtspPort").value, 10) || 554,
      onvif_port: parseInt(document.getElementById("cfgOnvifPort").value, 10) || 2020,
      rtsp_user: document.getElementById("cfgRtspUser").value.trim(),
      rtsp_pass: document.getElementById("cfgRtspPass").value,
      cloud_password: document.getElementById("cfgCloudPass").value,
    };

    const guardPayload = {
      enabled: document.getElementById("cfgGuardEnabled").value === "true",
      detection_interval_sec: parseFloat(document.getElementById("cfgInterval").value) || 2.0,
      cooldown_minutes: parseInt(document.getElementById("cfgCooldown").value, 10) || 120,
      similarity_threshold: parseFloat(document.getElementById("cfgThreshold").value) || 0.38,
      active_hours_start: document.getElementById("cfgHoursStart").value || "06:00",
      active_hours_end: document.getElementById("cfgHoursEnd").value || "23:00",
      tts_voice: document.getElementById("cfgVoice").value,
      default_greeting: document.getElementById("cfgDefaultGreeting").value.trim(),
      unknown_greeting: document.getElementById("cfgUnknownGreeting").value.trim(),
      auto_privacy_after_greeting: document.getElementById("cfgAutoPrivacy").checked,
      greet_unknown: document.getElementById("cfgGreetUnknown").checked,
      auto_tracking: document.getElementById("cfgAutoTracking").checked,
      tracking_return_home: document.getElementById("cfgTrackingReturnHome").checked,
    };

    await fetch("/api/config/camera", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(camPayload),
    });

    await fetch("/api/config/guard", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(guardPayload),
    });

    bootstrap.Modal.getInstance(document.getElementById("settingsModal"))?.hide();
    showStatus("Kamera- & KI-Einstellungen gespeichert!", "success");
    loadConfig();
  });

  // Clear Events
  document.getElementById("btnClearEvents")?.addEventListener("click", async () => {
    await fetch("/api/events", { method: "DELETE" });
    loadEvents();
  });

  // Initial Boot
  initTheme();
  loadConfig();
  loadEvents();
  setInterval(loadEvents, 5000);
})();
