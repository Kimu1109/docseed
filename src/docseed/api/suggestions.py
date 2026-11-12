"""整理の補助の API（/api/v1）。docs/design/extensions.md 8.5。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .. import documents as dm
from .. import llm, suggest
from ..config import Settings
from ..deps import Auth, get_conn, get_settings, require_admin, require_auth
from ..errors import ValidationFailed

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
                               llm_enabled=settings.llm_enabled, llm_model=settings.llm_model)
    out.pop("prefs")
    return out


@router.post("/documents/{doc_id}/suggestions/redo", status_code=202)
def api_redo(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
             settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    """要約とタグ（フォルダの候補も）をやり直す。要約は既にあっても上書きする。"""
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    return suggest.redo(conn, auth.user, d, embedder=embedder_of(request), enabled=settings.suggest_enabled,
                        llm_enabled=settings.llm_enabled)


class SummaryIn(BaseModel):
    summary: str


def _llm_on(settings: Settings) -> None:
    if not settings.llm_enabled:
        raise ValidationFailed("要約は有効になっていません")


@router.put("/documents/{doc_id}/summary")
def api_summary_put(doc_id: int, body: SummaryIn, conn: sqlite3.Connection = Depends(get_conn),
                    settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    """この文書の要約を手で直す（ほかの文書の要約は変えない）。"""
    _llm_on(settings)
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    if not body.summary.strip():
        raise ValidationFailed("要約を入力してください（消すときは DELETE を使ってください）")
    llm.set_manual_summary(conn, d, auth.user.id, body.summary)
    return {"summary": llm.document_summary(conn, d, settings.llm_model)}


@router.delete("/documents/{doc_id}/summary", status_code=204)
def api_summary_delete(doc_id: int, conn: sqlite3.Connection = Depends(get_conn),
                       settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    """この文書では要約を出さない（自動の要約は残し、restore で戻せる）。"""
    _llm_on(settings)
    llm.set_manual_summary(conn, dm.get_document(conn, auth.user, doc_id, "editor"), auth.user.id, "")


@router.post("/documents/{doc_id}/summary/restore")
def api_summary_restore(doc_id: int, conn: sqlite3.Connection = Depends(get_conn),
                        settings: Settings = Depends(get_settings), auth: Auth = Depends(require_auth)):
    """手で直した（消した）要約をやめ、自動で作った要約に戻す。"""
    _llm_on(settings)
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    llm.clear_manual_summary(conn, d, auth.user.id)
    return {"summary": llm.document_summary(conn, d, settings.llm_model)}


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
def api_rebuild_summaries(scope: str = Query("outdated", pattern="^(outdated|version|model)$"),
                          conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                          auth: Auth = Depends(require_admin)):
    """古い要約を作り直す（上書きする）。scope: outdated（両方）/ version（古い方式）/ model（別のモデル）。"""
    return {"queued": llm.enqueue_rebuild(conn, settings.llm_model, scope) if settings.llm_enabled else 0}
