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
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, has_anthropic_credentials, load_settings
from .mcp_gateway import McpGateway
from .models import ModelAdapter
from .orchestrator import Orchestrator
from .router import RouteDecision
from .speech import SpeechUnavailable, WhisperSTT

log = logging.getLogger("oreo.main")

FACE_APP = Path(__file__).resolve().parent.parent / "face_app"
MAX_AUDIO_BYTES = 10 * 1024 * 1024   # ~10 MB is minutes of compressed speech; questions are seconds
FILLERS = {"company": "Let me check the sales data.", "mixed": "Let me look that up."}


def create_app(settings: Settings | None = None, models: ModelAdapter | None = None,
               stt: Any | None = None) -> FastAPI:
    settings = settings or load_settings()
    voice = settings.raw.get("voice", {})
    stt = stt or WhisperSTT(voice.get("stt_model", "base.en"), voice.get("language", "en"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with McpGateway(settings.mcp_servers, settings.log_dir / "audit.jsonl") as gateway:
            app.state.oreo = Orchestrator(settings, models or ModelAdapter(), gateway)
            app.state.gateway = gateway
            app.state.lock = asyncio.Lock()  # one desk, one conversation: answer one question at a time
            yield

    app = FastAPI(title="Oreo", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=FACE_APP), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FACE_APP / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "name": settings.oreo.get("name", "Oreo"),
            "boss_name": settings.oreo.get("boss_name", ""),
            "tools": [t["name"] for t in app.state.gateway.tools],
            "session_timeout_seconds": settings.oreo.get("session_timeout_seconds", 120),
            "voice": {k: voice.get(k) for k in ("tts_voice", "tts_rate", "tts_pitch")},
        }

    @app.post("/api/transcribe")
    async def transcribe(audio: UploadFile = File(...)) -> dict[str, str]:
        data = await audio.read()
        if not data:
            raise HTTPException(400, "No audio received.")
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "That recording is too long.")
        suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
        try:
            text = await stt.transcribe(data, suffix=suffix)
        except SpeechUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            log.exception("Transcription failed")
            raise HTTPException(500, "I couldn't make out that recording.") from exc
        return {"text": text}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
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
                                            "filler": FILLERS.get(decision.route)})

                async with app.state.lock:
                    await socket.send_json({"type": "state", "state": "thinking", "question": text})
                    answer = await oreo.ask(text, on_route=on_route)
                await socket.send_json({"type": "answer", **asdict(answer)})
        except WebSocketDisconnect:
            pass

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Oreo's web server.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1 = this Mac only (default); 0.0.0.0 = other devices on your Wi-Fi")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()  # loads .env, so check the key after this
    if not has_anthropic_credentials():
        raise SystemExit("No ANTHROPIC_API_KEY found. Copy .env.example to .env and add your key.")
    import uvicorn
    print(f"Oreo is awake at http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
