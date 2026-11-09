"""Web クリップの API（/api/v1/webclips）。docs/design/extensions.md 8.4。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..config import Settings
from ..deps import Auth, client_ip, get_conn, get_settings, require_auth
from ..webclip import service as wc

router = APIRouter(prefix="/api/v1")


class ClipIn(BaseModel):
    folder_id: int
    url: str


@router.post("/webclips", status_code=202)
def api_clip_create(body: ClipIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    cid, dups = wc.create_clip(conn, settings, auth.user, body.folder_id, body.url, client_ip(request))
    return wc.clip_json(wc.get_clip(conn, auth.user, cid)) | {"already_saved": dups}


@router.get("/webclips/{clip_id}")
def api_clip(clip_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return wc.clip_json(wc.get_clip(conn, auth.user, clip_id))


@router.post("/webclips/{clip_id}/refetch", status_code=202)
def api_clip_refetch(clip_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    wc.refetch(conn, settings, auth.user, clip_id, client_ip(request))
    return wc.clip_json(wc.get_clip(conn, auth.user, clip_id))


@router.delete("/webclips/{clip_id}", status_code=204)
def api_clip_delete(clip_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    wc.delete_pending(conn, auth.user, clip_id)
