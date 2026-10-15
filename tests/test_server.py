"""Web server tests: face page, transcription endpoint and the WebSocket flow (no API key, no microphone)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import ModelAdapter
from backend.speech import SpeechUnavailable
from conftest import FakeClient, is_router, response, route_json, text_block, tool_use


class FakeSTT:
    def __init__(self, text="What were our top products last quarter?", error=None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, audio: bytes, suffix: str = ".webm") -> str:
        self.calls.append((len(audio), suffix))
        if self.error:
            raise self.error
        return self.text


def company_script(kw):
    if is_router(kw):
        latest = kw["messages"][-1]["content"].split("Latest message:")[-1]
        return route_json("general" if "joke" in latest else "company")
    if not kw.get("tools"):
        return response(text_block("Why did the robot go on holiday? To recharge."))
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        return response(tool_use("top_items", {"period": "last_quarter", "limit": 1}), stop_reason="tool_use")
    top = json.loads(last["content"][0]["content"])["items"][0]["name"]
    return response(text_block(f"According to the sales database, the top product was the {top}."))


def make_client(settings, stt=None, script=company_script):
    app = create_app(settings, ModelAdapter(FakeClient(script)), stt or FakeSTT())
    return TestClient(app)


def test_face_page_and_assets_are_served(settings):
    with make_client(settings) as client:
        page = client.get("/")
        assert page.status_code == 200 and "Tap to talk to Oreo" in page.text
        for asset in ("/static/app.js", "/static/face.css"):
            assert client.get(asset).status_code == 200


def test_health_lists_tools_and_voice_settings(settings):
    with make_client(settings) as client:
        data = client.get("/api/health").json()
    assert data["name"] == "Oreo" and "top_items" in data["tools"]
    assert data["session_timeout_seconds"] == 120 and "tts_pitch" in data["voice"]


def test_transcribe_returns_text(settings):
    stt = FakeSTT()
    with make_client(settings, stt) as client:
        res = client.post("/api/transcribe", files={"audio": ("question.mp4", b"fake-audio", "audio/mp4")})
    assert res.status_code == 200 and res.json()["text"].startswith("What were")
    assert stt.calls == [(10, ".mp4")]


def test_transcribe_rejects_empty_audio_and_reports_missing_whisper(settings):
    with make_client(settings, FakeSTT(error=SpeechUnavailable("faster-whisper is not installed"))) as client:
        assert client.post("/api/transcribe", files={"audio": ("q.webm", b"", "audio/webm")}).status_code == 400
        res = client.post("/api/transcribe", files={"audio": ("q.webm", b"x", "audio/webm")})
    assert res.status_code == 503 and "faster-whisper" in res.json()["detail"]


def test_websocket_sends_thinking_filler_then_answer(settings):
    with make_client(settings) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "What were our top products last quarter?"})
        first, second, answer = ws.receive_json(), ws.receive_json(), ws.receive_json()
    assert first == {"type": "state", "state": "thinking", "question": "What were our top products last quarter?"}
    assert second["route"] == "company" and second["filler"] == "Let me check the sales data."
    assert answer["type"] == "answer" and "top product was the" in answer["text"]
    assert answer["as_of"] == "2026-10-14" and answer["tools_used"] == ["top_items"]


def test_websocket_general_question_has_no_filler_and_reset_works(settings):
    with make_client(settings) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ask", "text": "Tell me a joke"})
        _, routed, answer = ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "reset"})
        reset = ws.receive_json()
    assert routed["route"] == "general" and routed["filler"] is None
    assert "recharge" in answer["text"]
    assert reset == {"type": "state", "state": "sleeping"}
