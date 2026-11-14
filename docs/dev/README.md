# 開発者ガイド

docseed のコードを直す人・機能を足す人向けです。

## 開発環境

```bash
python3 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

手元で起動する（HTTP で試すので Cookie の Secure を外す）:

```bash
export DOCSEED_DATA_DIR=./data DOCSEED_SECRET_KEY=dev-secret-key-change-me-0123456789 DOCSEED_COOKIE_SECURE=false
```

```bash
docseed create-admin --username admin && docseed serve --port 8000
```

`http://localhost:8000/` を開きます。文字の読み取りも試すなら、別の端末で `docseed fetch-models`（`pip install -e ".[tools]"` が要る）と `docseed worker`。API の説明は `/api/docs`。

## テストと検査

CI（`.github/workflows/ci.yml`）と同じものを手元で実行できます。

```bash
ruff check src tests
```

```bash
pytest
```

```bash
python scripts/build_icons.py --check
```

- `tests/test_docs.py` は文書の検査です（リンク切れ、設定・コマンドの載せ忘れ、文書の地図への載せ忘れ）。文書だけを直したときも実行してください: `pytest tests/test_docs.py`
- fail2ban・restic を使うテスト（`test_deploy.py`・`test_ops.py`）は、それらが無いと飛ばされます
- CI はほかに、Caddyfile の展開（証明書の方式ごと）と、強化した compose での起動を確かめます

## コードの地図

| 場所 | 内容 | 設計書 |
|---|---|---|
| `src/docseed/config.py` | 設定（`DOCSEED_*` を読む） | [配置・運用](../design/deployment.md) 3 章 |
| `src/docseed/cli.py` | `docseed` のコマンド | — |
| `src/docseed/app.py` | FastAPI のアプリの組み立て | — |
| `src/docseed/api/` | REST API（`/api/v1`）。`routes.py`・`uploads.py`（tus）・`webclips.py`・`suggestions.py`・`ask.py` | [API](../design/api.md) |
| `src/docseed/web/` | 画面。`views/` に画面ごとのルート、`templates/`（Jinja2）、`static/`（CSS・JS・htmx・アイコン） | [画面](../design/ui.md) |
| `src/docseed/jobs/` | ジョブキューと worker | [パイプライン](../design/pipeline.md) 2 章 |
| `src/docseed/pipeline/` | 取り出し・OCR・Office・文字起こし・埋め込み・派生ファイル | [パイプライン](../design/pipeline.md) |
| `src/docseed/search.py`・`rag.py`・`related.py` | 検索・質問に答える・関連する文書 | [検索](../design/search.md) |
| `src/docseed/documents.py`・`folders.py`・`versions.py`・`storage.py`・`resumable.py` | 文書・フォルダ・版・原本の保存・分割アップロード | [データモデル](../design/data-model.md) |
| `src/docseed/authn.py`・`passkeys.py`・`totp.py`・`oidc.py`・`security.py`・`ratelimit.py` | ログインと安全策 | [認証・権限・共有](../design/security.md)、[拡張機能](../design/extensions.md) 5 章 |
| `src/docseed/shares.py`・`inbox.py` | 共有リンク・受け取り箱 | [認証・権限・共有](../design/security.md) 4 章 |
| `src/docseed/webclip/` | URL の取り込み（安全な取得・描画） | [拡張機能](../design/extensions.md) 6 章 |
| `src/docseed/suggest.py`・`llm.py`・`tags.py` | 整理の補助・要約 | [拡張機能](../design/extensions.md) 7 章 |
| `src/docseed/monitor.py`・`metrics.py`・`notify.py`・`tlscheck.py`・`backup.py` | 監視・指標・通知・証明書・バックアップ | [配置・運用](../design/deployment.md) 5〜6 章 |
| `src/docseed/migrations/` | DB のマイグレーション（`NNNN_名前.sql`。番号順に適用） | [データモデル](../design/data-model.md) |
| `compose.yaml`・`docker/` | コンテナ・Caddyfile | [配置・運用](../design/deployment.md) 2 章 |
| `scripts/ops/`・`deploy/` | Pi 本体で使う運用スクリプト・systemd・fail2ban | [配置・運用](../design/deployment.md) 5 章 |
| `tests/` | テスト（ファイルの先頭の docstring に、対応する設計書の章を書く） | — |

## 決まりごと

- 文言・コメント・コミットのメッセージは日本語。やさしい言葉で書く（「確認する」より「確かめる」、「実行」より「行う」など、画面と文書の言葉に合わせる）
- モジュールとテストの先頭の docstring に、対応する設計書の章を書く（例 `docs/design/extensions.md 6 章`）
- 色は `static/app.css` の `:root` のトークンだけを使う。アイコンは `web/icons.txt` に足して `python scripts/build_icons.py`
- 設定を足すときは `config.py`・`.env.example`・[設定の一覧](../admin/configuration.md) の 3 か所
- DB を変えるときは `migrations/` に新しい番号のファイルを足す（既存のファイルは書き換えない）
- worker は外部と通信しない（外へ出るのは api・fetcher・webclip-browser だけ）

## 機能を足したら

**文書も同じ変更（同じ PR）で直します。** 何をどこに書くかは [文書の書き方と更新の手順](docs-guide.md)。
