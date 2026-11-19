"""Выборки для отчётов панели."""

from __future__ import annotations

import time

from .db import DB
from .periods import Period, buckets


def has_samples(db: DB, start: int, end: int) -> bool:
    return db.one("SELECT 1 FROM sample WHERE ts>=? AND ts<? LIMIT 1", (start, end)) is not None


def traffic_total(db: DB, start: int, end: int, client: str | None = None, chain: str | None = None) -> int | None:
    if not has_samples(db, start, end):
        return None
    sql, args = "SELECT COALESCE(SUM(up+down),0) AS b FROM traffic WHERE ts>=? AND ts<?", [start, end]
    if client:
        sql, args = sql + " AND client=?", args + [client]
    if chain:
        sql, args = sql + " AND chain=?", args + [chain]
    return int(db.one(sql, tuple(args))["b"])


def active_clients(db: DB, start: int, end: int) -> int | None:
    if not has_samples(db, start, end):
        return None
    return int(db.one("SELECT COUNT(DISTINCT client) AS n FROM traffic WHERE ts>=? AND ts<?", (start, end))["n"])


def series_for(p: Period, rows) -> tuple[list[int], list[int]]:
    """rows: (ts, bytes). Возвращает (значения по корзинам, начала корзин)."""
    starts = buckets(p)
    values = [0] * len(starts)
    if not starts:
        return values, starts
    for ts, b in rows:
        idx = (ts - starts[0]) // p.bucket
        if 0 <= idx < len(values):
            values[idx] += b
    return values, starts


def traffic_series(db: DB, p: Period, client: str | None = None) -> tuple[list[int], list[int]]:
    starts = buckets(p)
    if not starts:
        return [], []
    sql, args = "SELECT ts, up+down AS b FROM traffic WHERE ts>=? AND ts<?", [starts[0], p.end]
    if client:
        sql, args = sql + " AND client=?", args + [client]
    return series_for(p, [(r["ts"], r["b"]) for r in db.q(sql, tuple(args))])


def per_client(db: DB, start: int, end: int) -> dict[str, dict]:
    out = {}
    for r in db.q("SELECT client, SUM(up) AS up, SUM(down) AS down, MAX(ts) AS last FROM traffic WHERE ts>=? AND ts<? GROUP BY client", (start, end)):
        out[r["client"]] = {"up": int(r["up"]), "down": int(r["down"]), "bytes": int(r["up"] + r["down"]), "last": int(r["last"])}
    return out


def last_seen(db: DB) -> dict[str, int]:
    return {r["client"]: int(r["last"]) for r in db.q("SELECT client, MAX(ts) AS last FROM traffic GROUP BY client")}


def per_chain(db: DB, start: int, end: int) -> dict[str, int]:
    return {r["chain"]: int(r["b"]) for r in db.q("SELECT chain, SUM(up+down) AS b FROM traffic WHERE ts>=? AND ts<? GROUP BY chain", (start, end))}


def latest_sample(db: DB, node: str):
    return db.one("SELECT * FROM sample WHERE node=? ORDER BY ts DESC LIMIT 1", (node,))


def server_state(sample, now: int | None = None, interval: int = 300) -> tuple[str, str]:
    """(подпись, класс бейджа)."""
    now = now or int(time.time())
    if sample is None:
        return "нет данных", ""
    stale = now - sample["ts"] > interval * 3
    if not sample["ok"]:
        return "не отвечает", "bad"
    if not sample["active"]:
        return "Xray остановлен", "bad"
    if stale:
        return "данные устарели", "warn"
    if sample["in_sync"] == 0:
        return "нужен apply", "warn"
    return "работает", "good"


def latest_verify(db: DB) -> dict[str, tuple[bool, int, str]]:
    out = {}
    for r in db.q("SELECT chain, ok, ip, ts FROM verify WHERE rowid IN (SELECT MAX(rowid) FROM verify GROUP BY chain)"):
        out[r["chain"]] = (bool(r["ok"]), int(r["ts"]), r["ip"] or "")
    return out


def collector_state(db: DB, interval: int, now: int | None = None) -> tuple[str, str]:
    now = now or int(time.time())
    end = int(db.kv_get("collector.last_end", "0") or 0)
    if not end:
        return "нет отметок", ""
    if db.kv_get("collector.last_status") == "error" and now - end < interval * 3:
        return "ошибка", "bad"
    age = now - end
    if age > interval * 3:
        return "молчит", "bad"
    if age > interval * 1.5:
        return "опаздывает", "warn"
    return "работает", "good"
