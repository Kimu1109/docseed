"""映像の保存時に作る、軽いプレビュー用の動画（H.264・MP4）。実際の ffmpeg で確かめる。"""

import json
import shutil
import subprocess

import pytest

from docseed.pipeline import derive

requires_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                     reason="ffmpeg / ffprobe が無い")


def make_video(path, *, size="1280x720", codec="libx264", seconds=2, audio=True, vf=None):
    source = f"testsrc=size={size}:rate=25:duration={seconds}"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if vf:
        cmd += ["-vf", vf]
    if audio:
        cmd += ["-c:a", "aac"]
    else:
        cmd += ["-an"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height:format=duration", "-of", "json", str(path)],
                       capture_output=True, check=True)
    info = json.loads(r.stdout)
    return info["streams"][0], float(info["format"]["duration"])


@requires_ffmpeg
def test_landscape_video_gets_480p_h264_proxy(tmp_path):
    src = make_video(tmp_path / "src.mp4", size="1280x720")
    out = derive.derive(tmp_path / "data", src, "video/mp4", "a" * 64)
    assert out.view_path == f"derived/view/{'a' * 64}.mp4" and not out.warnings
    stream, duration = probe(tmp_path / "data" / out.view_path)
    assert stream["codec_name"] == "h264" and (stream["width"], stream["height"]) == (854, 480)
    assert abs(duration - 2) < 0.5


@requires_ffmpeg
def test_portrait_video_keeps_orientation_and_limits_long_side(tmp_path):
    src = make_video(tmp_path / "src.mp4", size="720x1280")
    out = derive.derive(tmp_path / "data", src, "video/mp4", "b" * 64)
    stream, _ = probe(tmp_path / "data" / out.view_path)
    assert (stream["width"], stream["height"]) == (480, 854)


@requires_ffmpeg
def test_odd_size_source_gets_even_dimensions(tmp_path):
    # mpeg4 は H.264 ではないので、小さくても作る。奇数の大きさから偶数の大きさにする
    src = make_video(tmp_path / "src.avi", size="641x361", codec="mpeg4", audio=False)
    out = derive.derive(tmp_path / "data", src, "video/x-msvideo", "c" * 64)
    stream, _ = probe(tmp_path / "data" / out.view_path)
    assert stream["codec_name"] == "h264" and stream["width"] % 2 == 0 and stream["height"] % 2 == 0


@requires_ffmpeg
def test_small_h264_video_is_not_transcoded(tmp_path):
    src = make_video(tmp_path / "small.mp4", size="320x240")
    out = derive.derive(tmp_path / "data", src, "video/mp4", "d" * 64)
    assert out.view_path is None and not out.warnings


@requires_ffmpeg
def test_proxy_can_be_turned_off(tmp_path):
    src = make_video(tmp_path / "src.mp4", size="1280x720")
    out = derive.derive(tmp_path / "data", src, "video/mp4", "e" * 64, video_proxy=False)
    assert out.view_path is None


@requires_ffmpeg
def test_audio_only_video_has_no_proxy(tmp_path):
    src = make_video(tmp_path / "audio.mp4", size="1280x720", audio=True, vf=None)
    # 映像のトラックを外した動画（音声だけ）
    only_audio = tmp_path / "only_audio.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vn", "-c:a", "aac",
                    str(only_audio)], check=True)
    out = derive.derive(tmp_path / "data", only_audio, "video/mp4", "f" * 64)
    assert out.view_path is None and not out.warnings


@requires_ffmpeg
def test_stop_request_passes_through_and_leaves_no_partial_file(tmp_path, monkeypatch):
    class Stop(Exception):
        pass

    monkeypatch.setattr(derive, "VIDEO_POLL_S", 0.2)
    src = make_video(tmp_path / "long.mp4", size="1920x1080", seconds=20)
    calls = []

    def tick():
        calls.append(1)
        raise Stop("停止")

    with pytest.raises(Stop):
        derive.derive(tmp_path / "data", src, "video/mp4", "9" * 64, tick=tick)
    assert calls
    view_dir = tmp_path / "data" / "derived" / "view"
    assert not view_dir.exists() or list(view_dir.glob("*")) == []


def test_view_route_serves_proxy_and_page_plays_it(login, conn, settings):
    from docseed.db import Tx
    from tests.test_media_preview import MP4
    alice = login("alice")
    doc = alice.upload(alice.root, {"clip.mp4": MP4}).json()["results"][0]["document"]
    rel = f"derived/view/{doc['sha256']}.mp4"
    (settings.data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / rel).write_bytes(b"proxy-bytes" * 10)
    with Tx(conn):
        conn.execute("UPDATE blobs SET view_path = ? WHERE sha256 = ?", (rel, doc["sha256"]))
    r = alice.get(f"/api/v1/documents/{doc['id']}/view")
    assert r.content == b"proxy-bytes" * 10 and r.headers["content-type"] == "video/mp4"
    page = alice.get(f"/d/{doc['id']}").text
    # 映像は画像として出さず、プレビュー用の動画を再生する
    assert "<img" not in page and f"/api/v1/documents/{doc['id']}/view" in page
