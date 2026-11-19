"""SQLite панели: настройки, журналы, замеры серверов, трафик, задачи."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings_log(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, key TEXT NOT NULL, old TEXT, new TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT, ip TEXT, action TEXT NOT NULL, target TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS audit_target ON audit(target, ts);
CREATE TABLE IF NOT EXISTS attempts(ip TEXT PRIMARY KEY, fails INTEGER NOT NULL, until INTEGER NOT NULL DEFAULT 0, last INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sample(ts INTEGER NOT NULL, node TEXT NOT NULL, ok INTEGER NOT NULL, active INTEGER, in_sync INTEGER, mem_mb INTEGER, swap_mb INTEGER, load REAL, disk_pct INTEGER, xray TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS sample_node_ts ON sample(node, ts);
CREATE TABLE IF NOT EXISTS traffic(ts INTEGER NOT NULL, node TEXT NOT NULL, chain TEXT NOT NULL, client TEXT NOT NULL, up INTEGER NOT NULL, down INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS traffic_ts ON traffic(ts);
CREATE INDEX IF NOT EXISTS traffic_client_ts ON traffic(client, ts);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, started INTEGER NOT NULL, finished INTEGER, log TEXT NOT NULL DEFAULT '', actor TEXT);
CREATE TABLE IF NOT EXISTS verify(ts INTEGER NOT NULL, chain TEXT NOT NULL, ok INTEGER NOT NULL, ip TEXT);
CREATE INDEX IF NOT EXISTS verify_chain_ts ON verify(chain, ts);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self._init_lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=15000")
            self._local.conn = c
        return c

    def init(self) -> None:
        with self._init_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = self.conn()
            c.executescript(SCHEMA)
            c.commit()
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> int:
        c = self.conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid or 0

    # --- служебное ---
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def audit(self, actor: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
        self.x("INSERT INTO audit(ts,actor,ip,action,target,detail) VALUES(?,?,?,?,?,?)", (int(time.time()), actor, ip, action, target, detail[:500]))

    def prune(self, days: int) -> None:
        cut = int(time.time()) - days * 86400
        for table in ("sample", "traffic", "verify"):
            self.x(f"DELETE FROM {table} WHERE ts < ?", (cut,))
        self.x("DELETE FROM jobs WHERE started < ?", (cut,))
