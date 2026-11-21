import hashlib
import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from app.photo.extractor import extract_document

app = FastAPI(title="짝짜꿍 조회 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[v.strip() for v in os.getenv("CORS_ORIGINS", "").split(",") if v.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


def rows(table, **filters):
    query = urlencode({"select": "*", **filters})
    request = Request(
        os.environ["SUPABASE_URL"] + "/rest/v1/" + table + "?" + query,
        headers={"apikey": os.environ["SUPABASE_SECRET_KEY"],
                 "Authorization": "Bearer " + os.environ["SUPABASE_SECRET_KEY"]},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError):
        logging.exception("Database read failed")
        raise HTTPException(503, "Database temporarily unavailable")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/stores")
def stores():
    return {"stores": [{"id": r["id"], "name": r["name"]} for r in rows("stores", order="name")]}


@app.get("/api/suppliers")
def suppliers(store_id: UUID):
    return {"suppliers": [{"id": r["id"], "name": r["name"]} for r in
                          rows("suppliers", store_id="eq." + str(store_id), order="name")]}


@app.get("/api/periods")
def periods(store_id: UUID, supplier_id: UUID):
    result = rows("periods", store_id="eq." + str(store_id),
                  supplier_id="eq." + str(supplier_id), order="period_start.desc")
    return {"is_example": False, "periods": [
        {"period_id": r["id"], "label": r["period_start"] + " ~ " + r["period_end"],
         "period_start": r["period_start"], "period_end": r["period_end"]} for r in result]}


@app.get("/api/periods/{period_id}")
def period(period_id: UUID):
    result = rows("periods", id="eq." + str(period_id))
    if not result:
        raise HTTPException(404, "Period not found")
    value = result[0]
    return {
        "period_id": value["id"], "store_id": value["store_id"],
        "supplier_id": value["supplier_id"], "period_start": value["period_start"],
        "period_end": value["period_end"], "is_example": False,
        "input_revision": value["input_revision"],
        "summary": {key: None for key in [
            "declared_qty", "billed_qty", "received_qty", "unit",
            "delta_qty", "check_amount", "billed_amount"]},
        "documents": document_list(period_id),
        "items": [], "confirmation_scope": "DOCUMENTS_ONLY",
        "match_result": "MISSING_DATA", "fact_status": "UNCONFIRMED",
        "work_status": "NEEDS_INFO", "blockers": ["거래 자료가 아직 등록되지 않았습니다."],
        "can_approve": False, "approvable_amount": None,
        "approved_amount": None, "payment_status": "UNCONFIRMED",
    }


def remote(path, method="GET", data=None, content_type="application/json"):
    request = Request(
        os.environ["SUPABASE_URL"] + path,
        data=data, method=method,
        headers={"apikey": os.environ["SUPABASE_SECRET_KEY"],
                 "Authorization": "Bearer " + os.environ["SUPABASE_SECRET_KEY"],
                 "Content-Type": content_type},
    )
    return urlopen(request, timeout=30)


def line_response(row):
    return {"line_id": row["id"], "delivery_no": row["delivery_no"],
            "delivery_date": row["delivery_date"], "item": row["raw_name"],
            "spec": row["raw_spec"], "unit": row["raw_unit"],
            "qty_read": row["qty_read"], "unit_price_read": row["unit_price_read"],
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
    }


def run_extraction(doc, data=None):
    """Read the original with the photo extractor and store its lines on the document."""
    doc_id = doc["id"]
    path = "/rest/v1/documents?id=eq." + doc_id
    try:
        if data is None:
            with remote("/storage/v1/object/authenticated/originals/" + doc["storage_path"]) as response:
                data = response.read()
        result = extract_document(data, doc["mime_type"], doc_id)
        start, end = result["period_start"], result["period_end"]
        if start and end and end < start:
            start = end = None
        with remote("/rest/v1/doc_lines?document_id=eq." + doc_id, "DELETE"):
            pass
        lines = [{"store_id": doc["store_id"], "document_id": doc_id,
                  "delivery_no": line["delivery_no"], "delivery_date": line["delivery_date"],
                  "raw_name": line["item"], "raw_spec": line["spec"], "raw_unit": line["unit"],
                  "qty_read": line["qty_read"], "unit_price_read": line["unit_price_read"],
                  "amount_read": line["amount_read"], "source_location": line["source_location"],
                  "extraction_status": "COMPLETED"} for line in result["lines"]]
        if lines:
            with remote("/rest/v1/doc_lines", "POST", json.dumps(lines).encode()):
                pass
        fields = {key: result[key] for key in (
            "supplier_name_read", "doc_no", "doc_date", "payment_due_on",
            "replaces_doc_no", "agreement_text", "agreement_source_location")}
        fields.update(period_start=start, period_end=end, extraction_status="COMPLETED",
                      metadata={**(doc.get("metadata") or {}), "doc_type_read": result["doc_type"]})
        status = "COMPLETED"
    except Exception:
        logging.exception("Extraction failed for document %s", doc_id)
        fields, status = {"extraction_status": "FAILED"}, "FAILED"
    try:
        with remote(path, "PATCH", json.dumps(fields).encode()):
            pass
    except (HTTPError, URLError, TimeoutError):
        logging.exception("Extraction status update failed for document %s", doc_id)
        status = "FAILED"
    logging.warning("Extraction %s for document %s", status, doc_id)
    return status


def document_list(period_id):
    links = rows("period_documents", period_id="eq." + str(period_id))
    if not links:
        return []
    ids = ",".join(link["document_id"] for link in links)
    return [document_response(v) for v in rows("documents", id="in.(" + ids + ")", order="created_at")]


@app.post("/api/periods/{period_id}/documents")
def upload_document(period_id: UUID, file: UploadFile = File(...),
                    doc_type: str = Form(...), request_key: str = Form(...)):
    if doc_type not in {"DELIVERY", "RECEIPT", "INVOICE", "CORRECTED_INVOICE"}:
        raise HTTPException(422, "Invalid document type")
    if not request_key or len(request_key) > 200:
        raise HTTPException(422, "Invalid request key")
    periods = rows("periods", id="eq." + str(period_id))
    if not periods:
        raise HTTPException(404, "Period not found")
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
    storage_path = periods[0]["store_id"] + "/" + digest
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
    return document_response(doc)


@app.post("/api/documents/{document_id}/extraction")
def retry_extraction(document_id: UUID):
    result = rows("documents", id="eq." + str(document_id))
    if not result:
        raise HTTPException(404, "Document not found")
    doc = result[0]
    if doc.get("source_mode", "FILE") != "FILE":
        raise HTTPException(422, "Only uploaded originals can be read")
    doc["extraction_status"] = run_extraction(doc)
    return document_response(doc)


@app.get("/api/documents/{document_id}/original")
def original(document_id: UUID):
    result = rows("documents", id="eq." + str(document_id))
    if not result:
        raise HTTPException(404, "Document not found")
    doc = result[0]
    try:
        with remote("/storage/v1/object/authenticated/originals/" + doc["storage_path"]) as response:
            data = response.read()
        return Response(data, media_type=doc["mime_type"],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except (HTTPError, URLError, TimeoutError):
        raise HTTPException(503, "Original temporarily unavailable")
