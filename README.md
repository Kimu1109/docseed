# docseed

Raspberry Pi 5 で動くローカルのドキュメントサーバーです。ファイルの保存・共有、OCR（PP-OCRv6）、日本語埋め込み（ruri-v3-70m）によるハイブリッド検索を、すべて手元の機械で行います。

## できること

- **保存と共有**: フォルダ、ドラッグ＆ドロップでのアップロード（切れても続きから）、過去の版、ゴミ箱、ユーザー間のフォルダの共有、外の人への共有リンク（受け取り用も）
- **中身で探す**: PDF・写真（HEIC も）・スキャン・Office 文書の文字を読み取り、語と意味の両方で検索。撮影日・タグ・種類での絞り込み
- **整理の補助**: 置き場所とタグの候補、小型の LLM による要約と「質問に答える」（任意）
- **安全なログイン**: パスキー、確認コード（2 段階認証）、Google などの OIDC
- **運用**: restic でのバックアップと毎月の復旧の練習、監視と通知、HTTPS の証明書の自動化、スマホ用の画面

## 文書

| 読む人 | 入口 |
|---|---|
| 使う人 | [利用者ガイド](docs/user/README.md) |
| サーバーを置く人・管理する人 | [管理者ガイド](docs/admin/README.md)（[導入](docs/admin/install.md)・[設定の一覧](docs/admin/configuration.md)・[コマンドの一覧](docs/admin/cli.md)・[困ったとき](docs/admin/troubleshooting.md)） |
| コードを直す人・機能を足す人 | [開発者ガイド](docs/dev/README.md)（[文書の書き方と更新の手順](docs/dev/docs-guide.md)） |
| 仕組みを知りたい人 | [設計書](docs/design.md) |

全体の地図は [docs/README.md](docs/README.md)。

## すぐに試す（Raspberry Pi・Docker）

詳しい手順と前提は [導入](https://github.com/Kimu1109/docseed/wiki/admin-install)。

```bash
git clone https://github.com/kimu1109/docseed.git && cd docseed
```

```bash
sudo mkdir -p /srv/docseed && sudo chown 1000:1000 /srv/docseed
```

```bash
cp .env.example .env   # DOCSEED_HOST と DOCSEED_SECRET_KEY を設定する
```

```bash
docker compose --profile tools run --rm --build tools   # モデルの取得（初回だけ。10〜20 分）
```

```bash
docker compose up -d --build
```

```bash
docker compose exec api docseed create-admin --username <名前>
```

`https://<DOCSEED_HOST>/` を開いてログインします。次に [HTTPS の証明書](https://github.com/Kimu1109/docseed/wiki/admin-https) と [バックアップ](https://github.com/Kimu1109/docseed/wiki/admin-backup) を設定してください。

## 開発

```bash
pip install -e ".[dev]" && ruff check src tests && pytest
```

詳しくは [開発者ガイド](docs/dev/README.md)。
