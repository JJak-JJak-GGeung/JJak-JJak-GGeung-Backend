"""Browser-scoped registration and read-only synthetic photo examples."""
import json
from base64 import b64encode
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path as PathParam
from pydantic import BaseModel, Field

from .auth import Owner, require_store
from .database import remote, rows, rpc

router = APIRouter()
SAMPLES = Path(__file__).with_name("sample_photos")


class Registration(BaseModel):
    name: str = Field(min_length=1, max_length=150)


@router.post("/api/stores")
def register_store(body: Registration, owner: Owner):
    return rpc("register_directory_name", {"p_owner": owner, "p_store": None, "p_name": body.name})


@router.post("/api/stores/{store_id}/suppliers")
def register_supplier(store_id: UUID, body: Registration, owner: Owner):
    return rpc("register_directory_name", {"p_owner": owner, "p_store": str(store_id), "p_name": body.name})


class PeriodOpen(BaseModel):
    store_id: UUID
    supplier_id: UUID
    week_start: date


def period_response(record, created):
    return {"period_id": record["id"], "label": record["period_start"] + " ~ " + record["period_end"],
            "period_start": record["period_start"], "period_end": record["period_end"],
            "created": created}


@router.post("/api/periods")
def open_period(body: PeriodOpen, owner: Owner):
    """Open (or reuse) the Monday-to-Sunday period so a manually registered
    supplier can be viewed before any photo has been confirmed."""
    if body.week_start.weekday() != 0:
        raise HTTPException(422, "PERIOD_WEEK_REQUIRED")
    if body.week_start > date.today() + timedelta(days=7):
        raise HTTPException(422, "PERIOD_TOO_FAR_AHEAD")
    require_store(body.store_id, owner)
    store, supplier = str(body.store_id), str(body.supplier_id)
    if not rows("suppliers", id="eq." + supplier, store_id="eq." + store):
        raise HTTPException(404, "SUPPLIER_NOT_FOUND")
    start, end = body.week_start.isoformat(), (body.week_start + timedelta(days=6)).isoformat()
    created = True
    try:
        payload = {"store_id": store, "supplier_id": supplier, "period_start": start, "period_end": end}
        with remote("/rest/v1/periods", "POST", json.dumps(payload).encode()):
            pass
    except HTTPError as error:
        if error.code != 409:  # unique (store, supplier, start, end): reuse the existing week
            raise HTTPException(503, "Database operation unavailable") from None
        created = False
    except (URLError, TimeoutError):
        raise HTTPException(503, "Database temporarily unavailable") from None
    found = rows("periods", store_id="eq." + store, supplier_id="eq." + supplier,
                 period_start="eq." + start, period_end="eq." + end)
    if not found:
        raise HTTPException(503, "Database operation unavailable")
    return period_response(found[0], created)


@router.get("/api/sample-deliveries")
def list_samples(owner: Owner):
    return {"samples": rows("directory_samples", select="code,item,stores(name),suppliers(name)", order="code")}


@router.get("/api/sample-deliveries/{code}")
def sample_photo(owner: Owner, code: str = PathParam(pattern=r"^S[0-9]{2}$")):
    photo = SAMPLES / f"{code}.png"
    if not photo.is_file() or not rows("directory_samples", select="code", code="eq." + code):
        raise HTTPException(404, "SAMPLE_NOT_FOUND")
    return {"filename": photo.name, "image": b64encode(photo.read_bytes()).decode("ascii")}
