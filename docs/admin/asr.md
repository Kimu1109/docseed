# 音声・動画の文字起こし（任意）

音声（mp3・m4a・wav など）と動画（mp4・mov など）を、10 分ごとに区切って文字起こしし、検索できるようにします。既定はオフで、オフのときは従来どおり「保存のみ」です。**Pi 5 の CPU で動かすので遅く**（1 時間の動画に数時間かかることがあります）、夜間などにまとめて処理される前提のオプション機能です。外部とは通信しません。

1. イメージに ffmpeg と whisper-cli（whisper.cpp）を入れて作り直します。`.env` で `DOCSEED_IMAGE_TARGET=asr` にし、`docker compose build` してから `up -d` します（作るのに Pi 5 で数十分かかります）。
2. モデルを取得します（`small` は約 0.5GB。RAM に余裕がなければ `base`）。

   ```bash
   docker compose exec api docseed fetch-models --asr small
   ```

3. `.env` で `DOCSEED_ASR_ENABLED=true` にし、`DOCSEED_ASR_MODEL=ggml-small.bin`（取得時に表示された名前）を確かめて、worker を起動し直します（`docker compose up -d`）。
4. すでに保存してある音声・動画は、管理画面の「形式が未対応だったファイルを再処理」で読み直せます。

- 文字起こしの行の先頭には動画の中の時刻（`[00:12:34]`）を付けるので、検索結果で場所が分かります。
- worker の上限（`mem_limit: 3g`）は、文字起こしの間も共有されます。メモリが足りないときは `DOCSEED_ASR_MODEL=ggml-base.bin` にするか、compose の `mem_limit` を上げてください。
- 無音や音楽だけの音声は、警告「音声から文字を取り出せませんでした」が出ます。
- 文字の精度は音声の質に大きく左右されます。大事な記録は、必ず元の音声で確かめてください。
- 設定: `DOCSEED_ASR_LANGUAGE`（既定 `ja`）・`DOCSEED_ASR_THREADS`（既定 3）・`DOCSEED_ASR_TIMEOUT_S`（10 分の区切り 1 つの上限、既定 1800）。

## 動画の再生用のプレビュー

文字起こしとは別に、映像（mp4・mov など）は保存時に軽い再生用の動画（H.264・MP4、長辺 854px 程度）を作って再生します（原本は変えません）。Pi 5 では 1 時間の動画に数時間かかることがあります。作らずに原本をそのまま再生するには `DOCSEED_VIDEO_PROXY=false`。

## 関連

- 設定: [設定の一覧](configuration.md#音声動画)
- 利用者向け: [扱えるファイルの音声・動画](../user/files.md#音声動画)
