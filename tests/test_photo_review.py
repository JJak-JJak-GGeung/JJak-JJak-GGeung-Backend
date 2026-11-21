import io
import json
import os
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from PIL import Image

from app.api.main import app
from app.api.auth import current_owner
from app.photo import vision


@pytest.fixture(autouse=True)
def authenticated_owner():
    owner = str(uuid4())
    app.dependency_overrides[current_owner] = lambda: owner
    with patch("app.api.auth.rows", return_value=[{"id": "owned-store"}]), patch("app.api.database.rows", return_value=[]):
        yield
    app.dependency_overrides.pop(current_owner, None)


def png():
    buf = io.BytesIO()
    Image.new("RGB", (300, 120), "white").save(buf, "PNG")
    return buf.getvalue()


def test_preview_then_confirm_only_after_review():
    raw = png()
    model_doc = {name: None for name in vision.Document.model_fields}
    row = {name: None for name in vision.Item.model_fields}
    row.update(item="품목", qty_read="0", unit_price_read="1.2", amount_read=0, box=[0, 0, 500, 500])
    model_doc.update(supplier_name_read="공급사", doc_type="DELIVERY", lines=[row])
    calls = []

    def provider(request):
        body = json.loads(request.content)
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert body["response_format"]["json_schema"]["strict"] is True
        calls.append(body["model"])
        return httpx.Response(200, json={"id":"test","model":"test","created":1,"object":"chat.completion",
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":json.dumps(model_doc)}}]})

    session = TestClient(app)
    sdk = OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(provider)))
    files = {"file": ("test.png",raw,"image/png")}
    st, sp, per = (str(uuid4()) for _ in range(3))
    with patch.dict(os.environ, {"SUPABASE_SECRET_KEY":"unit-test-signing-key"}), \
         patch.object(vision,"read_ocr",return_value={"lines":[]}), patch.object(vision,"OpenAI",return_value=sdk), \
         patch("app.api.photo_review.remote") as storage, patch("app.api.photo_review.rpc") as persist, \
         patch("app.api.photo_review.rows",return_value=[{"id":per}]) as scope:
        preview = session.post("/api/photo-previews",files=files)
        assert preview.status_code == 200, preview.text
        storage.assert_not_called()
        persist.assert_not_called()
        draft = preview.json()["draft"]
        assert draft["lines"][0]["qty_read"] == "0"
        assert draft["lines"][0]["source_location"]["region"] == [0,0,150,60]
        edited = {"item":"품목 수정", "qty_read":"3", "unit_price_read":"1.2","amount_read":None,"unit":"kg"}
        review = {"store_id":st,"supplier_id":sp,"period_id":per,"doc_type":"DELIVERY","lines":[edited]}
        fields = {"signature":preview.json()["signature"],"draft_json":json.dumps(draft),
                  "review_json":json.dumps(review),"request_key":"test-key"}
        url = "/api/photo-previews/confirm"
        changed = {**fields,"draft_json":json.dumps({**draft,"supplier_name_read":"다른 거래처"})}
        assert session.post(url, files=files,data=changed).status_code == 422
        scope.return_value = []
        assert session.post(url,files=files,data=fields).status_code == 422
        storage.assert_not_called()
        scope.return_value = [{"id":per}]
        persist.return_value = {"document_id":"same","period_id":per}
        assert session.post(url,files=files,data=fields).json()["document_id"] == "same"
        assert len(calls) == 1
        args = persist.call_args.args[1]
        assert args["p_draft"]["lines"][0]["qty_read"] == "0"
        assert args["p_edits"][0]["qty_read"] == "3"


def test_model_failure_no_silent_accept():
    with patch.object(vision,"read_ocr",return_value={"lines":[{}]}), \
         patch.object(vision,"read_model",side_effect=RuntimeError("model unavailable")), \
         patch("app.api.photo_review.rpc") as persist:
        response = TestClient(app).post("/api/photo-previews", files={"file":("x.png",png(),"image/png")})
        assert response.status_code == 503
        persist.assert_not_called()


def test_unknown_supplier_confirmed_only_under_current_owner(monkeypatch):
    from app.api import photo_review, auth
    raw = png()
    owner, shop = str(uuid4()), str(uuid4())
    app.dependency_overrides[current_owner] = lambda: owner
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-key")
    draft = {"store_name_read": "받는 곳", "supplier_name_read": "새 공급자", "lines": [{"item":"새 품목"}]}
    monkeypatch.setattr(photo_review, "extract_document", lambda *args: draft)
    monkeypatch.setattr(auth, "rows", lambda *a, **k: [])
    files = {"file": ("sample.png", raw, "image/png")}
    client = TestClient(app)
    with patch.object(photo_review,"rpc") as rpc, patch.object(photo_review,"remote") as storage:
        preview = client.post("/api/photo-previews",files=files)
        assert preview.status_code == 200
        rpc.assert_not_called()
        storage.assert_not_called()
        review = {"store_name":"받는 곳","supplier_name":"확인한 새 공급자","doc_type":"DELIVERY","week_start":"2026-11-09",
                  "lines":[{"item":"새 품목","qty_read":None,"unit":None,"unit_price_read":None,"amount_read":None}]}
        fields = {"signature":preview.json()["signature"],"draft_json":json.dumps(draft),"request_key":"k"}
        def save(r):
            return client.post("/api/photo-previews/confirm",files=files,data={**fields,"review_json":json.dumps(r)})
        assert save({**review,"store_name":None,"store_id":shop}).status_code == 404
        assert save({**review,"week_start":"2026-11-10"}).status_code == 422
        storage.assert_not_called()
        rpc.return_value={"document_id":"d","period_id":"p"}
        assert save(review).json()["document_id"]=="d"
        name,args=rpc.call_args.args
        assert name=="confirm_photo_with_context"
        assert args["p_owner"]==owner and args["p_review"]["supplier_name"]=="확인한 새 공급자"
        assert args["p_draft"]["supplier_name_read"]=="새 공급자"
