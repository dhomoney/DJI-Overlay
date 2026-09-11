/* The whole client. No build step and no framework: the page has one list, one
   image and one form, and the server does everything that is actually hard. */

const state = {
  config: null,
  clips: [],
  selected: null,
  flight: null,
  time: 0,
  previewToken: 0,
};

const el = (id) => document.getElementById(id);
const SETTING_IDS = [
  "mode", "alpha_codec", "template",
  "altitude_unit", "speed_unit", "vspeed_unit", "distance_unit",
  "opacity", "show_msl", "home", "srt_offset", "refresh_hz",
  "encoder", "crf", "preset", "start", "duration",
];
const STORAGE_KEY = "dji-overlay.settings";

/* -- helpers -------------------------------------------------------------- */

function clock(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function bytes(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

async function getJSON(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `${response.status} ${response.statusText}`);
  return body;
}

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label ?? value;
  return node;
}

/* -- settings ------------------------------------------------------------- */

function readSettings() {
  const settings = {};
  for (const id of SETTING_IDS) {
    const node = el(id);
    if (!node) continue;
    if (node.type === "checkbox") settings[id] = node.checked;
    else if (node.type === "number" || id === "opacity") {
      settings[id] = node.value === "" ? null : Number(node.value);
    } else settings[id] = node.value;
  }
  if (settings.home !== undefined && settings.home.trim() === "") settings.home = null;
  return settings;
}

function applySettings(settings) {
  for (const [id, value] of Object.entries(settings || {})) {
    const node = el(id);
    if (!node || value === null || value === undefined) continue;
    if (node.type === "checkbox") node.checked = Boolean(value);
    else node.value = value;
  }
  syncFormState();
}

function syncFormState() {
  el("alpha-codec-field").hidden = el("mode").value !== "alpha";
  el("opacity-value").textContent = `${Math.round(Number(el("opacity").value) * 100)}%`;
  updateOutputHint();
}

function updateOutputHint() {
  if (!state.selected) return;
  const settings = readSettings();
  const name = state.selected.name.replace(/\.[^.]+$/, "");
  const suffix = state.selected.name.match(/\.[^.]+$/)?.[0].toLowerCase() || ".mp4";
  const out = settings.mode === "alpha"
    ? `${name}_overlay${settings.alpha_codec === "png" ? "_%06d.png" : ".mov"}`
    : `${name}_hud${suffix}`;
  el("out-hint").textContent = `Writes ${out} next to the source.`;
}

function saveSettings() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(readSettings()));
  } catch { /* private windows and full quotas are not worth a broken page */ }
}

function restoreSettings() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch { stored = null; }
  applySettings({ ...state.config.defaults, ...(stored || {}) });
}

/* -- library -------------------------------------------------------------- */

async function loadLibrary() {
  const data = await getJSON("/api/library");
  state.clips = data.clips;
  const list = el("clip-list");
  list.replaceChildren();
  el("clip-count").textContent = `${data.clips.length} clip${data.clips.length === 1 ? "" : "s"}`;
  el("clip-empty").hidden = data.clips.length > 0;

  for (const clip of data.clips) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.className = "clip";
    button.disabled = !clip.ready;
    button.dataset.path = clip.path;

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = clip.name;

    const meta = document.createElement("div");
    meta.className = "meta";
    const facts = [];
    if (clip.resolution) facts.push(clip.resolution);
    if (clip.duration) facts.push(clock(clip.duration));
    if (clip.size) facts.push(bytes(clip.size));
    meta.append(document.createTextNode(facts.join(" · ")));

    if (!clip.ready) {
      const tag = document.createElement("span");
      tag.className = "tag missing";
      tag.textContent = "no .SRT";
      meta.append(tag);
    }
    if (clip.rendered) {
      const tag = document.createElement("span");
      tag.className = "tag done";
      tag.textContent = "rendered";
      meta.append(tag);
    }

    button.append(name, meta);
    button.addEventListener("click", () => selectClip(clip));
    item.append(button);
    list.append(item);
  }

  if (state.selected) markSelected(state.selected.path);
}

function markSelected(path) {
  for (const button of document.querySelectorAll(".clip")) {
    button.setAttribute("aria-current", String(button.dataset.path === path));
  }
}

async function selectClip(clip) {
  state.selected = clip;
  markSelected(clip.path);
  el("placeholder").hidden = true;
  el("work").hidden = false;
  el("clip-title").textContent = clip.name;
  el("flight-facts").replaceChildren();
  el("flight-warnings").replaceChildren();
  updateOutputHint();

  try {
    state.flight = await getJSON(
      `/api/flight?path=${encodeURIComponent(clip.path)}&home=${encodeURIComponent(readSettings().home || "")}`
    );
  } catch (error) {
    showPreviewState(error.message, true);
    return;
  }

  renderFlight(state.flight);

  const duration = state.flight.video.duration || state.flight.duration || 0;
  el("scrub").max = String(duration);
  el("scrub-total").textContent = `/ ${clock(duration)}`;
  // A third of the way in is usually airborne, where the HUD is worth looking at.
  setTime(Math.min(duration * 0.33, Math.max(0, duration - 1)));
}

function renderFlight(flight) {
  const units = readSettings();
  const list = el("flight-facts");
  list.replaceChildren();

  const rows = [
    ["Video", `${flight.video.resolution} · ${flight.video.fps.toFixed(2)} fps · ${flight.video.codec}`],
    ["Flight time", flight.flight_time],
    ["Samples", flight.samples.toLocaleString()],
    ["Recorded", flight.recorded ? flight.recorded.replace("T", " ") : "—"],
    ["Max height", formatSI(flight.max_rel_alt, units.altitude_unit, "altitude")],
    ["Max speed", formatSI(flight.max_speed, units.speed_unit, "speed")],
    ["Max distance", formatSI(flight.max_distance, units.distance_unit, "distance")],
    ["Path length", formatSI(flight.path_length, units.distance_unit, "distance")],
    ["Home", flight.home ? `${flight.home[0].toFixed(6)}, ${flight.home[1].toFixed(6)}` : "—"],
  ];
  for (const [term, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }

  const warnings = el("flight-warnings");
  warnings.replaceChildren();
  const notes = [...flight.warnings];
  if (flight.gps_rate && flight.fps && flight.gps_rate < flight.fps * 0.9) {
    notes.push(
      `GPS updates at ~${flight.gps_rate.toFixed(1)} Hz and altitude at ` +
      `~${(flight.alt_rate || 0).toFixed(1)} Hz, below the ${flight.fps.toFixed(0)} fps ` +
      `frame rate. Speed, vertical speed and heading are smoothed before differentiation.`
    );
  }
  for (const note of notes) {
    const div = document.createElement("div");
    div.className = "note";
    div.textContent = note;
    warnings.append(div);
  }
}

/* Conversion tables are small enough to mirror here so the facts panel can
   follow the unit dropdowns without a round trip. */
const FACTORS = {
  altitude: { m: [1, 0], ft: [3.280839895, 0] },
  speed: { "m/s": [1, 1], "km/h": [3.6, 1], mph: [2.236936292, 1], kn: [1.943844492, 1] },
  distance: { m: [1, 0], km: [0.001, 2], ft: [3.280839895, 0], mi: [0.000621371192, 2] },
};

function formatSI(value, unit, kind) {
  const entry = FACTORS[kind]?.[unit];
  if (value === null || value === undefined || !entry) return "—";
  const [factor, places] = entry;
  return `${(value * factor).toFixed(places)} ${unit}`;
}

/* -- preview -------------------------------------------------------------- */

let previewTimer = null;

function setTime(seconds) {
  state.time = Math.max(0, seconds);
  el("scrub").value = String(state.time);
  el("scrub-time").textContent = clock(state.time);
  schedulePreview();
}

function schedulePreview(delay = 140) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadPreview, delay);
}

function showPreviewState(message, isError = false) {
  const node = el("preview-state");
  node.textContent = message;
  node.classList.toggle("error", isError);
}

function loadPreview() {
  if (!state.selected) return;
  const settings = readSettings();
  const params = new URLSearchParams({
    path: state.selected.path,
    t: state.time.toFixed(2),
  });
  for (const [key, value] of Object.entries(settings)) {
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }

  const token = ++state.previewToken;
  el("preview-frame").classList.add("loading");
  showPreviewState("rendering…");

  const image = new Image();
  image.onload = () => {
    if (token !== state.previewToken) return;   // a newer request already won
    el("preview-image").src = image.src;
    el("preview-frame").classList.remove("loading");
    showPreviewState("");
  };
  image.onerror = async () => {
    if (token !== state.previewToken) return;
    el("preview-frame").classList.remove("loading");
    try {
      await getJSON(`/api/preview?${params.toString()}`);
      showPreviewState("Preview failed", true);
    } catch (error) {
      showPreviewState(error.message, true);
    }
  };
  image.src = `/api/preview?${params.toString()}`;
}

/* -- jobs ----------------------------------------------------------------- */

const jobNodes = new Map();

function renderJob(job) {
  let node = jobNodes.get(job.id);
  if (!node) {
    node = document.createElement("li");
    node.className = "job";
    node.innerHTML = `
      <div class="title"></div>
      <div class="right"><span class="state"></span> <button class="ghost cancel" type="button">Cancel</button></div>
      <div class="detail"></div>
      <div class="progress"><span></span></div>`;
    node.querySelector(".cancel").addEventListener("click", () => {
      fetch(`/api/jobs/${job.id}/cancel`, { method: "POST" });
    });
    jobNodes.set(job.id, node);
    el("job-list").prepend(node);
  }

  node.dataset.state = job.state;
  node.querySelector(".title").textContent = `${job.name} → ${job.output}`;
  node.querySelector(".state").textContent = job.state;
  node.querySelector(".progress span").style.width = `${job.state === "done" ? 100 : job.percent}%`;
  node.querySelector(".cancel").hidden = job.state === "done" || job.state === "failed" || job.state === "cancelled";

  const detail = node.querySelector(".detail");
  detail.classList.toggle("error", job.state === "failed");
  detail.classList.toggle("warn", job.state === "cancelled" || (job.warnings || []).length > 0);

  if (job.state === "running") {
    const eta = job.eta === null ? "" : ` · ${clock(job.eta)} left`;
    detail.textContent =
      `${job.done_frames.toLocaleString()} / ${job.total_frames.toLocaleString()} frames` +
      ` · ${clock(job.elapsed)} elapsed${eta}`;
  } else if (job.state === "done") {
    const s = job.summary || {};
    const reuse = s.reuse !== undefined ? ` · ${Math.round(s.reuse * 100)}% of frames reused` : "";
    detail.textContent =
      `${(s.frames || 0).toLocaleString()} frames in ${clock(job.elapsed)}${reuse}` +
      ((job.warnings || []).length ? ` · ${job.warnings.join(" ")}` : "");
  } else if (job.error) {
    detail.textContent = job.error;
  } else {
    detail.textContent = "waiting for the renderer";
  }

  el("job-empty").hidden = jobNodes.size > 0;
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onopen = () => { el("job-connection").textContent = "live"; };
  source.onerror = () => { el("job-connection").textContent = "reconnecting…"; };
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "snapshot") {
      for (const job of [...payload.jobs].reverse()) renderJob(job);
    } else if (payload.type === "job") {
      renderJob(payload.job);
      if (payload.job.state === "done") loadLibrary();
    }
  };
}

/* -- wiring --------------------------------------------------------------- */

async function submitJob(event) {
  event.preventDefault();
  if (!state.selected) return;
  const button = el("render");
  button.disabled = true;
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.selected.path, settings: readSettings() }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "The render could not be queued");
    renderJob(body);
    el("jobs-section").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    showPreviewState(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function wire() {
  el("settings").addEventListener("submit", submitJob);
  el("settings").addEventListener("input", () => {
    syncFormState();
    saveSettings();
    if (state.flight) renderFlight(state.flight);
    schedulePreview(250);
  });
  el("scrub").addEventListener("input", (event) => setTime(Number(event.target.value)));
  el("preview-here").addEventListener("click", () => loadPreview());
  el("refresh").addEventListener("click", loadLibrary);

  // Arrow keys nudge the scrubber a second at a time, shift for ten.
  document.addEventListener("keydown", (event) => {
    if (!state.selected || event.target.matches("input, select, textarea")) return;
    const step = event.shiftKey ? 10 : 1;
    if (event.key === "ArrowRight") setTime(state.time + step);
    else if (event.key === "ArrowLeft") setTime(state.time - step);
    else return;
    event.preventDefault();
  });
}

async function start() {
  state.config = await getJSON("/api/config");
  el("media-root").textContent = state.config.media_root;
  el("version").textContent = `v${state.config.version}`;

  for (const [kind, values] of Object.entries(state.config.units)) {
    const select = el(`${kind}_unit`);
    if (select) select.replaceChildren(...values.map((value) => option(value)));
  }
  el("template").replaceChildren(...state.config.templates.map((name) => option(name)));

  restoreSettings();
  wire();
  await loadLibrary();
  connectEvents();
}

start().catch((error) => {
  document.body.prepend(
    Object.assign(document.createElement("div"), {
      className: "note",
      textContent: `The server is not answering: ${error.message}`,
    })
  );
});
