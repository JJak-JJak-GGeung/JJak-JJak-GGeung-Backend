import io
from unittest.mock import patch
from urllib.error import URLError
from uuid import uuid4
from fastapi.testclient import TestClient
from app.api import main
from app.api.auth import current_owner
from app.photo.vision import iso


def test_original_date_formats_require_a_real_year_and_day():
    assert iso("2025.1.31") == iso("2025-01-31(금)") == "2025-01-31"
    assert iso("2026년 10월 15일") == "2026-10-15"
    assert iso("11/19") is None and iso("2025.2.29") is None


def test_storage_removal_only_after_database_approval_and_retry():
    owner, period, doc = [str(uuid4()) for _ in range(3)]
    main.app.dependency_overrides[current_owner] = lambda: owner
    try:
        client = TestClient(main.app)
        url = f"/api/periods/{period}/documents/{doc}/remove"
        with patch.object(main, "rpc", return_value={"path": "fixture/file"}) as db, patch.object(main, "remote") as storage:
            storage.side_effect = URLError("offline")
            assert client.post(url).json()["detail"] == "FILE_REMOVAL_PENDING"
            storage.side_effect = lambda *args: io.BytesIO()
            assert client.post(url).json()["removed"]
            assert storage.call_args_list[-2].args[:2] == ("/storage/v1/object/originals", "DELETE")
            assert db.call_args.args[1] == {"p_owner": owner, "p_period": period, "p_doc": doc}
            db.return_value = {"path": None}
            storage.reset_mock()
            assert client.post(url).status_code == 200
            storage.assert_not_called()
    finally:
        main.app.dependency_overrides.pop(current_owner, None)
