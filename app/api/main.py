import hashlib
import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid5
from typing import Literal
from pydantic import BaseModel, Field

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from app.photo.extractor import extract_document
from app.api.auth import Owner, current_owner, require_store
from app.api.photo_review import router as review_router
from app.api.database import remote, rows, rpc
from app.workflow.settlement import get_settlement
from app.voice.router import router as voice_router

app = FastAPI(title="짝짜꿍 조회 API")
app.include_router(voice_router, dependencies=[Depends(current_owner)])
app.include_router(review_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[v.strip() for v in os.getenv("CORS_ORIGINS", "").split(",") if v.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/stores")
def stores(owner: Owner):
    return {"stores": [{"id": r["id"], "name": r["name"]} for r in rows("stores", owner_user_id="eq." + owner, order="name")]}


@app.get("/api/suppliers")
def suppliers(store_id: UUID, owner: Owner):
    require_store(store_id, owner)
    return {"suppliers": [{"id": r["id"], "name": r["name"]} for r in
                          rows("suppliers", store_id="eq." + str(store_id), order="name")]}


@app.get("/api/periods")
def periods(store_id: UUID, supplier_id: UUID, owner: Owner):
    require_store(store_id, owner)
    result = rows("periods", store_id="eq." + str(store_id),
                  supplier_id="eq." + str(supplier_id), order="period_start.desc")
    return {"is_example": False, "periods": [
        {"period_id": r["id"], "label": r["period_start"] + " ~ " + r["period_end"],
         "period_start": r["period_start"], "period_end": r["period_end"]} for r in result]}


@app.get("/api/periods/{period_id}")
def period(period_id: UUID, owner: Owner):
    period_record(period_id, owner)
    result = get_settlement(period_id)
    result["documents"] = document_list(period_id)
    return result


def line_response(row):
    return {"line_id": row["id"], "delivery_no": row["delivery_no"],
            "delivery_date": row["delivery_date"], "item": row["raw_name"],
            "spec": row["raw_spec"], "unit": row["raw_unit"],
            "qty_read": None if row["qty_read"] is None else str(row["qty_read"]),
            "qty_corrected": None if row["qty_corrected"] is None else str(row["qty_corrected"]),
            "unit_price_read": None if row["unit_price_read"] is None else str(row["unit_price_read"]),
            "unit_price_corrected": None if row["unit_price_corrected"] is None else str(row["unit_price_corrected"]),
            "amount_corrected": row["amount_corrected"],
            "amount_read": row["amount_read"], "source_location": row["source_location"]}


def document_response(value, lines=None):
    if lines is None:
        lines = sorted(rows("doc_lines", document_id="eq." + value["id"]),
                       key=lambda r: ((r["source_location"] or {}).get("row") or 0))
    return {
        "document_id": value["id"], "filename": value["filename"],
        "file_hash": value["file_hash"], "doc_type": value["doc_type"],
        "extraction_status": value["extraction_status"],
        "lines": [line_response(r) for r in lines],
        "duplicate": value.get("duplicate", False),
        "input_revision": value.get("input_revision"),
        **{key: value.get(key) for key in (
            "supplier_name_read", "supplier_id", "doc_no", "doc_date", "period_start", "period_end",
            "replaces_doc_no", "agreement_text", "agreement_source_location", "source_mode")},
    }


def run_extraction(doc, data=None):
    """Persist one complete extraction atomically with stable source line IDs."""
    if doc["extraction_status"] in ("COMPLETED", "SUCCESS"):
        return doc["extraction_status"]
    try:
        if data is None:
            with remote("/storage/v1/object/authenticated/originals/" + doc["storage_path"]) as response:
                data = response.read()
        result = extract_document(data, doc["mime_type"], doc["id"])
        compact = lambda value: "".join((value or "").split())
        matches = [supplier for supplier in rows("suppliers", store_id="eq." + doc["store_id"])
                   if compact(supplier["name"]) == compact(result["supplier_name_read"]) and compact(supplier["name"])]
        result["supplier_id"] = matches[0]["id"] if len(matches) == 1 else None
        lines = [{**line, "id": str(uuid5(UUID(doc["id"]), "line-" + str(index)))}
                 for index, line in enumerate(result["lines"])]
        rpc("persist_extraction", {"p_document": doc["id"], "p_fields": result, "p_lines": lines})
        return "COMPLETED"
    except Exception:
        logging.exception("Extraction failed for document %s", doc["id"])
        try:
            with remote("/rest/v1/documents?id=eq." + doc["id"], "PATCH",
                        json.dumps({"extraction_status": "FAILED"}).encode()):
                pass
        except (HTTPError, URLError, TimeoutError):
            logging.exception("Could not mark document %s as FAILED", doc["id"])
        return "FAILED"


def document_list(period_id):
    links = rows("period_documents", period_id="eq." + str(period_id))
    if not links:
        return []
    ids = ",".join(link["document_id"] for link in links)
    return [document_response(v) for v in rows("documents", id="in.(" + ids + ")", order="created_at")]


@app.post("/api/periods/{period_id}/documents")
def upload_document(period_id: UUID, owner: Owner, file: UploadFile = File(...),
                    doc_type: str = Form(...), request_key: str = Form(...)):
    if doc_type not in {"DELIVERY", "RECEIPT", "INVOICE", "CORRECTED_INVOICE"}:
        raise HTTPException(422, "Invalid document type")
    if not request_key or len(request_key) > 200:
        raise HTTPException(422, "Invalid request key")
    selected = period_record(period_id, owner)
    data = file.file.read(10485761)
    if not data or len(data) > 10485760:
        raise HTTPException(413, "File must contain 1 byte to 10 MB")
    mime = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    if mime is None or file.content_type != mime:
        raise HTTPException(415, "Supported formats: PNG, JPEG, WebP")
    digest = hashlib.sha256(data).hexdigest()
    storage_path = selected["store_id"] + "/" + digest
    payload = hashlib.sha256((str(period_id) + ":" + doc_type + ":" + digest).encode()).hexdigest()
    try:
        try:
            with remote("/storage/v1/object/originals/" + storage_path, "POST", data, mime):
                pass
        except HTTPError as error:
            body = error.read()
            if error.code not in (400, 409) or b"Duplicate" not in body:
                raise
        args = {"p_period": str(period_id), "p_hash": digest, "p_path": storage_path,
                "p_filename": (file.filename or "original")[:255], "p_mime": mime,
                "p_size": len(data), "p_type": doc_type, "p_key": request_key,
                "p_payload": payload}
        with remote("/rest/v1/rpc/register_document", "POST", json.dumps(args).encode()) as response:
            doc = json.load(response)
    except HTTPError as error:
        body = error.read()
        if b"KEY_REUSE" in body:
            raise HTTPException(409, "KEY_REUSE")
        logging.error("Document storage request failed: HTTP %s", error.code)
        raise HTTPException(503, "Document storage unavailable")
    except (URLError, TimeoutError):
        raise HTTPException(503, "Document storage unavailable")
    if doc["extraction_status"] not in ("COMPLETED", "SUCCESS"):
        doc["extraction_status"] = run_extraction(doc, data)
        # Saved lines bump the period revision through a database trigger.
        doc["input_revision"] = rows("periods", id="eq." + str(period_id))[0]["input_revision"]
    return document_response({**rows("documents", id="eq." + doc["id"])[0],
                              "duplicate": doc.get("duplicate", False),
                              "input_revision": doc.get("input_revision")})


@app.post("/api/documents/{document_id}/extraction")
def retry_extraction(document_id: UUID, owner: Owner):
    result = rows("documents", id="eq." + str(document_id))
    if not result:
        raise HTTPException(404, "Document not found")
    doc = result[0]
    require_store(doc["store_id"], owner)
    if doc.get("source_mode", "FILE") != "FILE":
        raise HTTPException(422, "Only uploaded originals can be read")
    doc["extraction_status"] = run_extraction(doc)
    return document_response({**rows("documents", id="eq." + doc["id"])[0],
                              "duplicate": doc.get("duplicate", False),
                              "input_revision": doc.get("input_revision")})


@app.get("/api/documents/{document_id}/original")
def original(document_id: UUID, owner: Owner):
    result = rows("documents", id="eq." + str(document_id))
    if not result:
        raise HTTPException(404, "Document not found")
    doc = result[0]
    require_store(doc["store_id"], owner)
    if doc["source_mode"] != "FILE":
        raise HTTPException(422, "This document contains confirmed text")
    try:
        with remote("/storage/v1/object/authenticated/originals/" + doc["storage_path"]) as response:
            data = response.read()
        return Response(data, media_type=doc["mime_type"],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except (HTTPError, URLError, TimeoutError):
        raise HTTPException(503, "Original temporarily unavailable")


class RevisionRequest(BaseModel):
    expected_revision: int = Field(strict=True, ge=0)
    request_key: str = Field(min_length=1, max_length=200)
    actor_label: str = Field(default="검수 점주", min_length=1, max_length=100)


class ConfirmationRequest(RevisionRequest):
    kind: Literal["receipt_link", "replacement", "line_correction"]
    source_line_id: UUID | None = None
    document_id: UUID | None = None
    target_id: UUID | None = None
    evidence_refs: list[dict]
    corrected_fields: dict | None = None
    supersedes_receipt_id: UUID | None = None


class MemoRequest(RevisionRequest):
    confirmed_text: str = Field(min_length=1, max_length=10000)
    source_mode: Literal["VOICE_MEMO", "TYPED_TEXT"]


def period_record(period_id, owner):
    result = rows("periods", id="eq." + str(period_id))
    if not result:
        raise HTTPException(404, "Period not found")
    require_store(result[0]["store_id"], owner)
    return result[0]


@app.post("/api/periods/{period_id}/confirmations")
def confirm(period_id: UUID, request: ConfirmationRequest, owner: Owner):
    value = period_record(period_id, owner)
    body = request.model_dump(mode="json", exclude_none=True)
    for key in ("expected_revision", "request_key", "actor_label"):
        body.pop(key)
    saved = rpc("save_confirmation", {
        "p_store": value["store_id"], "p_period": value["id"],
        "p_expected": request.expected_revision, "p_key": request.request_key,
        "p_body": body, "p_actor_label": request.actor_label,
    })
    return {**saved, "settlement": get_settlement(period_id, refresh=True)}


@app.get("/api/transactions/{transaction_id}")
def transaction(transaction_id: UUID, owner: Owner):
    result = rows("recon_items", id="eq." + str(transaction_id))
    if not result:
        raise HTTPException(404, "Transaction not found")
    value = result[0]
    require_store(value["store_id"], owner)
    state = get_settlement(value["period_id"])
    item = next((item for item in state["items"] if item["transaction_id"] == str(transaction_id)), None)
    if not item:
        raise HTTPException(404, "Transaction not in current period")
    memos = rows("transaction_memos", transaction_id="eq." + str(transaction_id), order="confirmed_at")
    return {**item, "input_revision": state["input_revision"],
            "memos": [{**m, "memo_id": m["id"]} for m in memos]}


@app.post("/api/transactions/{transaction_id}/memos")
def save_memo(transaction_id: UUID, request: MemoRequest, owner: Owner):
    result = rows("recon_items", id="eq." + str(transaction_id))
    if not result:
        raise HTTPException(404, "Transaction not found")
    value = result[0]
    require_store(value["store_id"], owner)
    saved = rpc("save_confirmed_memo", {
        "p_store": value["store_id"], "p_transaction": str(transaction_id),
        "p_text": request.confirmed_text, "p_mode": request.source_mode,
        "p_expected": request.expected_revision, "p_key": request.request_key,
        "p_actor_label": request.actor_label,
    })
    return {**saved, "settlement": get_settlement(value["period_id"], refresh=True)}


def approval_response(value):
    return {"approval_id": value["id"], "period_id": value["period_id"],
            "input_revision": value["input_revision"], "version_no": value["version_no"],
            "approved_amount": int(value["confirmed_total"]),
            "approved_at": value["approved_at"], "payment_status": value["payment_status"]}


@app.post("/api/periods/{period_id}/approvals")
def approve(period_id: UUID, request: RevisionRequest, owner: Owner):
    value = period_record(period_id, owner)
    # approve_period only trusts a reconciliation published at the current revision.
    get_settlement(period_id)
    return rpc("approve_period", {
        "p_store": value["store_id"], "p_period": value["id"],
        "p_expected": request.expected_revision, "p_key": request.request_key,
        "p_actor_label": request.actor_label,
    })


@app.get("/api/periods/{period_id}/approvals")
def approvals(period_id: UUID, owner: Owner):
    period_record(period_id, owner)
    return {"approvals": [approval_response(value) for value in
                          rows("settlement_versions", period_id="eq." + str(period_id), order="version_no.desc")]}


@app.get("/api/approvals/{approval_id}")
def approval(approval_id: UUID, owner: Owner):
    result = rows("settlement_versions", id="eq." + str(approval_id))
    if not result:
        raise HTTPException(404, "Approval not found")
    value = result[0]
    require_store(value["store_id"], owner)
    snapshot = value["snapshot"]
    return {**approval_response(value), "snapshot": snapshot}
