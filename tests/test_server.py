"""Web server tests: face page, transcription endpoint and the WebSocket flow (no API key, no microphone)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import FILLERS, create_app
from backend.models import ModelAdapter
from backend.speech import SpeechUnavailable
from conftest import FakeClient, is_router, response, route_json, text_block, tool_use


class FakeSTT:
    def __init__(self, text="What were our top products last quarter?", error=None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, audio: bytes, suffix: str = ".webm", hotwords=None) -> str:
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


class FakeTTS:
    voice = "Ava (Premium)"

    def __init__(self):
        self.calls = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return b"RIFF-fake-wav"


def make_client(settings, stt=None, script=company_script, tts=None):
    app = create_app(settings, ModelAdapter(FakeClient(script)), stt or FakeSTT(), tts or FakeTTS())
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
    assert second["route"] == "company" and second["filler"] in FILLERS["company"]
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


def test_speak_returns_wav_in_the_mac_voice(settings):
    tts = FakeTTS()
    with make_client(settings, tts=tts) as client:
        assert client.get("/api/health").json()["voice"]["server_voice"] == "Ava (Premium)"
        res = client.post("/api/speak", json={"text": "Hi Siddartha!"})
        assert client.post("/api/speak", json={"text": "  "}).status_code == 404
    assert res.status_code == 200 and res.headers["content-type"] == "audio/wav"
    assert res.content == b"RIFF-fake-wav" and tts.calls == ["Hi Siddartha!"]


def test_report_downloads_only_serve_files_in_the_reports_folder(settings, tmp_path):
    folder = Path(settings.raw["reports"]["dir"])
    folder.mkdir(parents=True)
    (folder / "last-month.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "secret.pdf").write_bytes(b"secret")
    with make_client(settings) as client:
        ok = client.get("/reports/last-month.pdf")
        assert ok.status_code == 200 and ok.content == b"%PDF-1.4 fake"
        for bad in ("../secret.pdf", "..%2Fsecret.pdf", "missing.pdf", "config.yaml"):
            assert client.get(f"/reports/{bad}").status_code == 404


def ipad_client(settings, key="s3cret-pairing-key", **kw):
    app = create_app(settings, ModelAdapter(FakeClient(company_script)), FakeSTT(), FakeTTS(), pairing_key=key)
    # https base URL: the pairing cookie is Secure, like on the real iPad connection.
    return TestClient(app, base_url="https://oreo.local", client=("10.0.0.20", 50000), **kw)


def test_ipad_mode_refuses_unpaired_devices_on_the_wifi(settings):
    with ipad_client(settings) as client:
        for path in ("/", "/static/app.js", "/api/health", "/reports/x.pdf"):
            assert client.get(path).status_code == 401
        assert client.get("/?key=wrong-key").status_code == 401
        with pytest.raises(Exception):
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()


def test_ipad_pairing_link_sets_a_cookie_then_everything_works(settings):
    with ipad_client(settings, follow_redirects=False) as client:
        first = client.get("/?key=s3cret-pairing-key")
        assert first.status_code == 303 and first.headers["location"] == "/"
        cookie = first.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
        assert client.get("/").status_code == 200 and client.get("/api/health").status_code == 200
        # The home-screen app has its own cookies, so its start link carries the key.
        assert client.get("/manifest.webmanifest").json()["start_url"] == "/?key=s3cret-pairing-key"
        # The cookie is Secure, so (like Safari on the iPad) it only goes over wss://.
        with client.websocket_connect("wss://oreo.local/ws") as ws:
            ws.send_json({"type": "ask", "text": "What were our top products last quarter?"})
            assert ws.receive_json()["type"] == "state"


def test_this_mac_needs_no_key_and_the_app_manifest_is_served(settings):
    app = create_app(settings, ModelAdapter(FakeClient(company_script)), FakeSTT(), FakeTTS(), pairing_key="k")
    with TestClient(app) as client:   # requests from the Mac itself
        assert client.get("/").status_code == 200
        manifest = client.get("/manifest.webmanifest").json()
        assert manifest["display"] == "fullscreen" and client.get("/static/icon-180.png").status_code == 200
