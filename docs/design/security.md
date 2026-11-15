# 認証・権限・共有 詳細設計

[設計書](../design.md) の「8.3 セキュリティ」の詳細。

## 1. 脅威の想定

| 想定 | 対策の方針 |
|---|---|
| LAN 内の他の機器・同居人以外の人 | ログイン必須。管理者以外はユーザーを作れない |
| インターネットからの攻撃 | 原則公開しない（VPN 経由）。公開する場合も共有リンク以外はログイン必須、レート制限 |
| 共有リンクの漏えい・推測 | 推測できない長さのトークン、期限・パスワード・回数制限・無効化 |
| 悪意のあるファイルのアップロード | ファイルを実行しない、ブラウザで HTML として表示させない、処理はタイムアウトとメモリ上限つき |
| SSD の盗難 | 範囲外（必要ならディスク全体の暗号化で対処） |

## 2. 認証

パスキー・確認コード（TOTP）・OIDC と再認証は Phase 7 で追加した（[拡張機能](extensions.md) 5 章）。ログイン試行の制限（2.4）は、パスワード・確認コード・復旧コード・パスキーの失敗をまとめて数える。

### 2.1 ユーザー

- 初回起動時に管理者を作る: `docseed create-admin --username <name>`（パスワードは対話入力）。
- 管理者が管理画面でユーザーを作成する（自分で登録する機能は無し）。
- パスワード: 12 文字以上。argon2id（argon2-cffi の既定パラメータ: time_cost=3, memory_cost=64MiB, parallelism=4）でハッシュ化。
- 無効化（`is_active = 0`）するとそのユーザーのセッションと API トークンはすべて使えなくなる。

### 2.2 セッション（Web UI）

| 項目 | 内容 |
|---|---|
| セッション ID | 32 バイトの乱数。DB には SHA-256 を保存 |
| Cookie | `docseed_session`、`HttpOnly; Secure; SameSite=Lax; Path=/` |
| 有効期限 | 最終アクセスから 14 日（「ログインしたままにする」が無い場合はブラウザを閉じるまで、最大 12 時間） |
| ログイン時 | 新しいセッション ID を発行（セッション固定攻撃の対策） |
| パスワード変更時 | そのユーザーの他のセッションをすべて削除 |
| CSRF | セッションごとの CSRF トークンを HTML の meta に埋め込み、POST/PUT/PATCH/DELETE では `X-CSRF-Token` ヘッダ（フォームは hidden 項目）で送らせて照合 |

### 2.3 API トークン（CLI・スクリプト用）

- 設定画面で発行。形式 `dsk_<32 バイトの base64url>`。発行時に 1 回だけ表示し、DB には SHA-256 を保存。
- `Authorization: Bearer dsk_...` で送る。API トークンでの呼び出しは CSRF チェック不要。
- スコープ: `read`（閲覧・検索・ダウンロード）、`write`（アップロード・変更・削除）。共有リンクの作成・ユーザー管理は API トークンでは不可。

### 2.4 ログイン試行の制限

- 同じユーザー名または同じ IP で、15 分間に 5 回失敗したら 15 分間ロック。
- ただし、ログインに成功したことのある端末（署名付きの Cookie `docseed_device`。ユーザー名ごと・1 年）からの試行は、ユーザー名と IP ではなく端末ごとに数える（OWASP の「デバイス Cookie」）。攻撃者がユーザー名を狙って失敗を重ねても、本人は使い慣れた端末か、パスキー（IP だけで数える）でログインできる。ロックの案内にもそう表示する。
- 確認コード（2 段階目）の失敗はユーザー ID ごと（`2fa:<id>`）と IP、再認証の失敗はユーザー ID ごと（`reauth:<id>`）に、ユーザー名のロックとは別に数える（どちらもパスワードかセッションを持つ人しか試せないため）。
- 存在しないユーザー名を大量に試されてもメモリを使い切らないよう、数えるキーは 2 万までにする。
- 失敗回数はメモリに保持する（api は 1 プロセスのため）。再起動でリセットされるのは許容する。
- ユーザーが存在しない場合も、存在する場合と同じ時間をかけて同じエラーを返す（ユーザー名の存在を推測させない）。

## 3. 権限

### 3.1 ロール

| ロール | 対象 | できること |
|---|---|---|
| admin | システム | ユーザー管理、ジョブ・システム状態の確認、再処理の実行。**他人のファイルは見られない**（必要ならフォルダ共有を受ける） |
| owner | フォルダ | ルートフォルダの持ち主。配下のすべての操作、メンバーの追加・削除、共有リンクの作成 |
| editor | フォルダ | 閲覧、アップロード、名前変更・移動（同じ owner の範囲内）、削除、タグ付け、共有リンクの作成 |
| viewer | フォルダ | 閲覧、検索、ダウンロード |

- フォルダのメンバー設定はサブフォルダに継承される。子フォルダでより強いロールを与えることはできるが、弱めることはできない（簡単にするため）。
- 文書単位の権限は持たない。権限は必ずフォルダで決まる。
- 異なる owner のフォルダ間の移動はできない（コピーで対応。blob を共有するので容量は増えない）。

### 3.2 操作と必要なロール

| 操作 | 必要なロール |
|---|---|
| 一覧・閲覧・ダウンロード・検索 | viewer 以上 |
| アップロード、フォルダ作成、名前変更、移動、タグ、削除、復元 | editor 以上 |
| 共有リンクの作成・無効化 | editor 以上（無効化は作成者または owner） |
| メンバーの追加・変更・削除 | owner |
| ゴミ箱を空にする | owner |

### 3.3 実効ロールの計算

```sql
-- 指定フォルダの祖先（自分を含む）
WITH RECURSIVE ancestors(id, parent_id, owner_id) AS (
  SELECT id, parent_id, owner_id FROM folders WHERE id = :folder_id
  UNION ALL
  SELECT f.id, f.parent_id, f.owner_id
    FROM folders f JOIN ancestors a ON f.id = a.parent_id
)
SELECT CASE
         WHEN (SELECT owner_id FROM folders WHERE id = :folder_id) = :user_id THEN 'owner'
         ELSE (SELECT CASE MAX(CASE m.role WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 END)
                        WHEN 2 THEN 'editor' WHEN 1 THEN 'viewer' END
                 FROM folder_members m JOIN ancestors a ON a.id = m.folder_id
                WHERE m.user_id = :user_id)
       END AS role;
```

検索で使う「アクセスできるフォルダ一覧」:

```sql
WITH RECURSIVE accessible_folders(folder_id) AS (
  SELECT id FROM folders WHERE owner_id = :user_id AND deleted_at IS NULL
  UNION
  SELECT folder_id FROM folder_members WHERE user_id = :user_id
  UNION
  SELECT f.id FROM folders f JOIN accessible_folders a ON f.parent_id = a.folder_id
   WHERE f.deleted_at IS NULL
)
SELECT folder_id FROM accessible_folders;
```

- 権限チェックはすべて API 層の共通関数（`require_folder_role(user, folder_id, role)`）で行い、各エンドポイントで個別に書かない。
- 権限が無い場合、対象の存在を知られないように 403 ではなく 404 を返す。

## 4. 共有リンク

### 4.1 発行

| 項目 | 内容 |
|---|---|
| 対象 | 文書 1 件、またはフォルダ（配下を含む） |
| トークン | 32 バイトの乱数（base64url, 43 文字）。URL: `https://<host>/s/<token>` |
| 権限 | `read`（閲覧・ダウンロード）／`upload`（フォルダのみ。外部の人からファイルを受け取る。中身の一覧は見せない） |
| 期限 | 既定 7 日。「無期限」も選べる（UI で警告を表示） |
| パスワード | 任意。argon2id で保存 |
| ダウンロード回数上限 | 任意 |
| 無効化 | いつでも可能。対象の文書・フォルダを削除した場合も自動的に使えなくなる |

### 4.2 アクセス時の確認

1. トークンが存在し、`revoked_at` が NULL、期限内、回数上限未満であること
2. 対象の文書・フォルダが削除されていないこと
3. 作成者がまだ対象に editor 以上の権限を持ち、ユーザーが有効であること（権限を失った人のリンクは自動的に無効）
4. パスワード付きなら、パスワード確認済みであること
   - 確認に成功したら、トークンごとの署名付き Cookie（`docseed_share_<token の先頭 8 文字>`、有効 12 時間）を発行
   - パスワードの失敗は同じトークン・同じ IP で 15 分に 10 回まで

いずれかを満たさない場合は、理由を区別せず「リンクが無効か期限切れです」と表示する。

### 4.3 共有リンクで見える範囲

- 文書の共有: ファイル名・サイズ・プレビュー・ダウンロード。抽出テキストや他の文書は見せない。
- フォルダの共有: 配下のフォルダと文書の一覧、プレビュー、ダウンロード、フォルダの ZIP 一括ダウンロード（上限 2GB）。検索は提供しない。
- `upload` 権限: アップロード画面のみ。ファイル名の衝突は別名で保存。1 ファイル 1GB、1 リンク合計 10GB（`DOCSEED_SHARE_UPLOAD_LINK_GB`）、すべての受け取り用リンクの直近 24 時間の合計 20GB（`DOCSEED_SHARE_UPLOAD_DAILY_GB`）まで。保存先の空きが 10%（`DOCSEED_SHARE_UPLOAD_MIN_FREE_PCT`）を下回ったら受け取らない（ログインしたユーザーのアップロードは 5% まで使える）。受け取り用リンクの期限は 30 日以内（無期限は不可）。受け取ったファイルは `documents.uploaded_via_share_id` に記録し、一覧で「共有リンクから受け取り」と表示する。

すべてのアクセス（閲覧・ダウンロード・アップロード・パスワード失敗）を `audit_log` に記録する。

## 5. ファイルの配信

アップロードされたファイルを使った攻撃（保存型 XSS 等）を防ぐ。

| 項目 | 内容 |
|---|---|
| ダウンロード | 常に `Content-Disposition: attachment; filename*=UTF-8''...` |
| プレビュー（inline 表示） | 許可した形式のみ: PDF、PNG、JPEG、WebP、GIF、プレーンテキスト（`text/plain; charset=utf-8` で返す）、音声・動画（MP4・WebM・MOV・MP3・M4A・AAC・OGG・WAV・FLAC。`files.PLAYABLE_MEDIA` の一覧のもの。画面の `<video>`・`<audio>` で再生し、範囲指定（Range）に応える）。HTML・SVG・XML はプレビューしない |
| 共通ヘッダ | `X-Content-Type-Options: nosniff`、`Content-Security-Policy: default-src 'none'; sandbox`（ファイル配信のレスポンス）、`Cross-Origin-Resource-Policy: same-origin` |
| アプリの画面 | `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'`、`X-Frame-Options: DENY`（PDF プレビュー用の iframe は同一オリジンのみ許可）、`Referrer-Policy: same-origin` |
| ファイル名 | 保存時にパス区切り文字・制御文字を除去し、255 バイトまでに切り詰める。実体のパスには使わない |

## 6. ファイル処理の安全策

- 画像: Pillow の `MAX_IMAGE_PIXELS` を 1 億画素に設定（画像の展開爆弾対策）。
- PDF: 1 ファイル 2000 ページまで。1 ページの画像化・OCR にタイムアウト。
- Office: ZIP 展開後の合計サイズが 500MB を超えるもの、圧縮率が 200 倍を超えるもの、ZIP の項目が 2 万を超えるものは処理しない（`office_too_large`。保存・共有はできる）。Excel は 1 シート 100 万セルまで読む。外部参照（リンクされた画像等）は取得しない。パスワード付き・古い形式（OLE の複合ファイル）は開かない（`encrypted_office`）。
- worker コンテナはネットワークを外部に出さない設定にする（モデルは事前に配置済みのため不要）。メモリ上限 3GB を設定する。

## 7. 監査ログ

`audit_log` に次を記録し、管理画面（自分の操作は設定画面）で確認できるようにする。保持期間は 1 年。

- ログイン成功・失敗、ログアウト、パスワード変更
- アップロード、ダウンロード、削除、復元、完全削除
- フォルダのメンバー追加・変更・削除
- 共有リンクの作成・無効化・アクセス・パスワード失敗
- ユーザーの作成・無効化、API トークンの発行・無効化

## 8. インターネットに公開する場合の入口の制限

Tailscale や LAN の中だけでなく、ポートを開けて公開する場合に備え、Caddy（`docker/Caddyfile`）で次を行う。アプリ側の認証・権限チェックはそのまま残し、入口でもう一段止める（多層防御）。

| 項目 | 内容 |
|---|---|
| 内側の範囲 | `DOCSEED_TRUSTED_NETS`（既定 `private_ranges 100.64.0.0/10`: RFC 1918・ループバック・`fd00::/8`・Tailscale）。接続元は `client_ip` で判定する（10 章） |
| 外側から隠すパス | `/admin`、`/admin/*`、`/api/v1/admin`、`/api/v1/admin/*`、`/api/docs`、`/api/docs/*`、`/api/v1/openapi.json`、`/healthz` → 404（存在を知らせない。3.3 と同じ方針）。`%61dmin` や `%2F` のような符号化は Caddy が戻してから照合する。URL の接頭辞（`DOCSEED_BASE_PATH`、deployment.md 3.1）の下の同じパス（`/docseed/admin` など）も、接頭辞の中身を問わない `path_regexp` で 404 にする。外側に別のリバースプロキシを置くと接続元がそのプロキシに見えるため、そのプロキシの側でも止める（[パスの下で動かす](../admin/exposure.md#パスの下で動かす)） |
| IP アドレスで開くサイト | `DOCSEED_LAN_IP` のサイトは内側からだけ。外側からは応答せずに接続を切る（`abort`） |
| 追加するヘッダ | `Cross-Origin-Opener-Policy: same-origin`、`Permissions-Policy`（カメラ・マイク・位置情報・決済・USB などを無効）。アプリが同じヘッダを付けたときはアプリの値を優先する |
| 外すヘッダ | `Server`、`Via`、`X-Powered-By` |

- Docker の `healthcheck` は api に直接（`127.0.0.1:8000/healthz`）問い合わせるので、Caddy の制限の影響を受けない。
- 注意: Docker のポート公開では、IPv6 の接続やホスト自身からの接続は docker-proxy を通り、接続元が Docker のゲートウェイ（`172.16.0.0/12` 内）に見える。IPv6 で外部に公開する場合はこの範囲を内側に含めないこと（[インターネットに公開する](../admin/exposure.md#ルーターでポートを開けて公開する)）。

## 9. ログイン以外の流量の制限とセキュリティログ

ログインの失敗の制限（2.4）とは別に、接続元の IP ごとにリクエストの回数を数える（`src/docseed/ratelimit.py`）。api は 1 プロセスなのでメモリ上で数え、再起動で戻るのは許容する。数えるキーは 2 万まで。

| 規則 | 対象 | 上限 |
|---|---|---|
| login | `POST /login`、`/login/second-factor`、`/api/v1/auth/login`、`/auth/passkeys/login/options` | 1 分 30 回（成功も含む量の上限。パスキーの challenge を大量に作らせない） |
| oidc | `GET /auth/oidc/<名前>/start` | 1 分 30 回 |
| share | `/s/` 以下すべて | 1 分 600 回（共有フォルダのサムネイルの一覧で届かない程度） |
| share_upload | `POST /s/<token>/upload` | 1 時間 30 回 |
| search | `GET /search`、`/api/v1/search` | 1 分 60 回 |
| 無効なリンク | トークンが無効・期限切れ・無効化済みの `/s/<token>` | 15 分 20 回で、その IP の `/s/` をすべて止める（トークンを当てずっぽうに試す攻撃の対策）。リンクは有効で中のファイルが無いだけの 404 は数えない |

- 超えたら 429（`Retry-After` 付き）。`DOCSEED_RATE_LIMIT=false` で止められる。
- 接続元は Caddy が判定して `X-Forwarded-For` に 1 つだけ入れる（10 章の Cloudflare Tunnel を含む）。

### 9.1 セキュリティログと fail2ban

api は次を `<DOCSEED_DATA_DIR>/logs/security.log`（5MB × 3 世代）に 1 行 1 件で書く。

```
2026-11-09T14:00:00+0000 docseed-security event=login_failed ip=203.0.113.5
2026-11-09T14:00:01+0000 docseed-security event=rate_limited ip=2001:db8::1 rule=share
```

| event | いつ |
|---|---|
| `login_failed`・`second_factor_failed`・`reauth_failed` | パスワード・確認コード・再認証の失敗（監査ログと同時に書く） |
| `passkey_failed` | パスキーでのログインの失敗 |
| `share_password_failed` | 共有リンクのパスワードの失敗 |
| `share_invalid` | 無効な共有リンクを開いた |
| `rate_limited` | 9 章の上限を超えた（`rule=` に規則の名前） |

- `ip=` は IP アドレスの形のものだけを書く（fail2ban がそのまま遮断に使うため）。ほかの値は英数字以外を `_` に置き換える。
- `deploy/fail2ban/` のフィルタと jail を `scripts/ops/install-fail2ban.sh` が置く。10 分に 10 回で 1 時間遮断（`bantime.increment` で繰り返すたびに長く、最長 1 週間）。LAN・Tailscale・Docker の中のアドレスは遮断しない。
- Docker が公開したポートへの通信は `FORWARD` を通るので `DOCKER-USER` チェーンで、ホストのサービス向けには `INPUT` で止める（IPv4・IPv6 とも）。
- 確認済み: 実際の fail2ban 1.0 で、外部の IP が 10 回で `DOCKER-USER` から REJECT され、LAN の IP と回数の足りない IP は遮断されないこと。

## 10. Cloudflare Tunnel（ポートを開けずに公開する）

compose の `cloudflared`（profile `tunnel`）が Cloudflare へ外向きにつなぎ、Cloudflare が受けた通信を `https://caddy:<DOCSEED_HTTPS_PORT>` に流す。ルーターのポートを開けず、自宅の IP も出さずに公開できる。

| 項目 | 内容 |
|---|---|
| ネットワーク | cloudflared は `tunnel` ネットワーク（サブネット固定 `DOCSEED_TUNNEL_SUBNET`、既定 `10.213.47.0/28`）だけに入る。caddy は `default` と `tunnel` の両方。api・worker には直接届かない |
| 接続元 | Caddy は `tunnel` のサブネットから来たときだけ `CF-Connecting-IP` を信頼する（`servers { trusted_proxies static ...; client_ip_headers CF-Connecting-IP }`）。ほかの接続元が送ったヘッダは無視する |
| 内側・外側の判定 | 8 章の判定は `remote_ip`（直接の相手）ではなく `client_ip`（信頼したヘッダを反映した接続元）で行う。さらにトンネル経由は、ヘッダが無い・プライベートなアドレスを名乗るなどに関係なく、常に外側（管理系のパスは 404、IP のサイトは切断） |
| アプリへ渡す接続元 | Caddy が `X-Forwarded-For` を判定済みの接続元 1 つで上書きする（`header_up X-Forwarded-For {client_ip}`）。クライアントが送った `X-Forwarded-For` は届かない |
| コンテナ | ほかと同じ `x-hardening`（読み取り専用・特権なし）。トークンは `.env` の `DOCSEED_TUNNEL_TOKEN`（`TUNNEL_TOKEN` として渡す） |

- Cloudflare が HTTPS を終端するので、通信の中身を Cloudflare が見られる。気になる場合は使わない（[Cloudflare Tunnel](../admin/exposure.md#ポートを開けずに公開するcloudflare-tunnel)）。
- Cloudflare Access で公開する名前を保護し、共有リンク（`/s/*`）だけ Bypass にするのを推奨する。
- fail2ban（9.1）はトンネル経由の通信を止められない（ファイアウォールを通らないため）。アプリの中の制限（2.4・9 章）は本当の接続元で数えるので効く。
- 確認済み（ローカルの Docker でトンネルのネットワークから再現）: トンネル経由は `CF-Connecting-IP` が公開・プライベート・無しのいずれでも管理系が 404、アプリには `CF-Connecting-IP` の値が届き、クライアントが送った `X-Forwarded-For` は無視されること。トンネル以外から送った `CF-Connecting-IP` は無視されること。cloudflared が強化した設定のまま起動すること。

## 11. 監視と通知

api の中のスレッド（`src/docseed/security_notify.py`）が 1 分ごとに、前回からの監査ログ（`audit_log`）と状態（`monitor.status`）を確かめ、`DOCSEED_NOTIFY_URL` に知らせる。worker は外部と通信しない設定なので、外部へ出られる api が送る。

| 通知 | 条件 | 重複の抑え方 |
|---|---|---|
| 新しい接続元からのログイン | `login`・`passkey_login`・`oidc_login` の接続元が外側（`DOCSEED_TRUSTED_NETS` の外。既定は Caddy の `private_ranges` と Tailscale）で、同じユーザーが直近 180 日に同じ範囲（IPv4 は /24、IPv6 は /64）からログインしていない | ログインごとに 1 回 |
| ログインの失敗の急増 | 直近 15 分の `login_failed`・`second_factor_failed` が `DOCSEED_NOTIFY_LOGIN_FAILURES`（既定 20）回以上。多い接続元を 3 つ添える | 1 時間に 1 回 |
| 管理者の操作など | `user_create`・`user_update`・`auth_settings_change`・`auth_reset`・`totp_disable`・`passkey_clone_suspected`（これだけ error） | 記録ごとに 1 回 |
| 共有リンクのパスワードの失敗 | 同じリンクで 15 分に 5 回以上 | リンクごとに 1 時間に 1 回 |
| 確認が必要な項目 | `monitor.status` の error・warning のうち、前回は無かったもの | 出ている間は 1 回。消えてまた出たら再び |

- 状態は `settings` 表の `notify.last_audit_id`・`notify.cooldown`・`notify.alerts` に置く。初回（通知を設定した直後）は過去の記録と今ある項目を送らない。
- 1 回に送るのは 10 件まで。超えた分は 1 件にまとめる。
- 送り先の形式は URL から決める（`DOCSEED_NOTIFY_FORMAT=auto`）。ntfy は日本語の題名を送れるよう、ヘッダではなく JSON（`https://<server>/` に `topic` 付き）で送る。送信に失敗したら api のログに書き、送り直さない。
- `docseed notify --test [--title ...] [--message ...] [--level ...]` で任意の通知を送れる。ホストの定期処理（`scripts/ops/update-images.sh`）の結果の通知にも使う。
- 通知の本文にはユーザー名と接続元の IP が入る。第三者のサービス（ntfy.sh など）に送ることになるので README で注意する。
- 確認済み: compose の中で擬似の Webhook に、`docseed notify --test` とバックグラウンドの確認（監査ログに記録した管理者の操作）の両方が届くこと。本物の HTTP の送信は tests/test_security_notify.py でも確かめる。
