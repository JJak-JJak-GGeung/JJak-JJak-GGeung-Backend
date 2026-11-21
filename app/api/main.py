from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import Owner, current_owner, require_store
from app.api.photo_review import router as review_router
from app.photo.extractor import AMOUNT_FIELDS
from app.api.directory import router as directory_router
from app.api.photo_review_v2 import router as review_v2_router
from app.api.database import original_upload_path, remote, rows, rpc
from app.workflow.settlement import get_settlement
from app.voice.router import router as voice_router

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
app = FastAPI(title="짝짜꿍 조회 API")
app.include_router(voice_router, dependencies=[Depends(current_owner)])
app.include_router(review_router)
app.include_router(directory_router)
app.include_router(review_v2_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[v.strip() for v in (os.getenv("CORS_ORIGINS") or os.getenv("FRONTEND_ORIGIN", "")).split(",") if v.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/stores")
def stores(owner: Owner):
    return {"directory_registration": True,
            "stores": [{"id": r["id"], "name": r["name"]} for r in rows("stores", owner_user_id="eq." + owner, order="name")]}


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
    result["pending_removals"] = [r["document_id"] for r in rows("document_removals",
        period_id="eq." + str(period_id), purged_at="is.null")]
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
            "tax_amount_read": row.get("tax_amount_read"), "line_date_text": row.get("line_date_text"),
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
        "photo_review": (value.get("metadata") or {}).get("photo_review"),
        **{key: value.get(key) for key in (
            *AMOUNT_FIELDS, "trade_date", "issued_on", "quoted_on", "source_date_text",
            "supplier_name_read", "supplier_id", "selected_supplier_id", "doc_no", "doc_date", "period_start", "period_end",
            "replaces_doc_no", "agreement_text", "agreement_source_location", "source_mode")},
    }


def document_list(period_id):
    links = rows("period_documents", period_id="eq." + str(period_id))
    if not links:
        return []
    ids = ",".join(link["document_id"] for link in links)
    return [document_response(v) for v in rows("documents", id="in.(" + ids + ")", order="created_at")]


@app.post("/api/periods/{period_id}/documents")
def upload_document(period_id: UUID, owner: Owner, file: UploadFile = File(...),
                    doc_type: str = Form(...), request_key: str = Form(...)):
    raise HTTPException(409, "PHOTO_REVIEW_REQUIRED: use a photo preview and explicit confirmation")


@app.post("/api/documents/{document_id}/extraction")
def retry_extraction(document_id: UUID, owner: Owner):
    result = rows("documents", id="eq." + str(document_id))
    if not result:
        raise HTTPException(404, "Document not found")
    doc = result[0]
    require_store(doc["store_id"], owner)
    if doc.get("source_mode", "FILE") != "FILE":
        raise HTTPException(422, "Only uploaded originals can be read")
    if doc["extraction_status"] not in ("COMPLETED", "SUCCESS"):
        raise HTTPException(409, "PHOTO_REVIEW_REQUIRED: use a photo preview")
    return document_response(doc)


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

@app.post("/api/periods/{period_id}/documents/{document_id}/remove")
def remove_upload(period_id: UUID, document_id: UUID, owner: Owner):
    result = rpc("remove_uploaded_document", {"p_owner": owner, "p_period": str(period_id),
                                               "p_doc": str(document_id)})
    path = result["path"]
    if path is not None:
        try:
            # Storage's batch removal is idempotent for absent keys.
            with remote("/storage/v1/object/originals", "DELETE", json.dumps({"prefixes": [path]}).encode()):
                pass
            with remote("/rest/v1/document_removals?document_id=eq." + str(document_id), "PATCH",
                        json.dumps({"purged_at": datetime.now(timezone.utc).isoformat()}).encode()):
                pass
        except (HTTPError, URLError, TimeoutError):
            raise HTTPException(503, "FILE_REMOVAL_PENDING") from None
    return {"document_id": str(document_id), "removed": True}

class TradeDateEdit(BaseModel):
    expected: date | None
    trade_date: date | None


@app.post("/api/periods/{period_id}/documents/{document_id}/trade-date")
def edit_trade_date(period_id: UUID, document_id: UUID, edit: TradeDateEdit, owner: Owner):
    values = edit.model_dump(mode="json")
    rpc("correct_document_trade_date", {"p_owner": owner, "p_period": str(period_id),
        "p_doc": str(document_id), "p_expected": values["expected"], "p_date": values["trade_date"]})
    return {"trade_date": values["trade_date"]}



@app.get("/api/periods/{period_id}/review-context")
def review_context(period_id: UUID, owner: Owner):
    value = period_record(period_id, owner)
    return {key: value[key] for key in ("id", "store_id", "supplier_id", "input_revision")}
