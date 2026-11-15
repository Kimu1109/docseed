"""分割・再開できるアップロード（tus 1.0）の API。docs/design/api.md 3.3.2、処理は resumable.py。

    OPTIONS /api/v1/uploads          対応している版・拡張・上限
    POST    /api/v1/uploads          受け取りを始める（Upload-Length・Upload-Metadata。本文を付けてもよい）
    HEAD    /api/v1/uploads/{id}     受け取り済みの大きさ（Upload-Offset）
    PATCH   /api/v1/uploads/{id}     続きを送る（Content-Type: application/offset+octet-stream）
    GET     /api/v1/uploads/{id}     状態と、保存し終えていれば結果（JSON。docseed 独自）
    DELETE  /api/v1/uploads/{id}     取り消す

最後まで届いた PATCH（または POST）は、文書として保存してから 200 と JSON（GET と同じ形）を返す。
tus の決まりでは 204 だが、tus-js-client などは 2xx を成功として扱うので、そのまま使える。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .. import documents as dm
from .. import resumable as ru
from ..config import Settings
from ..deps import Auth, client_ip, get_conn, get_settings, get_store, require_auth
from ..errors import AppError, FileTooLarge, ValidationFailed
from ..storage import BlobStore
from .routes import doc_json

router = APIRouter(prefix="/api/v1/uploads")

OCTET = "application/offset+octet-stream"
WRITE_BUFFER = 1024 * 1024   # この大きさごとにまとめて（スレッドで）書く


def _tus_headers(**extra: str) -> dict[str, str]:
    return {"Tus-Resumable": ru.TUS_VERSION, "Cache-Control": "no-store", **extra}


def _check_version(request: Request) -> None:
    v = request.headers.get("tus-resumable")
    if v is not None and v != ru.TUS_VERSION:
        raise _Precondition()


class _Precondition(AppError):
    status, code, title = 412, "tus_version_unsupported", f"tus は {ru.TUS_VERSION} だけに対応しています"


def _int_header(request: Request, name: str, *, required: bool = True) -> int | None:
    raw = request.headers.get(name)
    if raw is None:
        if required:
            raise ValidationFailed(f"{name} ヘッダが必要です")
        return None
    if not raw.isdigit():
        raise ValidationFailed(f"{name} は 0 以上の整数にしてください")
    return int(raw)


def state_json(up: ru.Upload) -> dict[str, Any]:
    return {"id": up.id, "offset": up.offset, "length": up.length, "complete": up.result is not None,
            "expires_at": up.expires_at, "result": up.result}


def _finalize(conn: sqlite3.Connection, store: BlobStore, auth: Auth, up: ru.Upload, settings: Settings,
              ip: str | None) -> ru.Upload:
    """最後まで届いたものを文書にする（ファイルの読み直しがあるのでスレッドで呼ぶ）。失敗も結果として残す。"""
    path = ru.data_path(store, up.id)
    try:
        stored = store.adopt(path, up.filename)
        r = dm.upload(conn, store, auth.user, up.folder_id, up.filename, stored,
                      max_bytes=settings.max_upload_bytes, on_conflict=up.on_conflict,
                      relative_path=up.relative_path, ip=ip, keep_versions=settings.versions_keep)
        d = dm.get_document(conn, auth.user, r.document_id)
        result = {"filename": up.filename, "status": "replaced" if r.replaced else "created",
                  "deduplicated": r.deduplicated, "document": doc_json(conn, d)}
    except AppError as e:
        path.unlink(missing_ok=True)
        result = {"filename": up.filename, "status": "error", "error": {"code": e.code, "detail": e.detail}}
    ru.save_result(conn, up.id, result)
    up.result, up.offset = result, up.length
    return up


async def _receive(request: Request, store: BlobStore, up: ru.Upload) -> None:
    """本文を受け取り済みの続きに書き足す。接続が切れても、届いた分は残す（次は HEAD でそこから再開する）。"""
    remaining = up.length - up.offset
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > remaining:
        raise FileTooLarge("Upload-Length を超えて送られています")
    path = ru.data_path(store, up.id)
    buf = bytearray()
    written = 0
    f = await run_in_threadpool(path.open, "ab")
    try:
        async for chunk in request.stream():
            if written + len(buf) + len(chunk) > remaining:
                raise FileTooLarge("Upload-Length を超えて送られています")
            buf += chunk
            if len(buf) >= WRITE_BUFFER:
                data = bytes(buf)
                buf.clear()
                await run_in_threadpool(f.write, data)
                written += len(data)
    except ClientDisconnect:
        pass
    finally:
        if buf:
            await run_in_threadpool(f.write, bytes(buf))
        await run_in_threadpool(f.close)


async def _write_and_maybe_finish(request: Request, conn: sqlite3.Connection, store: BlobStore, auth: Auth,
                                  settings: Settings, up: ru.Upload, *, created: bool,
                                  expected: int | None = None) -> Response:
    with ru.lock(up.id):
        # lock を取ってから読み直す（直前まで別の接続が書いていたかもしれない）
        up = ru.get(conn, store, auth.user, up.id)
        if expected is not None and expected != up.offset:
            raise _mismatch(up.offset, expected)
        if up.result is None:
            if request.headers.get("content-type", "").split(";")[0].strip() == OCTET or not created:
                await _receive(request, store, up)
            up = ru.get(conn, store, auth.user, up.id)
            up.expires_at = ru.touch(conn, up.id)
            if up.offset == up.length:
                up = await run_in_threadpool(_finalize, conn, store, auth, up, settings, client_ip(request))
    headers = _tus_headers(**{"Upload-Offset": str(up.offset), "Upload-Expires": ru.http_date(up.expires_at)})
    if created:
        headers["Location"] = f"/api/v1/uploads/{up.id}"
    if up.result is not None:
        return JSONResponse(state_json(up), status_code=201 if created else 200, headers=headers)
    return Response(status_code=201 if created else 204, headers=headers)


@router.options("")
@router.options("/{upload_id}")
def tus_options(settings: Settings = Depends(get_settings)):
    return Response(status_code=204, headers=_tus_headers(**{
        "Tus-Version": ru.TUS_VERSION, "Tus-Extension": ru.TUS_EXTENSIONS,
        "Tus-Max-Size": str(settings.max_upload_bytes)}))


@router.post("")
async def tus_create(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                     auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                     settings: Settings = Depends(get_settings)):
    _check_version(request)
    if request.headers.get("upload-defer-length"):
        raise ValidationFailed("Upload-Defer-Length には対応していません。Upload-Length を付けてください")
    length = _int_header(request, "upload-length")
    meta = ru.parse_metadata(request.headers.get("upload-metadata"))
    up = await run_in_threadpool(ru.create, conn, store, auth.user, length=length, meta=meta,
                                 max_bytes=settings.max_upload_bytes)
    return await _write_and_maybe_finish(request, conn, store, auth, settings, up, created=True)


@router.head("/{upload_id}")
def tus_head(upload_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
             auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    _check_version(request)
    up = ru.get(conn, store, auth.user, upload_id)
    return Response(status_code=200, headers=_tus_headers(**{
        "Upload-Offset": str(up.offset), "Upload-Length": str(up.length),
        "Upload-Expires": ru.http_date(up.expires_at)}))


@router.get("/{upload_id}")
def upload_state(upload_id: str, conn: sqlite3.Connection = Depends(get_conn),
                 auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    return JSONResponse(state_json(ru.get(conn, store, auth.user, upload_id)), headers=_tus_headers())


@router.patch("/{upload_id}")
async def tus_patch(upload_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store),
                    settings: Settings = Depends(get_settings)):
    _check_version(request)
    if request.headers.get("content-type", "").split(";")[0].strip() != OCTET:
        raise _UnsupportedType()
    offset = _int_header(request, "upload-offset")
    up = ru.get(conn, store, auth.user, upload_id)
    if offset != up.offset:
        raise _mismatch(up.offset, offset)
    return await _write_and_maybe_finish(request, conn, store, auth, settings, up, created=False, expected=offset)


def _mismatch(actual: int, sent: int) -> ru.OffsetMismatch:
    return ru.OffsetMismatch(f"受け取り済みは {actual} バイトです（送られてきた位置は {sent}）。"
                             "HEAD で確かめてから続きを送ってください")


class _UnsupportedType(AppError):
    status, code, title = 415, "unsupported_media_type", f"Content-Type は {OCTET} にしてください"


@router.delete("/{upload_id}", status_code=204)
def tus_delete(upload_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn),
               auth: Auth = Depends(require_auth), store: BlobStore = Depends(get_store)):
    _check_version(request)
    ru.terminate(conn, store, auth.user, upload_id)
    return Response(status_code=204, headers=_tus_headers())
