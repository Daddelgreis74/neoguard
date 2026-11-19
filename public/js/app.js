(() => {
  const t = (key, vars = {}) => (window.neoI18n ? window.neoI18n.t(key, vars) : key);
  let currentConfig = null;
  let privacyEnabled = false;
  let selectedCameraId = localStorage.getItem("neoguard_selected_cam") || null;
  let settingsCameraId = null;
  let multiGridMode = false;

  const getCameras = () => currentConfig?.cameras || (currentConfig?.camera ? [currentConfig.camera] : []);
  const getActiveCamera = () => {
    const cams = getCameras();
    return cams.find((c) => c.id === selectedCameraId) || cams[0] || {};
  };
  const getSettingsCamera = () => {
    const cams = getCameras();
    return cams.find((c) => c.id === settingsCameraId) || getActiveCamera();
  };
  const camQuery = (camId = selectedCameraId) => (camId ? `camera_id=${encodeURIComponent(camId)}` : "");

  let statusTimer = null;
  const showStatus = (msg, type = "info") => {
    const el = document.getElementById("statusAlert");
    if (!el) return;
    el.className = `alert alert-${type} py-2 px-3 small rounded-3 floating-status-toast`;
    el.innerHTML = `<img src="icons3d/shield.png" class="icon3d icon3d-sm me-2" alt="">${msg}`;
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
    if (!isoStr) return t("person_never");
    try {
      const dt = new Date(isoStr);
      const localeMap = { de: "de-DE", en: "en-US", fr: "fr-FR", es: "es-ES" };
      const loc = localeMap[window.neoI18n?.getLang?.() || "en"] || "en-US";
      return dt.toLocaleString(loc, {
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
    const saved = localStorage.getItem("neoguard_theme") || "dark";
    document.documentElement.setAttribute("data-bs-theme", saved);
    const icon = document.getElementById("themeIcon");
    if (icon) icon.src = saved === "dark" ? "icons3d/sun.png" : "icons3d/night.png";
  };

  document.getElementById("btnThemeToggle")?.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-bs-theme") || "dark";
    const next = cur === "dark" ? "light" : "dark";
    localStorage.setItem("neoguard_theme", next);
    initTheme();
  });

  // -------------------- Multi-Camera Selector, Grid & Settings Helper --------------------
  const updateLiveCameraUI = (cam) => {
    if (!cam) return;
    document.getElementById("activeCameraTitle").textContent = cam.name || "Kamera 1";
    document.getElementById("cameraIpSubtitle").textContent =
      `IP: ${cam.host || "Nicht gesetzt"} · RTSP Port ${cam.rtsp_port || 554} · ONVIF Port ${cam.onvif_port || 2020}`;

    const autoTrack = cam.auto_tracking !== false;
    const trackSpan = document.getElementById("autoTrackState");
    if (trackSpan) {
      trackSpan.textContent = autoTrack ? t("state_on") : t("state_off");
      trackSpan.className = autoTrack ? "text-success fw-bold" : "text-secondary";
    }

    const defaultPresets = { "1": "Tür / Eingang", "2": "Raum-Mitte", "3": "Schreibtisch" };
    const presetNames = { ...defaultPresets, ...(cam.preset_names || {}) };
    ["1", "2", "3"].forEach((idx) => {
      const labelEl = document.getElementById(`presetLabel${idx}`);
      if (labelEl) labelEl.textContent = presetNames[idx] || defaultPresets[idx];
    });

    const streamImg = document.getElementById("liveStreamImg");
    if (streamImg && !multiGridMode) {
      const targetUrl = `/api/camera/stream.mjpg?${camQuery(cam.id)}`;
      if (!streamImg.getAttribute("src") || !streamImg.getAttribute("src").includes(`camera_id=${encodeURIComponent(cam.id || "")}`)) {
        streamImg.src = `${targetUrl}&t=${Date.now()}`;
      }
    }
  };

  const renderMultiGrid = () => {
    const grid = document.getElementById("multiCameraGrid");
    if (!grid || multiGridMode === false) return;
    const cams = getCameras();
    grid.innerHTML = cams
      .map(
        (c) => `
      <div class="col-md-6">
        <div class="multi-cam-card" data-grid-cam="${escapeHtml(c.id)}">
          <img src="/api/camera/stream.mjpg?camera_id=${encodeURIComponent(c.id)}&t=${Date.now()}" alt="${escapeHtml(c.name)}">
          <div class="d-flex justify-content-between align-items-center px-3 py-2">
            <span class="fw-semibold small">${escapeHtml(c.name)}</span>
            <span class="badge ${c.enabled !== false ? "text-bg-success" : "text-bg-secondary"}">${c.enabled !== false ? t("cam_active") : t("cam_paused")}</span>
          </div>
        </div>
      </div>`
      )
      .join("");
    grid.querySelectorAll("[data-grid-cam]").forEach((card) => {
      card.addEventListener("click", () => {
        selectedCameraId = card.getAttribute("data-grid-cam");
        localStorage.setItem("neoguard_selected_cam", selectedCameraId);
        setMultiGridMode(false);
        renderCameraSelector();
        updateLiveCameraUI(getActiveCamera());
      });
    });
  };

  const setMultiGridMode = (enabled) => {
    multiGridMode = enabled;
    const grid = document.getElementById("multiCameraGrid");
    const single = document.getElementById("singleCameraView");
    const btn = document.getElementById("btnToggleMultiGrid");
    if (grid) grid.classList.toggle("d-none", !enabled);
    if (single) single.classList.toggle("d-none", enabled);
    if (btn) {
      btn.classList.toggle("btn-neo-primary", enabled);
      btn.classList.toggle("btn-glass", !enabled);
    }
    if (enabled) {
      renderMultiGrid();
    } else {
      updateLiveCameraUI(getActiveCamera());
    }
  };

  document.getElementById("btnToggleMultiGrid")?.addEventListener("click", () => {
    setMultiGridMode(!multiGridMode);
  });

  const renderCameraSelector = () => {
    const bar = document.getElementById("cameraSelectorBar");
    if (!bar) return;
    const cams = getCameras();
    if (!cams.some((c) => c.id === selectedCameraId) && cams.length) {
      selectedCameraId = cams[0].id;
    }
    bar.innerHTML = cams
      .map(
        (c) => `
      <button type="button" class="btn btn-glass btn-sm cam-pill-btn ${!multiGridMode && c.id === selectedCameraId ? "active" : ""}" data-select-cam="${escapeHtml(c.id)}">
        <img src="icons3d/camera.png" class="icon3d icon3d-sm me-1" alt="">${escapeHtml(c.name)}
      </button>`
      )
      .join("");
    bar.querySelectorAll("[data-select-cam]").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedCameraId = btn.getAttribute("data-select-cam");
        localStorage.setItem("neoguard_selected_cam", selectedCameraId);
        if (multiGridMode) setMultiGridMode(false);
        renderCameraSelector();
        updateLiveCameraUI(getActiveCamera());
      });
    });
  };

  const populateSettingsCamera = (camId) => {
    const cams = getCameras();
    const cam = cams.find((c) => c.id === camId) || cams[0] || {};
    settingsCameraId = cam.id || "cam_1";

    const sel = document.getElementById("settingsCamSelect");
    if (sel) {
      sel.innerHTML = cams
        .map((c) => `<option value="${escapeHtml(c.id)}" ${c.id === settingsCameraId ? "selected" : ""}>${escapeHtml(c.name)} (${escapeHtml(c.host || "–")})</option>`)
        .join("");
    }
    const delBtn = document.getElementById("btnDeleteCamera");
    if (delBtn) delBtn.disabled = cams.length <= 1;

    document.getElementById("cfgCamName").value = cam.name || "Kamera 1";
    document.getElementById("cfgCamEnabled").checked = cam.enabled !== false;
    const brandSel = document.getElementById("cfgBrandPreset");
    if (brandSel) brandSel.value = cam.brand_preset || "tapo";
    document.getElementById("cfgHost").value = cam.host || "";
    document.getElementById("cfgRtspPort").value = cam.rtsp_port || 554;
    document.getElementById("cfgOnvifPort").value = cam.onvif_port || 2020;
    document.getElementById("cfgStreamPath").value = cam.stream_quality || "stream1";
    document.getElementById("cfgOnvifProfile").value = cam.onvif_profile || "profile_1";
    document.getElementById("cfgRtspUser").value = cam.rtsp_user || "";
    document.getElementById("cfgRtspPass").value = cam.rtsp_pass || "";
    document.getElementById("cfgCloudPass").value = cam.cloud_password || "";
    document.getElementById("cfgAutoTracking").checked = cam.auto_tracking !== false;
    document.getElementById("cfgTrackingReturnHome").checked = cam.tracking_return_home !== false;

    const speedVal = cam.tracking_speed ?? 1.4;
    const slider = document.getElementById("trackingSpeedSlider");
    const speedBadge = document.getElementById("trackingSpeedVal");
    if (slider) slider.value = speedVal;
    if (speedBadge) speedBadge.textContent = `${Number(speedVal).toFixed(1)}x`;

    const defaultPresets = { "1": "Tür / Eingang", "2": "Raum-Mitte", "3": "Schreibtisch" };
    const presetNames = { ...defaultPresets, ...(cam.preset_names || {}) };
    ["1", "2", "3"].forEach((idx) => {
      const inputEl = document.getElementById(`cfgPresetName${idx}`);
      if (inputEl) inputEl.value = presetNames[idx] || defaultPresets[idx];
    });
  };

  // -------------------- Load Config, Persons & Events --------------------
  const loadConfig = async () => {
    try {
      const res = await fetch("/api/config");
      const data = await res.json();
      currentConfig = data;

      const cams = getCameras();
      if (!cams.some((c) => c.id === selectedCameraId) && cams.length) {
        selectedCameraId = cams[0].id;
      }
      if (!cams.some((c) => c.id === settingsCameraId)) {
        settingsCameraId = selectedCameraId;
      }

      const guard = data.guard || {};
      const sys = data.system || {};

      renderCameraSelector();
      updateLiveCameraUI(getActiveCamera());
      if (multiGridMode) renderMultiGrid();

      document.getElementById("engineBadge").textContent =
        `${t("engine_local")} (${sys.face_engine || "OpenCV ONNX"})`;

      const badge = document.getElementById("guardActiveBadge");
      if (guard.enabled) {
        badge.className = "badge rounded-pill text-bg-success px-3 py-2";
        badge.innerHTML = `<img src="icons3d/shield.png" class="icon3d icon3d-sm me-1" alt="">${t("guard_active")} (${guard.active_hours_start}–${guard.active_hours_end})`;
      } else {
        badge.className = "badge rounded-pill text-bg-secondary px-3 py-2";
        badge.innerHTML = `<img src="icons3d/privacy.png" class="icon3d icon3d-sm me-1" alt="">${t("guard_paused")}`;
      }

      // Populate Settings Modal (3 Tabs)
      populateSettingsCamera(settingsCameraId);

      document.getElementById("cfgGuardEnabled").value = String(Boolean(guard.enabled));
      document.getElementById("cfgInterval").value = guard.detection_interval_sec ?? 2.0;
      document.getElementById("cfgCooldown").value = guard.cooldown_minutes ?? 120;
      document.getElementById("cfgThreshold").value = guard.similarity_threshold ?? 0.38;
      document.getElementById("cfgHoursStart").value = guard.active_hours_start || "06:00";
      document.getElementById("cfgHoursEnd").value = guard.active_hours_end || "23:00";
      document.getElementById("cfgAutoPrivacy").checked = Boolean(guard.auto_privacy_after_greeting);
      document.getElementById("cfgGreetUnknown").checked = Boolean(guard.greet_unknown);

      const authUserInput = document.getElementById("cfgAuthNewUser");
      if (authUserInput && data.auth?.username) {
        authUserInput.value = data.auth.username;
      }
      const aiOverlay = guard.ai_overlay !== false;
      const overlaySpan = document.getElementById("aiOverlayState");
      if (overlaySpan) {
        overlaySpan.textContent = aiOverlay ? t("state_on") : t("state_off");
        overlaySpan.className = aiOverlay ? "text-success fw-bold" : "text-secondary";
      }

      renderPersons(data.persons || []);
    } catch (err) {
      console.error("Failed to load config:", err);
    }
  };

  const renderPersons = (persons) => {
    const container = document.getElementById("personsList");
    const previewBar = document.getElementById("personsPreviewBar");
    const countBadge = document.getElementById("personsCountBadge");
    if (countBadge) countBadge.textContent = String(persons.length);

    if (previewBar) {
      if (!persons.length) {
        previewBar.innerHTML = `<span class="small text-secondary"><img src="icons3d/users.png" class="icon3d icon3d-sm me-1" alt="">${t("persons_empty_preview")}</span>`;
      } else {
        previewBar.innerHTML = persons
          .map((p) => {
            const latestPhoto = (p.photos || []).slice(-1)[0];
            const avatar = latestPhoto
              ? `<img src="/api/faces/${encodeURIComponent(latestPhoto)}" class="person-mini-avatar" alt="${escapeHtml(p.name)}">`
              : `<span class="person-mini-fallback">${escapeHtml((p.name || "?").charAt(0).toUpperCase())}</span>`;
            const pausedDot = p.enabled === false ? `<span class="text-secondary ms-1" style="font-size:0.65rem;">(${t("person_paused")})</span>` : "";
            return `<span class="person-mini-chip">${avatar}<span>${escapeHtml(p.name)}${pausedDot}</span></span>`;
          })
          .join("");
      }
    }

    if (!container) return;

    if (!persons.length) {
      container.innerHTML = `
        <div class="text-center text-secondary py-4 small">
          <img src="icons3d/users.png" class="icon3d-xl mb-2 d-block mx-auto" alt="">
          ${t("persons_empty_list")}
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
                <span class="badge bg-info bg-opacity-25 text-info ms-2">${p.photo_count || 0} ${t("person_photos")}</span>
                ${p.enabled === false ? `<span class="badge bg-secondary ms-1">${t("person_paused")}</span>` : ""}
                <div class="small text-secondary mt-1">
                  ${escapeHtml(p.greeting || "OK")}
                </div>
                <div class="small text-secondary">
                  ${t("person_last_seen")}: ${formatTime(p.last_seen || p.last_greeted)}
                </div>
              </div>
              <div class="d-flex gap-1">
                <button class="btn btn-glass btn-sm text-info" data-edit-person="${escapeHtml(p.id)}" title="Person bearbeiten">
                  <img src="icons3d/edit.png" class="icon3d" alt="Bearbeiten">
                </button>
                <button class="btn btn-glass btn-sm text-danger" data-delete-person="${escapeHtml(p.id)}" title="Person löschen">
                  <img src="icons3d/trash.png" class="icon3d" alt="Löschen">
                </button>
              </div>
            </div>
            <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mt-2 pt-2 border-top border-secondary border-opacity-25">
              <div class="d-flex gap-2 align-items-center">
                ${photos || `<span class="small text-warning">${t("person_no_photo")}</span>`}
              </div>
              <div class="d-flex gap-2">
                <button class="btn btn-glass btn-sm" data-capture-person="${escapeHtml(p.id)}">
                  <img src="icons3d/camera.png" class="icon3d me-1" alt="">${t("btn_cam_snap")}
                </button>
                <label class="btn btn-glass btn-sm mb-0">
                  <img src="icons3d/upload.png" class="icon3d me-1" alt="">${t("btn_upload")}
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
        container.innerHTML = `<div class="text-center text-secondary py-3 small">${t("events_empty")}</div>`;
        return;
      }

      container.innerHTML = events
        .map((ev) => {
          const snapHtml = ev.snapshot_url
            ? `<a href="${escapeHtml(ev.snapshot_url)}" target="_blank"><img src="${escapeHtml(ev.snapshot_url)}" class="event-snap" alt="Snapshot"></a>`
            : `<div class="event-snap d-flex align-items-center justify-content-center bg-dark"><img src="icons3d/users.png" class="icon3d" alt=""></div>`;

          const camBadge = ev.camera_name
            ? `<span class="badge bg-info bg-opacity-25 text-info">${escapeHtml(ev.camera_name)}</span>`
            : `<span class="badge text-bg-secondary">${t("events_recognized")}</span>`;

          return `
            <div class="event-item d-flex align-items-center justify-content-between gap-3">
              <div class="d-flex align-items-center gap-3">
                ${snapHtml}
                <div>
                  <div class="fw-semibold small">
                    ${escapeHtml(ev.person_name)}
                    <span class="text-secondary fw-normal ms-1">(${Math.round((ev.confidence || 0) * 100)}%)</span>
                  </div>
                  <div class="text-secondary" style="font-size: 0.72rem;">${formatTime(ev.timestamp)}</div>
                </div>
              </div>
              <div>${camBadge}</div>
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
      await fetch(`/api/camera/ptz/stop?${camQuery()}`, { method: "POST" });
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
        body: JSON.stringify({ direction, camera_id: selectedCameraId }),
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
        body: JSON.stringify({ preset_id, camera_id: selectedCameraId }),
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
        body: JSON.stringify({ preset_id, camera_id: selectedCameraId }),
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

  // Auto-Tracking Quick Toggle (per active camera)
  document.getElementById("btnAutoTrackToggle")?.addEventListener("click", async () => {
    const cam = getActiveCamera();
    const currentlyOn = cam.auto_tracking !== false;
    const nextVal = !currentlyOn;
    const res = await fetch(`/api/cameras/${encodeURIComponent(cam.id || "cam_1")}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_tracking: nextVal }),
    });
    if (res.ok) {
      showStatus(
        nextVal ? `Auto-Tracking für ${cam.name} aktiviert.` : `Auto-Tracking für ${cam.name} pausiert.`,
        "info"
      );
      loadConfig();
    }
  });

  // Tracking Speed Scale (- ||||||||||||| +) in Settings per selected settings camera
  const saveTrackingSpeed = async (val) => {
    const clamped = Math.max(0.3, Math.min(5.0, Math.round(val * 10) / 10));
    const slider = document.getElementById("trackingSpeedSlider");
    const badge = document.getElementById("trackingSpeedVal");
    if (slider) slider.value = clamped;
    if (badge) badge.textContent = `${clamped.toFixed(1)}x`;
    const camId = settingsCameraId || selectedCameraId || "cam_1";
    await fetch(`/api/cameras/${encodeURIComponent(camId)}`, {
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
      body: JSON.stringify({ enabled: privacyEnabled, camera_id: selectedCameraId }),
    });
    const data = await res.json();
    showStatus(
      privacyEnabled
        ? "Privatsphäre-Modus aktiviert (Video-Stream & KI pausiert)."
        : "Privatsphäre-Modus beendet (Kamera aktiv).",
      data.ok ? "info" : "warning"
    );
  });

  // Tapo C200 V5 TPAP Hardware Controls (Siren via Backchannel, LED, Night Vision)
  let hwLedOn = true;
  const nightModes = ["auto", "on", "off"];
  let nightModeIdx = 0;

  document.getElementById("btnHardwareSiren")?.addEventListener("click", async () => {
    const btn = document.getElementById("btnHardwareSiren");
    if (btn) btn.disabled = true;
    try {
      const res = await fetch("/api/camera/hardware", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ control: "siren", value: true, camera_id: settingsCameraId || selectedCameraId }),
      });
      const data = await res.json();
      showStatus(data.ok ? "Alarm-Sirene über Kamera-Lautsprecher ausgelöst!" : `Sirene Fehler: ${data.error}`, data.ok ? "warning" : "danger");
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  document.getElementById("btnHardwareLed")?.addEventListener("click", async () => {
    hwLedOn = !hwLedOn;
    const res = await fetch("/api/camera/hardware", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ control: "led", value: hwLedOn, camera_id: settingsCameraId || selectedCameraId }),
    });
    const data = await res.json();
    if (data.ok) {
      document.getElementById("hwLedState").textContent = hwLedOn ? "AN" : "AUS";
      showStatus(`Status-LED ${hwLedOn ? "eingeschaltet" : "ausgeschaltet"}.`, "info");
    } else {
      hwLedOn = !hwLedOn;
      showStatus(`LED Fehler: ${data.error}`, "warning");
    }
  });

  document.getElementById("btnHardwareNight")?.addEventListener("click", async () => {
    nightModeIdx = (nightModeIdx + 1) % nightModes.length;
    const mode = nightModes[nightModeIdx];
    const res = await fetch("/api/camera/hardware", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ control: "night_vision", value: mode, camera_id: settingsCameraId || selectedCameraId }),
    });
    const data = await res.json();
    if (data.ok) {
      const labels = { auto: "Auto", on: "Ein", off: "Aus" };
      document.getElementById("hwNightState").textContent = labels[mode] || mode;
      showStatus(`Nachtsicht auf "${labels[mode]}" gestellt.`, "info");
    } else {
      showStatus(`Nachtsicht Fehler: ${data.error}`, "warning");
    }
  });

  // Refresh snapshot
  document.getElementById("btnRefreshSnap")?.addEventListener("click", () => {
    const img = document.getElementById("liveStreamImg");
    if (img) img.src = `/api/camera/stream.mjpg?${camQuery()}&t=${Date.now()}`;
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
    showStatus("Prüfe Live-Frame auf bekannte & unbekannte Personen...", "info");
    const res = await fetch(`/api/guard/scan-now?${camQuery()}`, { method: "POST" });
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

  // TTS Volume Slider (10% - 200%) in Settings
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
    openCropEditorWithSrc(pid, `/api/camera/snapshot.jpg?${camQuery()}&t=${Date.now()}`);
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
      openCropEditorWithSrc(id, `/api/camera/snapshot.jpg?${camQuery()}&t=${Date.now()}`);
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

  // Brand Preset Auto-Fill for universal RTSP / ONVIF cameras
  const CAMERA_BRAND_PRESETS = {
    tapo: { rtsp_port: 554, onvif_port: 2020, stream_quality: "stream1", onvif_profile: "profile_1" },
    reolink: { rtsp_port: 554, onvif_port: 8000, stream_quality: "h264Preview_01_main", onvif_profile: "main" },
    hikvision: { rtsp_port: 554, onvif_port: 80, stream_quality: "Streaming/Channels/101", onvif_profile: "Profile_1" },
    dahua: { rtsp_port: 554, onvif_port: 80, stream_quality: "cam/realmonitor?channel=1&subtype=0", onvif_profile: "MediaProfile000" },
    imou: { rtsp_port: 554, onvif_port: 80, stream_quality: "cam/realmonitor?channel=1&subtype=0", onvif_profile: "MediaProfile000" },
    thingino: { rtsp_port: 554, onvif_port: 80, stream_quality: "ch0", onvif_profile: "Profile_0" },
  };

  document.getElementById("cfgBrandPreset")?.addEventListener("change", (e) => {
    const preset = CAMERA_BRAND_PRESETS[e.target.value];
    if (!preset) return;
    document.getElementById("cfgRtspPort").value = preset.rtsp_port;
    document.getElementById("cfgOnvifPort").value = preset.onvif_port;
    document.getElementById("cfgStreamPath").value = preset.stream_quality;
    document.getElementById("cfgOnvifProfile").value = preset.onvif_profile;
  });

  // Settings Camera Selector, + Kamera, Delete Kamera
  document.getElementById("settingsCamSelect")?.addEventListener("change", (e) => {
    populateSettingsCamera(e.target.value);
  });

  document.getElementById("btnAddCamera")?.addEventListener("click", async () => {
    const nextNum = getCameras().length + 1;
    const res = await fetch("/api/cameras", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: `Kamera ${nextNum}`,
        host: "192.168.178.50",
        enabled: true,
      }),
    });
    if (res.ok) {
      const created = await res.json();
      settingsCameraId = created.camera?.id || settingsCameraId;
      showStatus(`Neue Kamera "${created.camera?.name}" angelegt.`, "success");
      await loadConfig();
    }
  });

  document.getElementById("btnDeleteCamera")?.addEventListener("click", async () => {
    const cams = getCameras();
    if (cams.length <= 1) return;
    const camId = settingsCameraId || cams[0].id;
    const res = await fetch(`/api/cameras/${encodeURIComponent(camId)}`, { method: "DELETE" });
    if (res.ok) {
      showStatus("Kamera entfernt.", "info");
      settingsCameraId = null;
      await loadConfig();
    } else {
      const err = await res.json().catch(() => ({}));
      showStatus(err.detail || "Kamera konnte nicht gelöscht werden.", "warning");
    }
  });

  // Save Settings Form (3 Tabs: Kameras, KI & Erkennung, Login)
  document.getElementById("settingsForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const targetCamId = settingsCameraId || selectedCameraId || "cam_1";
    const camPayload = {
      name: document.getElementById("cfgCamName").value.trim() || "Kamera 1",
      enabled: document.getElementById("cfgCamEnabled").checked,
      brand_preset: document.getElementById("cfgBrandPreset")?.value || "tapo",
      host: document.getElementById("cfgHost").value.trim(),
      rtsp_port: parseInt(document.getElementById("cfgRtspPort").value, 10) || 554,
      onvif_port: parseInt(document.getElementById("cfgOnvifPort").value, 10) || 2020,
      stream_quality: document.getElementById("cfgStreamPath")?.value.trim() || "stream1",
      onvif_profile: document.getElementById("cfgOnvifProfile")?.value.trim() || "profile_1",
      rtsp_user: document.getElementById("cfgRtspUser").value.trim(),
      rtsp_pass: document.getElementById("cfgRtspPass").value,
      cloud_password: document.getElementById("cfgCloudPass").value,
      auto_tracking: document.getElementById("cfgAutoTracking").checked,
      tracking_return_home: document.getElementById("cfgTrackingReturnHome").checked,
      tracking_speed: parseFloat(document.getElementById("trackingSpeedSlider")?.value || "1.4"),
      preset_names: {
        "1": document.getElementById("cfgPresetName1")?.value.trim() || "Tür / Eingang",
        "2": document.getElementById("cfgPresetName2")?.value.trim() || "Raum-Mitte",
        "3": document.getElementById("cfgPresetName3")?.value.trim() || "Schreibtisch",
      },
    };

    const guardPayload = {
      enabled: document.getElementById("cfgGuardEnabled").value === "true",
      detection_interval_sec: parseFloat(document.getElementById("cfgInterval").value) || 2.0,
      cooldown_minutes: parseInt(document.getElementById("cfgCooldown").value, 10) || 120,
      similarity_threshold: parseFloat(document.getElementById("cfgThreshold").value) || 0.38,
      active_hours_start: document.getElementById("cfgHoursStart").value || "06:00",
      active_hours_end: document.getElementById("cfgHoursEnd").value || "23:00",
      auto_privacy_after_greeting: document.getElementById("cfgAutoPrivacy").checked,
      greet_unknown: document.getElementById("cfgGreetUnknown").checked,
    };

    await fetch(`/api/cameras/${encodeURIComponent(targetCamId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(camPayload),
    });

    await fetch("/api/config/guard", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(guardPayload),
    });

    const curAuthPass = document.getElementById("cfgAuthCurrentPass")?.value || "";
    const newAuthUser = document.getElementById("cfgAuthNewUser")?.value.trim() || "";
    const newAuthPass = document.getElementById("cfgAuthNewPass")?.value || "";
    if (curAuthPass && newAuthPass) {
      const pwRes = await fetch("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: curAuthPass,
          new_username: newAuthUser || undefined,
          new_password: newAuthPass,
        }),
      });
      if (!pwRes.ok) {
        const err = await pwRes.json().catch(() => ({}));
        showStatus(err.detail || "Passwort-Änderung fehlgeschlagen.", "danger");
        return;
      }
      document.getElementById("cfgAuthCurrentPass").value = "";
      document.getElementById("cfgAuthNewPass").value = "";
    }

    bootstrap.Modal.getInstance(document.getElementById("settingsModal"))?.hide();
    showStatus("Kamera- & KI-Einstellungen gespeichert!", "success");
    loadConfig();
  });

  // Clear Events
  document.getElementById("btnClearEvents")?.addEventListener("click", async () => {
    await fetch("/api/events", { method: "DELETE" });
    loadEvents();
  });

  // -------------------- Auth & Boot --------------------
  let eventsTimer = null;
  const showMainApp = () => {
    const loginSec = document.getElementById("loginSection");
    const mainSec = document.getElementById("mainSection");
    if (loginSec) {
      loginSec.classList.add("d-none");
      loginSec.classList.remove("d-flex");
    }
    if (mainSec) mainSec.classList.remove("d-none");
    loadConfig();
    loadEvents();
    if (!eventsTimer) eventsTimer = setInterval(loadEvents, 5000);
  };

  const showLoginScreen = () => {
    if (eventsTimer) {
      clearInterval(eventsTimer);
      eventsTimer = null;
    }
    const loginSec = document.getElementById("loginSection");
    const mainSec = document.getElementById("mainSection");
    if (mainSec) mainSec.classList.add("d-none");
    if (loginSec) {
      loginSec.classList.remove("d-none");
      loginSec.classList.add("d-flex");
    }
    const streamImg = document.getElementById("liveStreamImg");
    if (streamImg) streamImg.removeAttribute("src");
  };

  document.getElementById("loginForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("loginUsername").value.trim();
    const password = document.getElementById("loginPassword").value;
    const alertEl = document.getElementById("loginAlert");
    alertEl?.classList.add("d-none");

    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (res.ok) {
      document.getElementById("loginPassword").value = "";
      showMainApp();
    } else {
      const err = await res.json().catch(() => ({}));
      if (alertEl) {
        alertEl.textContent = err.detail || "Anmeldung fehlgeschlagen.";
        alertEl.classList.remove("d-none");
      }
    }
  });

  document.getElementById("btnLogout")?.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    showLoginScreen();
  });

  const setPersonsOpen = (open) => {
    const drawer = document.getElementById("personsDrawer");
    const chevron = document.getElementById("personsChevronIcon");
    const btn = document.getElementById("btnTogglePersons");
    if (drawer) drawer.classList.toggle("open", open);
    if (chevron) chevron.classList.toggle("rotated", open);
    if (btn) btn.setAttribute("aria-expanded", String(open));
    localStorage.setItem("neoguard_persons_open", open ? "1" : "0");
  };

  setPersonsOpen(localStorage.getItem("neoguard_persons_open") === "1");

  const togglePersonsDrawer = () => {
    const drawer = document.getElementById("personsDrawer");
    const next = !(drawer && drawer.classList.contains("open"));
    setPersonsOpen(next);
  };

  document.getElementById("btnTogglePersons")?.addEventListener("click", togglePersonsDrawer);
  document.getElementById("personsPreviewBar")?.addEventListener("click", togglePersonsDrawer);
  document.getElementById("personsPreviewBar")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      togglePersonsDrawer();
    }
  });

  document.querySelectorAll("#langSelectTop, #langSelectLogin").forEach((sel) => {
    sel?.addEventListener("change", (e) => {
      window.neoI18n?.setLang(e.target.value);
      if (currentConfig) {
        loadConfig();
        loadEvents();
      }
    });
  });

  const bootApp = async () => {
    initTheme();
    window.neoI18n?.apply();
    try {
      const res = await fetch("/api/auth/status");
      const data = await res.json();
      if (data.authenticated) {
        showMainApp();
      } else {
        showLoginScreen();
      }
    } catch {
      showLoginScreen();
    }
  };

  bootApp();
})();
