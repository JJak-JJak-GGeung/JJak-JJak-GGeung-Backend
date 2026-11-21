import io
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import auth, main
from app.workflow import photo


def test_supabase_identity_and_store_scope(monkeypatch):
    owner, store, other = (str(uuid4()) for _ in range(3))
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "test-public-key")

    def user(request, timeout):
        assert request.full_url == "https://supabase.example/auth/v1/user"
        assert request.get_header("Authorization") == "Bearer example-token"
        return io.BytesIO(json.dumps({"id": owner}).encode())

    monkeypatch.setattr(auth, "urlopen", user)
    monkeypatch.setattr(auth, "rows", lambda table, **filters: (
        [{"id": store}] if (table, filters) == (
            "stores", {"id": "eq." + store, "owner_user_id": "eq." + owner}
        ) else []
    ))
    monkeypatch.setattr(main, "rows", lambda table, **filters: [])
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer example-token"}
    assert client.get("/health").status_code == 200
    assert client.get("/api/stores").status_code == 401
    assert client.get("/api/suppliers", params={"store_id": store}, headers=headers).status_code == 200
    assert client.get("/api/suppliers", params={"store_id": other}, headers=headers).status_code == 404
    for url in ("/api/transcriptions", "/api/photo-previews"):
        assert client.post(url, files={"file": ("test.png", b"x", "image/png")}).status_code == 401


def test_direct_receipt_and_approval_use_verified_actor(monkeypatch):
    owner, store, period, target = (str(uuid4()) for _ in range(4))
    calls = []
    main.app.dependency_overrides[auth.current_owner] = lambda: owner
    monkeypatch.setattr(main, "period_record", lambda period_id, actor: {"id": period, "store_id": store})
    monkeypatch.setattr(main, "get_settlement", lambda *args, **kwargs: {"input_revision": 6})

    def rpc(name, payload):
        calls.append((name, payload))
        return {"input_revision": 6}

    monkeypatch.setattr(main, "rpc", rpc)
    try:
        client = TestClient(main.app)
        base = f"/api/periods/{period}"
        receipt = {
            "kind": "receipt_entry", "target_id": target, "received_qty": "0",
            "unit": "kg", "evidence_text": "검수 점주가 직접 확인",
            "expected_revision": 5, "request_key": "receipt-1", "actor_label": "spoofed",
        }
        assert client.post(base + "/confirmations", json=receipt).status_code == 200
        assert client.post(base + "/approvals", json={
            "expected_revision": 6, "request_key": "approval-1", "actor_label": "spoofed",
        }).status_code == 200
        assert [name for name, _ in calls] == ["save_direct_receipt", "approve_period"]
        assert calls[0][1]["p_body"]["received_qty"] == "0"
        assert all(payload["p_actor"] == owner and payload["p_actor_label"] == "점주"
                   for _, payload in calls)
    finally:
        main.app.dependency_overrides.pop(auth.current_owner, None)


def test_langgraph_uses_upload_bytes_or_loads_saved_original(monkeypatch):
    calls = []

    def reader(data, mime, document_id):
        calls.append((data, mime, document_id))
        return {"lines": [{"qty_read": "40"}]}

    fetched = []

    def remote(path):
        fetched.append(path)
        return io.BytesIO(b"saved")

    monkeypatch.setattr(photo, "remote", remote)
    graph = photo.compile_photo_graph(reader)
    context = {"mime": "image/png", "document_id": "doc", "storage_path": "store/file"}
    assert graph.invoke({**context, "data": b"upload"})["result"]["lines"][0]["qty_read"] == "40"
    assert fetched == []
    assert graph.invoke({**context, "data": None})["result"]["lines"][0]["qty_read"] == "40"
    assert calls == [(b"upload", "image/png", "doc"), (b"saved", "image/png", "doc")]
    assert fetched == ["/storage/v1/object/authenticated/originals/store/file"]
