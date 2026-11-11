"""コマンドライン: docseed serve / migrate / create-admin / reset-auth / purge-trash / backup-db など。"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import sqlite3
import sys

from .config import Settings
from .db import connect, migrate
from .errors import AppError


def _conn(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    migrate(conn)
    return conn


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn
    uvicorn.run("docseed.app:app_factory", factory=True, host=args.host, port=args.port,
                proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips, workers=1,
                log_level="info")


def cmd_migrate(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    applied = migrate(conn)
    print("適用しました: " + ", ".join(applied) if applied else "最新です")


def cmd_create_admin(args: argparse.Namespace) -> None:
    from .users import create_user
    settings = Settings.from_env()
    conn = _conn(settings)
    password = args.password or getpass.getpass("パスワード（12 文字以上）: ")
    if not args.password and password != getpass.getpass("パスワード（確認）: "):
        sys.exit("パスワードが一致しません")
    try:
        u = create_user(conn, args.username, args.display_name or args.username, password, role="admin")
    except AppError as e:
        sys.exit(f"作成できませんでした: {e.detail}")
    print(f"管理者「{u.username}」を作成しました")


def cmd_reset_auth(args: argparse.Namespace) -> None:
    """管理者自身がログインできなくなったとき用（docs/design/extensions.md 5.7）。"""
    from .authn import reset_auth
    from .users import find_by_username
    settings = Settings.from_env()
    conn = _conn(settings)
    row = find_by_username(conn, args.username)
    if row is None:
        sys.exit(f"ユーザー「{args.username}」が見つかりません")
    temp = reset_auth(conn, row["id"], actor_id=None, ip="cli")
    print(f"「{row['username']}」のログイン方法（パスキー・確認コード・外部アカウントの連携）を消し、"
          "すべての端末をログアウトしました。")
    print(f"仮のパスワード: {temp}")
    print("ログインしたら、設定画面でパスワードを変更してください。")


def cmd_purge_trash(args: argparse.Namespace) -> None:
    from .documents import purge_expired
    from .storage import BlobStore
    settings = Settings.from_env()
    conn = _conn(settings)
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.trash_retention_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    n = purge_expired(conn, BlobStore(settings.files_dir, settings.tmp_dir), cutoff)
    print(f"{n} 件を完全に削除しました（{settings.trash_retention_days} 日より前に削除されたもの）")


def cmd_worker(args: argparse.Namespace) -> None:
    from .jobs.worker import Worker
    from .logs import setup
    settings = Settings.from_env()
    setup(settings.log_level)
    settings.ensure_dirs()
    Worker(settings).run()


def cmd_fetcher(args: argparse.Namespace) -> None:
    from .logs import setup
    from .webclip.fetcher import Fetcher
    settings = Settings.from_env()
    setup(settings.log_level)
    settings.ensure_dirs()
    if not settings.webclip_enabled:
        sys.exit("URL の取り込みは無効です（.env の DOCSEED_WEBCLIP_ENABLED=true にしてください）")
    Fetcher(settings).run()


def cmd_eval_suggest(args: argparse.Namespace) -> None:
    import json as _json

    from .suggest import evaluate
    from .users import find_by_username
    conn = _conn(Settings.from_env())
    u = find_by_username(conn, args.username)
    if u is None:
        sys.exit(f"ユーザー「{args.username}」が見つかりません")
    rows = evaluate(conn, u["id"], sim_mins=[float(x) for x in args.sim_min.split(",")],
                    conf_mins=[float(x) for x in args.conf_min.split(",")], limit=args.limit)
    if args.json:
        print(_json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print("類似度の下限  確かさの下限  文書  1位の正解率  上位3の正解率  おすすめの数  おすすめの正解率")
    for r in rows:
        prec = "—" if r["recommended_precision"] is None else f"{r['recommended_precision']:.3f}"
        print(f"{r['sim_min']:>10.2f}  {r['conf_min']:>10.2f}  {r['documents']:>4}  {r['top1'] or 0:>10.3f}"
              f"  {r['top3'] or 0:>12.3f}  {r['recommended']:>10}  {prec:>14}")


def cmd_fetch_models(args: argparse.Namespace) -> None:
    from .models import fetch_all, fetch_llm_only
    if args.llm:
        fetch_llm_only(Settings.from_env(), args.llm, force=args.force)
        return
    fetch_all(Settings.from_env(), force=args.force)


def cmd_export_texts(args: argparse.Namespace) -> None:
    from .llm import export_texts
    from .users import find_by_username
    conn = _conn(Settings.from_env())
    uid = None
    if args.username:
        u = find_by_username(conn, args.username)
        if u is None:
            sys.exit(f"ユーザー「{args.username}」が見つかりません")
        uid = u["id"]
    files = export_texts(conn, args.out, limit=args.limit, user_id=uid)
    if not files:
        sys.exit("書き出せる文書がありません（処理済みで、本文が 100 字以上の文書が必要です）")
    print(f"{len(files)} 件を {args.out} に書き出しました")


def cmd_derive(args: argparse.Namespace) -> None:
    from .reprocess import enqueue_derive
    conn = _conn(Settings.from_env())
    n = enqueue_derive(conn, args.scope)
    print(f"{n} 件のサムネイル・プレビューの作成を受け付けました（worker が順に作ります）")


def cmd_reprocess(args: argparse.Namespace) -> None:
    from .reprocess import reprocess
    conn = _conn(Settings.from_env())
    n = reprocess(conn, args.scope, force_ocr=args.force_ocr)
    print(f"{n} 件を再処理の待ち行列に入れました")


def cmd_backup_db(args: argparse.Namespace) -> None:
    """SQLite のオンラインバックアップ API で DB のスナップショットを作る（deployment.md 5.1）。"""
    from .backup import BackupError, snapshot_db
    settings = Settings.from_env()
    conn = _conn(settings)
    try:
        dest = snapshot_db(conn, settings.data_dir / "backup", keep=args.keep)
    except BackupError as e:
        sys.exit(f"スナップショットを作れませんでした: {e}")
    print(f"→ {dest}")


def cmd_restore_db(args: argparse.Namespace) -> None:
    """スナップショットを DB の場所へ戻す（deployment.md 5.2）。api と worker は止めておく。"""
    from pathlib import Path

    from .backup import BackupError, latest_snapshot, restore_db
    settings = Settings.from_env()
    src = Path(args.snapshot) if args.snapshot != "latest" else latest_snapshot(settings.data_dir / "backup")
    if src is None:
        sys.exit(f"{settings.data_dir / 'backup'} にスナップショットがありません")
    try:
        moved = restore_db(src, settings.db_path, force=args.force)
    except BackupError as e:
        sys.exit(f"復元できませんでした: {e}")
    print(f"{src} を {settings.db_path} に復元しました")
    if moved:
        print(f"  元の DB は {moved} に退避しました（確認が済んだら削除して構いません）")
    print("  次に docker compose up -d で起動し、docseed verify-files で原本との対応を確認してください")


def cmd_verify_files(args: argparse.Namespace) -> None:
    """DB と原本ファイルの対応（存在・大きさ・SHA-256）を確かめる。問題があれば終了コード 1。"""
    import json

    from .backup import verify_files
    settings = Settings.from_env()
    settings.ensure_dirs()
    conn = connect(settings.db_path)

    def progress(i: int, n: int) -> None:
        if not args.json and sys.stderr.isatty() and (i % 200 == 0 or i == n):
            print(f"\r  {i} / {n}", end="" if i < n else "\n", file=sys.stderr, flush=True)

    rep = verify_files(conn, settings.files_dir, full=not args.quick, progress=progress)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        how = "大きさ" if args.quick else "大きさと SHA-256"
        print(f"{rep.checked} 件の原本を確認しました（{how}）")
        for sha in rep.missing:
            print(f"  ファイルが無い: {sha}")
        for sha in rep.mismatched:
            print(f"  内容が一致しない: {sha}")
        if rep.orphans:
            print(f"  DB に記録の無いファイル: {len(rep.orphans)} 件（問題ではありません。バックアップ後に"
                  "アップロードされた・削除されたファイルの可能性）")
        print("問題はありません" if rep.ok else f"問題: ファイルが無い {len(rep.missing)} 件、"
              f"一致しない {len(rep.mismatched)} 件")
    sys.exit(0 if rep.ok else 1)


def cmd_status(args: argparse.Namespace) -> None:
    """管理画面と同じ状態と警告を表示する。error の警告があれば終了コード 1。"""
    import json

    from .monitor import status
    settings = Settings.from_env()
    conn = _conn(settings)
    st = status(conn, settings)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        w = st["worker"]
        print(f"worker: {'動作中' if w['alive'] else '停止'}（最終応答 {w['last_seen'] or 'なし'}）")
        print("ジョブ: " + ", ".join(f"{k} {v}" for k, v in st["jobs"].items()))
        print(f"SSD の空き: {st['disk']['free_ratio'] * 100:.1f}%")
        if st["cpu_temp_c"] is not None:
            print(f"CPU 温度: {st['cpu_temp_c']:.1f}℃")
        b = st["backup"]
        print(f"DB スナップショット: {b['db_snapshot'] or 'なし'}（{b['db_snapshot_at'] or '-'}）")
        print(f"restic: {b['restic_at'] or '記録なし'}")
        for a in st["alerts"]:
            print(f"[{a['level']}] {a['message']}")
    sys.exit(1 if any(a["level"] == "error" for a in st["alerts"]) else 0)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="docseed")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Web サーバーを起動する（起動時にマイグレーションを適用）")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--forwarded-allow-ips", default="*", help="X-Forwarded-For を信頼する送信元（Caddy）")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("migrate", help="データベースのマイグレーションを適用する")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("create-admin", help="管理者ユーザーを作成する")
    s.add_argument("--username", required=True)
    s.add_argument("--display-name")
    s.add_argument("--password", help="省略時は対話入力（推奨）")
    s.set_defaults(func=cmd_create_admin)

    s = sub.add_parser("reset-auth", help="ユーザーのログイン方法をリセットし、仮のパスワードを発行する")
    s.add_argument("--username", required=True)
    s.set_defaults(func=cmd_reset_auth)

    s = sub.add_parser("purge-trash", help="保持期間を過ぎたゴミ箱の項目を完全に削除する")
    s.set_defaults(func=cmd_purge_trash)

    s = sub.add_parser("worker", help="OCR・埋め込みの処理を行う worker を起動する")
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("fetcher", help="URL の取り込み（Web クリップ）の取得を行う fetcher を起動する")
    s.set_defaults(func=cmd_fetcher)

    s = sub.add_parser("eval-suggest", help="フォルダの候補のしきい値を、分類済みの文書で評価する")
    s.add_argument("--username", required=True)
    s.add_argument("--sim-min", default="0.75,0.80,0.85", help="類似度の下限（カンマ区切りで複数）")
    s.add_argument("--conf-min", default="0.5,0.6,0.7", help="おすすめにする確かさの下限（カンマ区切り）")
    s.add_argument("--limit", type=int, default=300, help="評価する文書の数の上限（新しいものから）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_eval_suggest)

    s = sub.add_parser("fetch-models", help="OCR と埋め込みのモデルを取得する（初回に 1 度）")
    s.add_argument("--force", action="store_true", help="取得済みでも取り直す")
    s.add_argument("--llm", metavar="NAME", help="要約に使う LLM（GGUF）だけを取得する（例 lfm2.5-1.2b-jp）")
    s.set_defaults(func=cmd_fetch_models)

    s = sub.add_parser("export-texts", help="LLM の比較に使う文書のテキストを書き出す（scripts/bench_llm.sh）")
    s.add_argument("--out", required=True, help="書き出し先のディレクトリ")
    s.add_argument("--limit", type=int, default=30, help="件数（新しい文書から）")
    s.add_argument("--username", help="このユーザーがアップロードした文書だけにする")
    s.set_defaults(func=cmd_export_texts)

    s = sub.add_parser("derive", help="画像・PDF のサムネイルとプレビューを作る（文字の読み取りはやり直さない）")
    s.add_argument("scope", choices=["missing", "all"], help="missing: まだ無いもの / all: すべて作り直す")
    s.set_defaults(func=cmd_derive)

    s = sub.add_parser("reprocess", help="文書を再処理する")
    s.add_argument("scope", choices=["failed", "outdated", "unsupported", "all"],
                   help="failed: 失敗したもの / outdated: 古い版で処理したもの / "
                        "unsupported: 形式が未対応だったもの / all: すべて")
    s.add_argument("--force-ocr", action="store_true", help="テキスト層のある PDF も OCR する")
    s.set_defaults(func=cmd_reprocess)

    s = sub.add_parser("backup-db", help="データベースのスナップショットを作る")
    s.add_argument("--keep", type=int, default=7, help="残す世代数")
    s.set_defaults(func=cmd_backup_db)

    s = sub.add_parser("restore-db", help="データベースをスナップショットから復元する（api と worker を止めて実行）")
    s.add_argument("snapshot", nargs="?", default="latest",
                   help="スナップショットのパス（省略時は backup/ の最新）")
    s.add_argument("--force", action="store_true", help="worker が動いている形跡があっても実行する")
    s.set_defaults(func=cmd_restore_db)

    s = sub.add_parser("verify-files", help="DB と原本ファイルの対応（存在・SHA-256）を確かめる")
    s.add_argument("--quick", action="store_true", help="SHA-256 を計算せず、大きさだけを比べる")
    s.add_argument("--json", action="store_true", help="結果を JSON で出力する")
    s.set_defaults(func=cmd_verify_files)

    s = sub.add_parser("status", help="システムの状態と警告を表示する")
    s.add_argument("--json", action="store_true", help="結果を JSON で出力する")
    s.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
