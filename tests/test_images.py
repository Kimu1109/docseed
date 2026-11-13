"""Phase 6: 画像形式（HEIC / AVIF）とサムネイル・プレビュー・画像の情報。docs/design/extensions.md 4 章・10 章。"""

from __future__ import annotations

import io

import numpy as np
import pillow_heif
import pytest
from PIL import Image

from docseed.db import connect, migrate
from docseed.pipeline import derive, extract
from docseed.reprocess import enqueue_derive
from docseed.storage import detect_mime
from tests.test_pipeline import make_pdf, png, run_all, worker  # noqa: F401 - fixture

pillow_heif.register_heif_opener()


def heic(size=(64, 32), color=(200, 30, 30)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "HEIF", quality=80)
    return b.getvalue()


def heic_10bit() -> bytes:
    arr = np.full((32, 64, 3), 40000, dtype=np.uint16)
    h = pillow_heif.from_bytes(mode="RGB;16", size=(64, 32), data=arr.tobytes())
    b = io.BytesIO()
    h.save(b, quality=-1)
    return b.getvalue()


def avif(size=(64, 32)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(b, "AVIF", quality=80)
    return b.getvalue()


def jpeg_with_exif(size=(200, 100), *, orientation=1, gps=False) -> bytes:
    im = Image.new("RGB", size, "white")
    im.paste((0, 0, 0), (0, 0, 20, 20))                 # 左上に印を付けて向きを確かめる
    ex = Image.Exif()
    ex[0x0112] = orientation
    ex[0x010F] = "Apple"
    ex[0x0110] = "iPhone 16"
    ex.get_ifd(0x8769)[0x9003] = "2026:10:01 09:12:00"
    ex.get_ifd(0x8769)[0x9011] = "+09:00"
    if gps:
        g = ex.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = "N", (35.0, 39.0, 29.0), "E", (139.0, 44.0, 28.0)
    b = io.BytesIO()
    im.save(b, "JPEG", exif=ex)
    return b.getvalue()


def _webp(path):
    im = Image.open(path)
    assert im.format == "WEBP"
    assert not im.getexif() and "exif" not in im.info and "icc_profile" not in im.info
    return im


# ---------------------------------------------------------------------------
# 形式の判定と読み込み
# ---------------------------------------------------------------------------

def test_detect_heic_and_avif():
    assert detect_mime(heic()[:8192], "IMG_0001.HEIC") == "image/heic"
    assert detect_mime(avif()[:8192], "a.avif") == "image/avif"
    assert extract.kind("image/heic") == extract.kind("image/avif") == "image"


def test_extract_heic_avif_and_10bit(tmp_path):
    for name, data in (("a.heic", heic()), ("b.avif", avif()), ("c.heic", heic_10bit())):
        p = tmp_path / name
        p.write_bytes(data)
        mime = "image/avif" if name.endswith("avif") else "image/heic"
        assert extract.page_count(p, mime) == 1
        page = next(extract.iter_pages(p, mime))
        assert page.source == "ocr" and page.image.size == (64, 32)


def test_corrupt_image_is_failed(tmp_path):
    p = tmp_path / "x.heic"
    p.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 50)
    with pytest.raises(extract.Unsupported) as e:
        extract.page_count(p, "image/heic")
    assert e.value.code == "corrupt_file"


# ---------------------------------------------------------------------------
# 派生ファイル
# ---------------------------------------------------------------------------

def _derive(tmp_path, data: bytes, mime: str, sha="s" * 64):
    p = tmp_path / "orig"
    p.write_bytes(data)
    return derive.derive(tmp_path, p, mime, sha)


def test_heic_gets_thumb_and_view(tmp_path):
    d = _derive(tmp_path, heic(), "image/heic")
    assert d.thumb_path == "derived/thumbs/" + "s" * 64 + ".webp"
    assert d.view_path == "derived/view/" + "s" * 64 + ".webp"
    assert _webp(tmp_path / d.view_path).size == (64, 32)
    assert d.image_meta == {"width": 64, "height": 32, "has_gps": False}
    assert not d.warnings


def test_10bit_heic_becomes_8bit(tmp_path):
    d = _derive(tmp_path, heic_10bit(), "image/heic")
    im = _webp(tmp_path / d.view_path)
    assert im.mode in ("RGB", "RGBA")
    r, g, b = im.convert("RGB").getpixel((10, 10))
    assert 140 <= r <= 170 and abs(r - g) < 6          # 40000 / 65535 ≒ 0.61 → 156 前後


def test_avif_gets_view(tmp_path):
    d = _derive(tmp_path, avif(), "image/avif")
    assert d.thumb_path and d.view_path


def test_png_has_thumb_but_no_view(tmp_path):
    d = _derive(tmp_path, png((600, 300)), "image/png")
    assert _webp(tmp_path / d.thumb_path).size == (256, 128)
    assert d.view_path is None


def test_huge_image_gets_view(tmp_path):
    d = _derive(tmp_path, png((5000, 100)), "image/png")
    assert _webp(tmp_path / d.view_path).size == (2048, 41)


def test_rotated_jpeg(tmp_path):
    # orientation 6 = 時計回りに 90 度回して表示する
    d = _derive(tmp_path, jpeg_with_exif((200, 100), orientation=6), "image/jpeg")
    im = _webp(tmp_path / d.thumb_path).convert("RGB")
    assert im.size == (100, 200)
    assert d.image_meta["width"] == 100 and d.image_meta["height"] == 200
    # 左上の黒い印は、回すと右上に来る
    assert sum(im.getpixel((95, 3))) < 150 and sum(im.getpixel((3, 3))) > 600


def test_gps_jpeg_meta_and_no_exif_in_derived(tmp_path):
    d = _derive(tmp_path, jpeg_with_exif(gps=True), "image/jpeg")
    assert d.image_meta == {"width": 200, "height": 100, "taken_at": "2026-10-01T09:12:00+09:00",
                            "camera": "iPhone 16", "has_gps": True}
    _webp(tmp_path / d.thumb_path)                        # EXIF（位置情報）が入っていない


def test_icc_converted_to_srgb(tmp_path):
    from PIL import ImageCms
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    b = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 200, 10)).save(b, "PNG", icc_profile=icc)
    d = _derive(tmp_path, b.getvalue(), "image/png")      # 変換できないプロファイルでも失敗しない
    assert d.thumb_path and not d.warnings


def test_pdf_thumb(tmp_path):
    d = _derive(tmp_path, make_pdf(["Hello"]), "application/pdf")
    im = _webp(tmp_path / d.thumb_path)
    assert max(im.size) == 256 and d.view_path is None and d.image_meta is None


def test_failure_is_warning_not_error(tmp_path):
    d = _derive(tmp_path, b"not an image", "image/png")
    assert d.thumb_path is None and d.warnings[0]["code"] == "thumbnail_failed"
    assert _derive(tmp_path, b"text", "text/plain").warnings == []


# ---------------------------------------------------------------------------
# worker・配信・画面
# ---------------------------------------------------------------------------

def test_worker_heic_end_to_end(login, worker, settings):  # noqa: F811
    alice = login("alice")
    d = alice.upload(alice.root, {"IMG_0001.HEIC": heic()}).json()["results"][0]["document"]
    run_all(worker)
    got = alice.get(f"/api/v1/documents/{d['id']}").json()
    assert got["mime_type"] == "image/heic" and got["processing"]["status"] == "ready"
    assert worker.ocr.calls == 1
    r = alice.get(f"/api/v1/documents/{d['id']}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    r = alice.get(f"/api/v1/documents/{d['id']}/view")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    page = alice.get(f"/d/{d['id']}").text
    assert f'src="/api/v1/documents/{d["id"]}/view"' in page and "64 × 32 px" in page
    listing = alice.get(f"/f/{alice.root}").text
    assert f'src="/api/v1/documents/{d["id"]}/thumbnail"' in listing
    # 他人は取れない
    assert login("bob").get(f"/api/v1/documents/{d['id']}/thumbnail").status_code == 404


def test_view_falls_back_to_original(login, worker):  # noqa: F811
    alice = login("alice")
    d = alice.upload(alice.root, {"a.png": png()}).json()["results"][0]["document"]
    t = alice.upload(alice.root, {"a.txt": b"hello"}).json()["results"][0]["document"]
    run_all(worker)
    r = alice.get(f"/api/v1/documents/{d['id']}/view")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert alice.get(f"/api/v1/documents/{t['id']}/thumbnail").status_code == 404


def test_gps_warning_in_share_dialog_and_public_pages(login, worker):  # noqa: F811
    alice = login("alice")
    folder = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "旅行"}).json()
    d = alice.upload(folder["id"], {"photo.jpg": jpeg_with_exif(gps=True)}).json()["results"][0]["document"]
    run_all(worker)
    page = alice.get(f"/d/{d['id']}").text
    assert "2026/10/01 09:12" in page and "iPhone 16" in page and "撮影場所" in page
    html = alice.get("/x/share", params={"type": "folder", "id": alice.root}).text
    assert "位置情報（撮影場所）を含む写真が 1 件" in html
    html = alice.get("/x/share", params={"type": "document", "id": d["id"]}).text
    assert "位置情報（撮影場所）を含む写真が 1 件" in html
    # 共有リンクの公開ページでもサムネイル・プレビューを使う
    link = alice.post("/api/v1/shares", json={"target_type": "folder", "target_id": folder["id"]}).json()
    tok = link["url"].rsplit("/s/", 1)[1]
    from fastapi.testclient import TestClient
    anon = TestClient(alice.http.app)
    assert f"/s/{tok}/thumb/{d['id']}" in anon.get(f"/s/{tok}").text
    r = anon.get(f"/s/{tok}/thumb/{d['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert anon.get(f"/s/{tok}/view/{d['id']}").headers["content-type"] == "image/jpeg"


def test_derived_files_removed_with_blob(login, worker, settings):  # noqa: F811
    alice = login("alice")
    d = alice.upload(alice.root, {"p.heic": heic()}).json()["results"][0]["document"]
    run_all(worker)
    conn = connect(settings.db_path)
    row = conn.execute("SELECT thumb_path, view_path FROM blobs").fetchone()
    files = [settings.data_dir / row[0], settings.data_dir / row[1]]
    assert all(f.exists() for f in files)
    alice.delete(f"/api/v1/documents/{d['id']}")
    assert alice.delete(f"/api/v1/trash/document/{d['id']}").status_code == 204
    assert not any(f.exists() for f in files)


def test_derive_missing_job(login, worker, settings):  # noqa: F811
    alice = login("alice")
    alice.upload(alice.root, {"p.png": png(), "a.pdf": make_pdf(["x"]), "t.txt": b"t"})
    run_all(worker)
    conn = connect(settings.db_path)
    conn.execute("UPDATE blobs SET thumb_path = NULL, view_path = NULL, image_meta = NULL")
    assert enqueue_derive(conn, "missing") == 2               # テキストは対象外
    assert enqueue_derive(conn, "missing") == 0               # 重複して積まない
    calls = worker.ocr.calls
    run_all(worker)
    assert worker.ocr.calls == calls                          # OCR はやり直さない
    assert conn.execute("SELECT count(*) FROM blobs WHERE thumb_path IS NOT NULL").fetchone()[0] == 2


def test_migration_0002_after_0003(tmp_path, monkeypatch):
    """Phase 7（0003）を先に適用した DB にも 0002 を後から適用できる。"""
    from docseed import db
    allm = db._migrations()
    monkeypatch.setattr(db, "_migrations", lambda: [m for m in allm if m[0] in (1, 3)])
    conn = connect(tmp_path / "x.sqlite")
    assert migrate(conn) == ["0001_initial.sql", "0003_login_methods.sql"]
    conn.execute("INSERT INTO jobs(type, status, run_after, created_at) VALUES ('purge_trash', 'pending', 'x', 'x')")
    monkeypatch.setattr(db, "_migrations", lambda: allm)
    assert migrate(conn) == ["0002_derived_files.sql", "0004_web_clips.sql", "0005_suggestions.sql",
                             "0006_summary_search.sql", "0007_manual_summaries.sql", "0008_document_versions.sql"]
    assert conn.execute("SELECT type FROM jobs").fetchone()[0] == "purge_trash"
    conn.execute("INSERT INTO jobs(type, status, run_after, created_at) VALUES ('derive_blob', 'pending', 'x', 'x')")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(blobs)")}
    assert {"thumb_path", "view_path", "image_meta"} <= cols
