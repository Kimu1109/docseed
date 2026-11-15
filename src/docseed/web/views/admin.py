"""管理（ユーザー・容量の上限・ジョブ・通知・要約）。"""

from __future__ import annotations

import datetime as dt
import sqlite3

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import security
from ... import users as um
from ...audit import audit
from ...config import Settings
from ...db import Tx
from ...deps import (
    Auth,
    client_ip,
    get_conn,
    get_settings,
    require_admin,
)
from ...errors import AppError
from ..common import (
    base_context,
    router,
    templates,
)


@router.post("/admin/summaries/rebuild")
def admin_summaries_rebuild(scope: str = Form("outdated"), conn: sqlite3.Connection = Depends(get_conn),
                            auth: Auth = Depends(require_admin), settings: Settings = Depends(get_settings)):
    from ...llm import REBUILD_SCOPES, enqueue_rebuild
    if scope not in REBUILD_SCOPES:
        raise AppError("不明な範囲です")
    n = enqueue_rebuild(conn, settings.llm_model, scope) if settings.llm_enabled else 0
    return RedirectResponse(f"/admin?done={'summaries' if n else 'summaries_none'}", status_code=303)


def _llm_status(conn: sqlite3.Connection, settings: Settings) -> dict | None:
    if not settings.llm_enabled:
        return None
    from ...llm import llm_stats
    return llm_stats(conn, settings.llm_model) | {"model": settings.llm_model}


def jobs_panel(conn: sqlite3.Connection) -> dict:
    """ジョブの状況（件数・処理中・待ち・失敗）。管理画面と、5 秒ごとの再読み込みで同じものを使う。"""
    from ...api.routes import failed_jobs, job_counts, job_rows
    from ...monitor import WORKER_STALE_SECS, _age, _parse
    hb = conn.execute("SELECT value FROM settings WHERE key = 'worker.heartbeat'").fetchone()
    seen = _parse(hb[0]) if hb else None
    age = _age(seen, dt.datetime.now(dt.UTC))
    return {
        "job_counts": job_counts(conn),
        "running": job_rows(conn, "running", 30),
        "waiting": job_rows(conn, "pending", 50),
        "failed": failed_jobs(conn, 20),
        "worker_alive": age is not None and age < WORKER_STALE_SECS,
        "worker_seen": hb[0] if hb else None,
    }


def admin_context(request: Request, conn: sqlite3.Connection, auth: Auth, settings: Settings, **extra) -> dict:
    import shutil
    disk = shutil.disk_usage(settings.data_dir)
    stats = conn.execute(
        "SELECT (SELECT count(*) FROM documents WHERE deleted_at IS NULL) AS docs,"
        " (SELECT count(*) FROM blobs) AS blobs,"
        " (SELECT coalesce(sum(size_bytes), 0) FROM blobs) AS blob_bytes,"
        " (SELECT count(*) FROM jobs WHERE status IN ('pending', 'running')) AS jobs_waiting,"
        " (SELECT count(*) FROM jobs WHERE status = 'failed') AS jobs_failed").fetchone()
    db_bytes = sum(p.stat().st_size for p in settings.db_path.parent.glob(settings.db_path.name + "*"))
    from ...authn import get_auth_settings
    from ...monitor import status, system_info
    from ...notify import EVENT_LABEL, describe_destination, recent
    st = status(conn, settings)
    dests = [describe_destination(u) for u in settings.notify_urls]
    if settings.smtp_host and settings.notify_email:
        dests.append(f"メール（{', '.join(settings.notify_email)}）")
    notify = {"enabled": settings.notify_enabled, "destinations": dests, "recent": recent(conn, 10),
              "events": [EVENT_LABEL[e] for e in sorted(settings.notify_events) if e in EVENT_LABEL],
              "event_label": EVENT_LABEL}
    from ... import limits
    quotas = {u.id: limits.status(conn, u.id) for u in um.list_users(conn)}
    return base_context(request, conn, auth, nav="admin", users=um.list_users(conn), stats=stats,
                        quotas=quotas, default_quota=limits.default_quota(conn),
                        system=system_info(conn, settings), notify=notify,
                        disk=disk, db_bytes=db_bytes, sys=st, thermal_pause_c=settings.thermal_pause_c,
                        **jobs_panel(conn), error=None, form={},
                        llm=_llm_status(conn, settings),
                        **({"reset_done": None, "auth_settings": get_auth_settings(conn),
                            "oidc_enabled": bool(settings.oidc_providers)} | extra))


@router.post("/admin/quota")
def admin_quota_default(quota_gb: str = Form(""), conn: sqlite3.Connection = Depends(get_conn),
                        auth: Auth = Depends(require_admin)):
    from ...limits import parse_gb, set_default_quota
    set_default_quota(conn, parse_gb(quota_gb) or 0)
    return RedirectResponse("/admin?done=quota#quota", status_code=303)


@router.post("/admin/notify/test")
def admin_notify_test(conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
                      settings: Settings = Depends(get_settings)):
    from ...notify import send_test
    try:
        r = send_test(conn, settings)
    except ValueError as e:
        raise AppError(str(e)) from None
    return RedirectResponse(f"/admin?done={'notify_sent' if r['status'] == 'sent' else 'notify_failed'}#notify",
                            status_code=303)


@router.post("/admin/jobs/{job_id}/retry")
def admin_job_retry(job_id: int, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin)):
    from ...api.routes import retry_job
    retry_job(conn, job_id)
    return RedirectResponse("/admin?done=retried", status_code=303)


@router.post("/admin/reprocess")
def admin_reprocess(scope: str = Form(...), conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_admin)):
    from ...reprocess import reprocess
    if scope not in ("failed", "outdated", "unsupported"):
        raise AppError("不明な範囲です")
    reprocess(conn, scope)
    return RedirectResponse("/admin?done=reprocess", status_code=303)


@router.get("/admin/jobs/panel", response_class=HTMLResponse)
def admin_jobs_panel(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_admin), settings: Settings = Depends(get_settings)):
    """ジョブの状況の部分だけ（管理画面が 5 秒ごとに読み直す）。"""
    return templates.TemplateResponse(request, "_admin_jobs.html",
                                      base_context(request, conn, auth, **jobs_panel(conn)))


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
               settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(request, "admin.html", admin_context(request, conn, auth, settings))


@router.post("/admin/users", response_class=HTMLResponse)
def admin_user_create(request: Request, username: str = Form(""), display_name: str = Form(""),
                      password: str = Form(""), role: str = Form("user"),
                      conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
                      settings: Settings = Depends(get_settings)):
    try:
        u = um.create_user(conn, username, display_name, password, role)
        with Tx(conn):
            audit(conn, "user_create", user_id=auth.user.id, target_type="user", target_id=u.id,
                  ip=client_ip(request))
    except AppError as e:
        ctx = admin_context(request, conn, auth, settings) | {
            "error": e.detail, "form": {"username": username, "display_name": display_name, "role": role}}
        return templates.TemplateResponse(request, "admin.html", ctx, status_code=e.status)
    return RedirectResponse("/admin?done=user_created", status_code=303)


@router.post("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_update(user_id: int, request: Request, action: str = Form(...), password: str = Form(""),
                      quota_gb: str = Form(""),
                      conn: sqlite3.Connection = Depends(get_conn), auth: Auth = Depends(require_admin),
                      settings: Settings = Depends(get_settings)):
    try:
        if action == "disable":
            um.update_user(conn, user_id, is_active=False)
        elif action == "enable":
            um.update_user(conn, user_id, is_active=True)
        elif action == "make_admin":
            um.update_user(conn, user_id, role="admin")
        elif action == "make_user":
            um.update_user(conn, user_id, role="user")
        elif action == "quota":
            from ...limits import parse_gb, set_user_quota
            um.get_user(conn, user_id)
            set_user_quota(conn, user_id, parse_gb(quota_gb))
        elif action == "password":
            um.set_password(conn, user_id, password)
            security.delete_other_sessions(conn, user_id, None)
        else:
            raise AppError("不明な操作です")
        with Tx(conn):
            audit(conn, "user_update", user_id=auth.user.id, target_type="user", target_id=user_id,
                  ip=client_ip(request), op=action)
    except AppError as e:
        return templates.TemplateResponse(request, "admin.html",
                                          admin_context(request, conn, auth, settings) | {"error": e.detail},
                                          status_code=e.status)
    return RedirectResponse("/admin?done=" + ("quota#quota" if action == "quota" else "user_updated"),
                            status_code=303)
