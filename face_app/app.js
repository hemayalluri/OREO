// Oreo's face: tap to talk, local speech-to-text on the server, spoken answers in the browser.
"use strict";

const $ = (id) => document.getElementById(id);
const screen = $("screen");
const ui = { heard: $("heard"), said: $("said"), meta: $("meta"), status: $("status") };

const state = {
  face: "sleeping",     // sleeping | listening | recording | thinking | speaking (+ scanning/rejected in step 6)
  awake: false,
  busy: false,          // a question is being transcribed or answered
  config: { name: "Oreo", boss_name: "", session_timeout_seconds: 120, voice: {}, lines: {} },
  idleTimer: null,
  socket: null,
  pending: null,        // resolves when the answer for the current question arrives
  lastHeard: 0,
  wakeListening: false, // listening for "Hey Oreo" while asleep (starts after the first tap)         // when someone last asked something (hands-free sleeps after a quiet spell)
};

// ---------------------------------------------------------------------------
// Face and captions
// ---------------------------------------------------------------------------

const STATUS = {
  sleeping: "Tap Oreo to wake it up",
  listening: "Tap Oreo to ask a question",
  recording: "Listening… (tap again when you're done)",
  handsfree: "Listening… just talk (say “okay bye” or tap to put me to sleep)",
  wake: "Say “Hey Oreo” to wake me (or tap)",
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
  state.busy = false;
  cancelRecording();
  stopSpeaking();
  send({ type: "reset" });
  caption("", "", "");
  setFace("sleeping", state.wakeListening && wakeWord() ? STATUS.wake : undefined);
  listenForWakeWord();
}

// ---------------------------------------------------------------------------
// Speaking (the device's own text-to-speech: free and offline on macOS/iPadOS)
// ---------------------------------------------------------------------------

let voice = null;
function pickVoice() {
  const voices = speechSynthesis.getVoices();
  // tts_voice is a comma-separated preference list; the first installed one wins.
  const wanted = (state.config.voice.tts_voice || "").toLowerCase().split(",").map((w) => w.trim()).filter(Boolean);
  for (const w of wanted) {
    voice = voices.find((v) => v.lang.replace("_", "-").startsWith("en-US") && v.name.toLowerCase().includes(w))
      || voices.find((v) => v.name.toLowerCase().includes(w));
    if (voice) return;
  }
  voice = voices.find((v) => v.lang === "en-US" && v.localService)
    || voices.find((v) => v.lang.startsWith("en")) || null;
}
if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = pickVoice;

// On a Mac the server speaks with the chosen system voice (e.g. Ava Premium); the page plays it.
// One reused <audio> element: Safari only lets a page play sound without a tap if that
// element was first started during a tap (unlockSpeech does that with a silent clip).
const audioEl = new Audio();
let player = null;
const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
async function speakWithServer(text, face) {
  if (!state.config.voice.server_voice) return false;
  let url;
  try {
    const res = await fetch("/api/speak", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
    if (!res.ok) return false;
    url = URL.createObjectURL(await res.blob());
  } catch { return false; }
  stopSpeaking();
  if (face) setFace(face);
  return new Promise((resolve) => {
    let finished = false;
    const finish = (ok) => {
      if (finished) return;
      finished = true;
      audioEl.onended = audioEl.onerror = null;
      URL.revokeObjectURL(url);
      player = null;
      resolve(ok);
    };
    audioEl.src = url;
    player = () => { audioEl.pause(); finish(true); };  // stopSpeaking() calls this
    audioEl.onended = audioEl.onerror = () => finish(true);
    audioEl.play().catch(() => finish(false));
  });
}

async function speak(text, face = "speaking") {
  if (text && await speakWithServer(text, face)) return;
  return speakInBrowser(text, face);
}

function speakInBrowser(text, face = "speaking") {
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
  if (player) player();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}

// iOS only lets a page speak after a tap; speaking an empty line during the first tap unlocks it.
let unlocked = false;
function unlockSpeech() {
  // Safari only runs an AudioContext that was started during a tap; the level meter needs it.
  try {
    rec.ctx = rec.ctx || new (window.AudioContext || window.webkitAudioContext)();
    if (rec.ctx.state !== "running") rec.ctx.resume();
  } catch { /* no Web Audio: the recorder's time limits still work */ }
  if (unlocked) return;
  unlocked = true;
  audioEl.src = SILENT_WAV;
  audioEl.play().catch(() => {});
  if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(""));
}

// ---------------------------------------------------------------------------
// Recording with automatic stop when you stop talking
// ---------------------------------------------------------------------------

const rec = { stream: null, ctx: null, timer: null, current: null };
const SILENCE_MS = 1300, NO_SPEECH_MS = 7000, MAX_MS = 20000, WAKE_MAX_MS = 12000, LEVEL = 0.02;

// Hands-free: once awake, Oreo keeps listening after each answer and only sleeps
// after listen_timeout_seconds with nobody speaking (or when you say "okay bye").
const handsFree = () => state.config.voice.hands_free !== false;
// Wake word: while asleep, Oreo keeps listening (on this Mac, for free) for "Hey Oreo".
const wakeWord = () => state.config.voice.wake_word !== false;
const listenTimeoutMs = () => (state.config.voice.listen_timeout_seconds || 30) * 1000;

function listenAgain() {
  if (state.awake && handsFree()) startRecording("question");
  else if (!state.awake) listenForWakeWord();
  else setFace("listening");
}

function listenForWakeWord() {
  if (state.awake || !state.wakeListening || !wakeWord()) return;
  startRecording("wake");
}

async function openMic() {
  if (!navigator.mediaDevices?.getUserMedia) {
    caption("", "This browser can't use the microphone here. Type your question below instead.", "");
    return false;
  }
  try {
    rec.stream = rec.stream || await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    return true;
  } catch {
    caption("", "I need microphone permission to hear you. You can also type below.", "");
    return false;
  }
}

// mode "question": Oreo is awake and records your question.
// mode "wake": Oreo is asleep and only checks short bursts of speech for "Hey Oreo".
async function startRecording(mode = "question") {
  cancelRecording();
  if (!await openMic()) return;
  const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported?.(t)) || "";
  const session = { mode, chunks: [], heardSpeech: false, cancelled: false };
  session.recorder = new MediaRecorder(rec.stream, type ? { mimeType: type } : undefined);
  session.recorder.ondataavailable = (e) => e.data.size && session.chunks.push(e.data);
  session.recorder.onstop = () => onRecordingDone(session);
  rec.current = session;
  session.recorder.start(250);
  if (mode === "wake") setFace("sleeping", STATUS.wake);
  else { setFace("recording", handsFree() ? STATUS.handsfree : undefined); touch(); }
  watchLevel(session);
}

// Stop after a short silence once speech has started, or give up if nobody speaks.
function watchLevel(session) {
  rec.ctx = rec.ctx || new (window.AudioContext || window.webkitAudioContext)();
  if (rec.ctx.state === "suspended") rec.ctx.resume();
  const source = rec.ctx.createMediaStreamSource(rec.stream);
  const analyser = rec.ctx.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  const started = performance.now();
  let lastLoud = started;
  const wake = session.mode === "wake";
  const quietLimit = wake ? 10000 : (handsFree() ? listenTimeoutMs() : NO_SPEECH_MS);

  clearInterval(rec.timer);
  rec.timer = setInterval(() => {
    analyser.getFloatTimeDomainData(buf);
    const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length);
    const now = performance.now();
    if (rms > LEVEL) { session.heardSpeech = true; lastLoud = now; }
    const done = (session.heardSpeech && now - lastLoud > SILENCE_MS)
      || (!session.heardSpeech && now - started > quietLimit)
      || now - started > (wake ? WAKE_MAX_MS : MAX_MS);
    if (done) { clearInterval(rec.timer); source.disconnect(); stopRecording(); }
  }, 100);
}

function stopRecording() {
  clearInterval(rec.timer);
  const r = rec.current?.recorder;
  if (r && r.state !== "inactive") r.stop();
}

// Throw away the current recording (e.g. Oreo was put to sleep or tapped awake).
function cancelRecording() {
  if (rec.current) rec.current.cancelled = true;
  stopRecording();
  rec.current = null;
}

async function transcribe(session) {
  const blob = new Blob(session.chunks, { type: session.recorder.mimeType || "audio/webm" });
  const form = new FormData();
  form.append("audio", blob, "question" + (blob.type.includes("mp4") ? ".mp4" : ".webm"));
  form.append("mode", session.mode);
  const res = await fetch("/api/transcribe", { method: "POST", body: form });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "transcription failed");
  return data;  // {text, intent: wake|sleep|noise|question|ignore, question}
}

async function onRecordingDone(session) {
  if (session.cancelled) return;
  if (rec.current === session) rec.current = null;
  if (session.mode === "wake") return onWakeRecording(session);
  if (!state.awake) return;

  if (!session.heardSpeech) {
    // Nobody spoke: in hands-free mode that means the conversation is over.
    if (handsFree()) { await goToSleep(line("naps", "I'll take a little nap. Say Hey Oreo if you need me.")); }
    else setFace("listening");
    return;
  }
  setFace("thinking", "Working out what you said…");
  state.busy = true;
  try {
    const heard = await transcribe(session);
    state.busy = false;
    if (!state.awake) return;
    if (heard.intent === "sleep") {
      caption(heard.text, "", "");
      return goToSleep(line("goodbyes", "Okay, bye! Just say Hey Oreo when you need me."));
    }
    if (heard.intent === "noise" || !heard.text) {
      if (!handsFree()) { await speak("Sorry, I didn't catch that."); setFace("listening"); return; }
      // Background noise: keep listening quietly, but a noisy room shouldn't keep Oreo awake forever.
      if (performance.now() - state.lastHeard > listenTimeoutMs()) return goToSleep();
      return listenAgain();
    }
    await ask(heard.question || heard.text);
  } catch (err) {
    state.busy = false;
    caption("", "I couldn't hear that properly. Please try again, or type below.", String(err.message || err));
    if (state.awake && handsFree()) { await speak("Sorry, I couldn't hear that properly."); listenAgain(); }
    else setFace("listening");
  }
}

async function onWakeRecording(session) {
  if (state.awake) return;
  if (!session.heardSpeech) return listenForWakeWord();
  let heard = null;
  try { heard = await transcribe(session); } catch { /* try again below */ }
  if (state.awake || !state.wakeListening) return;
  if (heard?.intent !== "wake") return listenForWakeWord();
  await wakeUp(heard.question);
}

async function wakeUp(question = "") {
  cancelRecording();
  state.awake = true;
  state.lastHeard = performance.now();
  if (question) return ask(question);
  const hello = line("greetings", "Hey boss! What's up?");
  caption("", hello, "");
  await speak(hello, "speaking");
  if (state.awake) startRecording("question");
}

// A random line from config.yaml (greetings, goodbyes, naps), so Oreo doesn't repeat itself word for word.
let lastLine = "";
function line(kind, fallback) {
  const options = (state.config.lines?.[kind] || []).filter((l) => l !== lastLine);
  lastLine = options.length ? options[Math.floor(Math.random() * options.length)] : fallback;
  return lastLine;
}

async function goToSleep(goodbye) {
  cancelRecording();
  if (goodbye) await speak(goodbye);
  sleep();
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
  state.lastHeard = performance.now();
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
  touch();
  if (state.awake) listenAgain();
}

// ---------------------------------------------------------------------------
// Input: tap the face, or type
// ---------------------------------------------------------------------------

$("face").addEventListener("click", async () => {
  unlockSpeech();
  state.wakeListening = true;  // after the first tap, "Hey Oreo" works without tapping
  if (state.face === "recording") {
    // Hands-free: tapping while it listens puts Oreo to sleep. Otherwise a tap ends the question.
    if (handsFree()) return goToSleep();
    return stopRecording();
  }
  if (state.face === "speaking") { stopSpeaking(); return; }
  if (state.busy) return;
  if (!state.awake) return wakeUp();
  await startRecording("question");
});

$("typed").addEventListener("submit", async (e) => {
  e.preventDefault();
  unlockSpeech();
  const input = $("typed-text");
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  state.wakeListening = true;
  cancelRecording();
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
