"""Scope photo evidence to one browser profile without registering a user."""
from hashlib import sha256
import re
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import rows

bearer = HTTPBearer(auto_error=False)


def current_owner(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    if credentials is None or not re.fullmatch(r"device-v1\.[0-9a-f]{64}", credentials.credentials):
        raise HTTPException(401, "BROWSER_SESSION_REQUIRED")
    # The public database ID cannot be used as a bearer credential.
    return str(UUID(bytes=sha256(credentials.credentials.encode("ascii")).digest()[:16]))


Owner = Annotated[str, Depends(current_owner)]


def require_store(store_id: UUID | str, owner: str) -> None:
    if not rows("stores", id="eq." + str(store_id), owner_user_id="eq." + owner):
        raise HTTPException(404, "STORE_NOT_FOUND")
