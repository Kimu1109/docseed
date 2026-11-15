import json
import shutil
import subprocess
from pathlib import Path

import pytest

from docseed.db import connect
from docseed.jobs.process import Context
from docseed.jobs.worker import Worker
from docseed.pipeline import extract, transcribe
from docseed.pipeline.transcribe import Segment

from .test_pipeline import FakeEmbedder, FakeOcr, run_all

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg / ffprobe が無い環境では実行しない")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, timeout=120)


def make_wav(path: Path, seconds: int = 25) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", str(path))
    return path


def make_silent_video(path: Path) -> Path:
    """音声のトラックが無い動画（映像だけ）。"""
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", "-c:v", "mpeg4", "-an", str(path))
    return path


class FakeAsr:
    """whisper-cli の代わり。区切りごとに固定の 1 文を返す。"""
    name = "fake-asr"

    def __init__(self, text: str = "テスト文字起こしの本文です"):
        self.text = text
        self.calls: list[str] = []

    def transcribe(self, wav: Path, work: Path):
        self.calls.append(wav.name)
        return [Segment(0, 1000, self.text)] if self.text else []


# ---------------------------------------------------------------------------
# 部品（外部のプログラムを使わない）
# ---------------------------------------------------------------------------

def test_format_time_and_segments_json():
    assert transcribe.format_time(0) == "00:00:00"
    assert transcribe.format_time(61_500) == "00:01:01"
    assert transcribe.format_time(3_723_000) == "01:02:03"
    data = {"transcription": [
        {"timestamps": {"from": "00:00:00,000", "to": "00:00:02,500"},
         "offsets": {"from": 0, "to": 2500}, "text": " こんにちは "},
        {"offsets": {"from": 2500, "to": 4000}, "text": "   "},   # 空の区切りは捨てる
        {"offsets": {"from": 4000, "to": 6000}, "text": "会議を始めます"},
    ]}
    segs = transcribe.parse_segments(data)
    assert [(s.start_ms, s.text) for s in segs] == [(0, "こんにちは"), (4000, "会議を始めます")]


def test_slice_text_adds_offsets_as_timestamps():
    segs = [Segment(1_000, 3_000, "最初の行"), Segment(5_000, 6_000, "次の行")]
    text = transcribe.slice_text(segs, offset_s=600)   # 2 つ目の 10 分の区切り
    assert text == "[00:10:01] 最初の行\n[00:10:05] 次の行"


def test_media_kind_and_disabled_by_default():
    assert extract.kind("audio/mpeg") == "media"
    assert extract.kind("video/mp4") == "media"
    assert extract.kind("application/zip") == "unsupported"


def test_whisper_cli_missing_model_is_a_clear_error(tmp_path):
    eng = transcribe.WhisperCli("whisper-cli", tmp_path / "none.bin")
    with pytest.raises(RuntimeError, match="fetch-models --asr"):
        eng.transcribe(tmp_path / "a.wav", tmp_path)


def test_whisper_cli_missing_binary_is_a_clear_error(tmp_path):
    model = tmp_path / "m.bin"
    model.write_bytes(b"x")
    eng = transcribe.WhisperCli(str(tmp_path / "no-such-binary"), model)
    with pytest.raises(RuntimeError, match="見つかりません"):
        eng.transcribe(tmp_path / "a.wav", tmp_path)


def test_asr_settings_from_env(tmp_path):
    from docseed.config import Settings
    s = Settings.from_env({"DOCSEED_DATA_DIR": str(tmp_path), "DOCSEED_ASR_ENABLED": "true",
                           "DOCSEED_ASR_MODEL": "ggml-base.bin"})
    assert s.asr_enabled and s.asr_model_path == tmp_path / "models" / "asr" / "ggml-base.bin"
    assert Settings.from_env({"DOCSEED_DATA_DIR": str(tmp_path)}).asr_enabled is False


# ---------------------------------------------------------------------------
# ffmpeg を使う（実際の音声・動画を作って読む）
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_page_count_is_ceil_of_slices(tmp_path):
    wav = make_wav(tmp_path / "会議.wav", seconds=25)
    assert transcribe.page_count(wav) == 1           # 10 分の区切りなら 1 つ
    assert transcribe.page_count(make_wav(tmp_path / "長い.wav", 1)) == 1


@needs_ffmpeg
def test_split_audio_into_slices_is_16k_mono(tmp_path):
    wav = make_wav(tmp_path / "a.wav", seconds=25)
    out = tmp_path / "work"
    out.mkdir()
    parts = transcribe.split_audio(wav, out, slice_s=10)
    assert [p.name for p in parts] == ["part-00001.wav", "part-00002.wav", "part-00003.wav"]
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                      str(parts[0])], capture_output=True, check=True).stdout)
    a = info["streams"][0]
    assert (a["sample_rate"], a["channels"]) == ("16000", 1)


@needs_ffmpeg
def test_video_without_audio_is_unsupported(tmp_path):
    with pytest.raises(extract.Unsupported) as e:
        transcribe.page_count(make_silent_video(tmp_path / "無音.mp4"))
    assert e.value.code == "no_audio"


@needs_ffmpeg
def test_iter_pages_maps_slices_to_pages_and_resumes(tmp_path):
    wav = make_wav(tmp_path / "a.wav", seconds=25)
    eng = FakeAsr("区切りの文")
    pages = list(transcribe.iter_pages(wav, eng, slice_s=10, tmp_dir=tmp_path))
    assert [p.page_no for p in pages] == [1, 2, 3]
    assert pages[0].text == "[00:00:00] 区切りの文"
    assert pages[2].text == "[00:00:20] 区切りの文"
    assert all(p.source == "text" for p in pages)
    # 途中再開: 読み終えたページ（1）は読まない
    eng2 = FakeAsr()
    again = list(transcribe.iter_pages(wav, eng2, skip={1}, slice_s=10, tmp_dir=tmp_path))
    assert [p.page_no for p in again] == [2, 3] and len(eng2.calls) == 2


# ---------------------------------------------------------------------------
# worker から端から端まで（偽の文字起こし・偽の OCR・埋め込み）
# ---------------------------------------------------------------------------

def _worker(settings, app, asr):
    ctx = Context(settings.files_dir, ocr_factory=lambda: FakeOcr(), embedder_factory=FakeEmbedder, dpi=50,
                  asr=asr, tmp_dir=settings.tmp_dir)
    return Worker(settings, ctx)


@needs_ffmpeg
def test_worker_transcribes_audio_when_enabled(login, settings, app, tmp_path):
    alice = login("alice")
    wav = make_wav(tmp_path / "会議.wav", seconds=25)
    r = alice.upload(alice.root, {"会議.wav": wav.read_bytes()})
    eng = FakeAsr("来期の予算について話し合いました")
    run_all(_worker(settings, app, eng))
    d = r.json()["results"][0]["document"]
    got = alice.get(f"/api/v1/documents/{d['id']}").json()
    assert got["processing"]["status"] == "ready", got
    assert eng.calls == ["part-00001.wav"]
    conn = connect(settings.db_path)
    hit = conn.execute("SELECT c.blob_id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
                       " WHERE chunks_fts MATCH '\"予算について\"'").fetchall()
    assert len(hit) == 1
    text = conn.execute("SELECT text FROM pages WHERE blob_id = ?", (hit[0][0],)).fetchone()[0]
    assert text.startswith("[00:00:00] 来期の予算")


@needs_ffmpeg
def test_worker_media_is_kept_only_when_disabled(login, settings, app, tmp_path):
    alice = login("alice")
    wav = make_wav(tmp_path / "会議.wav", seconds=5)
    r = alice.upload(alice.root, {"会議.wav": wav.read_bytes()})
    run_all(_worker(settings, app, None))
    d = r.json()["results"][0]["document"]
    got = alice.get(f"/api/v1/documents/{d['id']}").json()
    assert got["processing"]["status"] == "unsupported", got
    assert "有効になっていません" in json.dumps(got["processing"], ensure_ascii=False)


@needs_ffmpeg
def test_worker_silent_audio_gets_no_speech_warning(login, settings, app, tmp_path):
    alice = login("alice")
    wav = make_wav(tmp_path / "無音.wav", seconds=5)
    r = alice.upload(alice.root, {"無音.wav": wav.read_bytes()})
    run_all(_worker(settings, app, FakeAsr(text="")))
    d = r.json()["results"][0]["document"]
    got = alice.get(f"/api/v1/documents/{d['id']}").json()
    assert got["processing"]["status"] == "ready", got
    assert "no_speech" in json.dumps(got["processing"], ensure_ascii=False)
