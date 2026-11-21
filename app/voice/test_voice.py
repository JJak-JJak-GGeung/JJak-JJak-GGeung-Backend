"""python -m pytest app/voice -q  (OpenAI 호출 없이 가짜 클라이언트로 검사)"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.voice import router as router_mod
from app.voice.transcribe import TranscriptionError, resolve_extension, transcribe_audio

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 4000
MP4 = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4000


class FakeClient:
    def __init__(self, text="  초안 문장  ", exc=None):
        self.calls = []
        self._text, self._exc = text, exc
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        return SimpleNamespace(text=self._text)


@pytest.mark.parametrize(
    "mime,data,ext",
    [
        ("audio/webm;codecs=opus", WEBM, "webm"),
        ("audio/mp4", MP4, "mp4"),
        ("audio/x-m4a", MP4, "m4a"),
        ("", WEBM, "webm"),
        ("application/octet-stream", MP4, "mp4"),
        (None, b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
    ],
)
def test_extension(mime, data, ext):
    assert resolve_extension(mime, data) == ext


def test_unsupported():
    with pytest.raises(TranscriptionError) as e:
        resolve_extension("text/plain", b"hello" * 300)
    assert e.value.status == 415


def test_calls_model_without_prompt_and_strips():
    c = FakeClient()
    assert transcribe_audio(WEBM, "audio/webm;codecs=opus", client=c) == "초안 문장"
    kw = c.calls[0]
    assert kw["language"] == "ko"
    assert "prompt" not in kw  # 기대 문장을 모델에 넣지 않는다
    name, data, ctype = kw["file"]
    assert name == "recording.webm" and data == WEBM and ctype == "audio/webm"


def test_empty_and_too_large():
    with pytest.raises(TranscriptionError) as e:
        transcribe_audio(b"\x1a\x45\xdf\xa3", "audio/webm", client=FakeClient())
    assert e.value.code == "EMPTY_AUDIO"
    with pytest.raises(TranscriptionError) as e:
        transcribe_audio(WEBM + b"\x00" * (25 * 1024 * 1024), "audio/webm", client=FakeClient())
    assert e.value.code == "AUDIO_TOO_LARGE"


def test_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TranscriptionError) as e:
        transcribe_audio(WEBM, "audio/webm")
    assert (e.value.code, e.value.status) == ("NO_API_KEY", 503)


def test_upstream_failure_wrapped():
    with pytest.raises(TranscriptionError) as e:
        transcribe_audio(WEBM, "audio/webm", client=FakeClient(exc=RuntimeError("429")))
    assert e.value.code == "TRANSCRIPTION_FAILED" and e.value.status == 502


def _app(monkeypatch, fake):
    monkeypatch.setattr(
        router_mod, "transcribe_audio", lambda d, m: transcribe_audio(d, m, client=fake)
    )
    app = FastAPI()
    app.include_router(router_mod.router)
    return TestClient(app)


def test_route_returns_only_draft_text(monkeypatch):
    api = _app(monkeypatch, FakeClient("장어 40킬로"))
    r = api.post("/api/transcriptions", files={"file": ("a.webm", WEBM, "audio/webm;codecs=opus")})
    assert r.status_code == 200
    assert r.json() == {"draft_text": "장어 40킬로"}


def test_route_error_shape(monkeypatch):
    api = _app(monkeypatch, FakeClient())
    r = api.post("/api/transcriptions", files={"file": ("a.txt", b"x" * 2000, "text/plain")})
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "UNSUPPORTED_AUDIO"


def test_missing_sdk_is_503(monkeypatch):
    import builtins

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "openai":
            raise ImportError("no openai")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(TranscriptionError) as e:
        transcribe_audio(WEBM, "audio/webm")
    assert (e.value.code, e.value.status) == ("NO_OPENAI_SDK", 503)
