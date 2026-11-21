"""GPT previews have no accounting writes. Human confirmation is atomic in the DB."""
import hashlib
import json
import logging
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.photo.extractor import MAX_BYTES, extract_document, validate_image
from app.photo.review import UnitContainsQuantity, fingerprint, prepare_review, review_warnings, sign, verify
from .database import original_upload_path, remote, rows, rpc
from .auth import Owner, require_store

router = APIRouter(tags=["photo-review"])


def photo(file):
    data = file.file.read(MAX_BYTES + 1)
    try:
        validate_image(data, file.content_type)
    except ValueError:
        raise HTTPException(422, "INVALID_PHOTO") from None
    return data


@router.get("/review", response_class=HTMLResponse)
def review_page():
    return Path(__file__).with_name("photo_review.html").read_text(encoding="utf-8")


@router.post("/api/photo-previews-v2")
def preview(owner: Owner, file: UploadFile = File(...)):
    data = photo(file)
    try:
        draft = extract_document(data, file.content_type, None)
        draft["_preview_owner"] = owner
        issued = int(time.time())
        signature = sign(hashlib.sha256(data).hexdigest(), draft, issued)
    except Exception as error:
        logging.warning("Photo preview failed type=%s", type(error).__name__)
        raise HTTPException(503, "PHOTO_READING_FAILED") from None
    return {"draft": draft, "issued_at": issued, "signature": signature, "requires_review": True,
            "warnings": review_warnings(draft)}


@router.post("/api/photo-previews-v2/confirm")
def confirm(owner: Owner, file: UploadFile = File(...), draft_json: str = Form(..., max_length=1000000),
            review_json: str = Form(..., max_length=1000000), signature: str = Form(..., max_length=64),
            issued_at: int = Form(...), store_id: UUID = Form(...), supplier_id: UUID = Form(...),
            period_id: UUID = Form(...), expected_revision: int = Form(..., ge=0),
            request_key: str = Form(..., min_length=1, max_length=200),
            actor_label: str = Form(..., min_length=1, max_length=100)):
    data = photo(file)
    try:
        draft = json.loads(draft_json)
        digest = verify(data, draft, issued_at, signature)
        if draft.get("_preview_owner") != owner:
            raise ValueError("PREVIEW_OWNER_MISMATCH")
        fields, edits = prepare_review(draft, json.loads(review_json))
    except UnitContainsQuantity:
        raise HTTPException(422, "UNIT_CONTAINS_QUANTITY") from None
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "INVALID_OR_EXPIRED_REVIEW") from None
    scope = {"store_id": str(store_id), "supplier_id": str(supplier_id), "period_id": str(period_id)}
    require_store(store_id, owner)
    if not rows("periods", id="eq." + str(period_id), store_id="eq." + str(store_id),
                supplier_id="eq." + str(supplier_id)):
        raise HTTPException(422, "REVIEW_SCOPE_MISMATCH")
    path = original_upload_path(str(store_id) + "/" + digest)
    try:
        with remote("/storage/v1/object/originals/" + path, "POST", data, file.content_type):
            pass
    except HTTPError as error:
        if error.code not in (400, 409) or b"Duplicate" not in error.read():
            raise HTTPException(503, "Original storage unavailable") from None
    except (URLError, TimeoutError):
        raise HTTPException(503, "Original storage unavailable") from None
    return rpc("confirm_photo_review_v2", {
        "p_period": str(period_id), "p_store": str(store_id), "p_supplier": str(supplier_id),
        "p_hash": digest, "p_filename": (file.filename or "photo")[:255], "p_path": path,
        "p_mime": file.content_type, "p_size": len(data), "p_key": request_key,
        "p_payload": fingerprint([digest, draft, fields, edits, scope, expected_revision, actor_label]),
        "p_type": fields["doc_type"], "p_draft": draft, "p_fields": fields,
        "p_edits": edits, "p_expected": expected_revision, "p_actor_label": actor_label, "p_owner": owner,
    })
