"""Authenticate Supabase sessions before using the server's database key."""

import json
import os
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import rows

bearer = HTTPBearer(auto_error=False)


def current_owner(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    invalid = HTTPException(401, "OWNER_AUTH_REQUIRED", headers={"WWW-Authenticate": "Bearer"})
    if credentials is None:
        raise invalid
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        raise HTTPException(503, "OWNER_AUTH_UNAVAILABLE")
    request = Request(url.rstrip("/") + "/auth/v1/user", headers={
        "apikey": key,
        "Authorization": "Bearer " + credentials.credentials,
    })
    try:
        with urlopen(request, timeout=10) as response:
            user = json.load(response)
        return str(UUID(user["id"]))
    except HTTPError as error:
        if error.code in (400, 401, 403):
            raise invalid from None
        raise HTTPException(503, "OWNER_AUTH_UNAVAILABLE") from None
    except (URLError, TimeoutError, ValueError, TypeError, KeyError):
        raise HTTPException(503, "OWNER_AUTH_UNAVAILABLE") from None


Owner = Annotated[str, Depends(current_owner)]


def require_store(store_id: UUID | str, owner: str) -> None:
    if not rows("stores", id="eq." + str(store_id), owner_user_id="eq." + owner):
        raise HTTPException(404, "STORE_NOT_FOUND")


def require_any_store(owner: Owner) -> None:
    if not rows("stores", owner_user_id="eq." + owner, limit="1"):
        raise HTTPException(403, "OWNER_STORE_REQUIRED")
