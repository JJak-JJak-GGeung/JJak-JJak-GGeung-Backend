import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException


def remote(path, method="GET", data=None, content_type="application/json"):
    key = os.environ["SUPABASE_SECRET_KEY"]
    request = Request(
        os.environ["SUPABASE_URL"] + path, data=data, method=method,
        headers={"apikey": key, "Authorization": "Bearer " + key,
                 "Content-Type": content_type},
    )
    return urlopen(request, timeout=40)


def rpc(name, payload):
    try:
        with remote("/rest/v1/rpc/" + name, "POST", json.dumps(payload).encode()) as response:
            return json.load(response)
    except HTTPError as error:
        value = json.loads(error.read())
        if error.code in (404, 409, 422):
            detail = {"code": value.get("message", "REQUEST_FAILED")}
            try:
                detail.update(json.loads(value.get("details") or "{}"))
            except (ValueError, TypeError):
                pass
            raise HTTPException(error.code, detail)
        raise HTTPException(503, "Database operation unavailable")
    except (URLError, TimeoutError):
        raise HTTPException(503, "Database temporarily unavailable")


def rows(table, **filters):
    try:
        query = urlencode({"select": "*", **filters})
        with remote("/rest/v1/" + table + "?" + query) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError):
        raise HTTPException(503, "Database temporarily unavailable")
