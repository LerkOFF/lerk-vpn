"""Обновление панели из git: проверить, установить (с проверкой новой версии), откатить, перезапуститься."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import __version__
from .db import DB

BRANCH = "main"
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


class Updater:
    def __init__(self, db: DB, root: Path | None = None):
        self.db = db
        self.root = root or repo_root()
        self._lock = threading.Lock()

    def git(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=timeout, env=ENV)

    @property
    def available(self) -> bool:
        return (self.root / ".git").exists()

    def current(self) -> dict:
        info = {"version": __version__, "commit": "", "subject": "", "date": ""}
        if not self.available:
            return info
        r = self.git("log", "-1", "--format=%h%n%s%n%cI")
        if r.returncode == 0:
            parts = r.stdout.strip().split("\n")
            info.update(commit=parts[0], subject=parts[1] if len(parts) > 1 else "", date=parts[2] if len(parts) > 2 else "")
        return info

    def check(self) -> dict:
        """Спрашивает origin, есть ли новые коммиты. Результат кладёт в kv."""
        res = {"ok": False, "ahead": 0, "commits": [], "error": ""}
        if not self.available:
            res["error"] = "панель установлена без git, обновление из панели недоступно"
            return res
        try:
            f = self.git("fetch", "--quiet", "origin", BRANCH, timeout=60)
            if f.returncode != 0:
                res["error"] = (f.stderr.strip().splitlines() or ["не удалось связаться с репозиторием"])[-1][:200]
            else:
                n = self.git("rev-list", "--count", f"HEAD..origin/{BRANCH}")
                res["ahead"] = int(n.stdout.strip() or 0)
                if res["ahead"]:
                    log = self.git("log", "--format=%h %s", "-n", "15", f"HEAD..origin/{BRANCH}")
                    res["commits"] = [ln for ln in log.stdout.splitlines() if ln]
                res["ok"] = True
        except (subprocess.TimeoutExpired, OSError) as exc:
            res["error"] = f"{type(exc).__name__}: {exc}"[:200]
        self.db.kv_set("update.checked", str(int(time.time())))
        self.db.kv_set("update.ahead", str(res["ahead"]) if res["ok"] else "")
        self.db.kv_set("update.error", res["error"])
        self.db.kv_set("update.commits", "\n".join(res["commits"]))
        return res

    def state(self) -> dict:
        return {
            "checked": int(self.db.kv_get("update.checked", "0") or 0),
            "ahead": self.db.kv_get("update.ahead"),
            "error": self.db.kv_get("update.error"),
            "commits": [c for c in self.db.kv_get("update.commits").split("\n") if c],
            "prev": self.db.kv_get("update.prev"),
        }

    def install(self) -> tuple[bool, str]:
        """git merge --ff-only, затем проверка: код импортируется и тесты проходят. Иначе откат."""
        if not self.available:
            return False, "панель установлена без git"
        if not self._lock.acquire(blocking=False):
            return False, "обновление уже выполняется"
        try:
            dirty = self.git("status", "--porcelain", "--untracked-files=no").stdout.strip()
            if dirty:
                return False, "в папке с кодом есть локальные правки, обновление остановлено"
            check = self.check()
            if not check["ok"]:
                return False, check["error"] or "не удалось проверить обновления"
            if not check["ahead"]:
                return True, "уже установлена последняя версия"
            prev = self.git("rev-parse", "HEAD").stdout.strip()
            m = self.git("merge", "--ff-only", f"origin/{BRANCH}")
            if m.returncode != 0:
                return False, "не удалось обновить: " + (m.stderr.strip().splitlines() or ["ошибка git"])[-1][:200]
            problem = self._verify()
            if problem:
                self.git("reset", "--hard", prev)
                return False, f"новая версия не прошла проверку и откатена: {problem}"
            self.db.kv_set("update.prev", prev)
            self.db.kv_set("update.ahead", "0")
            self.db.kv_set("update.commits", "")
            return True, f"обновлено на {check['ahead']} коммит(ов)"
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]
        finally:
            self._lock.release()

    def _verify(self) -> str:
        env = {**ENV, "PYTHONPATH": str(self.root / "src")}
        imp = subprocess.run([sys.executable, "-I", "-c", "import lerk_vpn.web.app"], capture_output=True, text=True, timeout=60, env=env, cwd=str(self.root))
        if imp.returncode != 0:
            return "код не импортируется: " + (imp.stderr.strip().splitlines() or ["?"])[-1][:160]
        if (self.root / "tests").is_dir():
            t = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], capture_output=True, text=True, timeout=180, env=env, cwd=str(self.root))
            if t.returncode != 0:
                return "тесты не прошли: " + (t.stderr.strip().splitlines() or ["?"])[-1][:160]
        return ""

    def rollback(self) -> tuple[bool, str]:
        prev = self.db.kv_get("update.prev")
        if not prev or not self.available:
            return False, "нечего откатывать"
        r = self.git("reset", "--hard", prev)
        if r.returncode != 0:
            return False, "откат не удался: " + (r.stderr.strip().splitlines() or ["ошибка git"])[-1][:200]
        self.db.kv_set("update.prev", "")
        return True, "возвращена предыдущая версия"


def restart_soon(delay: float = 1.5) -> None:
    """Перезапускает процесс панели после ответа браузеру. Под systemd достаточно выйти, он поднимет снова."""

    def go() -> None:
        time.sleep(delay)
        if os.environ.get("INVOCATION_ID"):
            os._exit(0)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    threading.Thread(target=go, daemon=True).start()
