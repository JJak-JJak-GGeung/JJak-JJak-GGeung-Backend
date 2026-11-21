"""check.py가 계약대로인 서버는 통과, 어긋난 서버는 실패로 판정하는지 확인한다."""

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.voice import check
from app.voice import router as router_mod

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 4000


def fake_server(monkeypatch, *, dup_on_retry=False, save_on_transcribe=False, accept_key_reuse=False):
    monkeypatch.setattr(router_mod, "transcribe_audio", lambda d, m: "초안")
    app = FastAPI()
    app.include_router(router_mod.router)
    st = {"rev": 7, "memos": [], "keys": {}}

    if save_on_transcribe:
        @app.middleware("http")
        async def leak(request, call_next):
            if request.url.path == "/api/transcriptions":
                st["memos"].append({"memo_id": "leak"})
            return await call_next(request)

    @app.get("/api/transactions/{tx}")
    def get_tx(tx: str):
        return {"transaction_id": tx, "input_revision": st["rev"], "memos": list(st["memos"])}

    @app.post("/api/transactions/{tx}/memos")
    def post_memo(tx: str, body: dict):
        key = body["request_key"]
        if key in st["keys"] and not dup_on_retry:
            prev_body, resp = st["keys"][key]
            if prev_body != body and not accept_key_reuse:
                raise HTTPException(409, {"code": "KEY_REUSE"})
            if prev_body == body:
                return resp
        st["rev"] += 1
        memo = {
            "memo_id": f"m{len(st['memos']) + 1}",
            "transaction_id": tx,
            "confirmed_text": body["confirmed_text"],
            "source_mode": body["source_mode"],
        }
        st["memos"].append(memo)
        resp = {"memo_id": memo["memo_id"], "transaction_id": tx, "input_revision": st["rev"]}
        st["keys"][key] = (body, resp)
        return resp

    return TestClient(app)


@pytest.fixture
def audio(tmp_path) -> Path:
    p = tmp_path / "rec.webm"
    p.write_bytes(WEBM)
    return p


def test_contract_server_passes(monkeypatch, audio):
    api = fake_server(monkeypatch)
    assert check.check_b1(api, audio, "초안").passed
    assert check.check_b2(api, audio, "T1", None).passed
    assert check.check_memo_save(api, "T1", None, "고친 문장", "VOICE_MEMO", "C1").passed
    assert check.check_memo_save(api, "T1", None, "직접 입력", "TYPED_TEXT", "C2").passed


def test_b1_detects_mismatch(monkeypatch, audio):
    assert not check.check_b1(fake_server(monkeypatch), audio, "다른 문장").passed


def test_b2_detects_save_on_transcribe(monkeypatch, audio):
    assert not check.check_b2(fake_server(monkeypatch, save_on_transcribe=True), audio, "T1", None).passed


def test_c1_detects_duplicate_on_retry(monkeypatch):
    api = fake_server(monkeypatch, dup_on_retry=True)
    assert not check.check_memo_save(api, "T1", None, "고친 문장", "VOICE_MEMO", "C1").passed


def test_c1_detects_key_reuse_accepted(monkeypatch):
    api = fake_server(monkeypatch, accept_key_reuse=True)
    assert not check.check_memo_save(api, "T1", None, "고친 문장", "VOICE_MEMO", "C1").passed
