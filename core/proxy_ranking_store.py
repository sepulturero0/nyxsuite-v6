"""Per-subnet proxy quality store for the Nyxify Proxy Ranking dashboard.

Every proxy the runner uses is grouped by subnet (default first two octets, e.g.
``130.24``) and scored good -> bad from how often it needed a retry, failed a
profile creation, or hit a ban. The runner process writes; the local API process
reads. SQLite in WAL mode handles the cross-process access the same way
``core/nyxify_task_store.py`` does.
"""
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.process_utils import APP_DATA_DIR

DATA_DIR = APP_DATA_DIR / "data"
DB_PATH = DATA_DIR / "proxy_ranking.db"

_IPV4_RE = re.compile(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})")

# Scoring weights: a ban is worse than a creation failure, worse than a retry.
_WEIGHT_RETRY = 1.0
_WEIGHT_CREATION_FAIL = 2.0
_WEIGHT_BAN = 3.0


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def subnet_of(proxy_value, octets=2):
    """Return the subnet key for a proxy string.

    ``"130.24.5.7:8080"`` and ``"user:pass@130.24.5.7:8080"`` both map to
    ``"130.24"``. A non-IPv4 (hostname) proxy falls back to its bare host label so
    it still ranks as its own bucket.
    """
    text = str(proxy_value or "").strip()
    if not text:
        return ""
    match = _IPV4_RE.search(text)
    if match:
        parts = list(match.groups())
        octets = max(1, min(4, int(octets)))
        return ".".join(parts[:octets])
    host = text.split("@")[-1].split("/")[0].split(":")[0].strip().lower()
    return host


def compute_score(uses, retries, creation_fails, ban_hits):
    """Lower is better. Normalized by uses so a heavily-used-but-clean subnet still
    ranks well against a rarely-used one. Rounded to 3 dp."""
    uses = max(1, int(uses or 0))
    penalty = (
        _WEIGHT_RETRY * float(retries or 0)
        + _WEIGHT_CREATION_FAIL * float(creation_fails or 0)
        + _WEIGHT_BAN * float(ban_hits or 0)
    )
    return round(penalty / uses, 3)


class ProxyRankingStore:
    _COLUMNS = {"uses", "retries", "creation_fails", "ban_hits"}

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
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
                CREATE TABLE IF NOT EXISTS proxy_subnets (
                    subnet TEXT PRIMARY KEY,
                    uses INTEGER NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0,
                    creation_fails INTEGER NOT NULL DEFAULT 0,
                    ban_hits INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def _bump(self, proxy_value, column, delta=1):
        if column not in self._COLUMNS:
            raise ValueError(f"Unknown proxy-ranking column: {column}")
        subnet = subnet_of(proxy_value)
        if not subnet:
            return
        now = _utc_now_iso()
        # ``column`` is validated against a fixed whitelist above, so this format
        # is not an injection vector.
        sql = (
            "INSERT INTO proxy_subnets (subnet, {col}, last_used_at, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(subnet) DO UPDATE SET "
            "{col} = {col} + ?, last_used_at = ?"
        ).format(col=column)
        with self._connect() as conn:
            conn.execute(sql, (subnet, delta, now, now, delta, now))

    def record_use(self, proxy_value):
        self._bump(proxy_value, "uses")

    def record_retry(self, proxy_value, reason=""):
        self._bump(proxy_value, "retries")

    def record_creation_fail(self, proxy_value):
        self._bump(proxy_value, "creation_fails")

    def record_ban_hit(self, proxy_value):
        self._bump(proxy_value, "ban_hits")

    def reset(self):
        """Clear all ranking history so the next proxy use starts a new baseline."""
        with self._connect() as conn:
            conn.execute("DELETE FROM proxy_subnets")

    def ranked(self):
        """All subnets with a computed score, sorted good -> bad."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT subnet, uses, retries, creation_fails, ban_hits, last_used_at "
                "FROM proxy_subnets"
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["score"] = compute_score(
                data["uses"], data["retries"], data["creation_fails"], data["ban_hits"]
            )
            out.append(data)
        # Good first: lowest penalty score, then fewest bans, then most uses.
        out.sort(key=lambda r: (r["score"], r["ban_hits"], -r["uses"]))
        return out
