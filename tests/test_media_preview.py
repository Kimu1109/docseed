"""音声・動画のプレビュー（画面で再生する）。文書の画面と共有ページの両方。"""

from docseed.files import can_preview, media_kind

# 形式の判定（filetype）に必要な先頭バイトだけを持つ、中身の無い見本
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 256
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 256
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 256
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 256
AVI = b"RIFF\x24\x08\x00\x00AVI LIST" + b"\x00" * 256


def test_media_kind_only_for_playable_types():
    assert media_kind("video/mp4") == "video"
    assert media_kind("video/x-m4v") == "video"
    assert media_kind("audio/mpeg") == "audio"
    assert media_kind("audio/x-wav") == "audio"
    # ブラウザで安定して再生できない形式は、再生の対象にしない
    assert media_kind("video/x-msvideo") is None
    assert media_kind("video/x-matroska") is None
    assert media_kind(None) is None
    assert can_preview("audio/ogg") and can_preview("video/webm")
    assert not can_preview("video/x-msvideo")


def test_media_served_inline_with_playable_type(login):
    alice = login("alice")
    files = alice.upload(alice.root, {"clip.mp4": MP4, "song.mp3": MP3, "rec.wav": WAV}).json()["results"]
    mp4, mp3, wav = (r["document"] for r in files)
    r = alice.get(f"/api/v1/documents/{mp4['id']}/content?inline=1")
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert alice.get(f"/api/v1/documents/{mp3['id']}/content?inline=1").headers["content-type"] == "audio/mpeg"
    assert alice.get(f"/api/v1/documents/{wav['id']}/content?inline=1").headers["content-type"] == "audio/wav"
    # 再生位置の移動（シーク）に必要な範囲指定に応える
    r = alice.get(f"/api/v1/documents/{mp4['id']}/content?inline=1", headers={"Range": "bytes=0-7"})
    assert r.status_code == 206 and len(r.content) == 8
    assert r.headers["accept-ranges"] == "bytes"


def test_unplayable_media_is_still_attachment_only(login):
    alice = login("alice")
    avi = alice.upload(alice.root, {"old.avi": AVI}).json()["results"][0]["document"]
    r = alice.get(f"/api/v1/documents/{avi['id']}/content?inline=1")
    assert r.headers["content-disposition"].startswith("attachment")


def test_document_page_plays_audio_and_video(login):
    alice = login("alice")
    files = alice.upload(alice.root, {"clip.mov": MOV, "song.mp3": MP3, "old.avi": AVI}).json()["results"]
    mov, mp3, avi = (r["document"] for r in files)
    page = alice.get(f"/d/{mov['id']}").text
    assert "<video" in page and f"/api/v1/documents/{mov['id']}/view" in page
    page = alice.get(f"/d/{mp3['id']}").text
    assert "<audio" in page and f"/api/v1/documents/{mp3['id']}/view" in page
    page = alice.get(f"/d/{avi['id']}").text
    assert "<video" not in page and "<audio" not in page
    assert "ブラウザで表示できません" in page


def test_share_page_plays_media(app, login):
    from tests.test_sharing import public, token_of
    alice = login("alice")
    d = alice.upload(alice.root, {"clip.mp4": MP4}).json()["results"][0]["document"]
    link = alice.post("/api/v1/shares", json={"target_type": "document", "target_id": d["id"]}).json()
    anon = public(app)
    page = anon.get(f"/s/{token_of(link['url'])}").text
    assert "<video" in page and f"/s/{token_of(link['url'])}/view/{d['id']}" in page
    # 共有された先でも、再生用の配信は同じ条件で通る
    r = anon.get(f"/s/{token_of(link['url'])}/preview/{d['id']}")
    assert r.headers["content-type"] == "video/mp4" and r.headers["content-disposition"].startswith("inline")
