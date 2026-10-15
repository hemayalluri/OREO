"""
Oreo's web server: serves the face page and connects it to the brain.

    python -m backend.main            # then open http://localhost:8000 (from the oreo/ folder)

Endpoints
  GET  /                 the face page (face_app/)
  GET  /api/health       is the brain up, which tools are connected
  POST /api/transcribe   recorded audio in, text out (local Whisper)
  WS   /ws               questions in; state changes and answers out:
                           -> {"type": "ask", "text": "..."} | {"type": "reset"}
                           <- {"type": "state", "state": "thinking", "route": ..., "filler": ...}
                           <- {"type": "answer", "text": ..., "route": ..., "as_of": ..., ...}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ipad
from .config import Settings, has_anthropic_credentials, load_settings
from .mcp_gateway import McpGateway
from .models import ModelAdapter
from .orchestrator import Orchestrator
from .phrases import classify
from .router import RouteDecision
from .speech import MacSayTTS, SpeechUnavailable, WhisperSTT

log = logging.getLogger("oreo.main")

FACE_APP = Path(__file__).resolve().parent.parent / "face_app"
MAX_AUDIO_BYTES = 10 * 1024 * 1024   # ~10 MB is minutes of compressed speech; questions are seconds
# Said while the tools run; picked at random so Oreo doesn't sound like a recording.
FILLERS = {
    "company": ["One sec, pulling up the numbers.", "Let me check.", "Ooh, good one. Checking now.",
                "Give me a second.", "On it!"],
    "mixed": ["Let me look into that.", "One sec, digging in.", "Hmm, let me check."],
    "live": ["Let me look that up.", "One sec, checking online.", "Let me see what's out there."],
}


def create_app(settings: Settings | None = None, models: ModelAdapter | None = None,
               stt: Any | None = None, tts: Any | None = None, pairing_key: str | None = None) -> FastAPI:
    """pairing_key: when set (iPad mode), every request that doesn't come from this Mac itself
    must carry the key, given once as ?key=... and then remembered in a cookie."""
    settings = settings or load_settings()
    voice = settings.raw.get("voice", {})
    stt = stt or WhisperSTT(voice.get("stt_model", "base.en"), voice.get("language", "en"))
    if tts is None and voice.get("mac_voice", True) and MacSayTTS.available():
        tts = MacSayTTS(voice.get("tts_voice", ""), voice.get("tts_rate", 1.0))
    server_voice = getattr(tts, "voice", None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gateway:
            app.state.oreo = Orchestrator(settings, models or ModelAdapter(), gateway)
            app.state.gateway = gateway
            app.state.lock = asyncio.Lock()  # one desk, one conversation: answer one question at a time
            yield

    app = FastAPI(title="Oreo", lifespan=lifespan)

    def paired(key: str | None, host: str | None) -> bool:
        return not pairing_key or ipad.is_local(host) or (
            bool(key) and secrets.compare_digest(key, pairing_key))

    @app.middleware("http")
    async def require_pairing(request, call_next):
        host = request.client.host if request.client else None
        if paired(request.cookies.get(ipad.COOKIE), host):
            return await call_next(request)
        if paired(request.query_params.get("key"), host):
            # First visit from the pairing link: remember the key and drop it from the address bar.
            response = RedirectResponse(request.url.path or "/", status_code=303)
            response.set_cookie(ipad.COOKIE, pairing_key, max_age=10 * 365 * 24 * 3600,
                                httponly=True, secure=True, samesite="strict")
            return response
        log.warning("Refused an unpaired request from %s", host)
        return HTMLResponse("<h1>Oreo isn't paired with this device</h1>"
                            "<p>Open the pairing link that Oreo printed on the Mac.</p>", status_code=401)

    @app.middleware("http")
    async def no_cache(request, call_next):
        # Always serve the newest face page (Safari otherwise keeps an old app.js after updates).
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def log_heard(mode: str, text: str, intent: str, size: int, seconds: float) -> None:
        """logs/heard.jsonl: what the microphone picked up and how Oreo read it (for tuning)."""
        path = settings.log_dir / "heard.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode, "text": text, "intent": intent,
                 "audio_bytes": size, "seconds": round(seconds, 2)}
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    app.mount("/static", StaticFiles(directory=FACE_APP), name="static")

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        """The home-screen app's manifest. A home-screen app on iPad keeps its own cookies, separate
        from Safari, so for a paired device the start link carries the key and the app pairs itself
        on launch. (Only paired devices get here: the pairing check runs first.)"""
        data = json.loads((FACE_APP / "manifest.webmanifest").read_text())
        if pairing_key:
            data["start_url"] = f"/?key={pairing_key}"
        return Response(json.dumps(data), media_type="application/manifest+json",
                        headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FACE_APP / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "name": settings.oreo.get("name", "Oreo"),
            "boss_name": settings.oreo.get("boss_name", ""),
            "lines": {k: settings.oreo.get(k, []) for k in ("greetings", "goodbyes", "naps")},
            "tools": [t["name"] for t in app.state.gateway.tools],
            "session_timeout_seconds": settings.oreo.get("session_timeout_seconds", 120),
            "voice": {**{k: voice.get(k) for k in ("tts_voice", "tts_rate", "tts_pitch",
                                                   "hands_free", "listen_timeout_seconds", "wake_word")},
                      "server_voice": server_voice},
        }

    reports_dir = (settings.root / settings.raw.get("reports", {}).get("dir", "reports")).resolve()

    @app.get("/reports/{name}")
    async def report_file(name: str) -> FileResponse:
        """Download a report Oreo made (only files directly inside the reports folder)."""
        path = (reports_dir / name).resolve()
        if path.parent != reports_dir or path.suffix not in (".pdf", ".docx", ".xlsx") or not path.is_file():
            raise HTTPException(404, "No such report.")
        return FileResponse(path, filename=name)

    @app.post("/api/speak")
    async def speak(body: dict[str, Any]) -> Response:
        """Text in, WAV out, spoken with the Mac's voice (used when the browser lacks it)."""
        text = str(body.get("text", "")).strip()[:2000]
        if not text or not server_voice:
            raise HTTPException(404, "No server voice.")
        try:
            return Response(await tts.synthesize(text), media_type="audio/wav")
        except SpeechUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/transcribe")
    async def transcribe(audio: UploadFile = File(...), mode: str = Form("question")) -> dict[str, str]:
        """mode="wake": Oreo is asleep, listening only for "Hey Oreo". mode="question": it's awake."""
        data = await audio.read()
        if not data:
            raise HTTPException(400, "No audio received.")
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "That recording is too long.")
        suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
        started = time.monotonic()
        try:
            # Hinting the name helps Whisper spell "Oreo" instead of "Ariel" or "Aurea". (A longer hint
            # like "Hey Oreo" makes Whisper drop those words from the transcript, so keep it to the name.)
            text = await stt.transcribe(data, suffix=suffix, hotwords="Oreo")
        except SpeechUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            log.exception("Transcription failed")
            log_heard(mode, f"ERROR: {exc}", "error", len(data), time.monotonic() - started)
            raise HTTPException(500, "I couldn't make out that recording.") from exc
        heard = classify(text, "wake" if mode == "wake" else "question")
        log_heard(mode, text, heard.intent, len(data), time.monotonic() - started)
        return {"text": text, "intent": heard.intent, "question": heard.question}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        if not paired(socket.cookies.get(ipad.COOKIE), socket.client.host if socket.client else None):
            await socket.close(code=4401)
            return
        await socket.accept()
        oreo: Orchestrator = app.state.oreo
        try:
            while True:
                msg = await socket.receive_json()
                if msg.get("type") == "reset":
                    oreo.reset()
                    await socket.send_json({"type": "state", "state": "sleeping"})
                    continue
                text = str(msg.get("text", "")).strip()[:1000]
                if msg.get("type") != "ask" or not text:
                    continue

                async def on_route(decision: RouteDecision) -> None:
                    await socket.send_json({"type": "state", "state": "thinking", "route": decision.route,
                                            "filler": random.choice(FILLERS[decision.route])
                                                      if decision.route in FILLERS else None})

                async with app.state.lock:
                    await socket.send_json({"type": "state", "state": "thinking", "question": text})
                    answer = await oreo.ask(text, on_route=on_route)
                await socket.send_json({"type": "answer", **asdict(answer)})
        except WebSocketDisconnect:
            pass

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Oreo's web server.")
    parser.add_argument("--ipad", action="store_true",
                        help="serve the face to an iPad on this Wi-Fi, over HTTPS with a pairing key")
    parser.add_argument("--host", default=None,
                        help="127.0.0.1 = this Mac only (default); 0.0.0.0 = other devices on your Wi-Fi")
    parser.add_argument("--port", type=int, default=None, help="default 8000, or 8443 with --ipad")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()  # loads .env, so the key check below can see it
    if not has_anthropic_credentials():
        raise SystemExit("No ANTHROPIC_API_KEY found. Copy .env.example to .env and add your key.")
    import uvicorn

    host = args.host or ("0.0.0.0" if args.ipad else "127.0.0.1")
    port = args.port or (8443 if args.ipad else 8000)
    if not args.ipad and ipad.is_local(host):
        print(f"Oreo is awake at http://localhost:{port}")
        uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
        return

    # Reachable from other devices: always HTTPS (the iPad's mic needs it) and a pairing key.
    certs = settings.root / "certs"
    name, ip = ipad.mac_hostname(), ipad.wifi_ip()
    cert, key, ca = ipad.ensure_certs(certs, name, ip)
    pairing_key = ipad.pairing_key(certs)
    print(f"Oreo is awake for your iPad (and at https://localhost:{port} on this Mac).\n")
    print("On the iPad, open this pairing link in Safari (keep it private):")
    print(f"    https://{name}:{port}/?key={pairing_key}")
    if ip:
        print(f"    (or https://{ip}:{port}/?key={pairing_key} if the name doesn't work)")
    print(f"\nFirst time only: AirDrop {ca} to the iPad and trust it (see the README, \"Oreo on the iPad\").")
    uvicorn.run(create_app(settings, pairing_key=pairing_key), host=host, port=port, log_level="warning",
                ssl_certfile=str(cert), ssl_keyfile=str(key))


if __name__ == "__main__":
    main()
