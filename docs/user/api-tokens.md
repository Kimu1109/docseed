# API トークン（スクリプトから使う）

スクリプトやコマンドラインからファイルを置く・探すときは、API トークンを使います。

## 発行する

1. 「設定」→「API トークン」で、名前（例「ノート PC のバックアップ」）と権限を選んで発行する
   - **閲覧・変更**: ファイルを置く・動かすこともできる
   - **閲覧のみ**: 一覧・検索・ダウンロードだけ
2. 表示されたトークン（`dsk_` で始まる）を控える。**この画面を離れると二度と表示されません**

トークンは **アカウントと同じ権限** を持ちます。人に渡さず、要らなくなったら「無効にする」を押してください。一覧の「最終使用」で、いつ使われたかが分かります。

## 使う

`Authorization: Bearer <トークン>` を付けて `https://<サーバーの名前>/api/v1/...` を呼びます。

```bash
export DS_URL=https://docseed.example.com DS_TOKEN=dsk_xxxxxxxx
```

自分の情報（マイファイルのフォルダの番号 `root_folder_id` など）:

```bash
curl -s -H "Authorization: Bearer $DS_TOKEN" "$DS_URL/api/v1/me"
```

ファイルを置く（`folder_id` はフォルダの番号。フォルダの画面の URL `/f/<番号>` の数字）:

```bash
curl -s -H "Authorization: Bearer $DS_TOKEN" -F folder_id=10 -F files=@見積書.pdf "$DS_URL/api/v1/documents"
```

検索する:

```bash
curl -s -G -H "Authorization: Bearer $DS_TOKEN" --data-urlencode "q=振込先 請求書" "$DS_URL/api/v1/search"
```

- 大きなファイルや、途中で切れやすい回線では、分割・再開できるアップロード（tus 1.0。`/api/v1/uploads`）を使います。tus に対応したクライアント（tus-js-client など）がそのまま使えます
- すべての API の説明は、サーバーの `/api/docs` で見られます（ログインが必要。家の中・Tailscale の中からだけ開けます）。詳しい仕様は [API の設計](../design/api.md)
- サーバーの証明書が手元の CA のもの（ブラウザで警告が出る）の場合、curl も証明書を確かめられずに止まります。管理者から CA の証明書をもらい、`--cacert` で渡してください
