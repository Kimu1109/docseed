"""監査ログ。docs/design/security.md 7 章。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import now


def audit(conn: sqlite3.Connection, action: str, *, user_id: int | None = None,
          target_type: str | None = None, target_id: int | None = None,
          ip: str | None = None, **detail: Any) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, user_id, action, target_type, target_id, ip, detail) VALUES (?,?,?,?,?,?,?)",
        (now(), user_id, action, target_type, target_id, ip,
         json.dumps(detail, ensure_ascii=False) if detail else None),
    )
