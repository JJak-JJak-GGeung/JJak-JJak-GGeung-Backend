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


def test_open_period_reuses_the_week(monkeypatch):
    from datetime import date, timedelta
    from urllib.error import HTTPError

    client = TestClient(main.app)
    headers = {"Authorization": "Bearer device-v1." + "cd" * 32}
    store, supplier = str(uuid4()), str(uuid4())
    monday = date.today() - timedelta(days=date.today().weekday() + 7)
    inserted = []

    class Done:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def remote(path, method="GET", data=None, content_type="application/json"):
        inserted.append(path)
        if len(inserted) > 1:
            raise HTTPError(path, 409, "duplicate", {}, None)
        return Done()

    def rows(table, **filters):
        if table == "suppliers":
            return [{"id": supplier}] if filters["id"] == "eq." + supplier else []
        return [{"id": "P1", "period_start": filters["period_start"][3:], "period_end": filters["period_end"][3:]}]

    monkeypatch.setattr(directory, "require_store", lambda *args: None)
    monkeypatch.setattr(directory, "remote", remote)
    monkeypatch.setattr(directory, "rows", rows)
    body = {"store_id": store, "supplier_id": supplier, "week_start": monday.isoformat()}
    assert client.post("/api/periods", json=body).status_code == 401
    first = client.post("/api/periods", headers=headers, json=body).json()
    again = client.post("/api/periods", headers=headers, json=body).json()
    assert first["period_id"] == again["period_id"] == "P1"
    assert (first["created"], again["created"]) == (True, False)
    assert first["period_end"] == (monday + timedelta(days=6)).isoformat()
    tuesday = {**body, "week_start": (monday + timedelta(days=1)).isoformat()}
    assert client.post("/api/periods", headers=headers, json=tuesday).status_code == 422
    stranger = {**body, "supplier_id": str(uuid4())}
    assert client.post("/api/periods", headers=headers, json=stranger).status_code == 404
