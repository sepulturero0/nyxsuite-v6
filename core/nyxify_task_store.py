import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.nyxify_runtime_config import load_nyxify_config
from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
DB_PATH = DATA_DIR / "nyxify_tasks.db"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value):
    return str(value or "").strip()


def _is_placeholder_username(username):
    normalized = _normalize_text(username).lower()
    if not normalized:
        return False
    return normalized.startswith("temp")


def _is_valid_email(email):
    normalized = _normalize_text(email)
    if not normalized or "@" not in normalized:
        return False
    local_part, _, domain = normalized.partition("@")
    return bool(local_part and "." in domain)


def _normalize_email(email):
    normalized = _normalize_text(email)
    return normalized if _is_valid_email(normalized) else ""


def _task_waiting_step(username="", full_auto_mode_enabled=False):
    missing = []
    if not _normalize_text(username):
        missing.append("username")
    elif _is_placeholder_username(username):
        if bool(full_auto_mode_enabled):
            return "getting_username"
        missing.append("real_username")
    if not missing:
        return ""
    return "waiting_for_" + "_and_".join(missing)


class NyxifyTaskStore:

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    row_key TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    proxy_address TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    adspower_profile_id TEXT NOT NULL DEFAULT '',
                    adspower_name TEXT NOT NULL DEFAULT '',
                    adspower_group TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    last_step TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    otp_request_status TEXT NOT NULL DEFAULT '',
                    otp_code TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'nyxify-extension',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN username TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN password TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN otp_request_status TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN otp_code TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN adspower_id TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_otp_status_updated ON tasks(otp_request_status, updated_at, id)")

    def _row_to_dict(self, row):
        payload = dict(row)
        try:
            payload["tags"] = json.loads(payload.pop("tags_json", "[]") or "[]")
        except Exception:
            payload["tags"] = []
        return payload

    def list_tasks(self, limit=500):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, row_key, model, ip_address, proxy_address, username, email, password, adspower_id, adspower_profile_id, adspower_name,
                       adspower_group, tags_json, status, last_step, error, otp_request_status, otp_code, source, created_at, updated_at
                FROM tasks
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_task_by_adspower_profile_id(self, profile_id):
        normalized_profile_id = _normalize_text(profile_id)
        if not normalized_profile_id:
            return None

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, row_key, model, ip_address, proxy_address, username, email, password, adspower_id, adspower_profile_id, adspower_name,
                       adspower_group, tags_json, status, last_step, error, otp_request_status, otp_code, source, created_at, updated_at
                FROM tasks
                WHERE LOWER(TRIM(COALESCE(adspower_profile_id, ''))) = LOWER(?)
                   OR LOWER(TRIM(COALESCE(adspower_id, ''))) = LOWER(?)
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (normalized_profile_id, normalized_profile_id),
            ).fetchone()

        return self._row_to_dict(row) if row else None

    def has_inflight_signups(self):
        """True if any Nyxify signup is currently PENDING or RUNNING.

        Used by the Nyx guard: while Nyxify has work in flight, a Nyx profile
        with no matching task is most likely a just-created one whose AdsPower id
        hasn't synced to SnapBoard yet, so the guard holds it rather than letting
        Nyx run too early.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('PENDING', 'RUNNING')"
            ).fetchone()
        try:
            return int(row[0]) > 0 if row else False
        except Exception:
            return False

    def has_running_signups(self):
        """True if any Nyxify signup is actively RUNNING right now.

        The id-sync gap the Nyx guard protects against (profile created in the
        AdsPower GUI but its id not yet written anywhere) only exists while a
        signup task is RUNNING — queued PENDING rows haven't created a profile
        yet, so they cannot explain an unmatched Nyx profile and must not hold
        the whole Nyx queue.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'RUNNING'"
            ).fetchone()
        try:
            return int(row[0]) > 0 if row else False
        except Exception:
            return False

    def get_pending_tasks(self):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, row_key, model, ip_address, proxy_address, username, email, password, adspower_id, adspower_profile_id, adspower_name,
                       adspower_group, tags_json, status, last_step, error, otp_request_status, otp_code, source, created_at, updated_at
                FROM tasks
                WHERE status = 'PENDING'
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_cleanup_delete_failed_tasks(self, limit=500):
        safe_limit = max(1, int(limit or 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, row_key, model, ip_address, proxy_address, username, email, password, adspower_id, adspower_profile_id, adspower_name,
                       adspower_group, tags_json, status, last_step, error, otp_request_status, otp_code, source, created_at, updated_at
                FROM tasks
                WHERE status = 'FAILED'
                  AND last_step = 'cleanup_delete_failed'
                  AND (
                    TRIM(COALESCE(adspower_profile_id, '')) <> ''
                    OR TRIM(COALESCE(adspower_id, '')) <> ''
                  )
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def claim_pending_tasks(self, limit=1):
        safe_limit = max(1, int(limit or 1))
        now = utc_now_iso()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, row_key, model, ip_address, proxy_address, username, email, password, adspower_id, adspower_profile_id, adspower_name,
                       adspower_group, tags_json, status, last_step, error, otp_request_status, otp_code, source, created_at, updated_at
                FROM tasks
                WHERE status = 'PENDING'
                  AND TRIM(COALESCE(username, '')) <> ''
                  AND LOWER(TRIM(COALESCE(username, ''))) NOT LIKE 'temp%'
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (safe_limit,)
            ).fetchall()

            if not rows:
                return []

            task_ids = [int(row["id"]) for row in rows]
            placeholders = ", ".join("?" for _ in task_ids)
            conn.execute(
                f"""
                UPDATE tasks
                SET status = 'RUNNING',
                    last_step = CASE
                        WHEN COALESCE(last_step, '') = '' THEN 'claimed'
                        ELSE last_step
                    END,
                    error = '',
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, *task_ids)
            )

        return [self._row_to_dict(row) for row in rows]

    def upsert_task(self, row_key, model, ip_address, proxy_address="", username="", email="", password="", adspower_id="", source="nyxify-extension"):
        now = utc_now_iso()
        normalized_row_key = _normalize_text(row_key)
        normalized_model = _normalize_text(model)
        normalized_ip = _normalize_text(ip_address)
        normalized_proxy = _normalize_text(proxy_address)
        normalized_username = _normalize_text(username)
        normalized_email = _normalize_email(email)
        normalized_password = _normalize_text(password)
        normalized_adspower_id = str(adspower_id or "").strip()
        full_auto_mode_enabled = bool(load_nyxify_config().get("full_auto_mode_enabled", False))
        waiting_step = _task_waiting_step(
            username=normalized_username,
            full_auto_mode_enabled=full_auto_mode_enabled,
        )

        if not normalized_row_key or not normalized_model or not normalized_ip:
            return None, "invalid"

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id, status, error, last_step
                FROM tasks
                WHERE row_key = ?
                """,
                (normalized_row_key,)
            ).fetchone()

            if existing:
                next_status = existing["status"]
                next_error = existing["error"]
                next_step = existing["last_step"]
                if str(existing["status"] or "").upper() == "FAILED":
                    next_status = "PENDING"
                    next_error = ""
                    next_step = ""

                if next_status == "PENDING":
                    next_step = waiting_step

                conn.execute(
                    """
                    UPDATE tasks
                    SET model = ?, ip_address = ?, proxy_address = ?, username = ?, email = ?, password = ?, adspower_id = ?, source = ?,
                        status = ?, error = ?, last_step = ?, updated_at = ?
                    WHERE row_key = ?
                    """,
                    (
                        normalized_model,
                        normalized_ip,
                        normalized_proxy,
                        normalized_username,
                        normalized_email,
                        normalized_password,
                        normalized_adspower_id,
                        source,
                        next_status,
                        next_error,
                        next_step,
                        now,
                        normalized_row_key,
                    )
                )
                return existing["id"], "updated"

            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    row_key, model, ip_address, proxy_address, username, email, password, adspower_id, adspower_profile_id, adspower_name,
                    adspower_group, tags_json, status, last_step, error, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '[]', 'PENDING', ?, '', ?, ?, ?)
                """,
                (
                    normalized_row_key,
                    normalized_model,
                    normalized_ip,
                    normalized_proxy,
                    normalized_username,
                    normalized_email,
                    normalized_password,
                    normalized_adspower_id,
                    waiting_step,
                    source,
                    now,
                    now,
                )
            )
            return cursor.lastrowid, "created"

    def update_task_state(
        self,
        task_id,
        status=None,
        last_step=None,
        error=None,
        adspower_id=None,
        adspower_profile_id=None,
        adspower_name=None,
        adspower_group=None,
        tags=None,
    ):
        updates = []
        values = []

        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if last_step is not None:
            updates.append("last_step = ?")
            values.append(last_step)
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        if adspower_id is not None:
            updates.append("adspower_id = ?")
            values.append(str(adspower_id or "").strip())
        if adspower_profile_id is not None:
            updates.append("adspower_profile_id = ?")
            values.append(str(adspower_profile_id or "").strip())
        if adspower_name is not None:
            updates.append("adspower_name = ?")
            values.append(str(adspower_name or "").strip())
        if adspower_group is not None:
            updates.append("adspower_group = ?")
            values.append(str(adspower_group or "").strip())
        if tags is not None:
            updates.append("tags_json = ?")
            values.append(json.dumps([str(tag).strip() for tag in tags if str(tag).strip()]))

        if not updates:
            return

        updates.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(task_id)

        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", tuple(values))

    def update_task_proxy(self, task_id, proxy_address):
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET proxy_address = ?, updated_at = ? WHERE id = ?",
                (str(proxy_address or "").strip(), utc_now_iso(), task_id),
            )

    def update_task_username(self, row_key, username):
        normalized_row_key = _normalize_text(row_key)
        normalized_username = _normalize_text(username)
        if not normalized_row_key or not normalized_username:
            return 0
        with self._connect() as conn:
            current = conn.execute(
                "SELECT status FROM tasks WHERE row_key = ? LIMIT 1",
                (normalized_row_key,),
            ).fetchone()
            full_auto_mode_enabled = bool(load_nyxify_config().get("full_auto_mode_enabled", False))
            next_step = _task_waiting_step(
                username=normalized_username,
                full_auto_mode_enabled=full_auto_mode_enabled,
            )
            if current and str(current["status"] or "").upper() == "PENDING":
                cursor = conn.execute(
                    "UPDATE tasks SET username = ?, last_step = ?, updated_at = ? WHERE row_key = ?",
                    (normalized_username, next_step, utc_now_iso(), normalized_row_key),
                )
            else:
                cursor = conn.execute(
                    "UPDATE tasks SET username = ?, updated_at = ? WHERE row_key = ?",
                    (normalized_username, utc_now_iso(), normalized_row_key),
                )
            return cursor.rowcount

    def update_task_last_step_by_row_key(self, row_key, last_step):
        normalized_row_key = _normalize_text(row_key)
        if not normalized_row_key:
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET last_step = ?, updated_at = ?
                WHERE row_key = ? AND status = 'PENDING'
                """,
                (str(last_step or "").strip(), utc_now_iso(), normalized_row_key),
            )
            return cursor.rowcount

    def update_task_email(self, row_key, email):
        normalized_row_key = _normalize_text(row_key)
        normalized_email = _normalize_email(email)
        if not normalized_row_key or not normalized_email:
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET email = ?, updated_at = ? WHERE row_key = ?",
                (normalized_email, utc_now_iso(), normalized_row_key),
            )
            return cursor.rowcount

    def replace_for_banned_account(
        self,
        row_key,
        model,
        ip_address,
        proxy_address="",
        username="",
        email="",
        password="",
    ):
        normalized_row_key = _normalize_text(row_key)
        normalized_model = _normalize_text(model)
        normalized_ip = _normalize_text(ip_address)
        normalized_proxy = _normalize_text(proxy_address)
        normalized_username = _normalize_text(username)
        normalized_email = _normalize_email(email)
        normalized_password = _normalize_text(password)

        if not normalized_row_key or not normalized_model or not normalized_ip:
            return 0

        now = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM tasks WHERE row_key = ?",
                (normalized_row_key,),
            ).fetchone()
            if existing:
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET model = ?,
                        ip_address = ?,
                        proxy_address = ?,
                        username = ?,
                        email = ?,
                        password = ?,
                        adspower_id = '',
                        adspower_profile_id = '',
                        adspower_name = '',
                        adspower_group = '',
                        tags_json = '[]',
                        status = 'PENDING',
                        last_step = 'replace_banned_pending',
                        error = '',
                        otp_request_status = '',
                        otp_code = '',
                        source = 'replace-banned',
                        updated_at = ?
                    WHERE row_key = ?
                    """,
                    (
                        normalized_model,
                        normalized_ip,
                        normalized_proxy,
                        normalized_username,
                        normalized_email,
                        normalized_password,
                        now,
                        normalized_row_key,
                    ),
                )
                return cursor.rowcount

            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    row_key, model, ip_address, proxy_address, username, email, password,
                    adspower_id, adspower_profile_id, adspower_name, adspower_group,
                    tags_json, status, last_step, error, otp_request_status, otp_code,
                    source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', '', '[]', 'PENDING',
                        'replace_banned_pending', '', '', '', 'replace-banned', ?, ?)
                """,
                (
                    normalized_row_key,
                    normalized_model,
                    normalized_ip,
                    normalized_proxy,
                    normalized_username,
                    normalized_email,
                    normalized_password,
                    now,
                    now,
                ),
            )
            return cursor.rowcount

    def remove_for_banned_account(self, row_key, proxy_address=""):
        normalized_row_key = _normalize_text(row_key)
        normalized_proxy = _normalize_text(proxy_address)
        if not normalized_row_key:
            return 0

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET proxy_address = ?,
                    adspower_id = '',
                    adspower_profile_id = '',
                    adspower_name = '',
                    last_step = 'remove_banned_proxy_changed',
                    error = '',
                    updated_at = ?
                WHERE row_key = ?
                """,
                (normalized_proxy, utc_now_iso(), normalized_row_key),
            )
            return cursor.rowcount

    def clear_all_tasks(self):
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM tasks")
            return cursor.rowcount

    def reset_failed_tasks(self):
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'PENDING', error = '', last_step = '', otp_request_status = '', otp_code = '', updated_at = ?
                WHERE status = 'FAILED'
                """,
                (utc_now_iso(),)
            )
            return cursor.rowcount

    def reset_orphaned_running_tasks(self):
        """Reset rows left in RUNNING back to PENDING so they get reprocessed.

        The single-instance RunnerLock means that at runner startup ANY task still
        marked RUNNING is necessarily orphaned (a previous run crashed, was stopped,
        or — as with the old group-resolution bug — got stuck), and would otherwise
        sit RUNNING forever because claim_pending_tasks only claims PENDING. The
        AdsPower fields are intentionally kept so _cleanup_stale_pending_profile can
        delete any half-created profile before the retry."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'PENDING',
                    error = '',
                    last_step = 'requeued_after_restart',
                    otp_request_status = '',
                    otp_code = '',
                    updated_at = ?
                WHERE status = 'RUNNING'
                """,
                (utc_now_iso(),)
            )
            return cursor.rowcount

    def remove_task_by_row_key(self, row_key):
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE row_key = ?", (str(row_key or "").strip(),))
            return cursor.rowcount

    def request_otp_for_row(self, row_key, email=""):
        normalized_row_key = _normalize_text(row_key)
        if not normalized_row_key:
            return 0
        normalized_email = _normalize_email(email)
        with self._connect() as conn:
            if normalized_email:
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET email = ?,
                        otp_request_status = 'PENDING',
                        otp_code = '',
                        updated_at = ?
                    WHERE row_key = ?
                    """,
                    (normalized_email, utc_now_iso(), normalized_row_key),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET otp_request_status = 'PENDING',
                        otp_code = '',
                        updated_at = ?
                    WHERE row_key = ?
                    """,
                    (utc_now_iso(), normalized_row_key),
                )
            return cursor.rowcount

    def get_pending_otp_request(self):
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT row_key, email, username, otp_request_status
                FROM tasks
                WHERE otp_request_status = 'PENDING'
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def store_otp_code(self, row_key, code, error=""):
        normalized_row_key = _normalize_text(row_key)
        normalized_code = _normalize_text(code)
        normalized_error = _normalize_text(error)
        if not normalized_row_key or not (normalized_code or normalized_error):
            return 0
        with self._connect() as conn:
            next_status = "READY" if normalized_code else "ERROR"
            cursor = conn.execute(
                """
                UPDATE tasks
                SET otp_request_status = ?,
                    otp_code = ?,
                    updated_at = ?
                WHERE row_key = ? AND otp_request_status = 'PENDING'
                """,
                (next_status, normalized_code or normalized_error, utc_now_iso(), normalized_row_key),
            )
            return cursor.rowcount

    def consume_otp_result(self, row_key):
        normalized_row_key = _normalize_text(row_key)
        if not normalized_row_key:
            return {"code": "", "error": ""}
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT otp_request_status, otp_code
                FROM tasks
                WHERE row_key = ? AND otp_request_status IN ('READY', 'ERROR')
                LIMIT 1
                """,
                (normalized_row_key,),
            ).fetchone()
            if not row:
                return {"code": "", "error": ""}
            status = _normalize_text(row["otp_request_status"])
            value = _normalize_text(row["otp_code"])
            conn.execute(
                """
                UPDATE tasks
                SET otp_request_status = '',
                    otp_code = '',
                    updated_at = ?
                WHERE row_key = ?
                """,
                (utc_now_iso(), normalized_row_key),
            )
        if status == "READY":
            return {"code": value, "error": ""}
        return {"code": "", "error": value}

    def consume_otp_code(self, row_key):
        return _normalize_text(self.consume_otp_result(row_key).get("code", ""))

    def clear_otp_request(self, row_key):
        normalized_row_key = _normalize_text(row_key)
        if not normalized_row_key:
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET otp_request_status = '',
                    otp_code = '',
                    updated_at = ?
                WHERE row_key = ?
                """,
                (utc_now_iso(), normalized_row_key),
            )
            return cursor.rowcount
