"""音声・動画の文字起こし（任意機能。DOCSEED_ASR_ENABLED=true のときだけ使う）。

ffmpeg で音声を 16kHz・モノラルの WAV に変えて 10 分ごとに区切り、区切りごとに whisper.cpp（whisper-cli）で読む。
区切り 1 つを「ページ」として保存するので、
- 途中で止まっても、読み終えたページは残して続きから読める（抽出と同じ途中再開）
- 文書の画面では 10 分ごとのページとして並び、検索結果には [時:分:秒] を付けて出す
外部へは通信しない（モデルは docseed fetch-models --asr で事前に取得する）。
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .extract import Page, Unsupported
from .text import normalize

SLICE_SECONDS = 600        # 区切りの長さ（秒）。1 ページ = 10 分
SAMPLE_RATE = 16000        # whisper.cpp の入力（16kHz・モノラル・16bit）
PROBE_TIMEOUT_S = 120
FFMPEG_TIMEOUT_S = 3600


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str


def format_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_segments(data: dict) -> list[Segment]:
    """whisper-cli の JSON（-oj）の transcription を読む。offsets はミリ秒。"""
    out: list[Segment] = []
    for item in data.get("transcription") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        off = item.get("offsets") or {}
        out.append(Segment(int(off.get("from", 0)), int(off.get("to", 0)), text))
    return out


def slice_text(segments: list[Segment], offset_s: float) -> str:
    """区切りの文字起こし。各行の先頭に、動画の中の時刻（[時:分:秒]）を付ける。"""
    lines = [f"[{format_time(seg.start_ms + int(offset_s * 1000))}] {seg.text.strip()}"
             for seg in segments if seg.text.strip()]
    return normalize("\n".join(lines))


def _run(args: list[str], timeout: int, what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise RuntimeError(f"{what} が見つかりません（{args[0]}）。"
                           "Docker イメージの作り方は docs/admin/asr.md を参照") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{what} が {timeout} 秒以内に終わりませんでした") from e


def probe(path: Path) -> tuple[float, bool]:
    """長さ（秒）と音声のトラックがあるかを返す。ffprobe で読めなければ壊れた（または未対応の）ファイル。"""
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
             PROBE_TIMEOUT_S, "ffprobe")
    if r.returncode != 0:
        raise Unsupported("corrupt_file", "音声・動画を読み込めませんでした（壊れているか、未対応の形式です）")
    try:
        info = json.loads(r.stdout)
        duration = float(info.get("format", {}).get("duration") or 0)
        has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except (ValueError, TypeError, AttributeError) as e:
        raise Unsupported("corrupt_file", "音声・動画の情報を読み取れませんでした") from e
    return duration, has_audio


def page_count(path: Path) -> int:
    """区切りの数（＝ページ数の見込み）。音声のトラックが無い動画は文字起こしできない。"""
    duration, has_audio = probe(path)
    if not has_audio:
        raise Unsupported("no_audio", "音声のトラックが無いため、文字起こしできません（保存・共有はできます）")
    return max(1, math.ceil(duration / SLICE_SECONDS))


def split_audio(path: Path, out_dir: Path, slice_s: int = SLICE_SECONDS) -> list[Path]:
    """音声（先頭のトラック）を 16kHz・モノラルの WAV に変え、slice_s 秒ごとに区切る。"""
    pattern = out_dir / "part-%05d.wav"
    r = _run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-map", "0:a:0",
              "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
              "-f", "segment", "-segment_time", str(slice_s), "-segment_start_number", "1",
              "-reset_timestamps", "1", str(pattern)],
             FFMPEG_TIMEOUT_S, "ffmpeg")
    if r.returncode != 0:
        raise Unsupported("corrupt_file", "音声を取り出せませんでした（壊れているか、未対応のコーデックです）")
    return sorted(out_dir.glob("part-*.wav"))


class WhisperCli:
    """whisper.cpp の whisper-cli を 1 区切りずつ呼ぶ。プロセスは区切りごとに終わるので、メモリは使い捨てになる。"""

    name = "whisper.cpp"

    def __init__(self, binary: str, model: Path, *, language: str = "ja", threads: int = 3,
                 timeout_s: int = 1800):
        self.binary = binary
        self.model = model
        self.language = language
        self.threads = threads
        self.timeout_s = timeout_s

    def transcribe(self, wav: Path, work: Path) -> list[Segment]:
        if not self.model.exists():
            raise RuntimeError(f"文字起こしのモデルがありません（{self.model}）。"
                               "docseed fetch-models --asr small で取得してください")
        prefix = work / wav.stem
        r = _run([self.binary, "-m", str(self.model), "-f", str(wav), "-l", self.language,
                  "-t", str(self.threads), "-ng", "-np", "-oj", "-of", str(prefix)],
                 self.timeout_s, "whisper-cli")
        out = Path(f"{prefix}.json")
        if r.returncode != 0 or not out.exists():
            tail = r.stderr.decode("utf-8", "replace")[-300:]
            raise RuntimeError(f"whisper-cli が失敗しました（終了コード {r.returncode}）: {tail}")
        try:
            return parse_segments(json.loads(out.read_text(encoding="utf-8")))
        finally:
            out.unlink(missing_ok=True)


def iter_pages(path: Path, engine, *, skip: set[int] | None = None, slice_s: int = SLICE_SECONDS,
               tmp_dir: Path | None = None) -> Iterator[Page]:
    """区切りごとに文字起こしして、ページとして順に返す。skip のページ番号は読まない（途中再開用）。

    engine は transcribe(wav, work) -> list[Segment] を持つもの（WhisperCli か、テスト用の偽物）。
    """
    skip = skip or set()
    with tempfile.TemporaryDirectory(prefix="asr-", dir=tmp_dir) as d:
        work = Path(d)
        wavs = split_audio(path, work, slice_s)
        if not wavs:
            raise Unsupported("no_audio", "音声が入っていないため、文字起こしできません")
        for n, wav in enumerate(wavs, start=1):
            if n in skip:
                wav.unlink(missing_ok=True)
                continue
            segments = engine.transcribe(wav, work)
            yield Page(n, "text", text=slice_text(segments, (n - 1) * slice_s))
            wav.unlink(missing_ok=True)
