"""整理の補助の API（/api/v1）。docs/design/extensions.md 8.5。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from .. import documents as dm
from .. import llm, suggest
from ..config import Settings
from ..deps import Auth, get_conn, get_settings, require_admin, require_auth

router = APIRouter(prefix="/api/v1")


def embedder_of(request: Request):
    """api の検索用の埋め込み器（フォルダのパスの埋め込みに使う）。読み込めなければ None。"""
    emb, _err = request.app.state.query_embedder.get()
    return emb


@router.get("/documents/{doc_id}/suggestions")
def api_suggestions(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    d = dm.get_document(conn, auth.user, doc_id)
    out = suggest.for_document(conn, d, embedder=embedder_of(request), enabled=settings.suggest_enabled,
                               llm_enabled=settings.llm_enabled)
    out.pop("prefs")
    return out


@router.post("/documents/{doc_id}/suggestions/{sid}/accept", status_code=204)
def api_accept(doc_id: int, sid: int, conn: sqlite3.Connection = Depends(get_conn),
               auth: Auth = Depends(require_auth)):
    suggest.accept(conn, auth.user, doc_id, sid)


@router.post("/documents/{doc_id}/suggestions/{sid}/reject", status_code=204)
def api_reject(doc_id: int, sid: int, conn: sqlite3.Connection = Depends(get_conn),
               auth: Auth = Depends(require_auth)):
    suggest.reject(conn, auth.user, doc_id, sid)


@router.get("/me/prefs")
def api_prefs(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    return suggest.get_prefs(conn, auth.user.id)


@router.patch("/me/prefs")
def api_prefs_patch(body: dict[str, bool], conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_auth)):
    return suggest.set_prefs(conn, auth.user.id, body)


@router.post("/admin/summaries/rebuild")
def api_rebuild_summaries(conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                          auth: Auth = Depends(require_admin)):
    return {"queued": llm.enqueue_rebuild(conn) if settings.llm_enabled else 0}
