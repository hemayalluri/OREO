// Oreo's face: tap to talk, local speech-to-text on the server, spoken answers in the browser.
"use strict";

const $ = (id) => document.getElementById(id);
const screen = $("screen");
const ui = { heard: $("heard"), said: $("said"), meta: $("meta"), status: $("status") };

const state = {
  face: "sleeping",     // sleeping | listening | recording | thinking | speaking (+ scanning/rejected in step 6)
  awake: false,
  busy: false,          // a question is being transcribed or answered
  config: { name: "Oreo", boss_name: "", session_timeout_seconds: 120, voice: {} },
  idleTimer: null,
  socket: null,
  pending: null,        // resolves when the answer for the current question arrives
};

// ---------------------------------------------------------------------------
// Face and captions
// ---------------------------------------------------------------------------

const STATUS = {
  sleeping: "Tap Oreo to wake it up",
  listening: "Tap Oreo to ask a question",
  recording: "Listening… (tap again when you're done)",
  thinking: "Thinking…",
  speaking: "Speaking… (tap to stop)",
};

function setFace(face, status) {
  state.face = face;
  screen.dataset.face = face;
  ui.status.textContent = status || STATUS[face] || "";
}

function caption(heard, said, meta) {
  if (heard !== undefined) ui.heard.textContent = heard ? `“${heard}”` : "";
  if (said !== undefined) ui.said.textContent = said || "";
  if (meta !== undefined) ui.meta.textContent = meta || "";
}

// Go back to sleep after the session timeout with no activity (matches the backend).
function touch() {
  clearTimeout(state.idleTimer);
  state.idleTimer = setTimeout(sleep, state.config.session_timeout_seconds * 1000);
}

function sleep() {
  state.awake = false;
  stopSpeaking();
  send({ type: "reset" });
  caption("", "", "");
  setFace("sleeping");
}

// ---------------------------------------------------------------------------
// Speaking (the device's own text-to-speech: free and offline on macOS/iPadOS)
// ---------------------------------------------------------------------------

let voice = null;
function pickVoice() {
  const voices = speechSynthesis.getVoices();
  const wanted = (state.config.voice.tts_voice || "").toLowerCase();
  voice = (wanted && voices.find((v) => v.name.toLowerCase().includes(wanted)))
    || voices.find((v) => v.lang === "en-US" && v.localService)
    || voices.find((v) => v.lang.startsWith("en")) || null;
}
if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = pickVoice;

function speak(text, face = "speaking") {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window) || !text) return resolve();
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    if (!voice) pickVoice();
    if (voice) u.voice = voice;
    u.rate = state.config.voice.tts_rate || 1.0;
    u.pitch = state.config.voice.tts_pitch || 1.15;
    // Safety net: some browsers never fire onend (no voices installed, tab in background).
    const guard = setTimeout(resolve, 4000 + text.split(/\s+/).length * 600);
    u.onend = u.onerror = () => { clearTimeout(guard); resolve(); };
    if (face) setFace(face);
    speechSynthesis.speak(u);
  });
}

function stopSpeaking() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}

// iOS only lets a page speak after a tap; speaking an empty line during the first tap unlocks it.
let unlocked = false;
function unlockSpeech() {
  if (unlocked || !("speechSynthesis" in window)) return;
  speechSynthesis.speak(new SpeechSynthesisUtterance(""));
  unlocked = true;
}

// ---------------------------------------------------------------------------
// Recording with automatic stop when you stop talking
// ---------------------------------------------------------------------------

const rec = { stream: null, recorder: null, chunks: [], ctx: null, timer: null };
// Speech counts as "loud" relative to the room's own noise, measured in the first half second,
// so quiet Mac microphones (with noise suppression on) still register.
const SILENCE_MS = 1800, NO_SPEECH_MS = 8000, MAX_MS = 20000, CALIBRATE_MS = 500, MIN_LEVEL = 0.004;

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    caption("", "This browser can't use the microphone here. Type your question below instead.", "");
    return;
  }
  try {
    rec.stream = rec.stream || await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch {
    caption("", "I need microphone permission to hear you. You can also type below.", "");
    return;
  }
  const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported?.(t)) || "";
  rec.chunks = [];
  rec.recorder = new MediaRecorder(rec.stream, type ? { mimeType: type } : undefined);
  rec.recorder.ondataavailable = (e) => e.data.size && rec.chunks.push(e.data);
  rec.recorder.onstop = onRecordingDone;
  rec.recorder.start(250);
  setFace("recording");
  touch();
  watchLevel();
}

// Stop after a short silence once speech has started, or give up if nobody speaks.
function watchLevel() {
  rec.ctx = rec.ctx || new (window.AudioContext || window.webkitAudioContext)();
  const source = rec.ctx.createMediaStreamSource(rec.stream);
  const analyser = rec.ctx.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  const started = performance.now();
  let heardSpeech = false, lastLoud = started, noise = 0, samples = 0, level = MIN_LEVEL;

  clearInterval(rec.timer);
  rec.timer = setInterval(() => {
    analyser.getFloatTimeDomainData(buf);
    const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length);
    const now = performance.now();
    if (now - started < CALIBRATE_MS) {
      noise += rms; samples += 1;
      level = Math.max(MIN_LEVEL, (noise / samples) * 2.5);
      return;
    }
    if (rms > level) { heardSpeech = true; lastLoud = now; }
    const done = (heardSpeech && now - lastLoud > SILENCE_MS)
      || (!heardSpeech && now - started > NO_SPEECH_MS)
      || now - started > MAX_MS;
    if (done) stopRecording();
  }, 100);
}

function stopRecording() {
  clearInterval(rec.timer);
  if (rec.recorder && rec.recorder.state !== "inactive") rec.recorder.stop();
}

async function onRecordingDone() {
  const blob = new Blob(rec.chunks, { type: rec.recorder.mimeType || "audio/webm" });
  const ext = blob.type.includes("mp4") ? ".mp4" : ".webm";
  setFace("thinking", "Working out what you said…");
  state.busy = true;
  try {
    const form = new FormData();
    form.append("audio", blob, "question" + ext);
    const res = await fetch("/api/transcribe", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "transcription failed");
    if (!data.text) {
      state.busy = false;
      await speak("Sorry, I didn't catch that.");
      setFace("listening");
      return;
    }
    await ask(data.text);
  } catch (err) {
    state.busy = false;
    caption("", "I couldn't hear that properly. Please try again, or type below.", String(err.message || err));
    setFace("listening");
  }
}

// ---------------------------------------------------------------------------
// Talking to the brain over a WebSocket
// ---------------------------------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  socket.onmessage = (e) => onServerMessage(JSON.parse(e.data));
  socket.onclose = () => { state.socket = null; setTimeout(connect, 1500); };
  state.socket = socket;
}

function send(msg) {
  if (state.socket?.readyState === WebSocket.OPEN) { state.socket.send(JSON.stringify(msg)); return true; }
  return false;
}

async function onServerMessage(msg) {
  if (msg.type === "state" && msg.state === "thinking") {
    setFace("thinking");
    if (msg.filler) speak(msg.filler, null);   // "Let me check the sales data." while tools run
  } else if (msg.type === "answer" && state.pending) {
    state.pending(msg);
    state.pending = null;
  }
}

async function ask(text) {
  state.busy = true;
  caption(text, "", "");
  setFace("thinking");
  touch();
  const answer = await new Promise((resolve) => {
    state.pending = resolve;
    if (!send({ type: "ask", text })) {
      state.pending = null;
      resolve({ text: "I can't reach my brain right now. Is the Oreo server running?", error: true });
    }
  });
  const meta = [];
  if (answer.sources?.length) meta.push(answer.sources.join(", "));
  if (answer.as_of) meta.push(`as of ${answer.as_of}`);
  caption(undefined, answer.text, meta.join(" · "));
  await speak(answer.text);
  state.busy = false;
  if (state.awake) setFace("listening");
  touch();
}

// ---------------------------------------------------------------------------
// Input: tap the face, or type
// ---------------------------------------------------------------------------

$("face").addEventListener("click", async () => {
  unlockSpeech();
  if (state.face === "recording") return stopRecording();
  if (state.face === "speaking") { stopSpeaking(); return; }
  if (state.busy) return;
  if (!state.awake) {
    state.awake = true;
    const who = state.config.boss_name ? ` ${state.config.boss_name}` : "";
    caption("", `Hi${who}! What would you like to know?`, "");
    await speak(`Hi${who}! What would you like to know?`, "speaking");
  }
  await startRecording();
});

$("typed").addEventListener("submit", async (e) => {
  e.preventDefault();
  unlockSpeech();
  const input = $("typed-text");
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  state.awake = true;
  await ask(text);
});

// ---------------------------------------------------------------------------

(async function init() {
  try {
    const res = await fetch("/api/health");
    Object.assign(state.config, await res.json());
  } catch { /* defaults are fine */ }
  document.title = state.config.name;
  if ("speechSynthesis" in window) pickVoice();
  connect();
  setFace("sleeping");
})();
