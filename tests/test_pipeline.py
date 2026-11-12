import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image

from docseed.db import connect
from docseed.jobs.process import Context
from docseed.jobs.worker import Worker
from docseed.pipeline import extract
from docseed.pipeline.ocr import OcrLine, order_lines
from docseed.pipeline.text import chunk_pages, normalize

# ---------------------------------------------------------------------------
# 部品
# ---------------------------------------------------------------------------

def make_pdf(pages: list[str]) -> bytes:
    """テキスト層つきの最小限の PDF（Helvetica, ASCII）を作る。"""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + n * 2
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + i * 2} 0 R"
                    f" /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for k, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % k + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def png(size=(200, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


class FakeOcr:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        return [OcrLine("領収書の再発行には本人確認書類が必要です。", 0.98, [(10, 10), (190, 10), (190, 30), (10, 30)]),
                OcrLine("ノイズ", 0.2, [(10, 50), (60, 50), (60, 70), (10, 70)])]


class FakeEmbedder:
    model_name = "fake/embed"
    dim = 384

    def _vec(self, t):
        seed = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
        v = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed_documents(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


# ---------------------------------------------------------------------------
# テキスト
# ---------------------------------------------------------------------------

def test_normalize():
    assert normalize("ＡＢＣ１２３\r\n行末  \n\n\n\n次") == "ABC123\n行末\n\n次"
    assert normalize("途中で\n切れた。", join_broken_lines=True) == "途中で切れた。"
    assert normalize("終わり。\n次の文", join_broken_lines=True) == "終わり。\n次の文"


def test_chunk_offsets_and_coverage():
    page = "\n\n".join(f"第{i}条（目的）\n" + "本契約の有効期間は締結日から一年間とし、自動更新するものとする。" * 6
                       for i in range(1, 11))
    chunks = chunk_pages([(1, page)])
    assert len(chunks) >= 3
    covered = set()
    for c in chunks:
        assert len(c.text) <= 800 + 100
        assert page[c.char_start:c.char_end].replace("\n", "") == c.text.replace("\n", "")
        covered.update(range(c.char_start, c.char_end))
    assert all(i in covered for i, ch in enumerate(page) if not ch.isspace())


def test_chunk_short_pages_merge_and_no_page_crossing():
    chunks = chunk_pages([(1, "表紙"), (2, "本文です。" * 40), (3, "最後のページの本文です。" * 20)])
    assert chunks[0].page_no == 1 and chunks[0].text.startswith("表紙")
    assert {c.page_no for c in chunks} == {1, 3}


def test_reading_order_two_columns():
    def ln(t, x, y):
        return OcrLine(t, 0.99, [(x, y), (x + 200, y), (x + 200, y + 20), (x, y + 20)])
    # 左右 2 段（同じ高さに左右の行が並ぶ）
    lines = [ln("右1", 400, 0), ln("左1", 0, 0), ln("左2", 0, 30), ln("右2", 400, 30), ln("左3", 0, 60)]
    _, text, vertical = order_lines(lines, 0.5)
    assert not vertical
    assert text.split("\n\n") == ["左1\n左2\n左3", "右1\n右2"]


def test_reading_order_vertical():
    def col(t, x):
        return OcrLine(t, 0.99, [(x, 0), (x + 20, 0), (x + 20, 200), (x, 200)])
    _, text, vertical = order_lines([col("左の列", 0), col("右の列", 100)], 0.5)
    assert vertical and text.index("右の列") < text.index("左の列")


# ---------------------------------------------------------------------------
# 抽出
# ---------------------------------------------------------------------------

def test_extract_text_encodings(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("日本語のテキスト".encode("cp932"))
    pages = list(extract.iter_pages(p, "text/plain"))
    assert pages[0].text == "日本語のテキスト" and pages[0].source == "text"


def test_extract_html(tmp_path):
    p = tmp_path / "a.html"
    p.write_text("<html><head><title>x</title><script>var a=1</script></head><body><p>本文&amp;記号</p></body></html>")
    assert list(extract.iter_pages(p, "text/html"))[0].text == "本文&記号"


def test_extract_pdf_text_layer_and_image(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["Hello docseed, this page has a text layer.", "x"]))
    pages = list(extract.iter_pages(p, "application/pdf", dpi=50))
    assert pages[0].source == "text_layer" and "text layer" in pages[0].text
    assert pages[1].source == "ocr" and pages[1].image is not None  # 文字が少ないページは OCR
    assert [x.page_no for x in extract.iter_pages(p, "application/pdf", dpi=50, skip={1})] == [2]


def test_extract_unsupported(tmp_path):
    p = tmp_path / "a.zip"
    p.write_bytes(b"PK\x03\x04")
    with pytest.raises(extract.Unsupported):
        extract.page_count(p, "application/zip")


# ---------------------------------------------------------------------------
# worker（偽の OCR・埋め込みで端から端まで）
# ---------------------------------------------------------------------------

@pytest.fixture()
def worker(settings, app):
    ocr = FakeOcr()
    ctx = Context(settings.files_dir, ocr_factory=lambda: ocr, embedder_factory=FakeEmbedder, dpi=50)
    w = Worker(settings, ctx)
    w.ocr = ocr
    return w


def run_all(w):
    while w.run_once():
        pass


def test_worker_processes_text_pdf_and_image(login, worker, settings):
    alice = login("alice")
    pdf = alice.upload(alice.root, {"契約.pdf": make_pdf(["This contract is valid for one year from signing."])})
    img = alice.upload(alice.root, {"領収書.png": png()})
    run_all(worker)
    conn = connect(settings.db_path)
    for r in (pdf, img):
        d = r.json()["results"][0]["document"]
        got = alice.get(f"/api/v1/documents/{d['id']}").json()
        assert got["processing"]["status"] == "ready", got
    # 全文索引とベクトルができている
    hit = conn.execute("SELECT c.blob_id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
                       " WHERE chunks_fts MATCH '\"本人確認書類\"'").fetchall()
    assert len(hit) == 1
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    n_vec = conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0]
    assert n_chunks == n_vec == 2
    # 低信頼度の行は本文に入らないが ocr_lines には残る
    page = conn.execute("SELECT * FROM pages WHERE source = 'ocr'").fetchone()
    assert "ノイズ" not in page["text"]
    assert conn.execute("SELECT count(*) FROM ocr_lines WHERE page_id = ?", (page["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT embedding_model FROM blobs WHERE status = 'ready'").fetchone()[0] == "fake/embed"


def test_text_endpoint(login, worker):
    alice = login("alice")
    d = alice.upload(alice.root, {"r.png": png()}).json()["results"][0]["document"]
    run_all(worker)
    t = alice.get(f"/api/v1/documents/{d['id']}/text").json()
    assert t["pages"][0]["source"] == "ocr" and "本人確認書類" in t["pages"][0]["text"]
    o = alice.get(f"/api/v1/documents/{d['id']}/ocr?page=1").json()
    assert len(o["lines"]) == 2 and o["width"] == 200


def test_worker_unsupported_and_corrupt(login, worker, settings):
    alice = login("alice")
    z = alice.upload(alice.root, {"a.zip": b"PK\x03\x04" + b"x" * 100}).json()["results"][0]["document"]
    bad = alice.upload(alice.root, {"b.pdf": b"%PDF-1.4 broken"}).json()["results"][0]["document"]
    run_all(worker)
    assert alice.get(f"/api/v1/documents/{z['id']}").json()["processing"]["status"] == "unsupported"
    b = alice.get(f"/api/v1/documents/{bad['id']}").json()["processing"]
    assert b["status"] == "failed" and json.loads(b["error"])["code"] == "corrupt_file"


def test_worker_retries_then_fails(login, worker, settings):
    alice = login("alice")
    d = alice.upload(alice.root, {"r.png": png()}).json()["results"][0]["document"]

    def boom(image):
        raise RuntimeError("OCR が落ちた")
    worker.ocr.recognize = boom
    conn = connect(settings.db_path)
    for _ in range(3):
        conn.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00Z' WHERE status = 'pending'")
        assert worker.run_once()
    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "failed" and job["attempts"] == 3 and "OCR が落ちた" in job["error"]
    assert alice.get(f"/api/v1/documents/{d['id']}").json()["processing"]["status"] == "failed"


def test_resume_skips_done_pages(login, worker, settings):
    alice = login("alice")
    buf = io.BytesIO()
    frames = [Image.new("RGB", (100, 50), "white") for _ in range(3)]
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:])
    alice.upload(alice.root, {"scan.tiff": buf.getvalue()})
    calls = {"n": 0}
    orig = worker.ocr.recognize

    def flaky(image):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("途中で落ちた")
        return orig(image)
    worker.ocr.recognize = flaky
    conn = connect(settings.db_path)
    worker.run_once()  # 1 ページ目は保存、2 ページ目で失敗
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 1
    conn.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00Z'")
    worker.run_once()
    assert calls["n"] == 4  # 1 ページ目はやり直さない（1 + 失敗 1 + 残り 2）
    assert conn.execute("SELECT status, page_count FROM blobs").fetchone()[:] == ("ready", 3)


def test_reprocess_force_ocr(login, worker, settings):
    alice = login("alice")
    d = alice.upload(alice.root, {"t.pdf": make_pdf(["This page has a text layer that is long enough."])})
    doc = d.json()["results"][0]["document"]
    run_all(worker)
    assert worker.ocr.calls == 0
    r = alice.post(f"/api/v1/documents/{doc['id']}/reprocess", json={"force_ocr": True})
    assert r.status_code == 202
    assert alice.get(f"/api/v1/documents/{doc['id']}").json()["processing"]["status"] == "pending"
    run_all(worker)
    assert worker.ocr.calls == 1
    t = alice.get(f"/api/v1/documents/{doc['id']}/text").json()
    assert t["pages"][0]["source"] == "ocr"


def test_purged_blob_job_is_harmless(login, worker, settings):
    alice = login("alice")
    d = alice.upload(alice.root, {"x.txt": "あいうえお".encode()}).json()["results"][0]["document"]
    alice.delete(f"/api/v1/documents/{d['id']}")
    alice.delete(f"/api/v1/trash/document/{d['id']}")
    run_all(worker)  # 実体が消えた後のジョブ（カスケードで消えている）でも落ちない


def test_heartbeat_and_daily(worker, settings):
    worker.heartbeat(force=True)
    worker.daily()
    conn = connect(settings.db_path)
    keys = {r[0] for r in conn.execute("SELECT key FROM settings")}
    assert {"worker.heartbeat", "worker.daily"} <= keys


# ---------------------------------------------------------------------------
# テキスト層のある PDF に貼られた画像の OCR
# ---------------------------------------------------------------------------

def make_pdf_with_images(text: str, images: list[tuple[tuple[int, int], tuple[float, float, float, float]]]) -> bytes:
    """テキスト層（1 行）と画像を持つ 1 ページの PDF。images は ((幅px, 高さpx), (x, y, 幅pt, 高さpt))。"""
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"]
    xobj = " ".join(f"/Im{k} {6 + k} 0 R" for k in range(len(images)))
    draw = "".join(f"q {w} 0 0 {h} {x} {y} cm /Im{k} Do Q " for k, (_px, (x, y, w, h)) in enumerate(images))
    content = f"BT /F1 12 Tf 72 760 Td ({text}) Tj ET {draw}".encode()
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
                b" /Resources << /Font << /F1 5 0 R >> /XObject << " + xobj.encode() + b" >> >> >>")
    objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for (pw, ph), _rect in images:
        raw = Image.new("RGB", (pw, ph), (200, 200, 200)).tobytes()
        objs.append(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB"
                    b" /BitsPerComponent 8 /Length %d >>\nstream\n" % (pw, ph, len(raw)) + raw + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for k, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % k + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


TEXT_LINE = "This page has a text layer and a figure below it."


def test_extract_embedded_images_filters_by_size(tmp_path):
    p = tmp_path / "fig.pdf"
    p.write_bytes(make_pdf_with_images(TEXT_LINE, [
        ((300, 150), (72, 300, 300, 150)),     # 図: 読む
        ((20, 20), (72, 100, 20, 20)),         # アイコン: 小さすぎる
        ((600, 800), (0, 0, 612, 792)),        # ページ全体（スキャン）: 読まない
    ]))
    pages = list(extract.iter_pages(p, "application/pdf", dpi=50, pdf_images=True))
    assert pages[0].source == "text_layer" and len(pages[0].images) == 1
    assert pages[0].images[0].size == (300, 150)
    # 既定（pdf_images=False）では画像を渡さない
    assert list(extract.iter_pages(p, "application/pdf", dpi=50))[0].images == []


def test_worker_reads_text_inside_pdf_images(login, worker, settings):
    alice = login("alice")
    d = alice.upload(alice.root, {"fig.pdf": make_pdf_with_images(TEXT_LINE, [((300, 150), (72, 300, 300, 150))])})
    run_all(worker)
    assert worker.ocr.calls == 1
    conn = connect(settings.db_path)
    page = conn.execute("SELECT * FROM pages WHERE source = 'text_layer'").fetchone()
    assert page["text"].startswith("This page has a text layer")
    assert "領収書の再発行には本人確認書類が必要です。" in page["text"] and "ノイズ" not in page["text"]
    assert page["ocr_confidence"] == pytest.approx(0.98)
    hit = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"本人確認書類\"'").fetchone()[0]
    assert hit == 1
    doc_id = d.json()["results"][0]["document"]["id"]
    assert "＋ページ内の画像から読み取り" in alice.get(f"/d/{doc_id}").text


def test_pdf_image_text_already_in_text_layer_is_not_doubled(login, settings, app):
    class EchoOcr(FakeOcr):
        def recognize(self, image):
            self.calls += 1
            return [OcrLine(TEXT_LINE, 0.99, [(10, 10), (190, 10), (190, 30), (10, 30)]),
                    OcrLine("図 1 売上の推移", 0.95, [(10, 40), (190, 40), (190, 60), (10, 60)])]

    ocr = EchoOcr()
    w = Worker(settings, Context(settings.files_dir, ocr_factory=lambda: ocr, embedder_factory=FakeEmbedder, dpi=50))
    alice = login("alice")
    alice.upload(alice.root, {"fig.pdf": make_pdf_with_images(TEXT_LINE, [((300, 150), (72, 300, 300, 150))])})
    run_all(w)
    text = connect(settings.db_path).execute("SELECT text FROM pages").fetchone()[0]
    assert text.count("text layer") == 1 and "図 1 売上の推移" in text


def test_pdf_images_can_be_turned_off(login, settings, app):
    ocr = FakeOcr()
    w = Worker(settings, Context(settings.files_dir, ocr_factory=lambda: ocr, embedder_factory=FakeEmbedder, dpi=50,
                                 pdf_images=False))
    alice = login("alice")
    alice.upload(alice.root, {"fig.pdf": make_pdf_with_images(TEXT_LINE, [((300, 150), (72, 300, 300, 150))])})
    run_all(w)
    assert ocr.calls == 0
    page = connect(settings.db_path).execute("SELECT text, ocr_confidence FROM pages").fetchone()
    assert "本人確認書類" not in page["text"] and page["ocr_confidence"] is None


def test_reprocess_outdated_only_redoes_changed_kinds(login, worker, settings):
    from docseed.jobs.process import PIPELINE_VERSION
    from docseed.reprocess import reprocess
    alice = login("alice")
    alice.upload(alice.root, {"a.pdf": make_pdf(["This page has a text layer that is long enough."]),
                              "b.txt": b"plain text file", "c.png": png()})
    run_all(worker)
    conn = connect(settings.db_path)
    conn.execute("UPDATE blobs SET pipeline_version = 1")
    assert reprocess(conn, "outdated") == 1          # PDF だけ（版 2 の変更は PDF だけに及ぶ）
    rows = dict(conn.execute("SELECT mime_type, pipeline_version FROM blobs").fetchall())
    assert rows["text/plain"] == rows["image/png"] == PIPELINE_VERSION
    run_all(worker)
    assert conn.execute("SELECT count(*) FROM blobs WHERE pipeline_version < ?", (PIPELINE_VERSION,)).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# OCR の行の位置を画像に重ねて表示（ページの画像）
# ---------------------------------------------------------------------------

def test_page_image_and_ocr_overlay(login, worker, settings):
    alice = login("alice")
    pdf = alice.upload(alice.root, {"p.pdf": make_pdf(["This page has a text layer that is long enough.", "x"])})
    img = alice.upload(alice.root, {"scan.png": png((800, 400))})
    txt = alice.upload(alice.root, {"t.txt": b"plain text"})
    run_all(worker)
    pdf_id, img_id, txt_id = (r.json()["results"][0]["document"]["id"] for r in (pdf, img, txt))
    for doc_id, page in ((pdf_id, 1), (pdf_id, 2), (img_id, 1)):
        r = alice.get(f"/api/v1/documents/{doc_id}/pages/{page}/image")
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
        assert r.headers["x-content-type-options"] == "nosniff"
        im = Image.open(io.BytesIO(r.content))
        assert max(im.size) == 1600 or doc_id == img_id      # PDF は長辺 1600px に描く
    assert Image.open(io.BytesIO(alice.get(f"/api/v1/documents/{img_id}/pages/1/image").content)).size == (800, 400)
    small = alice.get(f"/api/v1/documents/{pdf_id}/pages/1/image", params={"width": 400}).content
    assert max(Image.open(io.BytesIO(small)).size) == 400
    huge = alice.get(f"/api/v1/documents/{pdf_id}/pages/1/image", params={"width": 99999}).content
    assert max(Image.open(io.BytesIO(huge)).size) == 2000
    assert alice.get(f"/api/v1/documents/{pdf_id}/pages/3/image").status_code == 404
    assert alice.get(f"/api/v1/documents/{txt_id}/pages/1/image").status_code == 404
    assert login("bob").get(f"/api/v1/documents/{img_id}/pages/1/image").status_code == 404
    # OCR の行と、本文に使う信頼度の下限
    ocr = alice.get(f"/api/v1/documents/{img_id}/ocr", params={"page": 1}).json()
    assert ocr["min_score"] == 0.5 and ocr["width"] == 800 and len(ocr["lines"]) == 2
    # 画面: OCR したページにだけ「重ねて表示」がある（PDF の 2 ページ目は文字が少ないので OCR している）
    page = alice.get(f"/d/{img_id}").text
    assert f'data-ocr-toggle data-doc="{img_id}" data-page="1"' in page
    page = alice.get(f"/d/{pdf_id}").text
    assert f'data-doc="{pdf_id}" data-page="2"' in page and f'data-doc="{pdf_id}" data-page="1"' not in page


# ---------------------------------------------------------------------------
# ページ単位の読み直し
# ---------------------------------------------------------------------------

def _web(c, url, **form):
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.http.get("/settings").text).group(1)
    return c.http.post(url, data={"csrf_token": token, **form}, follow_redirects=False)


def test_reprocess_single_page(login, worker, settings):
    alice = login("alice")
    pdf = make_pdf(["Page one has a text layer that is long enough.", "Page two has a text layer that is long too.",
                    "Page three has a text layer, also long enough."])
    doc = alice.upload(alice.root, {"three.pdf": pdf}).json()["results"][0]["document"]["id"]
    run_all(worker)
    conn = connect(settings.db_path)
    blob = conn.execute("SELECT blob_id FROM documents WHERE id = ?", (doc,)).fetchone()[0]
    conn.execute("UPDATE blobs SET warnings = ? WHERE id = ?",
                 (json.dumps([{"code": "ocr_slow", "page": 1, "seconds": 200},
                              {"code": "ocr_slow", "page": 2, "seconds": 200}]), blob))
    conn.commit()
    before = {r[0]: r[1] for r in conn.execute("SELECT page_no, id FROM pages WHERE blob_id = ?", (blob,))}
    calls = worker.ocr.calls
    # 2 ページ目だけを、埋め込みテキストを使わず画像として読み直す
    page = alice.get(f"/d/{doc}").text
    assert f'action="/d/{doc}/pages/2/reprocess"' in page and "埋め込みテキストを使わず画像として読む" in page
    r = _web(alice, f"/d/{doc}/pages/2/reprocess", force_ocr="1")
    assert r.status_code == 303 and r.headers["location"].endswith("done=reprocess_page#p2")
    assert alice.get(f"/api/v1/documents/{doc}").json()["processing"]["status"] == "pending"
    # 処理待ちの間は、もう一度は頼めない
    assert alice.post(f"/api/v1/documents/{doc}/pages/1/reprocess", json={}).status_code == 422
    run_all(worker)
    assert worker.ocr.calls == calls + 1                         # OCR したのは 2 ページ目だけ
    rows = {r["page_no"]: r for r in conn.execute("SELECT * FROM pages WHERE blob_id = ?", (blob,))}
    assert rows[2]["source"] == "ocr" and "本人確認書類" in rows[2]["text"]
    assert rows[1]["id"] == before[1] and rows[3]["id"] == before[3]   # ほかのページはそのまま
    assert rows[1]["source"] == rows[3]["source"] == "text_layer"
    # 2 ページ目の文字で検索できる（チャンクを作り直した）。ほかのページの警告は残る
    hit = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"本人確認書類\"'").fetchone()[0]
    assert hit == 1
    warns = alice.get(f"/api/v1/documents/{doc}").json()["processing"]["warnings"]
    assert {"code": "ocr_slow", "page": 1, "seconds": 200} in warns and all(w.get("page") != 2 for w in warns)
    assert conn.execute("SELECT count(*) FROM chunks c LEFT JOIN chunk_vectors v ON v.rowid = c.id"
                        " WHERE c.blob_id = ? AND v.rowid IS NULL", (blob,)).fetchone()[0] == 0


def test_reprocess_single_page_validation(login, worker):
    alice = login("alice")
    pdf = alice.upload(alice.root, {"one.pdf": make_pdf(["Only page with a text layer that is long enough."])})
    txt = alice.upload(alice.root, {"t.txt": b"plain text"})
    run_all(worker)
    pdf_id, txt_id = (r.json()["results"][0]["document"]["id"] for r in (pdf, txt))
    assert alice.post(f"/api/v1/documents/{pdf_id}/pages/2/reprocess", json={}).status_code == 404
    assert alice.post(f"/api/v1/documents/{txt_id}/pages/1/reprocess", json={}).status_code == 422
    assert login("bob").post(f"/api/v1/documents/{pdf_id}/pages/1/reprocess", json={}).status_code == 404
    assert "このページを読み直す" not in alice.get(f"/d/{txt_id}").text
    assert alice.post(f"/api/v1/documents/{pdf_id}/pages/1/reprocess", json={"force_ocr": True}).status_code == 202
