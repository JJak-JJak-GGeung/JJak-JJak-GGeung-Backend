from uuid import uuid4
from fastapi.testclient import TestClient
from app.api import auth, main


def test_browser_evidence_reopens_only_for_same_profile(monkeypatch):
    store, period, approval, document = [str(uuid4()) for _ in range(4)]
    first = {"Authorization": "Bearer device-v1." + "ab" * 32}
    other = {"Authorization": "Bearer device-v1." + "cd" * 32}
    owner = auth.current_owner(auth.HTTPAuthorizationCredentials(scheme="Bearer", credentials=first["Authorization"].split()[1]))
    def lookup(table, **filters):
        if table == "stores":
            return [{"id": store, "name": "내 점포"}] if filters.get("owner_user_id") == "eq." + owner else []
        if table == "settlement_versions":
            return [{"id": approval, "store_id": store, "period_id": period, "input_revision": 2,
                     "version_no": 1, "confirmed_total": 100, "approved_at": "2026-11-21",
                     "payment_status": "UNCONFIRMED", "snapshot": {"documents": [document]}}]
        if table == "documents":
            return [{"id": document, "store_id": store, "source_mode": "FILE", "storage_path": "test", "mime_type": "image/png"}]
        return []
    monkeypatch.setattr(auth, "rows", lookup)
    monkeypatch.setattr(main, "rows", lookup)
    from io import BytesIO
    monkeypatch.setattr(main, "remote", lambda *a: BytesIO(b"test-original"))
    with TestClient(main.app) as client:
        assert client.get("/api/stores", headers=first).json()["stores"][0]["id"] == store
        assert client.get("/api/stores", headers=other).json()["stores"] == []
        assert client.get("/api/stores").status_code == 401
        assert client.get("/api/stores", headers={"Authorization": "Bearer " + owner}).status_code == 401
    with TestClient(main.app) as reopened:
        assert reopened.get("/api/approvals/" + approval, headers=first).json()["snapshot"]["documents"] == [document]
        assert reopened.get("/api/documents/" + document + "/original", headers=first).content == b"test-original"
        for path in ["/api/approvals/" + approval, "/api/documents/" + document + "/original"]:
            assert reopened.get(path, headers=other).status_code == 404


def test_direct_zero_receipt_uses_owner_and_existing_rpc(monkeypatch):
    period, store, target = [str(uuid4()) for _ in range(3)]
    actor = auth.HTTPAuthorizationCredentials(scheme="Bearer", credentials="device-v1." + "ab" * 32)
    owner = auth.current_owner(actor)
    monkeypatch.setattr(main, "period_record", lambda *args: {"id": period, "store_id": store})
    monkeypatch.setattr(main, "get_settlement", lambda *args, **kwargs: {})
    saved = []
    def save(name, params):
        saved.append((name, params))
        return {"receipt_id": target}
    monkeypatch.setattr(main, "rpc", save)
    endpoint = f"/api/periods/{period}/confirmations"
    headers = {"Authorization": "Bearer " + actor.credentials}
    base = {"expected_revision": 3, "request_key": "direct-qa", "target_id": target}
    with TestClient(main.app) as client:
        body = {**base, "kind": "receipt_entry", "evidence_text": "검수 계량", "received_qty": "0", "unit": "kg"}
        assert client.post(endpoint, json=body, headers=headers).status_code == 200
        assert saved[0][0] == "save_direct_receipt"
        assert saved[0][1]["p_body"]["received_qty"] == "0"
        assert saved[0][1]["p_actor"] == owner
        assert client.post(endpoint, json={**base, "kind": "receipt_link"}, headers=headers).status_code == 422
