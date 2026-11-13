"""モデルの取得（docseed fetch-models）。docs/design/deployment.md 4 章。

- OCR: RapidOCR が配布する PP-OCRv6（ONNX）を models/rapidocr に取得する
- 埋め込み: Hugging Face から ruri-v3 を取得し、ONNX に変換して models/embed/<名前> に置く
  （公式の配布物に ONNX が無いため自分で変換する。変換にだけ torch が必要なので、
   この処理は tools イメージ（pip install ".[tools]"）で 1 度だけ実行する）
- 取得したファイルの SHA-256 を models/manifest.json に記録する
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

from .config import Settings
from .pipeline.embed import model_dir


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def fetch_ocr(settings: Settings, model_type: str = "small") -> list[Path]:
    from .pipeline.ocr import RapidOcrEngine
    eng = RapidOcrEngine(settings.models_dir, model_type=model_type, allow_download=True)
    eng.load_all()
    files = sorted((settings.models_dir / "rapidocr").rglob("*.onnx"))
    names = " ".join(p.name for p in files)
    for part in ("det", "rec", "cls"):
        if f"_{part}" not in names:
            raise SystemExit(f"OCR モデル（{part}）を取得できませんでした: {settings.models_dir / 'rapidocr'}")
    return files


def fetch_embedding(settings: Settings, *, force: bool = False) -> list[Path]:
    dest = model_dir(settings.models_dir, settings.embed_model)
    onnx_path = dest / "onnx" / "model.onnx"
    if onnx_path.exists() and not force:
        print(f"  埋め込みモデルは取得済みです: {dest}")
        return sorted(dest.rglob("*"))
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise SystemExit(
            "埋め込みモデルの ONNX 変換には torch と sentence-transformers が必要です。\n"
            "  Docker: docker compose --profile tools run --rm tools\n"
            "  手元:   pip install -e \".[tools]\" してから docseed fetch-models") from e
    cache = settings.models_dir / ".hf-cache"
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"  {settings.embed_model} を取得して ONNX に変換しています（数分かかります）")
    m = SentenceTransformer(settings.embed_model, device="cpu", backend="onnx", cache_folder=str(cache))
    m.save_pretrained(str(tmp))
    if not (tmp / "onnx" / "model.onnx").exists() or not (tmp / "tokenizer.json").exists():
        raise SystemExit(f"変換結果が不完全です: {tmp}")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    shutil.rmtree(cache, ignore_errors=True)
    return sorted(dest.rglob("*"))


# 要約に使う LLM の候補（docs/design/extensions.md 7.4）。
# 名前: (Hugging Face のリポジトリ, GGUF のファイル名, ライセンス)
LLM_MODELS = {
    "sarashina2.2-1b": ("mmnga/sarashina2.2-1b-instruct-v0.1-gguf", "sarashina2.2-1b-instruct-v0.1-Q4_K_M.gguf", "MIT"),
    "sarashina2.2-1b-q8": ("mmnga/sarashina2.2-1b-instruct-v0.1-gguf", "sarashina2.2-1b-instruct-v0.1-Q8_0.gguf",
                           "MIT"),
    "lfm2.5-1.2b-jp": ("LiquidAI/LFM2.5-1.2B-JP-GGUF", "LFM2.5-1.2B-JP-Q4_K_M.gguf", "LFM Open License v1.0"),
    "qwen3-1.7b": ("unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q4_K_M.gguf", "Apache-2.0"),
    "qwen3-0.6b": ("ggml-org/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q8_0.gguf", "Apache-2.0"),
}


def asr_dir(settings: Settings) -> Path:
    return settings.models_dir / "asr"


# 音声・動画の文字起こし（whisper.cpp の ggml 形式。変換は要らない）。docs/design/pipeline.md 3.5
ASR_MODELS = {
    "base": ("ggerganov/whisper.cpp", "ggml-base.bin", "MIT"),
    "small": ("ggerganov/whisper.cpp", "ggml-small.bin", "MIT"),
}


def fetch_asr(settings: Settings, name: str, *, force: bool = False) -> Path:
    """ggml の Whisper モデルを models/asr/ に取得する（worker が読み取る）。"""
    if name not in ASR_MODELS:
        raise SystemExit(f"不明なモデルです: {name}（{', '.join(ASR_MODELS)} のどれか）")
    repo, filename, license_ = ASR_MODELS[name]
    dest = asr_dir(settings) / filename
    if dest.exists() and not force:
        print(f"  取得済みです: {dest}")
        return dest
    from huggingface_hub import hf_hub_download
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {repo} の {filename} を取得しています（ライセンス: {license_}）")
    path = Path(hf_hub_download(repo, filename, local_dir=str(dest.parent)))
    if path != dest:
        shutil.move(str(path), dest)
    shutil.rmtree(dest.parent / ".cache", ignore_errors=True)
    return dest


def llm_dir(settings: Settings) -> Path:
    return settings.models_dir / "llm"


def fetch_llm(settings: Settings, name: str, *, force: bool = False) -> Path:
    """GGUF を models/llm/ に取得する（llm サービスが読み取り専用で読む）。"""
    if name not in LLM_MODELS:
        raise SystemExit(f"不明なモデルです: {name}（{', '.join(LLM_MODELS)} のどれか）")
    repo, filename, license_ = LLM_MODELS[name]
    dest = llm_dir(settings) / filename
    if dest.exists() and not force:
        print(f"  取得済みです: {dest}")
        return dest
    from huggingface_hub import hf_hub_download
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {repo} の {filename} を取得しています（ライセンス: {license_}）")
    path = Path(hf_hub_download(repo, filename, local_dir=str(dest.parent)))
    if path != dest:
        shutil.move(str(path), dest)
    shutil.rmtree(dest.parent / ".cache", ignore_errors=True)
    return dest


def write_manifest(settings: Settings) -> Path:
    root = settings.models_dir
    files = {str(p.relative_to(root)): {"sha256": sha256_file(p), "bytes": p.stat().st_size}
             for p in sorted(root.rglob("*")) if p.is_file() and p.name != "manifest.json"
             and ".hf-cache" not in p.parts}
    out = root / "manifest.json"
    out.write_text(json.dumps({"created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                               "embed_model": settings.embed_model, "files": files},
                              ensure_ascii=False, indent=2))
    return out


def fetch_llm_only(settings: Settings, name: str, *, force: bool = False) -> None:
    settings.ensure_dirs()
    print("要約に使う LLM（GGUF）を取得しています")
    p = fetch_llm(settings, name, force=force)
    m = write_manifest(settings)
    print(f"完了しました → {p}\n  .env の DOCSEED_LLM_MODEL={p.name} にしてください（manifest: {m}）")


def fetch_all(settings: Settings, *, force: bool = False) -> None:
    settings.ensure_dirs()
    print("OCR モデル（PP-OCRv6）を取得しています")
    model_type = settings.ocr_engine.removeprefix("ppocrv6_")
    for p in fetch_ocr(settings, model_type):
        print(f"  {p.relative_to(settings.models_dir)}")
    print("埋め込みモデルを準備しています")
    fetch_embedding(settings, force=force)
    m = write_manifest(settings)
    print(f"完了しました → {m}")
