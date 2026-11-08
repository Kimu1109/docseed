"""Phase 5: Office 文書の抽出、バックアップ・復元・原本の照合、監視。"""

import datetime as dt
import io
import json
import os
import shutil
import zipfile
from collections import namedtuple

import docx
import openpyxl
import pptx
import pytest
from pptx.util import Inches

from docseed import backup, monitor
from docseed.cli import main as cli_main
from docseed.db import connect
from docseed.pipeline import extract, office
from docseed.reprocess import reprocess
from docseed.storage import detect_mime

from .test_pipeline import FakeEmbedder, run_all, worker  # noqa: F401 - worker はフィクスチャ

# ---------------------------------------------------------------------------
# Office 文書を作る
# ---------------------------------------------------------------------------


def make_docx() -> bytes:
    d = docx.Document()
    d.add_paragraph("はじめに置いた前書きの文章です。")
    d.add_heading("第1章 経費の精算", level=1)
    d.add_paragraph("交通費は月末までに申請してください。")
    d.add_heading("1.1 対象", level=3)   # 3 階層目では区切らない
    d.add_paragraph("電車とバスが対象です。")
    t = d.add_table(rows=2, cols=3)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(0, 2).text = "区分", "上限", "備考"
    t.cell(1, 0).text, t.cell(1, 1).text = "電車", "5000円"
    d.add_heading("第2章 休暇", level=2)
    d.add_paragraph("有給休暇は前日までに申請します。")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "請求一覧"
    ws.append(["請求番号", "取引先", "金額", "日付", "済"])
    ws.append(["A-001", "株式会社サンプル", 120000.0, dt.datetime(2026, 4, 1), True])
    ws.append([None, None, None])
    ws.append(["A-002", "見本商事", 3.5, dt.datetime(2026, 4, 2, 13, 30), False])
    ws2 = wb.create_sheet("メモ")
    ws2["B2"] = "振込先は別紙を参照"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx() -> bytes:
    prs = pptx.Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "四半期の報告"
    s1.placeholders[1].text = "売上は前年より増えました"
    s1.notes_slide.notes_text_frame.text = "数字は速報値と伝える"
    s2 = prs.slides.add_slide(prs.slide_layouts[6])  # 白紙
    grp = s2.shapes.add_group_shape()
    tb = grp.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    tb.text_frame.text = "グループ内の文字"
    tbl = s2.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(4), Inches(1)).table
    tbl.cell(0, 0).text, tbl.cell(0, 1).text = "地域", "件数"
    tbl.cell(1, 0).text, tbl.cell(1, 1).text = "関東", "12"
    prs.slides.add_slide(prs.slide_layouts[6])  # 文字の無いスライド
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# Office 文書の抽出
# ---------------------------------------------------------------------------

def test_detect_mime_office():
    assert detect_mime(make_docx()[:8192], "a.docx") == office.DOCX
    assert detect_mime(make_xlsx()[:8192], "a.xlsx") == office.XLSX
    assert detect_mime(make_pptx()[:8192], "a.pptx") == office.PPTX
    # 中身の判定が ZIP になっても、拡張子が Office なら Office として扱う
    assert detect_mime(b"PK\x03\x04" + b"\0" * 100, "x.xlsm") == "application/vnd.ms-excel.sheet.macroenabled.12"
    assert detect_mime(b"PK\x03\x04" + b"\0" * 100, "x.zip") == "application/zip"
    assert extract.kind("application/vnd.ms-excel.sheet.macroEnabled.12") == "xlsx"


def test_docx_pages_split_by_heading(tmp_path):
    p = write(tmp_path, "a.docx", make_docx())
    assert extract.page_count(p, office.DOCX) == 3
    pages = list(extract.iter_pages(p, office.DOCX))
    assert [x.source for x in pages] == ["office"] * 3
    assert pages[0].text == "はじめに置いた前書きの文章です。"
    assert pages[1].text.startswith("第1章 経費の精算\n交通費は")
    assert "1.1 対象" in pages[1].text and "区分\t上限\t備考\n電車\t5000円" in pages[1].text
    assert pages[2].text.startswith("第2章 休暇")
    # 途中再開: 処理済みのページを飛ばす
    assert [x.page_no for x in extract.iter_pages(p, office.DOCX, skip={1, 2})] == [3]


def test_xlsx_pages_per_sheet(tmp_path):
    p = write(tmp_path, "a.xlsx", make_xlsx())
    pages = list(extract.iter_pages(p, office.XLSX))
    assert len(pages) == 2
    assert pages[0].text.split("\n") == [
        "シート: 請求一覧",
        "請求番号\t取引先\t金額\t日付\t済",
        "A-001\t株式会社サンプル\t120000\t2026-04-01\tTRUE",
        "A-002\t見本商事\t3.5\t2026-04-02 13:30\tFALSE",
    ]
    assert pages[1].text == "シート: メモ\n\t振込先は別紙を参照"
    assert office.table_header(pages[0].text) == "シート: 請求一覧\n請求番号\t取引先\t金額\t日付\t済"
    assert office.table_header("ふつうの文章") is None


def test_xlsx_truncates_huge_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MAX_SHEET_CELLS", 6)
    p = write(tmp_path, "a.xlsx", make_xlsx())
    pages = list(extract.iter_pages(p, office.XLSX))
    assert pages[0].warnings == [{"code": "sheet_truncated", "sheet": "請求一覧", "cells": 6}]
    assert pages[1].warnings == []
    assert "A-002" not in pages[0].text


def test_pptx_pages_per_slide(tmp_path):
    p = write(tmp_path, "a.pptx", make_pptx())
    pages = list(extract.iter_pages(p, office.PPTX))
    assert len(pages) == 3
    assert pages[0].text == "四半期の報告\n\n売上は前年より増えました\n\nノート:\n数字は速報値と伝える"
    assert "グループ内の文字" in pages[1].text and "地域\t件数\n関東\t12" in pages[1].text
    assert pages[1].text.index("グループ内の文字") < pages[1].text.index("地域")  # 上にある図形が先
    assert pages[2].text == ""


def test_office_errors(tmp_path):
    # パスワード付き（中身は OLE の複合ファイル）・古い形式
    p = write(tmp_path, "secret.docx", office.OLE_MAGIC + b"\0" * 1000)
    with pytest.raises(extract.Unsupported) as e:
        extract.page_count(p, office.DOCX)
    assert e.value.code == "encrypted_office"
    # ZIP ではない
    p = write(tmp_path, "broken.xlsx", b"PK\x03\x04 broken")
    with pytest.raises(extract.Unsupported) as e:
        extract.page_count(p, office.XLSX)
    assert e.value.code == "corrupt_file"
    # ZIP だが Office の部品が無い
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    p = write(tmp_path, "empty.pptx", buf.getvalue())
    with pytest.raises(extract.Unsupported) as e:
        extract.page_count(p, office.PPTX)
    assert e.value.code == "corrupt_file"
    # 展開すると極端に大きくなる（ZIP 爆弾）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (5 * 1024 * 1024))
    p = write(tmp_path, "bomb.docx", buf.getvalue())
    with pytest.raises(extract.Unsupported) as e:
        extract.page_count(p, office.DOCX)
    assert e.value.code == "office_too_large"


class RecordingEmbedder(FakeEmbedder):
    inputs: list[str] = []

    def embed_documents(self, texts):
        RecordingEmbedder.inputs.extend(texts)
        return super().embed_documents(texts)


def test_worker_processes_office(login, worker, settings, monkeypatch):  # noqa: F811
    RecordingEmbedder.inputs = []
    worker.ctx._embedder_factory = RecordingEmbedder
    # 見出し行の付与を確かめるため、シートを長くして途中から始まるチャンクを作る
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "台帳"
    ws.append(["品名", "数量", "保管場所"])
    for i in range(80):
        ws.append([f"部品{i:03d}", i, f"倉庫{i % 5}の棚{i % 7}"])
    buf = io.BytesIO()
    wb.save(buf)

    alice = login("alice")
    res = alice.upload(alice.root, {"規程.docx": make_docx(), "台帳.xlsx": buf.getvalue(),
                                    "報告.pptx": make_pptx()}).json()["results"]
    run_all(worker)
    conn = connect(settings.db_path)
    for r in res:
        got = alice.get(f"/api/v1/documents/{r['document']['id']}").json()
        assert got["processing"]["status"] == "ready", got
    assert conn.execute("SELECT count(*) FROM pages WHERE source = 'office'").fetchone()[0] == 3 + 1 + 3
    hit = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"有給休暇\"'").fetchone()[0]
    assert hit == 1
    # シートの途中から始まるチャンクにも、シート名と見出し行が文脈として付く
    later = [t for t in RecordingEmbedder.inputs if t.startswith("台帳.xlsx\n") and "部品000" not in t]
    assert later and all(t.startswith("台帳.xlsx\nシート: 台帳\n品名\t数量\t保管場所\n") for t in later)
    # 画面にも表示できる
    page = alice.get(f"/d/{res[2]['document']['id']}")
    assert page.status_code == 200 and "スライド 1" in page.text


def test_reprocess_unsupported(login, worker, settings):  # noqa: F811
    alice = login("alice")
    d = alice.upload(alice.root, {"a.bin": b"\x00\x01binary" * 10}).json()["results"][0]["document"]
    run_all(worker)
    conn = connect(settings.db_path)
    assert conn.execute("SELECT status FROM blobs").fetchone()[0] == "unsupported"
    assert reprocess(conn, "unsupported") == 1
    run_all(worker)
    assert alice.get(f"/api/v1/documents/{d['id']}").json()["processing"]["status"] == "unsupported"


# ---------------------------------------------------------------------------
# バックアップ・復元・原本の照合
# ---------------------------------------------------------------------------

def test_snapshot_restore_and_verify(login, settings, conn):
    alice = login("alice")
    alice.upload(alice.root, {"a.txt": "一つ目".encode(), "b.txt": "二つ目".encode()})
    out = settings.data_dir / "backup"
    for day in range(1, 10):
        snap = backup.snapshot_db(conn, out, keep=7, when=dt.datetime(2026, 11, day))
    assert snap.name == "docseed-20261109.sqlite"
    assert len(list(out.glob(backup.SNAPSHOT_GLOB))) == 7 and not list(out.glob(".*.tmp"))
    assert backup.latest_snapshot(out) == snap
    assert not (out / (snap.name + "-wal")).exists()

    rep = backup.verify_files(conn, settings.files_dir)
    assert rep.ok and rep.checked == 2 and rep.orphans == []

    # スナップショットの後に増えたファイルは、復元すると「DB に記録の無いファイル」になる
    alice.upload(alice.root, {"c.txt": "三つ目".encode()})
    conn.close()
    with pytest.raises(backup.BackupError):  # worker が動いた直後は止める
        _beat(settings.db_path)
        backup.restore_db(snap, settings.db_path)
    moved = backup.restore_db(snap, settings.db_path, force=True)
    assert moved is not None and moved.exists()
    c2 = connect(settings.db_path)
    assert c2.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
    rep = backup.verify_files(c2, settings.files_dir)
    assert rep.ok and len(rep.orphans) == 1

    # 壊れた・無くなった原本を見つける
    rows = c2.execute("SELECT storage_path, sha256 FROM blobs ORDER BY id").fetchall()
    (settings.files_dir / rows[0][0]).write_bytes("書き換え".encode())
    (settings.files_dir / rows[1][0]).unlink()
    rep = backup.verify_files(c2, settings.files_dir)
    assert not rep.ok and rep.mismatched == [rows[0][1]] and rep.missing == [rows[1][1]]
    # 大きさだけの確認では、同じ大きさの書き換えは見逃す（--quick の制約）
    c2.close()


def _beat(db_path):
    c = connect(db_path)
    c.execute("INSERT INTO settings(key, value) VALUES ('worker.heartbeat', ?)"
              " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    c.close()


def test_restore_rejects_non_database(tmp_path):
    bad = tmp_path / "x.sqlite"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(backup.BackupError):
        backup.restore_db(bad, tmp_path / "db" / "docseed.sqlite")
    assert not (tmp_path / "db" / "docseed.sqlite").exists()


def test_cli_backup_restore_verify(login, settings, monkeypatch, capsys):
    alice = login("alice")
    alice.upload(alice.root, {"a.txt": b"hello"})
    monkeypatch.setenv("DOCSEED_DATA_DIR", str(settings.data_dir))
    cli_main(["backup-db"])
    with pytest.raises(SystemExit) as e:
        cli_main(["verify-files", "--json"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["checked"] == 1
    cli_main(["restore-db", "--force"])
    assert "復元しました" in capsys.readouterr().out
    for p in settings.files_dir.rglob("*"):
        if p.is_file() and ".tmp" not in p.parts:
            p.unlink()
    with pytest.raises(SystemExit) as e:
        cli_main(["verify-files"])
    assert e.value.code == 1


# ---------------------------------------------------------------------------
# 監視
# ---------------------------------------------------------------------------

Usage = namedtuple("Usage", "total used free")


def codes(st):
    return {a["code"]: a["level"] for a in st["alerts"]}


def test_status_alerts(settings, conn, monkeypatch):
    st = monitor.status(conn, settings)
    c = codes(st)
    assert c["worker_never"] == "warning" and c["db_backup_none"] == "warning" and c["restic_none"] == "info"

    # worker が止まった・失敗したジョブ・空き容量
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO settings(key, value) VALUES ('worker.heartbeat', ?)", (old,))
    conn.execute("INSERT INTO jobs(type, status, run_after, created_at) VALUES ('purge_trash', 'failed', ?, ?)",
                 (old, old))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(1000, 920, 80))
    c = codes(monitor.status(conn, settings))
    assert c["worker_stale"] == "error" and c["jobs_failed"] == "warning" and c["disk_low"] == "warning"
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(1000, 960, 40))
    assert codes(monitor.status(conn, settings))["disk_full"] == "error"
    monkeypatch.undo()

    # バックアップ: 新しいスナップショットと restic の成功 → 警告なし。古い・失敗 → 警告
    out = settings.data_dir / "backup"
    snap = backup.snapshot_db(conn, out)
    now = dt.datetime.now(dt.UTC)
    (out / backup.RESTIC_STATUS).write_text(json.dumps(
        {"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": True, "snapshot": "abcd1234"}))
    st = monitor.status(conn, settings)
    assert not {"db_backup_none", "db_backup_stale", "restic_none", "restic_failed"} & codes(st).keys()
    assert st["backup"]["restic"]["snapshot"] == "abcd1234"
    old_t = (now - dt.timedelta(hours=40)).timestamp()
    os.utime(snap, (old_t, old_t))
    (out / backup.RESTIC_STATUS).write_text(json.dumps({"time": old, "ok": False, "message": "disk full"}))
    c = codes(monitor.status(conn, settings))
    assert c["db_backup_stale"] == "warning" and c["restic_failed"] == "error"
    (out / backup.RESTIC_STATUS).write_text(json.dumps(
        {"time": (now - dt.timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": True}))
    assert codes(monitor.status(conn, settings))["restic_stale"] == "warning"


def test_status_thermal(settings, conn, monkeypatch):
    monkeypatch.setattr(monitor, "cpu_temp", lambda: 82.5)
    st = monitor.status(conn, settings)
    assert st["cpu_temp_c"] == 82.5 and codes(st)["thermal"] == "warning"


def test_admin_status_api_and_badge(login, settings):
    admin = login("admin")
    st = admin.get("/api/v1/admin/status").json()
    assert {"alerts", "worker", "jobs", "disk", "backup"} <= st.keys()
    assert login("alice").get("/api/v1/admin/status").status_code == 403
    page = admin.get("/admin")
    assert page.status_code == 200 and "確認が必要な項目" in page.text and "バックアップ" in page.text
    assert "alert-badge" in admin.get(f"/f/{admin.root}").text
    assert "alert-badge" not in login("alice").get(f"/f/{login('alice').root}").text


def test_json_log_formatter():
    import logging

    from docseed.logs import JsonFormatter
    try:
        raise ValueError('壊れた "引用符"')
    except ValueError:
        import sys
        rec = logging.LogRecord("docseed.x", logging.ERROR, __file__, 1, 'msg "%s"\n2行目', ("値",), sys.exc_info())
    line = JsonFormatter().format(rec)
    assert "\n" not in line
    d = json.loads(line)
    assert d["msg"] == 'msg "値"\n2行目' and d["level"] == "ERROR" and "ValueError" in d["exc"]
