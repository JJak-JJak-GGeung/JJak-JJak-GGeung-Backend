"""Browser-scoped registration and read-only synthetic photo examples."""
from base64 import b64encode
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path as PathParam
from pydantic import BaseModel, Field

from .auth import Owner
from .database import rows, rpc

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


@router.get("/api/sample-deliveries")
def list_samples(owner: Owner):
    return {"samples": rows("directory_samples", select="code,item,stores(name),suppliers(name)", order="code")}


@router.get("/api/sample-deliveries/{code}")
def sample_photo(owner: Owner, code: str = PathParam(pattern=r"^S[0-9]{2}$")):
    photo = SAMPLES / f"{code}.png"
    if not photo.is_file() or not rows("directory_samples", select="code", code="eq." + code):
        raise HTTPException(404, "SAMPLE_NOT_FOUND")
    return {"filename": photo.name, "image": b64encode(photo.read_bytes()).decode("ascii")}
