"""共有ダイアログ（フォルダのメンバー・共有リンク）。"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import documents as dm
from ... import folders as fo
from ... import users as um
from ...deps import (
    Auth,
    get_conn,
    require_session,
)
from ...errors import AppError
from ...i18n import N_, Labels
from ...urls import public_root
from ..common import (
    router,
    templates,
    toast,
)

ROLE_LABEL = Labels({"owner": N_("所有者"), "editor": N_("編集できる"), "viewer": N_("閲覧のみ")})


def gps_photo_count(conn: sqlite3.Connection, target_type: str, target_id: int) -> int:
    """共有の対象に含まれる、位置情報つきの写真の数（extensions.md 4.3）。"""
    gps = "json_extract(b.image_meta, '$.has_gps') = 1"
    if target_type == "document":
        return conn.execute(f"SELECT count(*) FROM documents d JOIN blobs b ON b.id = d.blob_id"
                            f" WHERE d.id = ? AND {gps}", (target_id,)).fetchone()[0]
    ids = fo.subtree_ids(conn, target_id)
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT count(*) FROM documents d JOIN blobs b ON b.id = d.blob_id"
                        f" WHERE d.folder_id IN ({marks}) AND d.deleted_at IS NULL AND {gps}", ids).fetchone()[0]


def render_share(request: Request, conn: sqlite3.Connection, auth: Auth, target_type: str, target_id: int,
                 message: str | None = None, new_id: int | None = None) -> HTMLResponse:
    from ...shares import list_shares
    if target_type == "folder":
        my_role = fo.require_folder_role(conn, auth.user, target_id, "viewer")
        members = fo.list_members(conn, auth.user, target_id)
        taken = {m["user_id"] for m in members}
        candidates = [dict(r) for r in conn.execute(
            "SELECT id, username, display_name FROM users WHERE is_active = 1 ORDER BY display_name")
            if r["id"] not in taken]
    elif target_type == "document":
        d = dm.get_document(conn, auth.user, target_id)
        my_role = fo.effective_role(conn, auth.user, d["folder_id"])
        members, candidates = [], []
    else:
        raise AppError("共有の対象が正しくありません")
    can_link = my_role in ("editor", "owner")
    links = list_shares(conn, auth.user, target_type=target_type, target_id=target_id) if can_link else []
    gps_count = gps_photo_count(conn, target_type, target_id) if can_link else 0
    html = templates.get_template("_share.html").render(
        request=request, target_type=target_type, target_id=target_id, members=members, candidates=candidates,
        my_role=my_role, links=links, can_link=can_link, role_label=ROLE_LABEL, new_id=new_id, gps_count=gps_count,
        base_url=public_root(request))
    if message:
        html += str(toast(message))
    return HTMLResponse(html)


@router.get("/x/share", response_class=HTMLResponse)
def share_dialog(request: Request, type: str, id: int, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_session)):
    return render_share(request, conn, auth, type, id)


@router.post("/x/folders/{folder_id}/members", response_class=HTMLResponse)
def share_member_set(folder_id: int, request: Request, user_id: int = Form(...), role: str = Form("viewer"),
                     conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    fo.set_member(conn, auth.user, folder_id, user_id, role)
    name = um.get_user(conn, user_id).display_name
    return render_share(request, conn, auth, "folder", folder_id, f"{name} さんを「{ROLE_LABEL[role]}」にしました")


@router.post("/x/folders/{folder_id}/members/{user_id}/remove", response_class=HTMLResponse)
def share_member_remove(folder_id: int, user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_session)):
    fo.remove_member(conn, auth.user, folder_id, user_id)
    return render_share(request, conn, auth, "folder", folder_id, "共有をやめました")


@router.post("/x/shares", response_class=HTMLResponse)
def share_link_create(request: Request, target_type: str = Form(...), target_id: int = Form(...),
                      permission: str = Form("read"), expires: str = Form("7d"), password: str = Form(""),
                      max_downloads: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                      auth: Auth = Depends(require_session)):
    from ...shares import create_share
    mx = int(max_downloads) if max_downloads.strip().isdigit() else None
    r = create_share(conn, auth.user, target_type, target_id, permission=permission, expires=expires,
                     password=password or None, max_downloads=mx)
    return render_share(request, conn, auth, target_type, target_id,
                        "リンクを作成しました。コピーして相手に送ってください", new_id=r["id"])


@router.post("/x/shares/{share_id}/revoke", response_class=HTMLResponse)
def share_link_revoke(share_id: int, request: Request, target_type: str = Form(...), target_id: int = Form(...),
                      conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_session)):
    from ...shares import revoke_share
    revoke_share(conn, auth.user, share_id)
    return render_share(request, conn, auth, target_type, target_id, "リンクを無効にしました")


@router.post("/settings/shares/{share_id}/revoke")
def settings_share_revoke(share_id: int, conn: sqlite3.Connection = Depends(get_conn),
                          auth: Auth = Depends(require_session)):
    from ...shares import revoke_share
    revoke_share(conn, auth.user, share_id)
    return RedirectResponse("/settings?done=share_revoked", status_code=303)
