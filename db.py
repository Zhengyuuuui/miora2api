"""SQLite 存储层：多账号、任务、产物、成本观测、请求日志。

约定说明
--------
* ``accounts`` 表结构对齐 ``exa_pool.db``（2026-09-27 Exa AI session），便于多项目统一管理。
* **签名 URL 会过期**（实测 validDuration=3600s），所以产物表同时存
  ``file_key``（永久标识）与 ``signed_url``（临时）。读取时若已过期可随时重签。
* 时间统一用 Unix epoch 秒（``REAL``），与既有 exa_pool.db 保持一致。

线程模型：FastAPI 的同步端点跑在线程池里，因此这里用 ``threading.Lock``
而不是假设单线程；所有写操作都在锁内完成。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

DB_PATH = os.environ.get(
    "MIORA_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "miora.db")
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        return _conn


def init_db() -> None:
    with _lock:
        conn = get_conn()
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()


def now() -> float:
    return time.time()


def set_setting(key: str, value: Any) -> None:
    with _lock:
        conn = get_conn()
        val = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, val),
        )
        conn.commit()


def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


# --------------------------------------------------------------------------- accounts


def upsert_account(
    account_id: str,
    auth_token: str,
    refresh_token: str | None = None,
    label: str = "",
    credits: float | None = None,
    credit_total: float | None = None,
    plan: str | None = None,
    country: str | None = None,
) -> None:
    ts = now()
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO accounts (id,label,auth_token,refresh_token,status,
                                  credits,credit_total,plan,country,created_at)
            VALUES (?,?,?,?,'unknown',?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,
                auth_token=excluded.auth_token,
                refresh_token=COALESCE(excluded.refresh_token, accounts.refresh_token),
                credits=excluded.credits,
                credit_total=excluded.credit_total,
                plan=excluded.plan,
                country=excluded.country
            """,
            (account_id, label, auth_token, refresh_token, credits, credit_total, plan, country, ts),
        )
        conn.commit()


def list_accounts(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM accounts"
    if enabled_only:
        sql += " WHERE enabled=1 AND status NOT IN ('disabled','expired')"
    sql += " ORDER BY weight DESC, credits DESC, last_used ASC"
    with _lock:
        rows = get_conn().execute(sql).fetchall()
    return [dict(r) for r in rows]


def pick_account() -> dict | None:
    """轮转选账号：跳过冷却中与禁用；同权重下最少使用的优先。"""
    ts = now()
    for acc in list_accounts(enabled_only=True):
        if acc["cooldown_until"] and acc["cooldown_until"] > ts:
            continue
        if acc["credits"] is not None and acc["credits"] <= 0:
            continue
        return acc
    return None


def get_account(account_id: str) -> dict | None:
    with _lock:
        row = get_conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def update_account(account_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "label", "auth_token", "refresh_token", "enabled", "status", "last_error",
        "cooldown_until", "requests", "successes", "failures", "credits",
        "credit_total", "plan", "country", "weight", "last_used",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k}=?" for k in sets)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE accounts SET {clause} WHERE id=?", (*sets.values(), account_id))
        conn.commit()


def bump_account(account_id: str, ok: bool, credits_after: float | None = None) -> None:
    field = "successes" if ok else "failures"
    with _lock:
        conn = get_conn()
        conn.execute(
            f"UPDATE accounts SET requests=requests+1, {field}={field}+1, last_used=? WHERE id=?",
            (now(), account_id),
        )
        if credits_after is not None:
            conn.execute("UPDATE accounts SET credits=? WHERE id=?", (credits_after, account_id))
        conn.commit()


# --------------------------------------------------------------------------- tasks


def create_task(
    task_id: str,
    media_type: str,
    model: str,
    account_id: str | None = None,
    task_kind: str | None = None,
    prompt: str | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    ref_count: int = 0,
    raw: Any = None,
) -> None:
    ts = now()
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO tasks (id,account_id,media_type,model,task_kind,prompt,status,
                               resolution,aspect_ratio,ref_count,raw,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, raw=excluded.raw, updated_at=excluded.updated_at
            """,
            (
                task_id, account_id, media_type, model, task_kind, prompt,
                resolution, aspect_ratio, ref_count,
                json.dumps(raw, ensure_ascii=False) if raw is not None else None, ts, ts,
            ),
        )
        conn.commit()


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status", "queue_state", "queue_position", "est_seconds", "progress",
        "file_key", "signed_url", "url_signed_at", "url_expires_at",
        "credits_used", "error", "raw", "account_id",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "raw" in sets and not isinstance(sets["raw"], (str, type(None))):
        sets["raw"] = json.dumps(sets["raw"], ensure_ascii=False)
    sets["updated_at"] = now()
    clause = ", ".join(f"{k}=?" for k in sets)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE tasks SET {clause} WHERE id=?", (*sets.values(), task_id))
        conn.commit()


def get_task(task_id: str) -> dict | None:
    with _lock:
        row = get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 50, status: str | None = None, media_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM tasks"
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status=?")
        params.append(status)
    if media_type:
        where.append("media_type=?")
        params.append(media_type)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def pending_tasks() -> list[dict]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM tasks WHERE status IN ('pending','processing') ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- media


def add_media(
    task_id: str,
    media_type: str,
    model: str | None = None,
    account_id: str | None = None,
    file_key: str | None = None,
    file_ext: str | None = None,
    bytes_: int | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_s: float | None = None,
    local_path: str | None = None,
    signed_url: str | None = None,
    url_expires_at: float | None = None,
) -> int:
    ts = now()
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """
            INSERT INTO media (task_id,account_id,media_type,model,file_key,file_ext,bytes,
                               width,height,duration_s,local_path,signed_url,url_expires_at,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id, account_id, media_type, model, file_key, file_ext, bytes_,
                width, height, duration_s, local_path, signed_url, url_expires_at, ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_media(media_id: int, **fields: Any) -> None:
    # 注意：本表列名是 `bytes`（SQL 关键字），调用方用 bytes_= 传入，这里做映射。
    # 直接对未知/未映射的键**静默忽略**是本模块的既定风格（见 update_task/update_account），
    # 但为避免"传了却没写"的隐性 bug，这里把 bytes_ 显式重命名而不是丢在 allowlist 外。
    if "bytes_" in fields:
        fields["bytes"] = fields.pop("bytes_")
    allowed = {
        "file_key", "file_ext", "bytes", "width", "height", "duration_s",
        "local_path", "signed_url", "url_expires_at", "model", "account_id",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k}=?" for k in sets)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE media SET {clause} WHERE id=?", (*sets.values(), media_id))
        conn.commit()


def list_media(limit: int = 50, media_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM media"
    params: list[Any] = []
    if media_type:
        sql += " WHERE media_type=?"
        params.append(media_type)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def media_by_task(task_id: str) -> list[dict]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM media WHERE task_id=? ORDER BY id ASC", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- cost matrix


def add_cost(
    model: str,
    media_type: str,
    account_id: str | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    credits: float | None = None,
    seconds: float | None = None,
    ok: bool = True,
    note: str | None = None,
) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO cost_matrix (account_id,model,media_type,resolution,aspect_ratio,
                                     credits,seconds,ok,note,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (account_id, model, media_type, resolution, aspect_ratio, credits, seconds,
             1 if ok else 0, note, now()),
        )
        conn.commit()


def cost_summary() -> list[dict]:
    with _lock:
        rows = get_conn().execute(
            """
            SELECT model, media_type, resolution,
                   COUNT(*)            AS runs,
                   SUM(ok)            AS ok_runs,
                   ROUND(AVG(credits),2) AS avg_credits,
                   ROUND(MIN(credits),2) AS min_credits,
                   ROUND(MAX(credits),2) AS max_credits,
                   ROUND(AVG(seconds),1) AS avg_seconds
            FROM cost_matrix
            GROUP BY model, media_type, resolution
            ORDER BY media_type, avg_credits ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- request log


def log_request(
    endpoint: str,
    method: str = "POST",
    account_id: str | None = None,
    status: int | None = None,
    code: str | None = None,
    ok: bool = True,
    ms: float | None = None,
    note: str | None = None,
) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO request_log (account_id,endpoint,method,status,code,ok,ms,note,ts)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (account_id, endpoint, method, status, code, 1 if ok else 0, ms, note, now()),
        )
        conn.commit()


def recent_logs(limit: int = 50) -> list[dict]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM request_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
