// Transcription Service: page logic. No dependencies and no build step.
//
// The page moves between four views: upload, progress, result and failed.
// The job id lives in the address (#job=<id>), so a refresh or a bookmark returns
// to the same transcript.
//
// Security: file names and transcript text come from outside, so they are always
// inserted with textContent and never as HTML.

const JOBS_URL = "/api/transcriptions";
const POLL_INTERVAL_MS = 1000;
const SLOWEST_POLL_INTERVAL_MS = 5000;
const RECENT_STORAGE_KEY = "transcription-service.recent";
const RECENT_LIMIT = 8;

// Languages Whisper supports that are written right to left.
const RIGHT_TO_LEFT_LANGUAGES = new Set(["ar", "fa", "he", "ps", "sd", "ur", "yi"]);

const byId = (id) => document.getElementById(id);

const state = {
  config: null, // limits and options from GET /api/config
  file: null, // the file chosen in the form
  upload: null, // the XMLHttpRequest of an upload in progress
  jobId: null, // the job this page is showing
  playableJobId: null, // the job whose media file is still in memory, for playback
  pollTimer: null,
  pollDelay: POLL_INTERVAL_MS,
  firstProgress: null, // {time, progress}: the first progress we saw, for the estimate
  cues: [], // [{start, end, element}] of the transcript on screen
  activeCue: null,
  transcriptText: "",
};

// ---------- Formatting ----------

function formatBytes(bytes) {
  const units = ["bytes", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 || Number.isInteger(value) ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

/** Seconds as m:ss, or h:mm:ss for recordings of an hour or more. */
function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

function formatTimeAgo(isoTime) {
  const seconds = Math.round((Date.parse(isoTime) - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  if (Math.abs(seconds) < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

function languageName(code) {
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return code; // not a code the browser knows
  }
}

function extensionOf(fileName) {
  const dot = fileName.lastIndexOf(".");
  return dot > 0 ? fileName.slice(dot).toLowerCase() : "";
}

// ---------- Views ----------

const VIEWS = ["upload", "progress", "result", "failed"];

function setPageTitle(prefix = "") {
  document.title = prefix ? `${prefix} · Transcription Service` : "Transcription Service";
}

function showView(name, { moveFocus = true } = {}) {
  for (const view of VIEWS) {
    byId(`view-${view}`).hidden = view !== name;
  }
  if (moveFocus) {
    // Focus the heading so keyboard and screen-reader users land on the new view.
    byId(`view-${name}`).querySelector("h1").focus();
  }
}

function showUpload({ error = "", moveFocus = true } = {}) {
  stopPolling();
  state.jobId = null;
  setUploadError(error);
  renderRecent();
  setPageTitle();
  showView("upload", { moveFocus });
}

function showFailure(reason, title = "This file could not be transcribed") {
  stopPolling();
  byId("failed-title").textContent = title;
  byId("failed-reason").textContent = reason;
  setPageTitle("Failed");
  showView("failed");
}

// ---------- Choosing a file ----------

/** Why the file cannot be uploaded, or "" if it looks fine. The server checks again. */
function problemWith(file) {
  const config = state.config;
  if (file.size === 0) return "This file is empty.";
  if (!config) return "";
  const extension = extensionOf(file.name);
  if (!config.extensions.includes(extension)) {
    const kind = extension ? `${extension.slice(1).toUpperCase()} files` : "Files without an extension";
    return `${kind} can't be transcribed. Choose an audio or video file such as MP3, WAV, M4A or MP4.`;
  }
  if (file.size > config.max_upload_bytes) {
    return `This file is ${formatBytes(file.size)}. The limit is ${formatBytes(config.max_upload_bytes)}.`;
  }
  return "";
}

function chooseFile(file) {
  const problem = problemWith(file);
  setUploadError(problem);
  state.file = problem ? null : file;
  byId("dropzone").classList.toggle("has-file", state.file !== null);
  byId("start-button").disabled = state.file === null;
  if (state.file) {
    byId("dropzone-title").textContent = file.name;
    byId("file-hint").textContent = `${formatBytes(file.size)}. Click to choose a different file.`;
  }
}

function setUploadError(message) {
  const element = byId("upload-error");
  element.textContent = message;
  element.hidden = message === "";
}

function wireDropzone() {
  const dropzone = byId("dropzone");
  const input = byId("file-input");

  input.addEventListener("change", () => {
    if (input.files.length > 0) chooseFile(input.files[0]);
  });
  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    dropzone.addEventListener(type, () => dropzone.classList.remove("is-dragover"));
  }
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer.files.length > 0) chooseFile(event.dataTransfer.files[0]);
  });
  // A file dropped next to the drop zone must not make the browser navigate to it.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (event) => event.preventDefault());
  }
}

// ---------- Uploading ----------

function startUpload() {
  const file = state.file;
  if (!file || state.upload) return;

  const form = new FormData();
  form.append("file", file);
  const language = byId("language").value;
  if (language) form.append("language", language);

  // XMLHttpRequest rather than fetch(): only it reports how much has been uploaded.
  const request = new XMLHttpRequest();
  request.open("POST", JOBS_URL);
  request.responseType = "json";
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) renderUploadProgress(event.loaded, event.total);
  });
  request.addEventListener("load", () => finishUpload(request, file));
  request.addEventListener("error", () =>
    endUpload("The upload was interrupted. Check the connection and try again."),
  );
  request.addEventListener("abort", () => endUpload(""));

  state.upload = request;
  renderUploadStarted(file);
  request.send(form);
}

function finishUpload(request, file) {
  if (request.status !== 202) {
    const reason = request.response?.message ?? `The server refused the upload (status ${request.status}).`;
    endUpload(reason);
    return;
  }
  const job = request.response;
  state.upload = null;
  state.playableJobId = job.id;
  rememberRecent({ id: job.id, filename: file.name, createdAt: job.created_at });
  window.location.hash = `job=${job.id}`; // the hashchange handler opens the job
}

function endUpload(error) {
  state.upload = null;
  showUpload({ error });
}

function renderUploadStarted(file) {
  byId("progress-title").textContent = file.name;
  setStage("upload", { state: "active", name: "Uploading", figures: `0 of ${formatBytes(file.size)}` });
  setMeter("upload-meter", 0);
  byId("cancel-upload").hidden = false;
  setStage("transcribe", { state: "pending", name: "Transcribing", figures: "" });
  setMeter("transcribe-meter", 0);
  byId("timeline-end").textContent = "";
  byId("leave-note").hidden = true;
  setStatus("");
  showView("progress");
}

function renderUploadProgress(loaded, total) {
  const percent = Math.floor((loaded / total) * 100);
  setMeter("upload-meter", loaded / total);
  setStage("upload", {
    state: "active",
    name: loaded < total ? "Uploading" : "Checking the file",
    figures: `${percent}% · ${formatBytes(loaded)} of ${formatBytes(total)}`,
  });
}

// ---------- Following a job ----------

function openJob(jobId) {
  stopPolling();
  state.jobId = jobId;
  state.firstProgress = null;
  state.pollDelay = POLL_INTERVAL_MS;
  poll();
}

async function poll() {
  const jobId = state.jobId;
  state.pollTimer = null;
  let response;
  try {
    response = await fetch(`${JOBS_URL}/${encodeURIComponent(jobId)}`);
  } catch {
    response = null; // the network is down, or the server is restarting
  }
  if (state.jobId !== jobId) return; // the user moved on while we were waiting

  if (response?.status === 404) {
    forgetRecent(jobId);
    showFailure("This transcript no longer exists on the server.", "Transcript not found");
    return;
  }
  if (!response?.ok) {
    setStatus("Connection lost. Trying again.");
    state.pollDelay = Math.min(state.pollDelay * 2, SLOWEST_POLL_INTERVAL_MS);
    scheduleNextPoll();
    return;
  }

  const job = await response.json();
  state.pollDelay = POLL_INTERVAL_MS;
  if (job.status === "completed") {
    renderResult(job);
  } else if (job.status === "failed") {
    showFailure(job.error ?? "Transcription failed.");
  } else {
    renderJobProgress(job);
    scheduleNextPoll();
  }
}

function scheduleNextPoll() {
  state.pollTimer = window.setTimeout(poll, state.pollDelay);
}

function stopPolling() {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function renderJobProgress(job) {
  byId("progress-title").textContent = job.filename;
  setStage("upload", { state: "done", name: "Uploaded", figures: formatBytes(job.size_bytes) });
  setMeter("upload-meter", 1);
  byId("cancel-upload").hidden = true;
  byId("leave-note").hidden = false;
  renderTimelineScale(job.duration_seconds);

  if (job.status === "queued") {
    const ahead = job.queue_position ?? 0;
    setStage("transcribe", {
      state: "active",
      name: "Waiting in line",
      figures: ahead === 1 ? "1 file ahead" : `${ahead} files ahead`,
    });
    setMeter("transcribe-meter", null);
    setStatus("The transcription starts as soon as the files ahead are done.");
  } else if (job.progress === 0) {
    setStage("transcribe", { state: "active", name: "Preparing the audio", figures: "" });
    setMeter("transcribe-meter", null);
    setStatus("");
  } else {
    setStage("transcribe", { state: "active", name: "Transcribing", figures: progressFigures(job) });
    setMeter("transcribe-meter", job.progress);
    setStatus("");
  }
  setPageTitle(job.progress > 0 ? `${Math.round(job.progress * 100)}%` : "Waiting");
  if (byId("view-progress").hidden) showView("progress");
}

function progressFigures(job) {
  const position = job.duration_seconds
    ? `${formatClock(job.progress * job.duration_seconds)} of ${formatClock(job.duration_seconds)}`
    : `${Math.round(job.progress * 100)}%`;
  const remaining = estimateRemaining(job.progress);
  return remaining ? `${position} · ${remaining}` : position;
}

/** A rough estimate from the progress made since this page started watching. */
function estimateRemaining(progress) {
  const now = Date.now();
  if (!state.firstProgress) {
    state.firstProgress = { time: now, progress };
    return "";
  }
  const gained = progress - state.firstProgress.progress;
  const elapsedSeconds = (now - state.firstProgress.time) / 1000;
  if (gained < 0.02 || elapsedSeconds < 3) return ""; // too early to say
  const remainingSeconds = ((1 - progress) / gained) * elapsedSeconds;
  if (remainingSeconds < 60) return "less than a minute left";
  return `about ${Math.round(remainingSeconds / 60)} min left`;
}

/** Draw the ticks of the timeline: the meter is a ruler of the recording itself. */
function renderTimelineScale(durationSeconds) {
  const meter = byId("transcribe-meter");
  if (!durationSeconds) {
    meter.style.setProperty("--ticks", "1");
    byId("timeline-end").textContent = "";
    return;
  }
  // The smallest step that keeps the ruler readable (at most 60 ticks).
  const steps = [1, 5, 10, 30, 60, 300, 600, 1800, 3600];
  const step = steps.find((seconds) => durationSeconds / seconds <= 60) ?? 3600;
  meter.style.setProperty("--ticks", String(durationSeconds / step));
  byId("timeline-end").textContent = formatClock(durationSeconds);
}

function setStage(stage, { state: stageState, name, figures }) {
  const element = byId(`stage-${stage}`);
  element.classList.toggle("is-pending", stageState === "pending");
  element.classList.toggle("is-done", stageState === "done");
  byId(`${stage}-stage-name`).textContent = name;
  byId(`${stage}-figures`).textContent = figures;
}

/** fraction is 0..1, or null when there is nothing to measure yet. */
function setMeter(id, fraction) {
  const meter = byId(id);
  meter.classList.toggle("is-indeterminate", fraction === null);
  if (fraction === null) {
    meter.removeAttribute("aria-valuenow");
    return;
  }
  meter.style.setProperty("--progress", String(fraction));
  meter.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
}

function setStatus(message) {
  const element = byId("progress-status");
  if (element.textContent !== message) element.textContent = message; // announce changes only
}

// ---------- The transcript ----------

function renderResult(job) {
  byId("result-title").textContent = job.filename;
  const facts = [
    job.language ? languageName(job.language) : null,
    job.duration_seconds ? formatClock(job.duration_seconds) : null,
    job.segments.length === 1 ? "1 segment" : `${job.segments.length} segments`,
  ];
  byId("result-facts").textContent = facts.filter(Boolean).join(" · ");

  for (const link of document.querySelectorAll(".downloads__link")) {
    link.href = `${JOBS_URL}/${encodeURIComponent(job.id)}/download?format=${link.dataset.format}`;
  }

  const playable = preparePlayer(job);
  renderTranscript(job.segments, playable);
  state.transcriptText = job.text;
  byId("transcript").lang = job.language ?? "";
  byId("transcript").dir = RIGHT_TO_LEFT_LANGUAGES.has(job.language) ? "rtl" : "ltr";
  byId("transcript").hidden = job.segments.length === 0;
  byId("no-speech").hidden = job.segments.length > 0;
  byId("copy-button").disabled = job.segments.length === 0;
  byId("copy-button").textContent = "Copy text";
  setPageTitle("Transcript ready");
  showView("result");
}

function renderTranscript(segments, playable) {
  const list = byId("transcript");
  const lines = document.createDocumentFragment();
  state.cues = [];
  state.activeCue = null;

  for (const segment of segments) {
    const item = document.createElement("li");
    item.className = "cue";

    const time = document.createElement(playable ? "button" : "span");
    time.className = "cue__time";
    time.dir = "ltr";
    time.textContent = formatClock(segment.start);
    if (playable) {
      time.type = "button";
      time.dataset.start = String(segment.start);
      time.setAttribute("aria-label", `Play from ${formatClock(segment.start)}`);
    }

    const text = document.createElement("p");
    text.className = "cue__text";
    text.textContent = segment.text;

    item.append(time, text);
    lines.append(item);
    state.cues.push({ start: segment.start, end: segment.end, element: item });
  }
  list.replaceChildren(lines);
}

/**
 * The media file never has to come back from the server: while the page that
 * uploaded it is still open, the browser can play the local file itself.
 */
function preparePlayer(job) {
  const player = byId("player");
  if (player.src) {
    URL.revokeObjectURL(player.src);
    player.removeAttribute("src");
  }
  const file = state.file;
  const playable =
    file !== null && state.playableJobId === job.id && player.canPlayType(file.type) !== "";
  if (playable) player.src = URL.createObjectURL(file);
  player.hidden = !playable;
  return playable;
}

function playFrom(seconds) {
  const player = byId("player");
  player.currentTime = seconds;
  player.play();
}

/** Mark the line that is being spoken, like a highlighter moving down the page. */
function highlightSpokenCue() {
  const player = byId("player");
  const time = player.currentTime;
  const index = state.cues.findLastIndex((cue) => cue.start <= time);
  const cue = index >= 0 && time <= state.cues[index].end + 0.3 ? state.cues[index] : null;
  if (cue === state.activeCue) return;
  state.activeCue?.element.classList.remove("is-active");
  cue?.element.classList.add("is-active");
  state.activeCue = cue;
  if (cue && !player.paused) cue.element.scrollIntoView({ block: "nearest" });
}

async function copyTranscript() {
  const button = byId("copy-button");
  try {
    await navigator.clipboard.writeText(state.transcriptText);
    button.textContent = "Copied";
  } catch {
    // No clipboard access (an insecure origin, for example): select the text instead.
    const range = document.createRange();
    range.selectNodeContents(byId("transcript"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    button.textContent = "Selected. Press Ctrl+C to copy";
  }
  window.setTimeout(() => (button.textContent = "Copy text"), 2500);
}

// ---------- Recent transcripts (kept in this browser only) ----------

function readRecent() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(RECENT_STORAGE_KEY));
    return Array.isArray(stored) ? stored : [];
  } catch {
    return []; // storage is unavailable or holds something unexpected
  }
}

function writeRecent(entries) {
  try {
    window.localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(entries.slice(0, RECENT_LIMIT)));
  } catch {
    // Private browsing can refuse storage. The list is a convenience, so carry on.
  }
}

function rememberRecent(entry) {
  writeRecent([entry, ...readRecent().filter((other) => other.id !== entry.id)]);
}

function forgetRecent(jobId) {
  writeRecent(readRecent().filter((entry) => entry.id !== jobId));
}

function renderRecent() {
  const entries = readRecent();
  const items = entries.map((entry) => {
    const item = document.createElement("li");
    item.className = "recent__item";

    const link = document.createElement("a");
    link.className = "recent__link";
    link.href = `#job=${encodeURIComponent(entry.id)}`;
    link.dir = "auto";
    link.textContent = entry.filename;

    const time = document.createElement("span");
    time.className = "recent__time";
    time.textContent = formatTimeAgo(entry.createdAt);

    item.append(link, time);
    return item;
  });
  byId("recent-list").replaceChildren(...items);
  byId("recent").hidden = entries.length === 0;
}

// ---------- Start-up ----------

async function loadConfig() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) return;
    state.config = await response.json();
  } catch {
    return; // the form still works; the server validates every upload anyway
  }
  const { extensions, languages, max_upload_bytes: maxBytes, model } = state.config;

  byId("file-input").accept = extensions.join(",");
  const common = ["MP3", "WAV", "M4A", "MP4", "MOV", "MKV", "WebM"].filter((name) =>
    extensions.includes(`.${name.toLowerCase()}`),
  );
  byId("file-hint").textContent = `${common.join(", ")} and more, up to ${formatBytes(maxBytes)}`;
  byId("model-name").textContent = `Whisper (${model})`;

  const options = languages
    .map((code) => ({ code, name: languageName(code) }))
    .sort((a, b) => a.name.localeCompare(b.name))
    .map(({ code, name }) => new Option(name, code));
  byId("language").append(...options);
}

/** Show what the address asks for: a job (#job=<id>) or the upload form. */
function route({ moveFocus = true } = {}) {
  if (state.upload) state.upload.abort();
  const jobId = new URLSearchParams(window.location.hash.slice(1)).get("job");
  if (jobId) {
    openJob(jobId);
  } else {
    showUpload({ moveFocus });
  }
}

function leaveJob() {
  // Drop "#job=..." from the address without adding a history entry, then show the form.
  window.history.replaceState(null, "", window.location.pathname);
  route();
}

function init() {
  wireDropzone();
  byId("upload-form").addEventListener("submit", (event) => {
    event.preventDefault();
    startUpload();
  });
  byId("cancel-upload").addEventListener("click", () => state.upload?.abort());
  byId("copy-button").addEventListener("click", copyTranscript);
  byId("another-button").addEventListener("click", leaveJob);
  byId("retry-button").addEventListener("click", leaveJob);
  byId("player").addEventListener("timeupdate", highlightSpokenCue);
  byId("transcript").addEventListener("click", (event) => {
    const timeButton = event.target.closest("button.cue__time");
    if (timeButton) playFrom(Number(timeButton.dataset.start));
  });

  window.addEventListener("hashchange", () => route());
  document.addEventListener("visibilitychange", () => {
    // Browsers slow down timers in background tabs, so refresh at once on return.
    const waitingForNextPoll = state.pollTimer !== null;
    if (!document.hidden && waitingForNextPoll) {
      stopPolling();
      poll();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.upload) event.preventDefault(); // closing the tab would cancel the upload
  });

  loadConfig();
  route({ moveFocus: false });
}

init();
