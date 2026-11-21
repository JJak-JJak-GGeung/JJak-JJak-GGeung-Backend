"""HTTP boundary tests; install requirements-dev.txt to run."""
import json
import os
import unittest
from unittest.mock import patch
import time

try:
    import fastapi
    import dotenv
    import httpx
except ImportError:
    READY = False
else:
    READY = True
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.api.auth import current_owner
    from app.photo.review import sign
    from app.photo.test_extractor import facts, image_bytes


@unittest.skipUnless(READY, "install requirements-dev.txt (FastAPI, dotenv, httpx unavailable)")
class PhotoApiTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PHOTO_REVIEW_SIGNING_KEY": "test-signing-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.owner = "00000000-0000-0000-0000-000000000004"
        app.dependency_overrides[current_owner] = lambda: self.owner
        self.addCleanup(app.dependency_overrides.clear)
        self.scope = patch("app.api.photo_review_v2.require_store")
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.path = patch("app.api.photo_review_v2.original_upload_path", side_effect=lambda base: base)
        self.path.start()
        self.addCleanup(self.path.stop)
        self.client = TestClient(app)

    def request(self):
        draft = facts()
        draft["document_id"] = None
        draft["_preview_owner"] = self.owner
        draft["lines"][0]["source_location"] = {"page": 1, "row": 1, "region": None}
        review = facts()
        review["lines"][0]["source_row"] = 1
        raw = image_bytes()
        issued = int(time.time())
        data = {"draft_json": json.dumps(draft), "review_json": json.dumps(review),
                "issued_at": str(issued), "signature": sign(__import__('hashlib').sha256(raw).hexdigest(), draft, issued),
                "store_id": "00000000-0000-0000-0000-000000000001",
                "supplier_id": "00000000-0000-0000-0000-000000000002",
                "period_id": "00000000-0000-0000-0000-000000000003", "expected_revision": "7",
                "request_key": "review-test-key", "actor_label": "test reviewer"}
        return data, {"file": ("photo.png", raw, "image/png")}

    def test_preview_does_not_write_or_confirm(self):
        with patch("app.api.photo_review_v2.extract_document", return_value=facts()), \
                patch("app.api.photo_review_v2.rpc") as rpc, patch("app.api.photo_review_v2.remote") as storage:
            response = self.client.post("/api/photo-previews-v2", files={"file": ("a.png", image_bytes(), "image/png")})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["requires_review"])
        rpc.assert_not_called()
        storage.assert_not_called()

    def test_confirm_preserves_draft_edits_and_expected_revision(self):
        data, files = self.request()
        review = json.loads(data["review_json"])
        review["lines"][0]["qty_read"] = "0"
        review["lines"].append({**review["lines"][0], "source_row": None})
        data["review_json"] = json.dumps(review)
        with patch("app.api.photo_review_v2.rows", return_value=[{}]), \
                patch("app.api.photo_review_v2.remote"), \
                patch("app.api.photo_review_v2.rpc", return_value={"document_id": "saved"}) as rpc:
            response = self.client.post("/api/photo-previews-v2/confirm", data=data, files=files)
        self.assertEqual(response.status_code, 200)
        name, payload = rpc.call_args.args
        self.assertEqual(name, "confirm_photo_review_v2")
        self.assertEqual(payload["p_expected"], 7)
        self.assertEqual(payload["p_draft"]["lines"][0]["qty_read"], "40")
        self.assertEqual(payload["p_edits"][0]["qty_read"], "0")
        self.assertIsNone(payload["p_edits"][1]["source_row"])

    def test_quantity_in_unit_never_writes_on_either_confirm_route(self):
        from hashlib import sha256
        from app.api.photo_review import seal

        data, files = self.request()
        review = json.loads(data["review_json"])
        review["lines"][0].update(qty_read="40", unit="36kg")
        data["review_json"] = json.dumps(review)
        legacy = dict(data)
        with patch.dict(os.environ, {"SUPABASE_SECRET_KEY": "test-only"}):
            legacy["signature"] = seal(sha256(files["file"][1]).hexdigest(), json.loads(data["draft_json"]))
            for route, module, form in [
                ("photo-previews", "photo_review", legacy),
                ("photo-previews-v2", "photo_review_v2", data),
            ]:
                with self.subTest(route=route), patch(f"app.api.{module}.remote") as storage, \
                        patch(f"app.api.{module}.rpc") as rpc:
                    response = self.client.post(f"/api/{route}/confirm", data=form, files=files)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["detail"], "UNIT_CONTAINS_QUANTITY")
                    storage.assert_not_called()
                    rpc.assert_not_called()

    def test_tampered_preview_never_writes(self):
        data, files = self.request()
        data["draft_json"] = data["draft_json"].replace('"40"', '"999"')
        with patch("app.api.photo_review_v2.rpc") as rpc, patch("app.api.photo_review_v2.remote") as storage:
            response = self.client.post("/api/photo-previews-v2/confirm", data=data, files=files)
        self.assertEqual(response.status_code, 422)
        rpc.assert_not_called()
        storage.assert_not_called()

    def test_scope_mismatch_never_writes(self):
        data, files = self.request()
        with patch("app.api.photo_review_v2.rows", return_value=[]), patch("app.api.photo_review_v2.rpc") as rpc:
            response = self.client.post("/api/photo-previews-v2/confirm", data=data, files=files)
        self.assertEqual(response.status_code, 422)
        rpc.assert_not_called()

    def test_provider_failure_is_safe(self):
        with patch("app.api.photo_review_v2.extract_document", side_effect=RuntimeError("secret")):
            response = self.client.post("/api/photo-previews-v2", files={"file": ("a.png", image_bytes(), "image/png")})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)

    def test_legacy_upload_cannot_bypass_review(self):
        response = self.client.post("/api/periods/00000000-0000-0000-0000-000000000003/documents",
                                    data={"doc_type": "DELIVERY", "request_key": "legacy"},
                                    files={"file": ("a.png", image_bytes(), "image/png")})
        self.assertEqual(response.status_code, 409)

    def test_preview_requires_browser_session(self):
        app.dependency_overrides.clear()
        response = self.client.post("/api/photo-previews-v2", files={"file": ("a.png", image_bytes(), "image/png")})
        self.assertEqual(response.status_code, 401)

    def test_other_browser_cannot_confirm_signed_draft(self):
        data, files = self.request()
        app.dependency_overrides[current_owner] = lambda: "00000000-0000-0000-0000-000000000099"
        with patch("app.api.photo_review_v2.rpc") as rpc:
            response = self.client.post("/api/photo-previews-v2/confirm", data=data, files=files)
        self.assertEqual(response.status_code, 422)
        rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
