"""Периоды отчётов. Всё время московское, границы считает код, а не база."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
KEYS = [("today", "Сегодня"), ("yesterday", "Вчера"), ("7d", "7 дней"), ("30d", "30 дней"), ("month", "Месяц"), ("all", "Всё")]
LABELS = dict(KEYS)


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: int
    end: int
    prev_start: int | None
    prev_end: int | None
    bucket: int  # секунд в одной точке спарклайна

    @property
    def has_prev(self) -> bool:
        return self.prev_start is not None


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def get_period(key: str, now: int | None = None) -> Period:
    if key not in LABELS:
        key = "7d"
    ts = now if now is not None else int(time.time())
    dt = datetime.fromtimestamp(ts, MSK)
    today = _midnight(dt)
    if key == "today":
        start, end = today, dt
        prev = (today - timedelta(days=1), today - timedelta(days=1) + (dt - today))
        bucket = 3600
    elif key == "yesterday":
        start, end = today - timedelta(days=1), today
        prev = (today - timedelta(days=2), today - timedelta(days=1))
        bucket = 3600
    elif key in ("7d", "30d"):
        n = 7 if key == "7d" else 30
        start, end = today - timedelta(days=n - 1), dt
        span = end - start
        prev = (start - span, start)
        bucket = 86400
    elif key == "month":
        start, end = today.replace(day=1), dt
        pm_end = start
        pm_start = (start - timedelta(days=1)).replace(day=1)
        prev = (pm_start, min(pm_start + (end - start), pm_end))
        bucket = 86400
    else:
        first = None
        start, end, prev, bucket = datetime.fromtimestamp(0, MSK), dt, None, 86400 * 7
        return Period(key, LABELS[key], 0, int(end.timestamp()), None, None, bucket)
    return Period(key, LABELS[key], int(start.timestamp()), int(end.timestamp()), int(prev[0].timestamp()), int(prev[1].timestamp()), bucket)


def buckets(p: Period, start_override: int | None = None) -> list[int]:
    """Начала корзин для спарклайна."""
    start = start_override if start_override is not None else p.start
    if p.key == "all":
        start = max(start, p.end - 26 * p.bucket)
    out, t = [], start
    while t < p.end and len(out) < 200:
        out.append(t)
        t += p.bucket
    return out


def fmt_bytes(n: float | int | None) -> str:
    if n is None:
        return "нет данных"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return "0 Б"


def fmt_dt(ts: int | None) -> str:
    if not ts:
        return "нет данных"
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y %H:%M")


def fmt_ago(ts: int | None, now: int | None = None) -> str:
    if not ts:
        return "нет данных"
    d = (now if now is not None else int(time.time())) - ts
    if d < 90:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


def compare(cur: float | None, prev: float | None, lower_better: bool = False) -> tuple[str, str]:
    """(текст, css-класс) сравнения с прошлым таким же периодом."""
    if cur is None or prev is None:
        return "нет данных", "muted"
    if prev == 0:
        return ("новое" if cur else "без изменений"), ("good" if cur and not lower_better else "muted")
    pct = (cur - prev) / prev * 100
    if abs(pct) < 0.5:
        return "без изменений", "muted"
    good = (pct < 0) if lower_better else (pct > 0)
    sign = "+" if pct > 0 else "−"
    return f"{sign}{abs(pct):.0f}% к прошлому", "good" if good else "bad"
