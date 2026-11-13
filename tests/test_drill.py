"""復旧の練習の自動化（16）: scripts/ops/restore-drill.sh --auto と、その結果の警告・表示・指標。"""

import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from docseed import backup, monitor

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "ops"


def _ts(days_ago: float = 0) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(settings, **kw):
    d = settings.data_dir / "backup"
    d.mkdir(parents=True, exist_ok=True)
    (d / backup.DRILL_STATUS).write_text(json.dumps({"time": _ts(), "auto": True, "snapshot": "abcd1234"} | kw))


def _codes(conn, settings):
    return {a["code"]: a["level"] for a in monitor.status(conn, settings)["alerts"]}


def test_drill_alerts(settings, conn):
    assert _codes(conn, settings)["drill_none"] == "info"
    _write(settings, ok=True, result="ok", last_success=_ts())
    assert not {k for k in _codes(conn, settings) if k.startswith("drill_")}
    _write(settings, ok=False, result="failed", last_success=_ts(3), message="一致しません")
    c = _codes(conn, settings)
    assert c["drill_failed"] == "error"
    assert "一致しません" in next(a["message"] for a in monitor.status(conn, settings)["alerts"]
                                 if a["code"] == "drill_failed")
    _write(settings, ok=False, result="skipped", last_success=_ts(3), message="空きが足りません")
    assert _codes(conn, settings)["drill_skipped"] == "warning"
    _write(settings, ok=True, result="ok", last_success=_ts(monitor.DRILL_STALE_DAYS + 1))
    assert _codes(conn, settings)["drill_stale"] == "warning"


def test_drill_shown_in_admin_and_metrics(login, settings):
    admin = login("admin")
    assert "復旧の練習" in admin.get("/admin").text and "記録がありません" in admin.get("/admin").text
    _write(settings, ok=False, result="skipped", last_success=_ts(31), restore_size=2048)
    page = admin.get("/admin").text
    assert "空きが足りず飛ばした" in page and "最後の成功" in page and "毎月の自動" in page
    m = admin.get("/api/v1/admin/metrics").text
    assert 'docseed_backup_last_success_timestamp_seconds{kind="restore_drill"}' in m
    assert "docseed_restore_drill_ok 0" in m
    st = admin.get("/api/v1/admin/status").json()
    assert st["backup"]["drill"]["result"] == "skipped" and st["backup"]["drill_ok_at"]


def test_systemd_units_installed_together():
    svc = (ROOT / "deploy/systemd/docseed-restore-drill.service").read_text()
    timer = (ROOT / "deploy/systemd/docseed-restore-drill.timer").read_text()
    assert "@DOCSEED_DIR@/scripts/ops/restore-drill.sh --auto" in svc
    assert "OnCalendar=Sun *-*-01..07 05:30:00" in timer and "Persistent=true" in timer
    install = (SCRIPTS / "install-backup.sh").read_text()
    assert "docseed-restore-drill.service docseed-restore-drill.timer" in install
    assert "docseed-restore-drill.timer" in install.split("systemctl enable --now", 1)[1].splitlines()[0]
    # 練習の結果のファイルはバックアップに入れない（復元したものが次の練習の記録と混ざらないように）
    assert "restore-drill-status.json" in (SCRIPTS / "backup.sh").read_text()


def test_scripts_are_valid_bash():
    for name in ("restore-drill.sh", "backup.sh", "install-backup.sh"):
        subprocess.run(["bash", "-n", str(SCRIPTS / name)], check=True)


# ---------------------------------------------------------------------------
# 本物の restic で: backup.sh → restore-drill.sh --auto
# ---------------------------------------------------------------------------

@pytest.fixture()
def drill_env(tmp_path, settings, app, login):
    if shutil.which("restic") is None:
        pytest.skip("restic がありません（CI では入れている）")
    from docseed import documents as dm
    from docseed.db import connect
    from docseed.storage import BlobStore
    from docseed.users import get_user
    login("alice")
    conn = connect(settings.db_path)
    alice = get_user(conn, 2)
    store = BlobStore(settings.files_dir, settings.tmp_dir)
    # alice のルートフォルダにファイルを置く
    root = conn.execute("SELECT id FROM folders WHERE owner_id = ? AND parent_id IS NULL", (alice.id,)).fetchone()[0]
    for i in range(3):
        dm.upload(conn, store, alice, root, f"f{i}.txt", io.BytesIO(f"hello {i}".encode() * 50), max_bytes=10 ** 6)
    venv_bin = Path(sys.executable).parent
    env = dict(os.environ, PATH=f"{venv_bin}:{os.environ['PATH']}", DOCSEED_DATA_DIR=str(settings.data_dir),
               DOCSEED_SECRET_KEY=settings.secret_key, DOCSEED_EXEC="docseed", DOCSEED_LOCAL="1",
               DOCSEED_BACKUP_CONF=str(tmp_path / "none.env"), RESTIC_REPOSITORY=str(tmp_path / "repo"),
               RESTIC_PASSWORD="pw", DOCSEED_DRILL_DIR=str(tmp_path / "drill"), DOCSEED_DIR=str(tmp_path / "proj"))
    (tmp_path / "proj").mkdir()
    subprocess.run(["restic", "init", "-q"], env=env, check=True)
    subprocess.run([str(SCRIPTS / "backup.sh")], env=env, check=True, capture_output=True)
    return env, settings, tmp_path


def _drill(env, *args, **kw):
    return subprocess.run([str(SCRIPTS / "restore-drill.sh"), "--auto", *args], env=dict(env, **kw),
                          capture_output=True, text=True)


def _status(settings):
    return backup.restore_drill_status(settings.data_dir / "backup")


def test_restore_drill_ok_skip_and_failure(drill_env):
    env, settings, tmp = drill_env
    r = _drill(env)
    assert r.returncode == 0, r.stdout + r.stderr
    st = _status(settings)
    assert st["result"] == "ok" and st["ok"] is True and st["auto"] is True
    assert st["snapshot"] and st["restore_size"] > 0 and st["last_success"] == st["time"]
    assert "3 件の原本を確認しました" in r.stdout
    assert list((tmp / "drill").iterdir()) == []                       # 復元したものは消す
    first_ok = st["last_success"]

    # 空きが足りなければ飛ばす（終了コード 0、最後の成功は引き継ぐ）
    r = _drill(env, DOCSEED_DRILL_MARGIN="100000000")
    assert r.returncode == 0, r.stderr
    st = _status(settings)
    assert st["result"] == "skipped" and st["ok"] is False and "足りません" in st["message"]
    assert st["last_success"] == first_ok

    # 原本が DB と合わないバックアップは失敗にする
    f = next(p for p in (settings.files_dir).rglob("*") if p.is_file() and ".tmp" not in p.parts)
    f.chmod(0o644)
    f.write_bytes(b"tampered")
    subprocess.run([str(SCRIPTS / "backup.sh")], env=env, check=True, capture_output=True)
    r = _drill(env)
    assert r.returncode == 1
    st = _status(settings)
    assert st["result"] == "failed" and "一致しません" in st["message"] and st["last_success"] == first_ok

    # restic の設定が無いときも記録する
    r = _drill(env, RESTIC_REPOSITORY="")
    assert r.returncode != 0 and _status(settings)["result"] == "failed"
    assert list((tmp / "drill").iterdir()) == []
