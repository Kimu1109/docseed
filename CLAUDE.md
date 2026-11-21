# CLAUDE.md

docseed: Raspberry Pi 5 で動くローカルのドキュメントサーバー（保存・共有・OCR・ハイブリッド検索）。Python 3.12 / FastAPI / Jinja2 + htmx / SQLite（WAL・FTS5・sqlite-vec）/ Docker Compose（caddy・api・worker と任意のコンテナ）。

## コマンド

```bash
pip install -e ".[dev]"          # 開発の依存
ruff check src tests             # lint（行の長さ 120）
pytest                           # すべてのテスト
pytest tests/test_docs.py        # 文書の検査だけ（文書を直したら必ず）
python scripts/build_icons.py --check   # アイコンのスプライトが icons.txt と合っているか
```

## どこに何があるか

- コードの地図と決まりごと: `docs/dev/README.md`
- 設計（なぜそう作ったか。章の番号はコードやテストから参照される）: `docs/design.md` と `docs/design/`
- 文書の全体の地図: `docs/README.md`
- `docs/` の Markdown は画面の「ヘルプ」（`/help`）でもそのまま出る（`src/docseed/helpdocs.py`）。リンクは相対パス、生の HTML は書かない
- main に push されると `docs/` が GitHub の Wiki に写る（`scripts/wiki_sync.py`。一方向）。Wiki は直さない

## 決まりごと（要点）

- 文言・コメント・コミットのメッセージ・文書は **日本語**。やさしい言葉（「確認」より「確かめる」など。周りのコードと文書の言葉づかいに合わせる）
- モジュールとテストの先頭の docstring に、対応する設計書の章を書く
- 設定を足す: `src/docseed/config.py` + `.env.example` + `docs/admin/configuration.md`
- DB を変える: `src/docseed/migrations/` に新しい番号のファイル（既存は書き換えない）
- worker は外部と通信しない。外へ出るのは api・fetcher・webclip-browser だけ
- 画面の文言は日本語で書いて `_()`（テンプレート・Python）・`T()`（JS）・`N_()`（定数）を通し、英語の訳を `src/docseed/locales/en.json` に足す（`pytest tests/test_i18n.py`。`docs/design/ui.md` 1.2）
- 色は `static/app.css` の `:root` のトークンだけ。アイコンは `web/icons.txt` に足して `scripts/build_icons.py`

## 機能を足す・変えたら、文書も同じ変更で直す

手順の全体は `docs/dev/docs-guide.md`。最低限:

| 変えたもの | 直す文書 |
|---|---|
| 画面に見えるもの | `docs/user/`（画面の言葉だけで書く。`.env`・`docker compose`・`DOCSEED_*` は書かない。管理者の作業が要るなら「**管理者が有効にしたときだけ** 使えます。」） |
| 管理者の作業・任意の機能 | `docs/admin/`（任意の機能は新しいページ + `docs/admin/README.md` の表）。ひな形: `docs/dev/templates/` |
| `DOCSEED_*` | `docs/admin/configuration.md`（テストが照合する） |
| `docseed` のコマンド | `docs/admin/cli.md`（テストが照合する） |
| 更新のときの追加の作業 | `docs/admin/upgrade.md` の表の一番上 |
| 新しいページ | そのガイドの `README.md` の表（テストが照合する） |
| 仕組み | `docs/design/` の該当の章（番号は変えない） |

- 画面の文言は、テンプレート（`src/docseed/web/templates/`）にあるとおりに「」で書く。推測で書かず、テンプレートを grep して確かめる
- 既定の値・上限の数は `config.py` やコードから確かめて書く
- `docs/research/` は記録なので書き換えない
