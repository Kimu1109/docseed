"""コマンドライン: docseed serve / migrate / create-admin / purge-trash / backup-db。"""

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
    import logging

    from .jobs.worker import Worker
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level,
                        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    settings.ensure_dirs()
    Worker(settings).run()


def cmd_fetch_models(args: argparse.Namespace) -> None:
    from .models import fetch_all
    fetch_all(Settings.from_env(), force=args.force)


def cmd_reprocess(args: argparse.Namespace) -> None:
    from .reprocess import reprocess
    conn = _conn(Settings.from_env())
    n = reprocess(conn, args.scope, force_ocr=args.force_ocr)
    print(f"{n} 件を再処理の待ち行列に入れました")


def cmd_backup_db(args: argparse.Namespace) -> None:
    """SQLite のオンラインバックアップ API で DB のスナップショットを作る（deployment.md 5.1）。"""
    settings = Settings.from_env()
    conn = _conn(settings)
    out_dir = settings.data_dir / "backup"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"docseed-{dt.datetime.now().strftime('%Y%m%d')}.sqlite"
    target = sqlite3.connect(dest)
    with target:
        conn.backup(target)
    target.close()
    keep = sorted(out_dir.glob("docseed-*.sqlite"))[: -args.keep] if args.keep else []
    for old in keep:
        old.unlink()
    print(f"→ {dest}")


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

    s = sub.add_parser("purge-trash", help="保持期間を過ぎたゴミ箱の項目を完全に削除する")
    s.set_defaults(func=cmd_purge_trash)

    s = sub.add_parser("worker", help="OCR・埋め込みの処理を行う worker を起動する")
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("fetch-models", help="OCR と埋め込みのモデルを取得する（初回に 1 度）")
    s.add_argument("--force", action="store_true", help="取得済みでも取り直す")
    s.set_defaults(func=cmd_fetch_models)

    s = sub.add_parser("reprocess", help="文書を再処理する")
    s.add_argument("scope", choices=["failed", "outdated", "all"],
                   help="failed: 失敗したもの / outdated: 古い版で処理したもの / all: すべて")
    s.add_argument("--force-ocr", action="store_true", help="テキスト層のある PDF も OCR する")
    s.set_defaults(func=cmd_reprocess)

    s = sub.add_parser("backup-db", help="データベースのスナップショットを作る")
    s.add_argument("--keep", type=int, default=7, help="残す世代数")
    s.set_defaults(func=cmd_backup_db)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
