"""Preview has no accounting side effects. Only an explicit review enters a period."""
import hashlib
import hmac
import json
import logging
import os
from urllib.error import HTTPError
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError

from .database import remote, rows, rpc
from app.photo.vision import DocType, decimal_text, extract_document

router = APIRouter()


class ReviewLine(BaseModel):
    item: str = Field(min_length=1, max_length=300)
    unit: str | None = Field(max_length=60)
    qty_read: str | None
    unit_price_read: str | None
    amount_read: int | None = Field(strict=True, ge=-9007199254740991, le=9007199254740991)


class Review(BaseModel):
    store_id: UUID
    supplier_id: UUID
    period_id: UUID
    doc_type: DocType
    lines: list[ReviewLine] = Field(min_length=1, max_length=300)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(digest, draft):
    # Bind original and server reading, independently of worker or process restarts.
    key = os.environ["SUPABASE_SECRET_KEY"].encode()
    return hmac.digest(key, ("photo-review-v1:" + digest + fingerprint(draft)).encode(), "sha256").hex()


def photo(file):
    data = file.file.read(10485761)
    if not data or len(data) > 10485760:
        raise HTTPException(413, "Invalid photo size")
    return data, hashlib.sha256(data).hexdigest()


@router.post("/api/photo-previews")
def preview(file: UploadFile = File(...)):
    data, digest = photo(file)
    try:
        draft = extract_document(data, file.content_type, None)
    except Exception as error:
        # Neither original images nor provider responses belong in logs.
        logging.warning("Photo preview failed (%s)", type(error).__name__)
        raise HTTPException(503, "PHOTO_READING_FAILED") from None
    if not draft["lines"] or len(draft["lines"]) > 300:
        raise HTTPException(422, "NO_REVIEWABLE_LINES")
    return {"draft": draft, "signature": seal(digest, draft)}


@router.post("/api/photo-previews/confirm")
def confirm(file: UploadFile = File(...), draft_json: str = Form(...), signature: str = Form(...),
            review_json: str = Form(...), request_key: str = Form(..., min_length=1, max_length=200)):
    data, digest = photo(file)
    try:
        draft = json.loads(draft_json)
        review = Review.model_validate_json(review_json)
    except (ValueError, ValidationError):
        raise HTTPException(422, "INVALID_REVIEW") from None
    if not signature.isascii() or not hmac.compare_digest(seal(digest, draft), signature):
        raise HTTPException(422, "PREVIEW_CHANGED")
    if len(review.lines) != len(draft["lines"]):
        raise HTTPException(422, "REVIEW_LINE_COUNT")
    for line in review.lines:
        if not line.item.strip() or any(v is not None and decimal_text(v) is None
                                       for v in (line.qty_read, line.unit_price_read)):
            raise HTTPException(422, "INVALID_REVIEW_LINE")
    ids = review.model_dump(mode="json")
    period = rows("periods", id="eq." + ids["period_id"], store_id="eq." + ids["store_id"],
                  supplier_id="eq." + ids["supplier_id"])
    if not period:
        raise HTTPException(422, "REVIEW_SCOPE_MISMATCH")
    path = ids["store_id"] + "/" + digest
    try:
        with remote("/storage/v1/object/originals/" + path, "POST", data, file.content_type):
            pass
    except HTTPError as err:
        if err.code not in (400,409) or b"Duplicate" not in err.read():
            raise HTTPException(503, "Original storage unavailable") from None
    return rpc("confirm_photo_review", {
        "p_period": ids["period_id"], "p_store": ids["store_id"], "p_supplier": ids["supplier_id"],
        "p_hash": digest, "p_filename": (file.filename or "photo")[:255], "p_path": path,
        "p_mime": file.content_type, "p_size": len(data), "p_key": str(request_key),
        "p_payload": fingerprint([digest, draft, ids]), "p_type": review.doc_type,
        "p_draft": draft, "p_edits": ids["lines"],
    })
