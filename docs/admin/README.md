# 管理者ガイド

docseed を **置く・設定する・見守る人** 向けです。画面の使い方は [利用者ガイド](../user/README.md) にあります。どちらのガイドも、docseed の画面の左のメニューの **「ヘルプ」** で読めます（管理者には管理者ガイドのタブも出ます）。

前提: Raspberry Pi 5（RAM 8GB 以上、16GB 推奨）、NVMe SSD、Raspberry Pi OS 64-bit、Docker Engine と Compose プラグイン。コマンドはすべて **リポジトリのディレクトリ**（`git clone` した場所）で実行します。

## 最初に

| 順 | ページ | 内容 |
|---|---|---|
| 1 | [導入](install.md) | 取得・`.env`・モデル・起動・管理者の作成 |
| 2 | [HTTPS の証明書](https.md) | 内部 CA / 手元の証明書 / Tailscale / DNS-01。パスキーを使うなら必須 |
| 3 | [ユーザーとログイン](users.md) | ユーザーの追加、2 段階認証の必須化、Google などでのログイン、容量の上限 |
| 4 | [バックアップと復旧](backup.md) | **導入したら必ず**。restic で別の機器へ |

## 外から使う

| ページ | 内容 |
|---|---|
| [インターネットに公開する](exposure.md) | 入口の制限、fail2ban、Cloudflare Tunnel、パスの下で動かす（`/docseed/`） |
| [別の Web サーバーの下に置く](reverse-proxy.md) | Apache・nginx がすでに 80 / 443 を使っている場合 |

## 任意の機能

既定ではオフ（または控えめ）の機能です。必要なものだけ有効にします。

| 機能 | ページ | 外部との通信 | 追加のメモリの目安 |
|---|---|---|---|
| 受け取り箱（LAN の共有フォルダから取り込む） | [受け取り箱](inbox.md) | なし | ほぼなし |
| URL の取り込み（Web クリップ） | [Web クリップ](webclip.md) | **あり**（fetcher だけ） | 384MB（描画を使うと +1GB） |
| 要約・タグの提案・質問に答える（小型の LLM） | [小型の LLM](llm.md) | なし | 2GB |
| 音声・動画の文字起こし | [文字起こし](asr.md) | なし | worker の 3GB の中 |
| 通知（ntfy・Discord・Slack・メール） | [通知](notifications.md) | **あり**（api から宛先へ） | なし |

置き場所・タグの候補は既定で有効です（負荷はほぼありません）。調整は [検索と候補の調整](tuning.md)。

## 運用

| ページ | 内容 |
|---|---|
| [状態の確認](monitoring.md) | 管理画面、警告、`docseed status`、Prometheus |
| [更新](upgrade.md) | `git pull` からの手順、版ごとの追加の作業 |
| [検索と候補の調整](tuning.md) | 検索の品質の測り方、意味検索の基準、候補のしきい値 |
| [困ったとき](troubleshooting.md) | 起動しない・証明書・ポート・処理が進まない |

## リファレンス

| ページ | 内容 |
|---|---|
| [設定の一覧](configuration.md) | `.env` に書けるすべての `DOCSEED_*` |
| [コマンドの一覧](cli.md) | `docseed` のコマンドと、`scripts/` の運用スクリプト |
| [設計: 配置・運用](../design/deployment.md) | コンテナ構成・強化の理由・バックアップの設計 |
| [設計: 認証・権限・共有](../design/security.md) | 安全策の詳細 |
