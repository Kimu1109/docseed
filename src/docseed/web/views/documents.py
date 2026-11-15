"""文書の画面（プレビュー・過去の版・関連する文書・再処理・要約・整理の候補）。"""

from __future__ import annotations

import json
import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import documents as dm
from ... import folders as fo
from ... import users as um
from ...audit import audit
from ...config import Settings
from ...db import Tx
from ...deps import (
    Auth,
    client_ip,
    get_conn,
    get_settings,
    get_store,
    require_auth,
)
from ...errors import AppError, NotFound, ValidationFailed
from ...files import can_preview
from ...storage import BlobStore
from ...versions import list_versions
from .. import helpers as h
from ..common import (
    base_context,
    display_location,
    router,
    templates,
)


@router.post("/d/{doc_id}/suggestions/{sid}/{action}")
def document_suggestion(doc_id: int, sid: int, action: str, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_auth)):
    from ...suggest import accept, reject
    if action == "accept":
        accept(conn, auth.user, doc_id, sid)
    elif action == "reject":
        reject(conn, auth.user, doc_id, sid)
    else:
        raise AppError("不明な操作です")
    return RedirectResponse(f"/d/{doc_id}?done={'suggest_' + action}", status_code=303)


TEXT_PREVIEW_BYTES = 200_000


PAGES_SHOWN = 30


def text_preview_of(store: BlobStore, d: sqlite3.Row) -> tuple[str | None, bool]:
    """テキストの文書なら先頭 TEXT_PREVIEW_BYTES を（本文, 切り詰めたか）で返す。"""
    if not (d["mime_type"].startswith("text/") and can_preview(d["mime_type"])):
        return None, False
    p = store.path(d["storage_path"])
    if not p.exists():
        return None, False
    with p.open("rb") as fh:
        raw = fh.read(TEXT_PREVIEW_BYTES + 1)
    return raw[:TEXT_PREVIEW_BYTES].decode("utf-8", "replace"), len(raw) > TEXT_PREVIEW_BYTES


def markdown_preview_of(d: sqlite3.Row, text: str | None):
    """Markdown の文書なら、プレビューに出す HTML（mdview.py）。ほかは None。"""
    from ...mdview import is_markdown, render
    if text is None or not is_markdown(d["mime_type"], d["filename"]):
        return None
    return render(text)


@router.get("/d/{doc_id}", response_class=HTMLResponse)
def document_page(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    d = dm.get_document(conn, auth.user, doc_id)
    role = fo.effective_role(conn, auth.user, d["folder_id"])
    text_preview, truncated = text_preview_of(store, d)
    targets = [(fid, label) for fid, label in fo.folders_of_owner(conn, d["owner_id"])
               if fo.effective_role(conn, auth.user, fid) in ("editor", "owner")]
    pages = conn.execute("SELECT page_no, source, text, ocr_confidence FROM pages WHERE blob_id = ?"
                         " ORDER BY page_no LIMIT ?", (d["blob_id"], PAGES_SHOWN)).fetchall()
    return templates.TemplateResponse(request, "document.html", base_context(
        request, conn, auth, doc=d, role=role, can_edit=role in ("editor", "owner"),
        crumbs=fo.path(conn, d["folder_id"]), tags=dm.document_tags(conn, doc_id), text_preview=text_preview,
        md_html=markdown_preview_of(d, text_preview), truncated=truncated, targets=targets,
        owner=um.get_user(conn, d["owner_id"]),
        pages=pages, pages_shown=PAGES_SHOWN, error=h.problem_message(d["error"]),
        warnings=h.warning_messages(d["warnings"]), image_meta=json.loads(d["image_meta"] or "null"),
        clip=clip_for(conn, d), webclip_enabled=request.app.state.settings.webclip_enabled,
        suggest_enabled=request.app.state.settings.suggest_enabled,
        live=_live_state(conn, request.app.state.settings, d),
        llm_enabled=request.app.state.settings.llm_enabled,
        sugg=_suggestions_for(request, conn, d) if role in ("editor", "owner") else None,
        versions=list_versions(conn, auth.user, doc_id), versions_keep=request.app.state.settings.versions_keep,
        fav=_opened(conn, auth, doc_id),
        nav="files" if d["owner_id"] == auth.user.id else "shared"))


def _opened(conn: sqlite3.Connection, auth: Auth, doc_id: int) -> bool:
    """文書の画面を開いた: 最近開いたファイルに記録し、お気に入りかを返す。"""
    from ... import personal as ps
    ps.record_open(conn, auth.user, doc_id)
    return ps.is_favorite(conn, auth.user, "document", doc_id)


@router.post("/d/{doc_id}/versions")
async def document_version_upload(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                                  auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                                  settings: Settings = Depends(get_settings)):
    from ...versions import upload_new_version
    form = await request.form()
    f = form.get("file")
    if f is None or isinstance(f, str) or not f.filename:
        raise ValidationFailed("ファイルを選んでください")
    changed = upload_new_version(conn, store, auth.user, doc_id, f.filename, f.file,
                                 max_bytes=settings.max_upload_bytes, keep_versions=settings.versions_keep,
                                 ip=client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done={'version_uploaded' if changed else 'version_same'}", status_code=303)


@router.post("/d/{doc_id}/versions/{version_id}/restore")
def document_version_restore(doc_id: int, version_id: int, request: Request,
                             conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                             store: BlobStore = Depends(get_store), settings: Settings = Depends(get_settings)):
    from ...versions import restore_version
    restore_version(conn, store, auth.user, doc_id, version_id, keep_versions=settings.versions_keep,
                    ip=client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done=version_restored", status_code=303)


@router.post("/d/{doc_id}/versions/{version_id}/delete")
def document_version_delete(doc_id: int, version_id: int, request: Request,
                            conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth),
                            store: BlobStore = Depends(get_store)):
    from ...versions import delete_version
    delete_version(conn, store, auth.user, doc_id, version_id, ip=client_ip(request))
    return RedirectResponse(f"/d/{doc_id}?done=version_deleted", status_code=303)


def _suggestions_for(request: Request, conn: sqlite3.Connection, d: sqlite3.Row) -> dict:
    from ...api.suggestions import embedder_of
    from ...suggest import for_document
    s = request.app.state.settings
    return for_document(conn, d, embedder=embedder_of(request), enabled=s.suggest_enabled, llm_enabled=s.llm_enabled,
                        llm_model=s.llm_model)


def clip_for(conn: sqlite3.Connection, d: sqlite3.Row) -> dict | None:
    from ...webclip.service import clip_for_document, error_message
    c = clip_for_document(conn, d["id"])
    return dict(c) | {"error_message": error_message(c)} if c else None


@router.post("/d/{doc_id}/reprocess")
def document_reprocess(doc_id: int, request: Request, force_ocr: str | None = Form(None),
                       conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    from ...reprocess import enqueue_reprocess
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    enqueue_reprocess(conn, [d["blob_id"]], force_ocr=bool(force_ocr))
    with Tx(conn):
        audit(conn, "reprocess", user_id=auth.user.id, target_type="document", target_id=doc_id,
              ip=client_ip(request), force_ocr=bool(force_ocr))
    return RedirectResponse(f"/d/{doc_id}?done=reprocess", status_code=303)


@router.post("/d/{doc_id}/redo")
def document_redo(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                  auth: Auth = Depends(require_auth)):
    """要約とタグ（と、フォルダの候補）をやり直す。docs/design/extensions.md 7.4。"""
    from ...api.suggestions import embedder_of
    from ...suggest import redo
    s = request.app.state.settings
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    r = redo(conn, auth.user, d, embedder=embedder_of(request), enabled=s.suggest_enabled, llm_enabled=s.llm_enabled)
    done = {"queued": "redo", "too_short": "redo_short"}.get(r["summary"], "redo_suggest")
    return RedirectResponse(f"/d/{doc_id}?done={done}", status_code=303)


@router.post("/d/{doc_id}/summary")
def document_summary_edit(doc_id: int, request: Request, action: str = Form(...), summary: str = Form(""),
                          conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    """要約を手で直す（save）・消す（hide）・自動の要約に戻す（restore）。この文書だけに効く。"""
    from ... import llm
    if not request.app.state.settings.llm_enabled:
        raise AppError("要約は有効になっていません")
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    if action == "save":
        if not summary.strip():
            raise AppError("要約を入力してください（消すときは「要約を消す」を押してください）")
        llm.set_manual_summary(conn, d, auth.user.id, summary)
        done = "summary_saved"
    elif action == "hide":
        llm.set_manual_summary(conn, d, auth.user.id, "")
        done = "summary_hidden"
    elif action == "restore":
        llm.clear_manual_summary(conn, d, auth.user.id)
        done = "summary_restored"
    else:
        raise AppError("不明な操作です")
    return RedirectResponse(f"/d/{doc_id}?done={done}", status_code=303)


def _live_state(conn: sqlite3.Connection, settings: Settings, d: sqlite3.Row) -> dict:
    """文書の画面の自動更新に使う状態。key（状態・要約の作り直し中か・要約の日時）が変われば画面を読み直す。"""
    from ...llm import summary_pending
    pending = settings.llm_enabled and summary_pending(conn, d["blob_id"])
    summ = conn.execute("SELECT created_at FROM blob_summaries WHERE blob_id = ?", (d["blob_id"],)).fetchone()
    return {"key": f"{d['status']}|{int(pending)}|{summ[0] if summ else ''}",
            "busy": d["status"] in ("pending", "processing") or pending,
            "status": d["status"], "done": d["progress_done"], "total": d["progress_total"]}


@router.get("/x/d/{doc_id}/related", response_class=HTMLResponse)
def document_related(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth)):
    """文書の画面の「関連する文書」（開いた後に読み込む。似ている文書が無ければ何も出さない）。"""
    from ...related import related_documents
    d = dm.get_document(conn, auth.user, doc_id)
    items = related_documents(conn, auth.user, d)
    labels: dict[int, str] = {}
    paths = {r["folder_id"]: display_location(conn, auth, r["folder_id"], labels) for r in items}
    return templates.TemplateResponse(request, "_related.html", {"request": request, "items": items,
                                                                 "paths": paths})


@router.get("/x/d/{doc_id}/state")
def document_state(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                   auth: Auth = Depends(require_auth)):
    """文書の画面の自動更新（app.js）。進み具合（done/total）は書き換えるだけ。"""
    return _live_state(conn, request.app.state.settings, dm.get_document(conn, auth.user, doc_id))


@router.post("/d/{doc_id}/pages/{page_no}/reprocess")
def document_page_reprocess(doc_id: int, page_no: int, request: Request, force_ocr: str | None = Form(None),
                            conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    """このページだけを読み直す（PDF・画像）。"""
    reprocess_one_page(request, conn, auth, doc_id, page_no, bool(force_ocr))
    return RedirectResponse(f"/d/{doc_id}?done=reprocess_page#p{page_no}", status_code=303)


def reprocess_one_page(request: Request, conn: sqlite3.Connection, auth: Auth, doc_id: int, page_no: int,
                       force_ocr: bool) -> None:
    from ...pipeline.extract import kind
    from ...reprocess import enqueue_pages
    d = dm.get_document(conn, auth.user, doc_id, "editor")
    if kind(d["mime_type"]) not in ("pdf", "image"):
        raise ValidationFailed("ページごとの読み直しは PDF と画像だけです")
    if d["status"] in ("pending", "processing"):
        raise ValidationFailed("処理が終わってから読み直してください")
    if not 1 <= page_no <= (d["page_count"] or 0):
        raise NotFound("そのページはありません")
    if not enqueue_pages(conn, d["blob_id"], [page_no], force_ocr=force_ocr):
        raise ValidationFailed("処理が終わってから読み直してください")
    with Tx(conn):
        audit(conn, "reprocess", user_id=auth.user.id, target_type="document", target_id=doc_id,
              ip=client_ip(request), page=page_no, force_ocr=force_ocr)


@router.post("/d/{doc_id}")
def document_save(doc_id: int, filename: str = Form(...), folder_id: int = Form(...), tags: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_auth)):
    dm.update_document(conn, auth.user, doc_id, filename=filename, folder_id=folder_id,
                       tags=[t for t in tags.replace("、", ",").split(",")])
    return RedirectResponse(f"/d/{doc_id}?done=saved", status_code=303)


@router.post("/d/{doc_id}/delete")
def document_delete(doc_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_auth)):
    folder_id = dm.get_document(conn, auth.user, doc_id)["folder_id"]
    dm.delete_document(conn, auth.user, doc_id, ip=client_ip(request))
    return RedirectResponse(f"/f/{folder_id}?done=deleted", status_code=303)
