from base64 import b64decode
from uuid import uuid4
from fastapi.testclient import TestClient
from app.api import directory, main


def test_directory_contract(monkeypatch):
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer device-v1." + "ab" * 32}
    store = str(uuid4())
    calls = []

    def rpc(name, payload):
        calls.append((name, payload))
        return {"id": store, "name": payload["p_name"], "created": True}

    monkeypatch.setattr(directory, "rpc", rpc)
    assert client.post("/api/stores", json={"name": "우리 가게"}).status_code == 401
    assert client.post("/api/stores", headers=headers, json={"name": "우리 가게"}).json()["id"] == store
    assert client.post(f"/api/stores/{store}/suppliers", headers=headers, json={"name": "새 거래처"}).json()["created"]
    assert [r["p_store"] for _, r in calls] == [None, store]
    assert calls[0][1]["p_owner"] == calls[1][1]["p_owner"]
    monkeypatch.setattr(directory, "rows", lambda *args, **kwargs: [{"code": "S01"}])
    assert client.get("/api/sample-deliveries", headers=headers).json()["samples"][0]["code"] == "S01"
    for code in ("S01", "S02", "S03"):
        response = client.get("/api/sample-deliveries/" + code, headers=headers).json()
        assert b64decode(response["image"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert client.get("/api/sample-deliveries/S99", headers=headers).status_code == 404
