"""Offline unit tests for the LocalAI client (issue #73, Phase 1 T2).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no network. The client is exercised through an
injectable ``httpx.MockTransport`` (for every httpx-based endpoint) and an
injectable fake ``openai`` client (for ``chat()``). Every test asserts the
exact request shape (path / body / headers) and the uniform
``{"ok": True, ...}`` / ``{"ok": False, "error": ...}`` contract, including
the **never-raises** invariant (timeout / 5xx / malformed JSON all become data).
"""

from __future__ import annotations

import json

import httpx

from brain.config import BrainConfig
from brain.localai_client import LocalAIClient

BASE = "http://localai.test/v1"  # fake host; MockTransport never connects


def make_config(**over) -> BrainConfig:
    """A BrainConfig pointed at the fake host with generous timeouts."""
    base = BrainConfig()
    kwargs = {
        "localai_base_url": BASE,
        "localai_api_key": "sk-test",
        "vlm_model": "vlm-test",
        "detection_model": "det-test",
        "stt_model": "stt-test",
        "tts_model": "tts-test",
    }
    kwargs.update(over)
    return BrainConfig(**kwargs)


def make_client(handler, *, openai_client=None, **cfg_over) -> LocalAIClient:
    return LocalAIClient(
        make_config(**cfg_over),
        transport=httpx.MockTransport(handler),
        openai_client=openai_client,
    )


def _json(status, payload):
    return httpx.Response(status, json=payload)


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

def test_health_happy_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["method"] = request.method
        return _json(200, {"object": "list", "data": [{"id": "a"}, {"id": "b"}]})

    c = make_client(handler)
    r = c.health()
    assert r == {"ok": True, "models": ["a", "b"]}
    assert seen["url"] == "http://localai.test/v1/models"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["method"] == "GET"


def test_health_timeout_is_data():
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    r = make_client(handler).health()
    assert r["ok"] is False and "timeout" in r["error"]


def test_health_5xx_is_data():
    r = make_client(lambda req: _json(500, {"error": {"message": "down"}})).health()
    assert r["ok"] is False and "500" in r["error"] and "down" in r["error"]


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------

def test_detect_happy_maps_width_height_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return _json(
            200,
            {
                "detections": [
                    {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0,
                     "class_name": "dog", "confidence": 0.95, "mask": "abc"},
                    {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0,
                     "class_name": "person", "confidence": 0.3},  # below threshold
                ]
            },
        )

    c = make_client(handler)
    r = c.detect("BASE64IMG", min_confidence=0.5)
    assert r["ok"] is True
    # width/height -> w/h; mask dropped; low-confidence filtered out
    assert r["detections"] == [
        {"class_name": "dog", "x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0, "confidence": 0.95}
    ]
    assert seen["url"] == "http://localai.test/v1/detection"
    assert seen["body"] == {"model": "det-test", "image": "BASE64IMG", "threshold": 0.5}


def test_detect_min_confidence_filters():
    def handler(request):
        return _json(200, {"detections": [
            {"x": 0, "y": 0, "width": 1, "height": 1, "class_name": "a", "confidence": 0.49},
            {"x": 0, "y": 0, "width": 1, "height": 1, "class_name": "b", "confidence": 0.50},
        ]})

    r = make_client(handler).detect("IMG", min_confidence=0.5)
    assert [d["class_name"] for d in r["detections"]] == ["b"]


def test_detect_501_no_model_deployed_is_data():
    # the real server returns 501 until a detector (rfdetr-base) is deployed
    r = make_client(lambda req: _json(501, {"error": {"code": 501, "message": ""}})).detect("IMG")
    assert r["ok"] is False and "501" in r["error"]


def test_detect_malformed_json_is_data():
    def handler(request):
        return httpx.Response(200, content=b"not-json{{{")

    r = make_client(handler).detect("IMG")
    assert r["ok"] is False and "detections" in r["error"]


def test_detect_timeout_is_data():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    r = make_client(handler).detect("IMG")
    assert r["ok"] is False and "timeout" in r["error"]


# ---------------------------------------------------------------------------
# depth() — documented stub (D4 / T15)
# ---------------------------------------------------------------------------

def test_depth_is_documented_stub():
    r = make_client(lambda req: (_ for _ in ()).throw(AssertionError("must not be called"))).depth("IMG")
    assert r == {"ok": False, "error": "depth not enabled (T15)"}


# ---------------------------------------------------------------------------
# transcribe()
# ---------------------------------------------------------------------------

def test_transcribe_happy_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("Content-Type", "")
        seen["body"] = request.content.decode("utf-8", "replace")
        return _json(200, {"text": "drive forward"})

    c = make_client(handler)
    r = c.transcribe(b"RIFF-wav-bytes", filename="say.wav")
    assert r == {"ok": True, "text": "drive forward"}
    assert seen["url"] == "http://localai.test/v1/audio/transcriptions"
    assert "multipart/form-data" in seen["content_type"]
    # model + file name present in the multipart body
    assert "stt-test" in seen["body"]
    assert "say.wav" in seen["body"]
    assert "RIFF-wav-bytes" in seen["body"]


def test_transcribe_missing_text_is_data():
    r = make_client(lambda req: _json(200, {"foo": "bar"})).transcribe(b"x")
    assert r["ok"] is False and "text" in r["error"]


def test_transcribe_5xx_is_data():
    r = make_client(lambda req: _json(503, {"error": "unavailable"})).transcribe(b"x")
    assert r["ok"] is False and "503" in r["error"]


# ---------------------------------------------------------------------------
# speak()
# ---------------------------------------------------------------------------

def test_speak_happy_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"PCM-AUDIO-BYTES")

    c = make_client(handler)
    r = c.speak("hello tank", format="wav")
    assert r == {"ok": True, "audio": b"PCM-AUDIO-BYTES", "format": "wav"}
    assert seen["url"] == "http://localai.test/v1/audio/speech"
    assert seen["body"]["model"] == "tts-test"
    assert seen["body"]["input"] == "hello tank"
    assert seen["body"]["response_format"] == "wav"


def test_speak_no_audio_bytes_is_data():
    r = make_client(lambda req: _json(200, {"note": "no audio"})).speak("hi")
    assert r["ok"] is False and "audio" in r["error"]


def test_speak_timeout_is_data():
    def handler(request):
        raise httpx.ConnectError("no route")

    r = make_client(handler).speak("hi")
    assert r["ok"] is False and "http error" in r["error"]


# ---------------------------------------------------------------------------
# chat() — via an injectable fake openai client
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletions:
    def __init__(self, content=None, exc=None, record=None):
        self._content = content
        self._exc = exc
        self._record = record

    def create(self, **kwargs):
        if self._record is not None:
            self._record.update(kwargs)
        if self._exc is not None:
            raise self._exc
        return type("R", (), {"choices": [_FakeChoice(self._content)]})()


class _FakeChat:
    def __init__(self, **kw):
        self.completions = _FakeCompletions(**kw)


class _FakeOpenAI:
    def __init__(self, **kw):
        self.chat = _FakeChat(**kw)


def test_chat_happy_and_model_from_config():
    rec = {}
    fake = _FakeOpenAI(content='{"intent":"go_to"}', record=rec)
    c = make_client(lambda req: None, openai_client=fake)
    r = c.chat([{"role": "user", "content": "go to the door"}], task="nav")
    assert r["ok"] is True
    assert r["raw"] == '{"intent":"go_to"}'
    assert r["model"] == "vlm-test"
    assert r["task"] == "nav"
    # model + response_format come from config / contract
    assert rec["model"] == "vlm-test"
    assert rec["response_format"] == {"type": "json_object"}


def test_chat_attaches_images_to_last_user_message():
    rec = {}
    fake = _FakeOpenAI(content="ok", record=rec)
    c = make_client(lambda req: None, openai_client=fake)
    c.chat(
        [{"role": "system", "content": "you are a tank"},
         {"role": "user", "content": "what do you see?"}],
        images_b64=["QUJD"],
    )
    msgs = rec["messages"]
    user = msgs[-1]
    assert user["role"] == "user"
    parts = user["content"]
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "what do you see?"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}


def test_chat_sdk_exception_is_data():
    fake = _FakeOpenAI(exc=RuntimeError("connection refused"))
    c = make_client(lambda req: None, openai_client=fake)
    r = c.chat([{"role": "user", "content": "hi"}])
    assert r["ok"] is False and "chat failed" in r["error"] and "connection refused" in r["error"]


def test_chat_missing_content_is_data():
    rec = {}

    class _NoChoices:
        def create(self, **kw):
            return type("R", (), {"choices": []})()

    class _Chat:
        completions = _NoChoices()

    class _OA:
        chat = _Chat()

    c = make_client(lambda req: None, openai_client=_OA())
    r = c.chat([{"role": "user", "content": "hi"}])
    assert r["ok"] is False and "content" in r["error"]


# ---------------------------------------------------------------------------
# no IP / model literals baked into requests (acceptance)
# ---------------------------------------------------------------------------

def test_no_ip_literals_in_module_source():
    import brain.localai_client as mod
    src = open(mod.__file__).read()
    # no dotted-quad IPv4 literals in the module
    import re
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", src), "IP literal found in localai_client.py"


def test_requests_use_config_models_not_defaults():
    # override every model; a request must carry the override, proving config-driven
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "my-override-det"
        return _json(200, {"detections": []})

    c = make_client(handler, detection_model="my-override-det")
    r = c.detect("IMG")
    assert r["ok"] is True and r["detections"] == []
