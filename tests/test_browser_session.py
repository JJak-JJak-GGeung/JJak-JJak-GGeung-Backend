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
