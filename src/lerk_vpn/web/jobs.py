"""Долгие операции (установка, проверка, добавление сервера) в фоне с журналом в базе."""

from __future__ import annotations

import threading
import time
import traceback

from .db import DB

MAX_LOG = 60_000


class Jobs:
    def __init__(self, db: DB):
        self.db = db
        self._busy = threading.Lock()

    def running(self) -> bool:
        return self._busy.locked()

    def start(self, kind: str, title: str, actor: str, fn) -> int | None:
        """Запускает fn(log) в потоке. Если другая операция уже идёт, возвращает None."""
        if not self._busy.acquire(blocking=False):
            return None
        job_id = self.db.x("INSERT INTO jobs(kind,title,status,started,actor) VALUES(?,?,?,?,?)", (kind, title, "running", int(time.time()), actor))

        def log(line: str) -> None:
            line = str(line)
            row = self.db.one("SELECT log FROM jobs WHERE id=?", (job_id,))
            text = (row["log"] + line + "\n")[-MAX_LOG:]
            self.db.x("UPDATE jobs SET log=? WHERE id=?", (text, job_id))

        def run() -> None:
            status = "error"
            try:
                result = fn(log)
                status = "ok" if result in (None, 0, True) else "error"
            except Exception as exc:
                log(f"ошибка: {exc}")
                if not isinstance(exc, (ValueError,)):
                    log(traceback.format_exc(limit=3))
            finally:
                self.db.x("UPDATE jobs SET status=?, finished=? WHERE id=?", (status, int(time.time()), job_id))
                self._busy.release()

        threading.Thread(target=run, name=f"job-{job_id}", daemon=True).start()
        return job_id

    def mark_stale(self) -> None:
        """После перезапуска панели незавершённые задачи помечаются прерванными."""
        self.db.x("UPDATE jobs SET status='error', finished=? WHERE status='running'", (int(time.time()),))
